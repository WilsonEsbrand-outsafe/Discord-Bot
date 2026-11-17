# cogs/lobby.py — /로비 (3.8.2, 테스트 서버 먼저). 규칙 · 수치는 services/lobby.py
# 11/17 규완님 '/경마 방식인데 저렇게 띄우라는 거지' · '/로비 -> 여러 사건 -> 개표 -> 임기 이렇게. 임기 도중 사건이 일어나서 문제가 생길 수도 있게'
# /로비 금액 → 후보 6명과 배당(당선 확률로 정함) → 후보 버튼 → 선거운동 사건 → 개표 → 당선자 임기 → 정산. 경마처럼 한 판에 끝난다.
# 베팅금은 후보표를 띄울 때 먼저 빠져나간다 (60초 안에 안 고르면 잃는다, 경마와 같다).
import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import lobby as L
from services import ui
from services.economy_db import EconomyDB

SECTION = "대선 중계"
GAP, GAP_BIG = 3.5, 5.0      # 장면 사이 (초) — 11/17 규완님 '너무 빨라서 못알아먹겠어' (1.6 → 3.5, 큰 사건은 5초)
STAGES = ("선거운동", "개표", "임기")


def _bar(p: float, width: int = 10) -> str:
    n = max(0, min(width, round(p / 100 * width)))
    return "█" * n + "░" * (width - n)


def _steps(now: int) -> str:
    """'**선거운동** > 개표 > 임기' — 지금 어느 단계인지."""
    return "  >  ".join(f"**{x}**" if k == now else x for k, x in enumerate(STAGES))


def standings(cands: list[dict], sup: list[float], pick: int, prev: list[float] | None = None) -> str:
    """지지율 높은 순, 한 줄에 한 명. 바뀐 만큼 (+3) 표시. 빠진 후보는 맨 아래 한 줄."""
    live = sorted((i for i in range(len(cands)) if sup[i] > 0), key=lambda i: -sup[i])
    top = max(sup) or 1   # 막대는 1위를 꽉 차게 (차이가 눈에 보이게)
    out = []
    for i in live:
        name = f"**{cands[i]['name']}** (내 후보)" if i == pick else cands[i]["name"]
        diff = ""
        if prev is not None and prev[i] > 0 and abs(round(sup[i]) - round(prev[i])) >= 2:   # 사건으로 크게 바뀐 후보만
            diff = f"  ({round(sup[i]) - round(prev[i]):+d})"
        out.append(f"`{_bar(sup[i] * 100 / top, 12)}` {round(sup[i]):>2}%  {name}{diff}")
    gone = [cands[i]["name"] + (" (내 후보)" if i == pick else "") for i in range(len(cands)) if sup[i] <= 0]
    if gone:
        out.append("\n사퇴: " + ", ".join(gone))
    return "\n".join(out)


def ticket_card(user, amount: int, g: dict, no: int) -> discord.Embed:
    e = ui.card(f"제{no}회 축구 봇 대선", f"후보 여섯 명이 출마했어요. 로비할 후보를 버튼으로 골라 주세요.\n로비 금액 **{amount:,}원**",
                ui.INFO, user, SECTION)
    for i, c in enumerate(g["cands"]):
        o = g["odds"][i]
        e.add_field(name=f"{i + 1}번 {c['name']}", inline=True,
                    value=f"{c['party']}\n지지율 {round(g['start'][i])}%\n배당 **{o}배**\n당선되면 {int(amount * o):,}원")
    e.add_field(name="알아 둘 것", inline=False, value=(
        "배당은 당선 확률로 정해져요. 고른 후보가 당선되지 못하면 로비한 돈을 잃어요.\n"
        "당선돼도 임기 중 공약이 멈추면 번 돈이 절반, 탄핵되면 로비한 돈을 잃어요.\n"
        "60초 안에 고르지 않으면 로비한 돈은 사라져요."))
    return e


class PickView(discord.ui.View):
    def __init__(self, cog: "Lobby", user, amount: int, g: dict, no: int):
        super().__init__(timeout=60)
        self.cog, self.user, self.amount, self.g, self.no = cog, user, amount, g, no
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


def frames(g: dict, pick: int, no: int) -> list[tuple[str, str, int, float]]:
    """연출 장면 [(제목, 본문, 색, 다음 장면까지 초)] — 선거운동 사건 → 개표 → 임기."""
    cands = g["cands"]
    out = [(f"제{no}회 축구 봇 대선", f"{_steps(0)}\n\n선거운동이 시작됐어요.\n\n" + standings(cands, g["start"], pick), ui.DARK, GAP)]
    prev = g["start"]
    for k, ev in enumerate(g["events"]):
        big = ev["out"] is not None
        head = "속보" if big else f"선거운동 {k + 1}일차 뉴스"
        out.append((head, f"{_steps(0)}\n\n**{ev['text']}**\n\n" + standings(cands, ev["sup"], pick, prev),
                    ui.LOSE if big else ui.DARK, GAP_BIG if big else GAP))
        prev = ev["sup"]
    for pct, sh in g["count"]:
        if pct < 100:
            out.append((f"개표율 {pct}%", f"{_steps(1)}\n\n개표가 진행되고 있어요. 이건 득표율이에요.\n\n" + standings(cands, sh, pick),
                        ui.DARK, GAP))
    w = cands[g["winner"]]
    mine = "  (내 후보)" if g["winner"] == pick else ""
    out.append(("개표 끝", f"{_steps(1)}\n\n**{w['name']} 후보 당선{mine}**\n득표율 {g['final'][g['winner']]:.1f}%\n\n"
                + standings(cands, g["final"], pick), ui.GOLD, GAP_BIG))
    for k, t in enumerate(g["term"]):
        color = {"impeach": ui.DOOM, "suspend": ui.LOSE}.get(t["kind"], ui.DARK)
        note = {"impeach": "\n\n탄핵됐어요. 이 대통령에게 로비한 사람은 로비한 돈을 잃어요.",
                "suspend": "\n\n공약이 멈췄어요. 이 대통령에게 로비한 사람은 번 돈이 절반이 돼요."}.get(t["kind"], "")
        out.append((f"{w['name']} 대통령 임기 {k + 1}년차", f"{_steps(2)}\n\n**{t['text']}**{note}", color,
                    GAP_BIG if t["kind"] != "ok" else GAP))
    return out


def result_card(user, g: dict, pick: int, r: dict, bal: int) -> discord.Embed:
    cands, c = g["cands"], g["cands"][pick]
    if r["why"] == "out":
        head, color = f"{c['name']} 후보가 선거에서 빠져서 로비한 돈을 잃었어요.", ui.LOSE
    elif r["why"] == "impeach":
        head, color = f"{c['name']} 후보가 당선됐지만 탄핵돼서 로비한 돈을 잃었어요.", ui.DOOM
    elif r["why"] == "suspend":
        head, color = f"{c['name']} 후보가 당선됐지만 공약이 멈춰서 번 돈이 절반이 됐어요. (배당 {r['odds']}배)", ui.WIN
    elif r["why"] is None:
        head, color = f"{c['name']} 후보 당선! 배당 {r['odds']}배를 받았어요.", ui.GOLD
    else:
        head, color = f"{c['name']} 후보가 낙선해서 로비한 돈을 잃었어요.", ui.LOSE
    e = ui.card("대선 결과", head, color, user, SECTION)
    e.add_field(name="최종 순위", inline=False, value="\n".join(
        f"{n + 1}위  " + (f"**{cands[i]['name']}** (내 후보)" if i == pick else cands[i]["name"])
        + ("  사퇴" if cands[i]["out"] else "") for n, i in enumerate(g["order"])))
    e.add_field(name="정산", value=ui.won(r["delta"]), inline=True)
    e.add_field(name="잔액", value=f"{bal:,}원", inline=True)
    return e


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
        if not release.preview_lobby(interaction.guild_id):
            return await interaction.response.send_message("/로비 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        user, amount = interaction.user, int(amount)
        await interaction.response.defer()
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "로비할 돈이 모자라요", f"`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**", ui.LOSE, user, SECTION))
        g, no = await asyncio.to_thread(L.play), random.randint(1, 30)
        await self.db.add_balance(user.id, -amount)   # 베팅금 먼저 차감 (경마와 같다)
        view = PickView(self, user, amount, g, no)
        try:
            view.message = await interaction.followup.send(embed=ticket_card(user, amount, g, no), view=view, wait=True)
        except discord.HTTPException:
            await self.db.add_balance(user.id, amount)
            raise

    async def run(self, interaction: discord.Interaction, view: PickView, pick: int):
        g, user, amount = view.g, view.user, view.amount
        r = L.settle_game(g, pick, amount)
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
            wait = 2.0
            for title, body, color, nxt in scenes:
                await asyncio.sleep(wait)
                await interaction.edit_original_response(embed=ui.card(title, body, color, user, SECTION))
                wait = nxt
        except discord.HTTPException:
            pass
        e = result_card(user, g, pick, r, bal)
        try:
            await interaction.edit_original_response(embed=e)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))
