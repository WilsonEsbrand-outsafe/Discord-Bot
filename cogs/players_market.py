# cogs/players_market.py
from __future__ import annotations

import io
import time
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 한글 폰트 설정 (Ubuntu)
matplotlib.rcParams["font.family"] = "NanumGothic"
matplotlib.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지

from services.economy_db import EconomyDB
from services.player_market_db import PlayerMarketDB, PACKS
from services.notifier import send_notify

_PRICE_LABELS = ["🔴 대박", "🟠 이득", "🟡 본전", "🟢 손해", "⚪ 폭망"]

def _price_label(player_price: int, pack_price: int) -> str:
    """현재가 / 팩 단가 비율로 결과 등급 라벨 반환."""
    if pack_price <= 0:
        return "⚪ 폭망"
    ratio = player_price / pack_price
    if ratio >= 3.0: return "🔴 대박"
    if ratio >= 1.3: return "🟠 이득"
    if ratio >= 0.9: return "🟡 본전"
    if ratio >= 0.5: return "🟢 손해"
    return "⚪ 폭망"

def _embed(title: str, desc: str, user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=0x2ecc71)
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return e

# ───────────────── 즉시판매 UI ─────────────────
_SORT_LABELS = [
    ("💰 가격↓", "price", True),
    ("📊 OVR↓",  "ovr",   True),
    ("🏃 포지션", "pos",   False),
]

class QuickSellView(discord.ui.View):
    """보유 선수 즉시판매 인터랙티브 UI — 정렬·페이지·복수선택·전체판매 지원"""
    PAGE_SIZE = 25

    def __init__(self, holdings: list, pm, money, user: discord.abc.User, now_ts: int):
        super().__init__(timeout=180)
        # 아마추어·은퇴 제외
        self.holdings = [
            h for h in holdings
            if not str(h[0]).startswith("AMT_") and int(h[7]) == 0
        ]
        self.pm = pm
        self.money = money
        self.user = user
        self.now_ts = now_ts
        self.sort_key = "price"
        self.sort_desc = True
        self.page = 0
        self._rebuild()

    # ── 정렬·페이지 헬퍼 ──
    def _sorted(self):
        idx = {"price": 9, "ovr": 5, "pos": 3}[self.sort_key]
        return sorted(self.holdings, key=lambda x: x[idx], reverse=self.sort_desc)

    @property
    def _total_pages(self):
        return max(1, (len(self.holdings) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_items(self):
        s = self._sorted()
        return s[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]

    # ── UI 재구성 ──
    def _rebuild(self):
        self.clear_items()
        items = self._page_items()
        if not items:
            return

        # Row 0: 선수 다중 선택 드롭다운
        options = [
            discord.SelectOption(
                label=f"{name} x{qty}"[:25],
                description=f"{pos} OVR{ovr} | {int(price):,}→{int(price)//2:,}원"[:50],
                value=pid,
            )
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in items
        ]
        sel = discord.ui.Select(
            placeholder="판매할 선수 선택 (복수 선택 가능)",
            min_values=1, max_values=len(options),
            options=options, row=0,
        )
        sel.callback = self._on_select
        self.add_item(sel)

        # Row 1: 정렬 버튼
        for label, key, desc in _SORT_LABELS:
            active = (self.sort_key == key)
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
                row=1,
            )
            btn.callback = self._make_sort_cb(key, desc)
            self.add_item(btn)

        # Row 2: 페이지 이동 + 전체 판매
        if self._total_pages > 1:
            prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary,
                                     row=2, disabled=self.page == 0)
            prev.callback = self._prev
            self.add_item(prev)

            next_ = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary,
                                      row=2, disabled=self.page >= self._total_pages - 1)
            next_.callback = self._next
            self.add_item(next_)

        all_btn = discord.ui.Button(
            label=f"🗑️ 전체 판매 ({len(self.holdings)}명)",
            style=discord.ButtonStyle.danger, row=2,
        )
        all_btn.callback = self._sell_all
        self.add_item(all_btn)

    def make_embed(self) -> discord.Embed:
        items = self._page_items()
        total_receive = sum(int(h[9]) for h in self.holdings) // 2
        lines = [
            f"`{name}` x{qty} | {pos} OVR{ovr} | {int(price):,}원 → **{int(price)//2:,}원**"
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in items
        ]
        desc = (
            f"보유 **{len(self.holdings)}명** | 전체 즉판 예상: **{total_receive:,}원**\n"
            f"페이지 {self.page+1}/{self._total_pages}\n\n"
            + "\n".join(lines)
        )
        return discord.Embed(title="💸 즉시판매", description=desc, color=0xe74c3c)

    # ── 콜백 팩토리 ──
    def _make_sort_cb(self, key, desc):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
            self.sort_key = key; self.sort_desc = desc; self.page = 0
            self._rebuild()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        return cb

    async def _prev(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        self.page -= 1; self._rebuild()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        self.page += 1; self._rebuild()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    # ── 판매 처리 ──
    async def _do_sell(self, interaction: discord.Interaction, pids: list[str]):
        total_payout, sold, failed = 0, 0, 0
        detail_lines = []  # 소량(≤10)일 때만 개별 표시

        for pid in pids:
            matching = next((h for h in self.holdings if h[0] == pid), None)
            qty = int(matching[8]) if matching else 1
            ok, msg, payout = await self.pm.direct_instant_sell(
                user_id=self.user.id, player_id=pid, qty=qty,
                now_ts=self.now_ts, add_balance=self.money.add_balance,
            )
            if ok:
                total_payout += payout
                sold += 1
                if len(pids) <= 10:
                    detail_lines.append(msg)
            else:
                failed += 1
                if len(pids) <= 10:
                    detail_lines.append(f"❌ {msg}")
            self.holdings = [h for h in self.holdings if h[0] != pid]

        bal = await self.money.get_balance(self.user.id)

        # 결과 메시지 — 대량이면 요약, 소량이면 상세
        if detail_lines:
            body = "\n".join(detail_lines)
        else:
            body = f"✅ **{sold}명** 판매 완료" + (f"  |  ❌ 실패 {failed}건" if failed else "")
        body += f"\n\n💰 총 실수령: **{total_payout:,}원** | 잔액: **{bal:,}원**"

        # 2000자 초과 방지
        if len(body) > 1900:
            body = f"✅ **{sold}명** 판매 완료\n💰 총 실수령: **{total_payout:,}원** | 잔액: **{bal:,}원**"

        await interaction.followup.send(body, ephemeral=True)

        if not self.holdings:
            self.clear_items()
            await interaction.edit_original_response(
                embed=discord.Embed(title="💸 즉시판매", description="판매할 선수가 없습니다.", color=0x95a5a6),
                view=self,
            )
        else:
            self.page = min(self.page, self._total_pages - 1)
            self._rebuild()
            await interaction.edit_original_response(embed=self.make_embed(), view=self)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._do_sell(interaction, interaction.data["values"])

    async def _sell_all(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._do_sell(interaction, [h[0] for h in list(self.holdings)])


class PlayersMarket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()
        self.expire_task.start()

    def cog_unload(self):
        self.expire_task.cancel()

    async def cog_load(self):
        await self.pm.ensure_bootstrap(int(time.time()))

    @tasks.loop(minutes=10)
    async def expire_task(self):
        """만료된 이적시장 매물 자동 반환 (10분마다)"""
        try:
            expired = await self.pm.expire_listings(int(time.time()))
            if expired:
                print(f"[이적시장] 만료 처리: {len(expired)}건")
                for info in expired:
                    dm_embed = discord.Embed(
                        title="⏰ 이적시장 매물 만료",
                        description=(
                            f"**{info['name']}** x{info['qty']}장\n"
                            f"매물이 만료되어 보유 목록으로 돌아왔습니다."
                        ),
                        color=0xe67e22,
                    )
                    await send_notify(self.bot, self.money, info["seller_id"], "매물_만료", dm_embed)
        except Exception as e:
            print(f"[이적시장] expire_task 오류: {e}")

    @expire_task.before_loop
    async def before_expire_task(self):
        await self.bot.wait_until_ready()

    # ───────────────── 자동완성 ─────────────────
    async def player_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """선수 정보·시세 조회용 — 은퇴 선수도 포함"""
        try:
            rows = await self.pm.search_players(current, limit=10)
            choices = []
            for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
                tag = " (은퇴)" if int(retired) == 1 else ""
                label = f"{name}{tag} | {pos} OVR{ovr} | {int(price):,}원"
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices
        except Exception:
            return []

    async def active_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """구매용 — 활동 중인 선수만 표시"""
        try:
            rows = await self.pm.search_players(current, limit=15)
            choices = []
            for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
                if int(retired) == 1:
                    continue
                label = f"{name} | {pos} OVR{ovr} | {int(price):,}원"
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices[:10]
        except Exception:
            return []

    async def holding_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """판매용 — 내가 보유한 활동 선수 전체에서 검색"""
        try:
            rows = await self.pm.list_holdings(interaction.user.id, limit=9999)
            q = current.lower()
            choices = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                if int(retired) == 1:
                    continue
                label = f"{name} x{qty} | {pos} OVR{ovr} | {int(price):,}원"
                if q and q not in name.lower() and q not in pid.lower() and q not in pos.lower():
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices[:25]
        except Exception:
            return []

    async def pack_type_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """팩 종류 자동완성"""
        pack_emoji = {"브론즈": "🥉", "실버": "🥈", "골드": "🥇", "플래티넘": "💎", "아이콘": "👑"}
        return [
            app_commands.Choice(
                name=f"{pack_emoji.get(k, '🎁')} {k}팩  |  {PACKS[k]['price']:,}원 / 장",
                value=k,
            )
            for k in PACKS
            if not current or current in k
        ]

    async def retired_holding_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """방출용 — 내가 보유한 은퇴 선수 전체에서 검색"""
        try:
            rows = await self.pm.list_holdings(interaction.user.id, limit=9999)
            q = current.lower()
            choices = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                if int(retired) != 1:
                    continue
                label = f"(은퇴) {name} x{qty} | {pos} OVR{ovr}"
                if q and q not in name.lower() and q not in pid.lower():
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices[:25]
        except Exception:
            return []

    async def my_listing_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """내 매물 자동완성 (즉시판매·취소용)"""
        try:
            rows = await self.pm.get_my_listings(interaction.user.id)
            now = int(time.time())
            choices = []
            for lid, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
                can_instant = now >= int(instant_sell_at)
                tag = "✅즉시가능" if can_instant else "⏳대기중"
                label = f"#{lid} {name} x{qty} | {int(price_per):,}원 | {tag}"
                if current and current not in label and current not in str(lid):
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=str(lid)))
            return choices[:10]
        except Exception:
            return []

    async def listing_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """이적시장 매물 자동완성 (구매용)"""
        try:
            rows = await self.pm.get_listings(current, limit=10)
            choices = []
            for lid, seller_id, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
                label = f"#{lid} {name} x{qty} | {pos} OVR{ovr}/{potg} | {int(price_per):,}원"
                choices.append(app_commands.Choice(name=label[:100], value=str(lid)))
            return choices[:10]
        except Exception:
            return []

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

            status_emoji = "🟢" if st.is_open else "🔴"
            msg = (
                f"{status_emoji} **{st.reason}**\n"
                f"운영 시간: 매일 **09:00 ~ 23:00 KST**\n"
                f"다음 변경: <t:{st.next_change_ts}:f>"
            )
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
    @app_commands.describe(player_id="선수 이름 또는 ID")
    @app_commands.autocomplete(player_id=player_id_autocomplete)
    async def player_info(self, interaction: discord.Interaction, player_id: str):
        await interaction.response.defer(ephemeral=True)
        row = await self.pm.get_player(player_id)
        if not row:
            return await interaction.followup.send("선수를 찾을 수 없습니다.", ephemeral=True)

        (pid, name, nation, pos, age, ovr, potg, basev, retired, price, floor_p, ceil_p, last_ts) = row
        have = await self.pm.get_holding(interaction.user.id, pid)
        tag = "은퇴" if int(retired) == 1 else "활동"
        retire_note = "\n- ⚠️ 은퇴 선수는 기준가의 30%로 방출 가능" if int(retired) == 1 else ""

        desc = (
            f"**{name}** (`{pid}`)\n"
            f"- 상태: **{tag}**\n"
            f"- 국적/포지션: {nation} / {pos}\n"
            f"- 나이/OVR/POT: {age}세 / **{ovr}** / **{potg}**\n"
            f"- 기준가: **{int(basev):,}원**\n"
            f"- 현재가: **{int(price):,}원**\n"
            f"- 가격 범위: {int(floor_p):,} ~ {int(ceil_p):,}\n"
            f"- 내 보유: **{have}장**"
            f"{retire_note}"
        )
        await interaction.followup.send(embed=_embed("📌 선수 정보", desc, interaction.user), ephemeral=True)

    # ───────────────── 보유 ─────────────────
    @app_commands.command(name="내선수", description="내가 보유한 선수 목록을 봅니다.")
    @app_commands.describe(페이지="페이지 번호 (기본 1, 페이지당 20명)")
    async def holdings(self, interaction: discord.Interaction, 페이지: int = 1):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        try:
            per_page = 20
            페이지 = max(1, 페이지)
            offset = (페이지 - 1) * per_page

            total_count = await self.pm.count_holdings(interaction.user.id)
            if total_count == 0:
                return await interaction.followup.send("보유한 선수가 없습니다.")

            total_pages = max(1, (total_count + per_page - 1) // per_page)
            if 페이지 > total_pages:
                return await interaction.followup.send(f"해당 페이지가 없습니다. (최대 {total_pages}페이지)")

            rows = await self.pm.list_holdings(interaction.user.id, limit=per_page, offset=offset)
            total_value = await self.pm.portfolio_value(interaction.user.id)

            lines = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                tag = " (은퇴)" if int(retired) == 1 else ""
                lines.append(f"`{pid}` {name}{tag} x{qty} / {pos} / OVR {ovr} / POT {potg} / {int(price):,}원")

            header = (
                f"총 **{total_count}명** 보유 | 전체 평가액: **{total_value:,}원**\n"
                f"페이지 {페이지} / {total_pages}\n\n"
            )

            await interaction.followup.send(
                embed=_embed("📦 내 보유", header + "\n".join(lines), interaction.user)
            )

        except (discord.NotFound, discord.HTTPException):
            return

    # ───────────────── 거래 ─────────────────
    @app_commands.command(name="판매", description="선수를 이적시장에 등록합니다. (12h 후 즉시판매 가능 / 72h 후 자동 만료)")
    @app_commands.describe(player_id="등록할 선수", 가격="1장당 희망 가격(원)", 수량="등록 수량")
    @app_commands.autocomplete(player_id=holding_player_autocomplete)
    async def sell(self, interaction: discord.Interaction, player_id: str, 가격: int, 수량: int = 1):
        await interaction.response.defer(ephemeral=True)
        now = int(time.time())
        ok, msg = await self.pm.create_listing(
            seller_id=interaction.user.id,
            player_id=player_id,
            qty=수량,
            price_per=가격,
            now_ts=now,
        )
        await interaction.followup.send(
            embed=_embed("📋 이적시장 등록" if ok else "❌ 등록 실패", msg, interaction.user),
            ephemeral=True,
        )

    # ───────────────── 팩 ─────────────────
    @app_commands.command(name="선수팩", description="선수팩을 구매합니다. (종류별 확률/가격 차등)")
    @app_commands.describe(종류="브론즈/실버/골드/플래티넘/아이콘", 장수="1~10")
    @app_commands.autocomplete(종류=pack_type_autocomplete)
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

        pack_price_per = PACKS[종류]["price"]
        label_cnt = {k: 0 for k in _PRICE_LABELS}
        lines = []
        total_value = 0

        for pid, pull_price in results:
            label = _price_label(pull_price, pack_price_per)
            label_cnt[label] += 1
            row = await self.pm.get_player(pid)
            if not row:
                lines.append(f"• {label} `{pid}`")
                continue
            (_, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row
            total_value += int(price)
            lines.append(f"• {label} `{pid}` {name} ({nation}) {pos} / OVR {ovr} / **{int(price):,}원**")

        bal = await self.money.get_balance(interaction.user.id)
        grade_summary = " / ".join(f"{k} {v}" for k, v in label_cnt.items() if v > 0)
        summary = (
            f"{msg}\n"
            f"팩 단가: **{pack_price_per:,}원** / 현재 잔액: **{bal:,}원**\n"
            f"{grade_summary}\n"
            f"획득 현재가 합: **{total_value:,}원**\n\n"
            f"획득 목록(최대 10개 표시):\n" + "\n".join(lines[:10])
        )
        await interaction.followup.send(embed=_embed("🎁 선수팩 결과", summary, interaction.user))

    # ───────────────── 시세 그래프 ─────────────────
    @app_commands.command(name="시세", description="선수 가격 변동 그래프를 봅니다.")
    @app_commands.describe(player_id="선수 이름 또는 ID", hours="조회 시간(기본 24시간)")
    @app_commands.autocomplete(player_id=player_id_autocomplete)
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

    # ───────────────── 팩 정보 ─────────────────
    @app_commands.command(name="팩정보", description="팩 종류별 가격과 뽑기 분포를 확인합니다.")
    async def pack_info(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        pack_emoji = {"브론즈": "🥉", "실버": "🥈", "골드": "🥇", "플래티넘": "💎", "아이콘": "👑"}

        embed = discord.Embed(
            title="🎁 팩 정보",
            description=(
                "팩 가격 = 뽑기 분포의 중심\n"
                "**팩 단가와 비슷한 선수**가 가장 많이 나오고,\n"
                "비싼 선수일수록 확률이 급격히 낮아집니다.\n\n"
                "🔴 대박 `≥ 3배` · 🟠 이득 `≥ 1.3배` · 🟡 본전 `≥ 0.9배`\n"
                "🟢 손해 `≥ 0.5배` · ⚪ 폭망 `< 0.5배`"
            ),
            color=0x2ecc71,
        )

        for pack_name, pack_data in PACKS.items():
            price = pack_data["price"]
            icon = pack_emoji.get(pack_name, "🎁")
            embed.add_field(
                name=f"{icon} {pack_name}팩  |  {price:,}원 / 장",
                value=f"중심가 **{price:,}원** 근처 선수가 가장 많이 출현",
                inline=False,
            )

        embed.set_footer(text="최대 10장까지 한 번에 구매 가능 · 수수료 없음")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ───────────────── 이적시장 ─────────────────

    @app_commands.command(name="이적시장", description="유저들이 올린 이적시장 매물을 조회합니다.")
    @app_commands.describe(검색어="선수명/국적/포지션/등급 검색 (비우면 최신순)", 페이지="페이지 번호")
    async def transfer_market(self, interaction: discord.Interaction, 검색어: str = "", 페이지: int = 1):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        per_page = 10
        페이지 = max(1, 페이지)
        offset = (페이지 - 1) * per_page

        total = await self.pm.count_listings(검색어)
        if total == 0:
            return await interaction.followup.send(
                embed=_embed("🏟️ 이적시장", "현재 등록된 매물이 없습니다.", interaction.user)
            )

        total_pages = max(1, (total + per_page - 1) // per_page)
        if 페이지 > total_pages:
            return await interaction.followup.send(f"❌ 페이지가 없습니다. (최대 {total_pages}페이지)")

        rows = await self.pm.get_listings(검색어, limit=per_page, offset=offset)
        now = int(time.time())

        lines = []
        for lid, seller_id, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
            time_left = max(0, int(expires_at) - now)
            h = time_left // 3600
            lines.append(
                f"`#{lid}` **{name}** | {pos} OVR **{ovr}** / {potg}등급\n"
                f"　{nation} · {age}세 | **{int(price_per):,}원** × {qty}장 | 만료 {h}h"
            )

        header = f"총 **{total}건** 매물 | 페이지 {페이지}/{total_pages}\n\n"
        await interaction.followup.send(
            embed=_embed("🏟️ 이적시장", header + "\n".join(lines), interaction.user)
        )

    @app_commands.command(name="구매", description="이적시장 매물을 구매합니다. (플랫폼 수수료 5%)")
    @app_commands.describe(매물번호="매물 번호 (/이적시장 에서 확인)", 수량="구매 수량")
    @app_commands.autocomplete(매물번호=listing_autocomplete)
    async def buy_transfer(self, interaction: discord.Interaction, 매물번호: str, 수량: int = 1):
        await interaction.response.defer(ephemeral=True)
        try:
            lid = int(str(매물번호).lstrip("#"))
        except ValueError:
            return await interaction.followup.send("❌ 올바른 매물 번호를 입력하세요.", ephemeral=True)

        now = int(time.time())
        ok, msg, notify_info = await self.pm.buy_listing(
            listing_id=lid,
            buyer_id=interaction.user.id,
            qty=수량,
            now_ts=now,
            get_balance=self.money.get_balance,
            add_balance=self.money.add_balance,
        )
        bal = await self.money.get_balance(interaction.user.id)
        if ok:
            msg += f"\n현재 잔액: **{bal:,}원**"
            # 판매자 DM 알림
            if notify_info:
                dm_embed = discord.Embed(
                    title="🏷️ 이적시장 매물 판매됨",
                    description=(
                        f"**{notify_info['name']}** x{notify_info['qty']}장이 판매됐습니다.\n"
                        f"판매가: **{notify_info['price']:,}원**/장\n"
                        f"수령액: **{notify_info['seller_gets']:,}원** (수수료 5% 제외)"
                    ),
                    color=0x2ecc71,
                )
                await send_notify(self.bot, self.money, notify_info["seller_id"], "매물_판매", dm_embed)
        await interaction.followup.send(
            embed=_embed("✅ 이적 구매" if ok else "❌ 구매 실패", msg, interaction.user),
            ephemeral=True,
        )

    @app_commands.command(name="내매물", description="내가 이적시장에 등록한 활성 매물을 확인합니다.")
    async def my_listings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        rows = await self.pm.get_my_listings(interaction.user.id)
        if not rows:
            return await interaction.followup.send(
                embed=_embed("📋 내 매물", "등록된 매물이 없습니다.", interaction.user),
                ephemeral=True,
            )

        now = int(time.time())
        lines = []
        for lid, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
            can_instant = now >= int(instant_sell_at)
            instant_tag = "✅ 즉시판매 가능" if can_instant else f"⏳ {max(0, int(instant_sell_at) - now) // 3600}h 후 즉시판매"
            h_left = max(0, int(expires_at) - now) // 3600
            lines.append(
                f"`#{lid}` **{name}** x{qty} | {pos} OVR {ovr}\n"
                f"　**{int(price_per):,}원**/장 | 만료 {h_left}h | {instant_tag}"
            )

        await interaction.followup.send(
            embed=_embed("📋 내 매물", "\n".join(lines), interaction.user),
            ephemeral=True,
        )

    @app_commands.command(name="매각", description="이적시장 등록 후 12시간 뒤 즉시 판매 가능. 기준가의 70% 지급.")
    @app_commands.describe(매물번호="매각할 매물 번호 (/내매물 에서 확인)")
    @app_commands.autocomplete(매물번호=my_listing_autocomplete)
    async def instant_sell(self, interaction: discord.Interaction, 매물번호: str):
        await interaction.response.defer(ephemeral=True)
        try:
            lid = int(str(매물번호).lstrip("#"))
        except ValueError:
            return await interaction.followup.send("❌ 올바른 매물 번호를 입력하세요.", ephemeral=True)

        now = int(time.time())
        ok, msg = await self.pm.instant_sell_listing(
            listing_id=lid,
            seller_id=interaction.user.id,
            now_ts=now,
            add_balance=self.money.add_balance,
        )
        if ok:
            bal = await self.money.get_balance(interaction.user.id)
            msg += f"\n현재 잔액: **{bal:,}원**"
        await interaction.followup.send(
            embed=_embed("💸 매각 완료" if ok else "❌ 매각 실패", msg, interaction.user),
            ephemeral=True,
        )

    @app_commands.command(name="이적취소", description="이적시장 매물을 취소하고 선수를 돌려받습니다.")
    @app_commands.describe(매물번호="취소할 매물 번호 (/내매물 에서 확인)")
    @app_commands.autocomplete(매물번호=my_listing_autocomplete)
    async def cancel_listing_cmd(self, interaction: discord.Interaction, 매물번호: str):
        await interaction.response.defer(ephemeral=True)
        try:
            lid = int(str(매물번호).lstrip("#"))
        except ValueError:
            return await interaction.followup.send("❌ 올바른 매물 번호를 입력하세요.", ephemeral=True)

        ok, msg = await self.pm.cancel_listing(
            listing_id=lid,
            seller_id=interaction.user.id,
        )
        await interaction.followup.send(
            embed=_embed("✅ 매물 취소" if ok else "❌ 취소 실패", msg, interaction.user),
            ephemeral=True,
        )

    @app_commands.command(name="즉시판매", description="보유 선수를 기준가 50%에 즉시 매각합니다. 정렬·복수선택·전체판매 지원.")
    async def quick_sell(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        holdings = await self.pm.list_holdings(interaction.user.id, limit=9999)
        if not holdings:
            return await interaction.followup.send("보유한 선수가 없습니다.", ephemeral=True)

        now = int(time.time())
        view = QuickSellView(holdings, self.pm, self.money, interaction.user, now)
        if not view.holdings:
            return await interaction.followup.send("즉시판매 가능한 선수가 없습니다. (아마추어·은퇴 선수 제외)", ephemeral=True)

        await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=True)

    @app_commands.command(name="방출", description="은퇴 선수를 기준가의 30%에 즉시 방출합니다.")
    @app_commands.describe(player_id="방출할 은퇴 선수 ID", qty="수량")
    @app_commands.autocomplete(player_id=retired_holding_autocomplete)
    async def release(self, interaction: discord.Interaction, player_id: str, qty: int = 1):
        await interaction.response.defer(ephemeral=True)

        row = await self.pm.get_player(player_id)
        if not row:
            return await interaction.followup.send("❌ 선수를 찾을 수 없습니다.", ephemeral=True)

        # row: pid, name, nation, pos, age, ovr, potg, basev, retired, price, floor_p, ceil_p, last_ts
        retired = int(row[8])
        name    = row[1]
        if retired != 1:
            return await interaction.followup.send(
                embed=_embed(
                    "❌ 방출 실패",
                    f"**{name}**은(는) 은퇴 선수가 아닙니다.\n활성 선수는 `/판매`로 이적시장에 등록하세요.",
                    interaction.user,
                ),
                ephemeral=True,
            )

        now = int(time.time())
        ok, msg = await self.pm.sell_to_market(
            user_id=interaction.user.id,
            player_id=player_id,
            qty=qty,
            now_ts=now,
            add_balance=self.money.add_balance,
        )
        if ok:
            bal = await self.money.get_balance(interaction.user.id)
            msg += f"\n현재 잔액: **{bal:,}원**"
        await interaction.followup.send(
            embed=_embed("💀 선수 방출" if ok else "❌ 방출 실패", msg, interaction.user),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(PlayersMarket(bot))
        