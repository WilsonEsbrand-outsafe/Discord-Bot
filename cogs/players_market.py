# cogs/players_market.py
from __future__ import annotations

import io
import time
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 한글 폰트 설정 (Ubuntu)
matplotlib.rcParams["font.family"] = "NanumGothic"
matplotlib.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지

from services.economy_db import EconomyDB
from services.player_market_db import PlayerMarketDB, PACKS

def _embed(title: str, desc: str, user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=0x2ecc71)
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return e

class PlayersMarket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()

    async def cog_load(self):
        await self.pm.ensure_bootstrap(int(time.time()))

    # ───────────────── 시장 상태 ─────────────────
    @app_commands.command(name="시장", description="시장 오픈/클로즈 상태를 확인합니다.")
    async def market_status(self, interaction: discord.Interaction):
        # ✅ 3초 제한 회피: 먼저 defer
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

        try:
            now = int(time.time())
            st = await self.pm.market_status(now)

            msg = f"**{st.reason}**\n다음 변경: <t:{st.next_change_ts}:f>"
            await interaction.followup.send(
                embed=_embed("📈 선수 시장", msg, interaction.user),
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {type(e).__name__}", ephemeral=True)

    # ───────────────── 선수 검색/정보 ─────────────────
    @app_commands.command(name="선수검색", description="선수를 검색합니다. (이름/국적/포지션/ID)")
    @app_commands.describe(q="검색어(비우면 고가 TOP)")
    async def search(self, interaction: discord.Interaction, q: str = ""):
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

        rows = await self.pm.search_players(q, limit=10)
        if not rows:
            return await interaction.followup.send("결과가 없습니다.")

        lines = []
        for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
            tag = " (은퇴)" if int(retired) == 1 else ""
            lines.append(f"`{pid}` {name}{tag} / {nation} / {pos} / {age}세 / OVR {ovr} / POT {potg} / **{int(price):,}원**")

        await interaction.followup.send(embed=_embed("🔎 선수검색", "\n".join(lines), interaction.user))

    @app_commands.command(name="선수", description="선수 상세 정보를 봅니다.")
    @app_commands.describe(player_id="선수 ID")
    async def player_info(self, interaction: discord.Interaction, player_id: str):
        await interaction.response.defer()
        row = await self.pm.get_player(player_id)
        if not row:
            return await interaction.followup.send("선수를 찾을 수 없습니다.")

        (pid, name, nation, pos, age, ovr, potg, basev, retired, price, floor_p, ceil_p, last_ts) = row
        have = await self.pm.get_holding(interaction.user.id, pid)
        tag = "은퇴" if int(retired) == 1 else "활동"

        desc = (
            f"**{name}** (`{pid}`)\n"
            f"- 상태: **{tag}**\n"
            f"- 국적/포지션: {nation} / {pos}\n"
            f"- 나이/OVR/POT: {age}세 / **{ovr}** / **{potg}**\n"
            f"- 기준가: **{int(basev):,}원**\n"
            f"- 현재가: **{int(price):,}원**\n"
            f"- 가격 범위: {int(floor_p):,} ~ {int(ceil_p):,}\n"
            f"- 내 보유: **{have}장**"
        )
        await interaction.followup.send(embed=_embed("📌 선수 정보", desc, interaction.user))

    # ───────────────── 보유 ─────────────────
    @app_commands.command(name="보유", description="내가 보유한 선수 목록을 봅니다.")
    async def holdings(self, interaction: discord.Interaction):
        # ✅ interaction 만료/이미 응답 케이스 안전 처리
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        try:
            rows = await self.pm.list_holdings(interaction.user.id, limit=25)
            if not rows:
                return await interaction.followup.send("보유한 선수가 없습니다.")

            lines = []
            total = 0
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                tag = " (은퇴)" if int(retired) == 1 else ""
                v = int(qty) * int(price)
                total += v
                lines.append(f"`{pid}` {name}{tag} x{qty} / {pos} / OVR {ovr} / POT {potg} / {int(price):,}원")

            await interaction.followup.send(
                embed=_embed("📦 내 보유", f"총 평가액: **{total:,}원**\n\n" + "\n".join(lines), interaction.user)
            )

        except (discord.NotFound, discord.HTTPException):
            # followup 단계에서 interaction이 죽었을 때도 조용히 종료
            return

    # ───────────────── 거래 ─────────────────
    @app_commands.command(name="구매", description="시장가로 선수를 구매합니다. (시장 오픈 시간만)")
    @app_commands.describe(player_id="선수 ID", qty="수량")
    async def buy(self, interaction: discord.Interaction, player_id: str, qty: int = 1):
        await interaction.response.defer(ephemeral=True)
        now = int(time.time())
        ok, msg = await self.pm.buy_from_market(
            user_id=interaction.user.id,
            player_id=player_id,
            qty=qty,
            now_ts=now,
            get_balance=self.money.get_balance,
            add_balance=self.money.add_balance,
        )
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="판매", description="시장가로 선수를 판매합니다. (수수료 5%, 시장 오픈 시간만)")
    @app_commands.describe(player_id="선수 ID", qty="수량")
    async def sell(self, interaction: discord.Interaction, player_id: str, qty: int = 1):
        await interaction.response.defer(ephemeral=True)
        now = int(time.time())
        ok, msg = await self.pm.sell_to_market(
            user_id=interaction.user.id,
            player_id=player_id,
            qty=qty,
            now_ts=now,
            add_balance=self.money.add_balance,
        )
        await interaction.followup.send(msg, ephemeral=True)

    # ───────────────── 팩 ─────────────────
    @app_commands.command(name="선수팩", description="선수팩을 구매합니다. (종류별 확률/가격 차등)")
    @app_commands.describe(종류="브론즈/실버/골드/플래티넘/아이콘", 장수="1~10")
    async def pack(self, interaction: discord.Interaction, 종류: str, 장수: int = 1):
        await interaction.response.defer()

        종류 = (종류 or "").strip()
        if 종류 not in PACKS:
            kinds = ", ".join(PACKS.keys())
            return await interaction.followup.send(embed=_embed("❌ 선수팩", f"존재하지 않는 팩입니다.\n가능: {kinds}", interaction.user))

        now = int(time.time())
        ok, msg, results = await self.pm.buy_pack(
            user_id=interaction.user.id,
            pack_type=종류,
            pulls=장수,
            now_ts=now,
            get_balance=self.money.get_balance,
            add_balance=self.money.add_balance,
        )
        if not ok or not results:
            return await interaction.followup.send(embed=_embed("❌ 선수팩", msg, interaction.user))

        grade_cnt = {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0}
        lines = []
        total_value = 0

        for pid, g in results:
            grade_cnt[g] = grade_cnt.get(g, 0) + 1
            row = await self.pm.get_player(pid)
            if not row:
                lines.append(f"• `{pid}` / POT {g}")
                continue
            (_, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row
            total_value += int(price)
            lines.append(f"• `{pid}` {name} ({nation}) {pos} / OVR {ovr} / POT {g} / {int(price):,}원")

        bal = await self.money.get_balance(interaction.user.id)
        price = PACKS[종류]["price"]
        summary = (
            f"{msg}\n"
            f"팩 단가: **{price:,}원** / 현재 잔액: **{bal:,}원**\n"
            f"등급: S {grade_cnt['S']} / A {grade_cnt['A']} / B {grade_cnt['B']} / C {grade_cnt['C']} / D {grade_cnt['D']}\n"
            f"획득 현재가 합: **{total_value:,}원**\n\n"
            f"획득 목록(최대 10개 표시):\n" + "\n".join(lines[:10])
        )
        await interaction.followup.send(embed=_embed("🎁 선수팩 결과", summary, interaction.user))

    # ───────────────── 시세 그래프 ─────────────────
    @app_commands.command(name="시세", description="선수 가격 변동 그래프를 봅니다.")
    @app_commands.describe(player_id="선수 ID", hours="조회 시간(기본 24시간)")
    async def chart(self, interaction: discord.Interaction, player_id: str, hours: int = 24):
        import datetime as dt
        import matplotlib.dates as mdates
        from matplotlib.ticker import FuncFormatter

        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

        hours = max(1, min(168, int(hours)))
        now = int(time.time())
        since = now - hours * 3600

        row = await self.pm.get_player(player_id)
        if not row:
            return await interaction.followup.send("선수를 찾을 수 없습니다.")
        (pid, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row

        hist = await self.pm.price_history(pid, since_ts=since, limit=400)
        if len(hist) < 2:
            return await interaction.followup.send("그래프 데이터가 아직 부족합니다. (시장 틱이 쌓여야 합니다)")

        xs = [dt.datetime.fromtimestamp(t) for (t, _p) in hist]
        ys = [int(p) for (_t, p) in hist]

        current_price = int(ys[-1])
        prev_price = int(ys[-2])
        diff = current_price - prev_price
        pct = (diff / prev_price * 100) if prev_price else 0.0
        sign = "+" if diff > 0 else ""
        diff_text = f"{sign}{diff:,}원 ({sign}{pct:.2f}%)"

        # ✅ 그래프 생성(블로킹)을 스레드로 분리
        def _make_png() -> bytes:
            plt.figure(figsize=(8, 4.5))
            plt.plot(xs, ys, marker="o", markersize=3, linewidth=1.5)
            plt.title(f"{name} ({pid})")
            plt.xlabel("시간")
            plt.ylabel("가격(원)")

            ax = plt.gca()
            ax.grid(True, linestyle="--", alpha=0.35)

            locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))

            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}"))
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=160)
            plt.close()
            return buf.getvalue()

        png_bytes = await asyncio.to_thread(_make_png)

        file = discord.File(fp=io.BytesIO(png_bytes), filename="chart.png")
        e = _embed(
            "📊 시세",
            f"**{name}** (`{pid}`)\n{nation} / {pos} / {age}세 / OVR {ovr} / POT {potg}\n"
            f"현재가: **{current_price:,}원**\n직전가: **{prev_price:,}원**\n변동: **{diff_text}**",
            interaction.user,
        )
        e.set_image(url="attachment://chart.png")
        await interaction.followup.send(embed=e, file=file)

async def setup(bot):
    await bot.add_cog(PlayersMarket(bot))
        