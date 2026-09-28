# cogs/quiz.py
# 축구 퀴즈 미니게임 — /퀴즈, /오늘의퀴즈, /퀴즈랭킹, /퀴즈프로필
import asyncio
import random
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import quiz as Q


def _fmt_left_to_midnight(now_ts: int) -> str:
    left = (Q.day_key(now_ts) + 1) * 86400 - Q.KST - now_ts
    return f"{left // 3600}시간 {left % 3600 // 60}분"


class AnswerModal(discord.ui.Modal, title="정답 입력"):
    answer = discord.ui.TextInput(label="선수 이름 (한글/영문 모두 가능)", max_length=50)

    def __init__(self, game: "GameView"):
        super().__init__(timeout=game.q.limit + 10)
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        g = self.game
        text = str(self.answer).strip()
        if g.elapsed() > g.q.limit + 3:  # 모달 입력 시간은 약간 봐준다
            return await g.finish(interaction, False, f"⏰ 시간 초과 (입력: {text})")
        ok = Q.is_correct(text, g.q)
        await g.finish(interaction, ok, f"입력: **{text}**")


class GameView(discord.ui.View):
    def __init__(self, cog: "Quiz", user: discord.abc.User, guild_id: int, q: Q.Question, daily: bool):
        super().__init__(timeout=None)  # 제한 시간은 _timer 가 관리 (View timeout 은 상호작용마다 연장됨)
        self.cog, self.user, self.guild_id, self.q, self.daily = cog, user, guild_id, q, daily
        self.hints_used = 0
        self.done = False
        self.message: discord.Message | None = None
        self.started = time.monotonic()
        self.deadline = datetime.now(timezone.utc) + timedelta(seconds=q.limit)
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None

        if q.choices:
            for i, c in enumerate(q.choices):
                b = discord.ui.Button(label=f"{'ABCD'[i]}. {c}"[:80], style=discord.ButtonStyle.secondary, row=i // 2)
                b.callback = self._choice_cb(i)
                self.add_item(b)
        else:
            self._add("✍️ 정답 입력", discord.ButtonStyle.primary, self._answer_cb)
            if q.hints:
                self._add("💡 힌트 (-20점)", discord.ButtonStyle.secondary, self._hint_cb)
            self._add("🏳️ 포기", discord.ButtonStyle.danger, self._giveup_cb)

    def _add(self, label, style, cb):
        b = discord.ui.Button(label=label, style=style)
        b.callback = cb
        self.add_item(b)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def start_timer(self):
        self._timer_task = asyncio.create_task(self._timer())

    async def _timer(self):
        await asyncio.sleep(self.q.limit)
        await self.finish(None, False, "⏰ 시간 초과")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인이 시작한 퀴즈만 풀 수 있습니다. `/퀴즈`로 직접 도전해 보세요!", ephemeral=True)
            return False
        return True

    # ── 화면 ──
    def embed(self) -> discord.Embed:
        title = ("🌎 오늘의 퀴즈 · " if self.daily else "") + Q.KINDS[self.q.kind]
        e = discord.Embed(title=title, description="\n".join(self.q.lines), color=0x3498DB)
        if self.hints_used:
            e.add_field(
                name=f"💡 힌트 ({self.hints_used}/{len(self.q.hints)})",
                value="\n".join(f"{i + 1}. {h}" for i, h in enumerate(self.q.hints[:self.hints_used])),
                inline=False,
            )
        e.add_field(name="⏱️ 마감", value=discord.utils.format_dt(self.deadline, "R"), inline=True)
        e.add_field(name="난이도", value=f"{Q.DIFF_LABEL[self.q.difficulty]} (x{Q.DIFF_MULT[self.q.difficulty]:g})", inline=True)
        e.set_author(name=self.user.display_name, icon_url=self.user.display_avatar.url)
        tip = "한 번만 제출할 수 있습니다" + (" · 힌트 1개당 -20점" if self.q.hints else "")
        e.set_footer(text=tip + (" · 오늘의 퀴즈는 점수·상금 x2" if self.daily else ""))
        return e

    # ── 버튼 ──
    def _choice_cb(self, idx: int):
        async def cb(interaction: discord.Interaction):
            ok = idx == self.q.answer_idx
            await self.finish(interaction, ok, f"선택: **{'ABCD'[idx]}. {self.q.choices[idx]}**")
        return cb

    async def _answer_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AnswerModal(self))

    async def _hint_cb(self, interaction: discord.Interaction):
        if self.hints_used >= len(self.q.hints):
            return await interaction.response.send_message("더 이상 힌트가 없습니다.", ephemeral=True)
        self.hints_used += 1
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _giveup_cb(self, interaction: discord.Interaction):
        await self.finish(interaction, False, "🏳️ 포기")

    # ── 종료 ──
    async def finish(self, interaction: discord.Interaction | None, correct: bool, note: str):
        async with self._lock:
            if self.done:
                if interaction:
                    await interaction.response.send_message("이미 끝난 퀴즈입니다.", ephemeral=True)
                return
            self.done = True
        if self._timer_task and self._timer_task is not asyncio.current_task():
            self._timer_task.cancel()
        self.cog.active.pop(self.user.id, None)
        self.stop()
        for item in self.children:
            item.disabled = True
            if self.q.choices and getattr(item, "label", "").startswith(f"{'ABCD'[self.q.answer_idx]}. "):
                item.style = discord.ButtonStyle.success

        try:
            res = await self.cog.db.record(
                self.user.id, self.guild_id, self.q, correct, self.hints_used,
                min(self.elapsed(), self.q.limit), int(time.time()), daily=self.daily,
            )
        except Exception as ex:
            print(f"[QUIZ] 기록 실패: {ex!r}")
            res = None

        e = self._result_embed(correct, note, res)
        try:
            if interaction:
                await interaction.response.edit_message(embed=e, view=self)
            elif self.message:
                await self.message.edit(embed=e, view=self)
        except discord.HTTPException as ex:
            print(f"[QUIZ] 결과 표시 실패: {ex!r}")

    def _result_embed(self, correct: bool, note: str, res: dict | None) -> discord.Embed:
        title = ("🌎 오늘의 퀴즈 · " if self.daily else "") + ("✅ 정답!" if correct else "❌ 오답")
        e = discord.Embed(
            title=title,
            description="\n".join(self.q.lines) + f"\n\n{note}\n정답: **{self.q.answer}**",
            color=0x2ECC71 if correct else 0xE74C3C,
        )
        e.set_author(name=self.user.display_name, icon_url=self.user.display_avatar.url)
        if res is None:
            e.add_field(name="⚠️", value="기록 저장 중 오류가 발생했습니다.", inline=False)
            return e
        e.add_field(name="점수", value=f"**+{res['score']}**", inline=True)
        if res["reward"]:
            e.add_field(name="상금", value=f"**+{res['reward']:,}원**", inline=True)
        elif correct:
            e.add_field(name="상금", value="오늘 상금 한도 소진", inline=True)
        e.add_field(name="🔥 연승", value=f"{res['streak']} (최고 {res['best']})", inline=True)
        if not self.daily:
            e.set_footer(text=f"오늘 남은 상금 횟수 {res['paid_left']}/{Q.PAID_PER_DAY} · /퀴즈랭킹 · /퀴즈프로필")
        return e


class MenuView(discord.ui.View):
    def __init__(self, cog: "Quiz", user: discord.abc.User):
        super().__init__(timeout=60)
        self.cog, self.user = cog, user
        for kind, label in Q.KINDS.items():
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
            b.callback = self._cb(kind)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("`/퀴즈`로 직접 시작해 주세요.", ephemeral=True)
            return False
        return True

    def _cb(self, kind: str):
        async def cb(interaction: discord.Interaction):
            self.stop()
            await self.cog.start_game(interaction, kind)
        return cb


class Quiz(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Q.QuizDB()
        self.active: dict[int, GameView] = {}
        # ponytail: 최근 출제 기록은 메모리에만 둔다 — 재시작하면 초기화, 문제가 수백 개로 늘면 DB로 옮길 것
        self.recent: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=20))

    async def _busy(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.active:
            await interaction.response.send_message("⏳ 진행 중인 퀴즈가 있습니다. 먼저 끝내 주세요.", ephemeral=True)
            return True
        return False

    async def start_game(self, interaction: discord.Interaction, kind: str, q: Q.Question | None = None, daily: bool = False):
        if await self._busy(interaction):
            return
        uid = interaction.user.id
        if q is None:
            seen = self.recent[(uid, kind)]
            q = Q.pick(kind, random.Random(), exclude=set(seen))
            seen.append(q.qid)
        view = GameView(self, interaction.user, interaction.guild_id, q, daily)
        self.active[uid] = view
        try:
            if interaction.message is not None:   # 메뉴 버튼에서 시작
                await interaction.response.edit_message(embed=view.embed(), view=view)
                view.message = interaction.message
            else:                                  # 슬래시 명령에서 바로 시작
                await interaction.response.send_message(embed=view.embed(), view=view)
                view.message = await interaction.original_response()
        except Exception:
            self.active.pop(uid, None)
            raise
        view.started = time.monotonic()
        view.start_timer()

    @app_commands.command(name="퀴즈", description="축구 퀴즈! 선수·커리어·이적시장·상식 중 골라 도전하세요.")
    @app_commands.guild_only()
    async def quiz(self, interaction: discord.Interaction):
        if await self._busy(interaction):
            return
        e = discord.Embed(
            title="⚽ 축구 퀴즈",
            description=(
                "문제 유형을 선택하세요.\n\n"
                f"🕵️ **선수 맞히기** · 🔀 **커리어 맞히기** — 이름 직접 입력, {Q.TYPED_LIMIT}초\n"
                f"💸 **이적시장 퀴즈** · 🧠 **축구 상식** — 4지선다, {Q.CHOICE_LIMIT}초\n\n"
                f"정답 100점(힌트 1개당 -20) × 난이도 + 빠른 정답·연승 보너스\n"
                f"상금: 1점당 {Q.MONEY_PER_POINT}원 (하루 {Q.PAID_PER_DAY}회)"
            ),
            color=0x3498DB,
        )
        await interaction.response.send_message(embed=e, view=MenuView(self, interaction.user))

    @app_commands.command(name="오늘의퀴즈", description="하루 한 번! 모두에게 같은 문제가 나옵니다. (점수·상금 x2)")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        if await self._busy(interaction):
            return
        now_ts = int(time.time())
        streak = await self.db.start_daily(interaction.user.id, now_ts)
        if streak is None:
            return await interaction.response.send_message(
                f"✅ 오늘의 퀴즈는 이미 참여했습니다. 다음 문제까지 **{_fmt_left_to_midnight(now_ts)}**", ephemeral=True
            )
        q = Q.daily_question(Q.day_key(now_ts))
        q.lines = [f"🔥 연속 참여 **{streak}일**", *q.lines]
        await self.start_game(interaction, q.kind, q=q, daily=True)

    @app_commands.command(name="퀴즈랭킹", description="이 서버의 퀴즈 점수 TOP 10")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.db.leaderboard(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message("아직 퀴즈 기록이 없습니다. `/퀴즈`로 첫 기록을 남겨 보세요!")
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **{score:,}점** ({won}/{played}, {won / played * 100:.0f}%)"
            for i, (uid, score, played, won) in enumerate(rows)
        ]
        e = discord.Embed(title="🏆 축구 퀴즈 랭킹", description="\n".join(lines), color=0xF1C40F)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="퀴즈프로필", description="퀴즈 전적을 확인합니다.")
    @app_commands.describe(유저="확인할 유저 (비우면 나)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, 유저: discord.Member | None = None):
        user = 유저 or interaction.user
        p = await self.db.profile(user.id, interaction.guild_id)
        today = Q.day_key(int(time.time()))
        daily_streak = p["daily_streak"] if p["daily_last"] >= today - 1 else 0
        rate = f"{p['won'] / p['played'] * 100:.1f}%" if p["played"] else "-"
        e = discord.Embed(title="⚽ 퀴즈 프로필", color=0x3498DB)
        e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        e.add_field(name="총점", value=f"**{p['total_score']:,}**" + (f" (서버 {p['rank']}위)" if p["rank"] else ""), inline=True)
        e.add_field(name="전적", value=f"{p['played']}전 {p['won']}승", inline=True)
        e.add_field(name="정답률", value=rate, inline=True)
        e.add_field(name="🔥 현재 연승", value=str(p["streak"]), inline=True)
        e.add_field(name="🏅 최고 연승", value=str(p["best"]), inline=True)
        e.add_field(name="🌎 오늘의 퀴즈 연속", value=f"{daily_streak}일", inline=True)
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Quiz(bot))
