# cogs/trade.py
from __future__ import annotations

import time
import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.economy_db import EconomyDB
from services.player_market_db import PlayerMarketDB


def _embed(title: str, desc: str, color: int = 0x2ecc71) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


class TradeView(discord.ui.View):
    """트레이드 수락/거절 버튼 (24시간 타임아웃)"""

    def __init__(self, trade_id: int, cog: "Trade"):
        super().__init__(timeout=86400)
        self.trade_id = trade_id
        self.cog = cog
        self._done = False

    def _disable_all(self):
        for item in self.children:
            item.disabled = True  # type: ignore

    @discord.ui.button(label="✅ 수락", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._done:
            return await interaction.response.send_message("이미 처리된 트레이드입니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        ok, msg = await self.cog.pm.accept_trade(
            trade_id=self.trade_id,
            receiver_id=interaction.user.id,
            now_ts=int(time.time()),
            get_balance=self.cog.money.get_balance,
            add_balance=self.cog.money.add_balance,
        )

        if ok:
            self._done = True
            self._disable_all()
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

        await interaction.followup.send(
            embed=_embed("✅ 트레이드 수락" if ok else "❌ 수락 실패", msg, 0x2ecc71 if ok else 0xe74c3c),
            ephemeral=True,
        )

    @discord.ui.button(label="❌ 거절", style=discord.ButtonStyle.danger)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._done:
            return await interaction.response.send_message("이미 처리된 트레이드입니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        ok, msg = await self.cog.pm.reject_trade(
            trade_id=self.trade_id,
            receiver_id=interaction.user.id,
        )

        if ok:
            self._done = True
            self._disable_all()
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

        await interaction.followup.send(
            embed=_embed("거절됨" if ok else "❌ 거절 실패", msg, 0x95a5a6 if ok else 0xe74c3c),
            ephemeral=True,
        )

    async def on_timeout(self):
        self._disable_all()
        # 메시지 참조 없이 버튼 비활성화 불가 — DB 만료 처리는 expire_task 가 담당


class Trade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()
        self.expire_task.start()

    def cog_unload(self):
        self.expire_task.cancel()

    @tasks.loop(minutes=15)
    async def expire_task(self):
        try:
            count = await self.pm.expire_trades(int(time.time()))
            if count > 0:
                print(f"[트레이드] 만료 처리: {count}건")
        except Exception as e:
            print(f"[트레이드] expire_task 오류: {e}")

    @expire_task.before_loop
    async def before_expire_task(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _parse_pids(s: str) -> list[str]:
        if not s or not s.strip():
            return []
        return [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]

    # ───────────────── 자동완성 ─────────────────

    async def my_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """내선수 자동완성 — 쉼표 구분 다중 입력 지원. 마지막 토큰 기준으로 내 보유 선수 검색."""
        try:
            parts = [p.strip() for p in current.replace("，", ",").split(",")]
            prefix = ", ".join(parts[:-1])   # 이미 확정된 앞 부분
            last   = parts[-1]               # 현재 입력 중인 마지막 토큰

            rows = await self.pm.list_holdings(interaction.user.id, limit=50)
            choices = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                if int(retired) == 1:
                    continue
                label = f"{name} x{qty} | {pos} OVR{ovr} | {int(price):,}원"
                if last and last.lower() not in name.lower() and last.lower() not in pid.lower():
                    continue
                value = f"{prefix}, {pid}".lstrip(", ") if prefix else pid
                choices.append(app_commands.Choice(name=label[:100], value=value))
            return choices[:10]
        except Exception:
            return []

    async def all_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """원하는선수 자동완성 — 쉼표 구분 다중 입력 지원. 마지막 토큰 기준으로 전체 선수 검색."""
        try:
            parts = [p.strip() for p in current.replace("，", ",").split(",")]
            prefix = ", ".join(parts[:-1])
            last   = parts[-1]

            rows = await self.pm.search_players(last, limit=10)
            choices = []
            for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
                if int(retired) == 1:
                    continue
                label = f"{name} | {pos} OVR{ovr} | {int(price):,}원"
                value = f"{prefix}, {pid}".lstrip(", ") if prefix else pid
                choices.append(app_commands.Choice(name=label[:100], value=value))
            return choices[:10]
        except Exception:
            return []

    # ───────────────── 커맨드 ─────────────────

    @app_commands.command(name="트레이드", description="다른 유저에게 선수 트레이드를 제안합니다.")
    @app_commands.describe(
        상대방="트레이드 상대",
        내선수="내가 주는 선수 (자동완성 지원, 여러 명은 선택 후 쉼표로 추가)",
        원하는선수="내가 받을 선수 (자동완성 지원, 여러 명은 선택 후 쉼표로 추가)",
        내현금="내가 추가로 지불할 현금 (없으면 0)",
        원하는현금="상대가 추가로 지불할 현금 (없으면 0)",
    )
    @app_commands.autocomplete(내선수=my_player_autocomplete, 원하는선수=all_player_autocomplete)
    async def propose(
        self,
        interaction: discord.Interaction,
        상대방: discord.Member,
        내선수: str = "",
        원하는선수: str = "",
        내현금: int = 0,
        원하는현금: int = 0,
    ):
        await interaction.response.defer(ephemeral=True)

        if 상대방.id == interaction.user.id:
            return await interaction.followup.send("❌ 자기 자신에게는 트레이드를 제안할 수 없습니다.", ephemeral=True)
        if 상대방.bot:
            return await interaction.followup.send("❌ 봇에게는 트레이드를 제안할 수 없습니다.", ephemeral=True)
        if 내현금 < 0 or 원하는현금 < 0:
            return await interaction.followup.send("❌ 현금 금액은 0 이상이어야 합니다.", ephemeral=True)

        prop_pids = self._parse_pids(내선수)
        recv_pids = self._parse_pids(원하는선수)

        if not prop_pids and not recv_pids and 내현금 == 0 and 원하는현금 == 0:
            return await interaction.followup.send("❌ 트레이드 내용이 비어 있습니다.", ephemeral=True)

        ok, result, details = await self.pm.create_trade(
            proposer_id=interaction.user.id,
            receiver_id=상대방.id,
            proposer_pids=prop_pids,
            receiver_pids=recv_pids,
            proposer_cash=내현금,
            receiver_cash=원하는현금,
            now_ts=int(time.time()),
            get_balance=self.money.get_balance,
        )

        if not ok:
            return await interaction.followup.send(
                embed=_embed("❌ 트레이드 제안 실패", result, 0xe74c3c),
                ephemeral=True,
            )

        trade_id = result

        # 임베드 작성
        prop_lines = [f"• {name} (`{pid}`) x{qty}" for pid, name, qty in details["proposer_items"]]
        if 내현금 > 0:
            prop_lines.append(f"• 현금 **{내현금:,}원**")

        recv_lines = [f"• {name} (`{pid}`) x{qty}" for pid, name, qty in details["receiver_items"]]
        if 원하는현금 > 0:
            recv_lines.append(f"• 현금 **{원하는현금:,}원**")

        prop_name = interaction.user.display_name
        desc = (
            f"**{prop_name}**님의 트레이드 제안입니다.\n\n"
            f"📤 **{prop_name}이(가) 주는 것:**\n"
            + ("\n".join(prop_lines) if prop_lines else "없음")
            + f"\n\n📥 **{prop_name}이(가) 받길 원하는 것:**\n"
            + ("\n".join(recv_lines) if recv_lines else "없음")
            + f"\n\n제안 번호: **#{trade_id}** | 24시간 후 자동 만료\n"
            f"아래 버튼으로 수락하거나 거절하세요."
        )
        embed = _embed("🤝 트레이드 제안", desc, 0x3498db)
        view = TradeView(trade_id, self)

        sent_ok = False
        try:
            dm = await 상대방.create_dm()
            await dm.send(embed=embed, view=view)
            sent_ok = True
            await interaction.followup.send(
                embed=_embed(
                    "✅ 트레이드 제안 전송",
                    f"{상대방.mention}에게 DM으로 전송됐습니다.\n"
                    f"제안 번호: **#{trade_id}**\n\n"
                    f"⚠️ 제안한 선수는 트레이드가 확정/취소될 때까지 보유 목록에서 제외됩니다.",
                    0x2ecc71,
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            pass

        if not sent_ok:
            # DM 불가 → 채널 멘션
            try:
                await interaction.channel.send(f"{상대방.mention}", embed=embed, view=view)
                await interaction.followup.send(
                    embed=_embed(
                        "✅ 트레이드 제안 전송 (채널)",
                        f"{상대방.mention}의 DM이 닫혀 있어 채널에 전송됐습니다.\n제안 번호: **#{trade_id}**",
                        0xf39c12,
                    ),
                    ephemeral=True,
                )
            except Exception:
                await interaction.followup.send(
                    "❌ 상대방에게 트레이드 제안을 전송하지 못했습니다.", ephemeral=True
                )

    @app_commands.command(name="트레이드취소", description="내가 제안한 트레이드를 취소하고 선수를 돌려받습니다.")
    @app_commands.describe(제안번호="취소할 트레이드 번호")
    async def cancel(self, interaction: discord.Interaction, 제안번호: int):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await self.pm.cancel_trade(
            trade_id=제안번호,
            proposer_id=interaction.user.id,
        )
        await interaction.followup.send(
            embed=_embed("✅ 트레이드 취소" if ok else "❌ 취소 실패", msg, 0x2ecc71 if ok else 0xe74c3c),
            ephemeral=True,
        )

    @app_commands.command(name="트레이드목록", description="나와 관련된 대기 중인 트레이드를 확인합니다.")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.pm.get_my_pending_trades(interaction.user.id)

        if not rows:
            return await interaction.followup.send(
                embed=_embed("🤝 트레이드 목록", "대기 중인 트레이드가 없습니다.", 0x2ecc71),
                ephemeral=True,
            )

        now = int(time.time())
        lines = []
        for trade_id, proposer_id, receiver_id, p_cash, r_cash, expires_at in rows:
            role = "📤 제안자" if int(proposer_id) == interaction.user.id else "📥 수신자"
            h_left = max(0, int(expires_at) - now) // 3600
            cash_note = ""
            if int(p_cash) > 0:
                cash_note += f" | 제안자 {int(p_cash):,}원 지불"
            if int(r_cash) > 0:
                cash_note += f" | 수신자 {int(r_cash):,}원 지불"
            lines.append(f"`#{trade_id}` {role} | 만료 {h_left}h{cash_note}")

        await interaction.followup.send(
            embed=_embed("🤝 트레이드 목록", "\n".join(lines), 0x2ecc71),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Trade(bot))
