# cogs/lobby.py — /로비 (3.8.2, 테스트 서버 먼저). 규칙 · 수치는 services/lobby.py
# 11/17 규완님 '/경마 방식인데 저렇게 띄우라는 거지' · '/로비 -> 여러 사건 -> 개표 -> 임기 이렇게. 임기 도중 사건이 일어나서 문제가 생길 수도 있게'
# /로비 금액 → 후보 6명과 상금표 → 후보 버튼 → 선거운동 사건 → 개표 → 당선자 임기 → 정산. 경마처럼 한 판에 끝난다.
# 베팅금은 후보표를 띄울 때 먼저 빠져나간다 (60초 안에 안 고르면 잃는다, 경마와 같다).
import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

import release
from cogs.race import prize_lines, race_prizes
from services import lobby as L
from services import ui
from services.economy_db import EconomyDB

SECTION = "대선 중계"
GAP, GAP_LAST = 1.6, 2.2     # 장면 사이 (초)


def _bar(p: float, width: int = 12) -> str:
    n = max(0, min(width, round(p / 100 * width)))
    return "█" * n + "░" * (width - n)


def board(cands: list[dict], sup: list[float], pick: int) -> str:
    """후보 번호순 막대 (빠진 후보는 한 줄 글자)."""
    out = []
    for i, c in enumerate(cands):
        name = f"**{c['name']}** (내 후보)" if i == pick else c["name"]
        if sup[i] <= 0:
            out.append(f"`{i + 1}` {name} {c['party']}  사퇴")
        else:
            out.append(f"`{i + 1}` `{_bar(sup[i])}` {sup[i]:4.1f}%  {name} {c['party']}")
    return "\n".join(out)


def ticket_card(user, amount: int, g: dict, table: list[int], no: int) -> discord.Embed:
    lines = [f"`{i + 1}` **{c['name']}** {c['party']}\n　 지지율 {g['start'][i]:.1f}%" for i, c in enumerate(g["cands"])]
    e = ui.card(f"제{no}회 축구 봇 대선 후보 등록",
                '> *"후보 여섯 명이 출마했습니다! 어느 후보에게 로비하시겠어요?"*\n\n' + "\n".join(lines)
                + f"\n\n**상금표** (최종 순위)  베팅 **{amount:,}원**\n" + prize_lines(amount, table),
                ui.INFO, user, SECTION)
    e.set_footer(text="고른 후보가 사퇴하거나 합당하면 건 돈을 돌려받아요.\n"
                      "당선돼도 임기 중 공약이 멈추면 상금이 절반, 탄핵되면 상금이 없어요.\n"
                      "60초 안에 후보를 고르지 않으면 베팅금은 돌려받지 못합니다.")
    return e


class PickView(discord.ui.View):
    def __init__(self, cog: "Lobby", user, amount: int, g: dict, table: list[int], no: int):
        super().__init__(timeout=60)
        self.cog, self.user, self.amount, self.g, self.table, self.no = cog, user, amount, g, table, no
        self.message, self.done = None, False
        for i, c in enumerate(g["cands"]):
            b = discord.ui.Button(label=f"{i + 1}번 {c['name']}", style=discord.ButtonStyle.primary, row=i // 3)
            b.callback = self._picker(i)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("로비를 시작한 사람만 후보를 고를 수 있어요.", ephemeral=True)
            return False
        return True

    def _picker(self, i: int):
        async def pick(interaction: discord.Interaction):
            if self.done:
                return await interaction.response.defer()
            self.done = True
            self.stop()
            await self.cog.run(interaction, self, i)
        return pick

    async def on_timeout(self):
        if self.done or self.message is None:
            return
        e = ui.card("로비 실패", '> *"끝내 후보를 고르지 못했습니다. 로비한 돈은 사라졌어요."*'
                    f"\n\n`정산` **{ui.won(-self.amount)}**", ui.LOSE, self.user, SECTION)
        try:
            await self.message.edit(embed=e, view=None)
        except discord.HTTPException:
            pass


def frames(g: dict, pick: int, no: int) -> list[tuple[str, str, int]]:
    """연출 장면 [(제목, 본문, 색)] — 선거운동 사건 → 개표 → 임기."""
    cands, title = g["cands"], f"제{no}회 축구 봇 대선"
    out = [(f"{title} 선거운동 시작", '> *"선거운동이 시작됐습니다!"*\n\n' + board(cands, g["start"], pick), ui.DARK)]
    for k, ev in enumerate(g["events"]):
        head = "속보" if ev["out"] is not None else f"선거운동 {k + 1}일차"
        out.append((f"{title} {head}", f'> *"{ev["text"]}"*\n\n' + board(cands, ev["sup"], pick),
                    ui.LOSE if ev["out"] is not None else ui.DARK))
    for pct, sh in g["count"]:
        if pct < 100:
            out.append((f"{title} 개표 {pct}%", '> *"개표가 진행되고 있습니다!"*\n\n' + board(cands, sh, pick), ui.DARK))
    w = cands[g["winner"]]
    out.append((f"{title} 개표 끝", f'> *"{w["name"]} 후보 당선! 득표율 {g["final"][g["winner"]]:.1f}%"*\n\n'
                + board(cands, g["final"], pick), ui.GOLD))
    for k, t in enumerate(g["term"]):
        color = {"impeach": ui.DOOM, "suspend": ui.LOSE}.get(t["kind"], ui.DARK)
        out.append((f"{w['name']} 대통령 임기 {k + 1}년차", f'> *"{t["text"]}"*', color))
    return out


def result_card(user, g: dict, pick: int, r: dict, bal: int) -> discord.Embed:
    cands, c = g["cands"], g["cands"][pick]
    ranks = "\n".join(f"{n + 1}위 " + (f"**{cands[i]['name']}** (내 후보)" if i == pick else cands[i]["name"])
                      + ("  사퇴" if cands[i]["out"] else "") for n, i in enumerate(g["order"]))
    if r["why"] == "out":
        head, color = f"{c['name']} 후보가 선거에서 빠져서 로비한 돈을 돌려받았어요.", ui.EVEN
    elif r["why"] == "impeach":
        head, color = f"{c['name']} 후보가 당선됐지만 탄핵돼서 상금이 사라졌어요.", ui.DOOM
    elif r["why"] == "suspend":
        head, color = f"{c['name']} 후보가 당선됐지만 공약이 멈춰서 상금이 절반이 됐어요.", ui.WIN
    elif r["rank"] == 0:
        head, color = f"{c['name']} 후보 당선! 탁월한 안목이에요.", ui.GOLD
    else:
        head, color = f"{c['name']} 후보는 {r['rank'] + 1}위로 낙선했어요.", ui.tone(r["delta"])
    return ui.card("대선 결과", f"{head}\n\n{ranks}\n\n`정산` **{ui.won(r['delta'])}**\n`잔액` **{bal:,}원**", color, user, SECTION)


class Lobby(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()

    async def cog_load(self):
        # 예전 일주일 선거: 열려 있던 선거에 로비한 돈을 돌려주고 공약 효과를 끈다 (테스트 서버만 쓰던 것)
        try:
            n = await L.void_weekly(L.LobbyDB())
            if n:
                print(f"[대선] 일주일 선거 닫음: {n}명 로비금 환불")
        except Exception as ex:
            print(f"[대선] 일주일 선거 닫기 오류: {type(ex).__name__}: {ex}")

    @app_commands.command(name="로비", description="대선 후보 여섯 명 중 한 명에게 로비! 사건, 개표, 임기까지 한 판에 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="로비 금액 (최소 1,000원)")
    async def lobby(self, interaction: discord.Interaction, amount: app_commands.Range[int, L.GAME_MIN]):
        if not release.preview_382(interaction.guild_id):
            return await interaction.response.send_message("/로비 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        user, amount = interaction.user, int(amount)
        await interaction.response.defer()
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "로비할 돈이 모자라요", f"`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**", ui.LOSE, user, SECTION))
        g, table, no = L.play(), race_prizes(L.CANDIDATES_N, interaction.guild_id), random.randint(1, 30)
        await self.db.add_balance(user.id, -amount)   # 베팅금 먼저 차감 (경마와 같다)
        view = PickView(self, user, amount, g, table, no)
        try:
            view.message = await interaction.followup.send(embed=ticket_card(user, amount, g, table, no), view=view, wait=True)
        except discord.HTTPException:
            await self.db.add_balance(user.id, amount)
            raise

    async def run(self, interaction: discord.Interaction, view: PickView, pick: int):
        g, user, amount = view.g, view.user, view.amount
        r = L.settle_game(g, pick, amount, view.table)
        await interaction.response.edit_message(embed=ui.card(
            f"제{view.no}회 축구 봇 대선", f'> *"{g["cands"][pick]["name"]} 후보에게 {amount:,}원 로비! 선거운동이 시작됩니다…"*',
            ui.DARK, user, SECTION), view=None)
        # 결과를 먼저 저장하고 연출한다 (연출이 끊겨도 돈은 정확하다). 베팅금은 이미 빠져 있으니 돌려주면서 순이익을 더한다.
        try:
            bal = await self.db.add_balance(user.id, amount + r["delta"])
        except Exception as ex:
            return await interaction.followup.send(f"DB 오류: {type(ex).__name__}")
        scenes = frames(g, pick, view.no)
        try:
            for k, (title, body, color) in enumerate(scenes):
                await asyncio.sleep(GAP_LAST if k >= len(scenes) - 2 else GAP)
                await interaction.edit_original_response(embed=ui.card(title, body, color, user, SECTION))
            await asyncio.sleep(GAP)
        except discord.HTTPException:
            pass
        e = result_card(user, g, pick, r, bal)
        try:
            await interaction.edit_original_response(embed=e)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))
