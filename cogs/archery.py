# cogs/archery.py — 🏹 /양궁 베팅액 (2.8)
# 레이스 틀 그대로: 출전표(궁수 소개 · 배당표)를 띄울 때 베팅금을 먼저 빼고, 60초 안에 [시작!]을 안 누르면 실격.
# 발마다 과녁 + 조준 게이지 그림(움직이는 GIF)을 올리고 [쏘기!] — 판정은 게이지를 올린 시각과 누른 시각(디스코드 기준)으로만.
# 6발이 끝나면 합계 점수 배당 + X 보너스로 정산 (한 판 순이익 최대 MAX_PAYOUT).
import asyncio
import io
import math
import random
import time

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

import release
from services import archery as A
from services import ui
from services.economy_db import ITEMS, EconomyDB

MIN_BET = 1_000
SECTION = "🎙️ 양궁 중계"
ITEM_KEYS = ("bow_pro", "windbreaker", "focus")
START_TIMEOUT = 60

# ───────────── 그림 ─────────────
W, H = 440, 520
CX, CY, RING = 220, 220, 20          # 과녁 가운데 · 칸 하나(점수 1) 픽셀
RING_COLORS = ((255, 214, 0), (255, 214, 0), (230, 40, 45), (230, 40, 45), (40, 120, 220), (40, 120, 220),
               (25, 25, 30), (25, 25, 30), (245, 245, 245), (245, 245, 245))   # 10·9 금 · 8·7 빨강 · 6·5 파랑 · 4·3 검정 · 2·1 흰색
BG = (21, 21, 30)
GAUGE_Y, GAUGE_X0, GAUGE_X1 = 470, 40, 400
FPS = 12


def _target(shots: list[tuple[float, float]], wind: tuple[float, float] | None) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    for k in range(10, 0, -1):   # 바깥(1점)부터
        r = k * RING
        d.ellipse((CX - r, CY - r, CX + r, CY + r), fill=RING_COLORS[k - 1],
                  outline=(90, 90, 90) if k in (3, 4) else (40, 40, 40))
    r = A.X_R * RING
    d.ellipse((CX - r, CY - r, CX + r, CY + r), outline=(120, 90, 0), width=1)
    d.line((CX - 4, CY, CX + 4, CY), fill=(60, 50, 0))
    d.line((CX, CY - 4, CX, CY + 4), fill=(60, 50, 0))
    for i, (x, y) in enumerate(shots):
        px, py = CX + x * RING, CY - y * RING
        last = i == len(shots) - 1
        rad = 7 if last else 5
        d.ellipse((px - rad, py - rad, px + rad, py + rad), fill=(0, 255, 140) if last else (255, 255, 255),
                  outline=(0, 0, 0), width=2)
    if wind:   # 오른쪽 위: 바람 화살표 (길이 = 세기)
        speed, ang = wind
        ox, oy, L = 400, 30, 6 + speed * 4
        ex, ey = ox + L * math.cos(ang), oy - L * math.sin(ang)
        d.ellipse((ox - 26, oy - 26, ox + 26, oy + 26), outline=(120, 140, 170), width=2)
        d.line((ox - L * math.cos(ang), oy + L * math.sin(ang), ex, ey), fill=(170, 220, 255), width=4)
        a1, a2 = ang + 2.6, ang - 2.6
        d.polygon([(ex, ey), (ex + 9 * math.cos(a1), ey - 9 * math.sin(a1)), (ex + 9 * math.cos(a2), ey - 9 * math.sin(a2))],
                  fill=(170, 220, 255))
    return im


def _gauge(im: Image.Image, pos: float | None):
    """pos -1 ~ 1 (None = 게이지 없음)."""
    d = ImageDraw.Draw(im)
    mid = (GAUGE_X0 + GAUGE_X1) / 2
    d.rounded_rectangle((GAUGE_X0, GAUGE_Y - 12, GAUGE_X1, GAUGE_Y + 12), 10, fill=(45, 45, 60))
    for half, col in ((180, (90, 50, 50)), (108, (160, 90, 40)), (40, (230, 190, 0)), (12, (255, 240, 120))):
        d.rounded_rectangle((mid - half, GAUGE_Y - 12, mid + half, GAUGE_Y + 12), 8, fill=col)
    if pos is not None:
        x = mid + pos * (GAUGE_X1 - GAUGE_X0) / 2
        d.polygon([(x, GAUGE_Y - 4), (x - 10, GAUGE_Y - 26), (x + 10, GAUGE_Y - 26)], fill=(255, 255, 255))
        d.line((x, GAUGE_Y - 14, x, GAUGE_Y + 16), fill=(255, 255, 255), width=3)


def aim_gif(shots, wind, sweep: float) -> bytes:
    """과녁 + 바늘이 왼쪽 → 오른쪽으로 sweep 초 동안 한 번 지나가는 GIF (끝에서 멈춤)."""
    base = _target(shots, wind)
    n = max(2, round(sweep * FPS))
    frames = []
    for i in range(n + 1):
        f = base.copy()
        _gauge(f, -1 + 2 * i / n)
        frames.append(f.quantize(colors=64, method=Image.Quantize.MEDIANCUT))
    buf = io.BytesIO()
    durs = [round(1000 / FPS)] * n + [60_000]   # 마지막(오른쪽 끝)에서 오래 멈춘다
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=durs, disposal=1, optimize=False)
    return buf.getvalue()


def target_png(shots, wind=None, needle: float | None = None) -> bytes:
    im = _target(shots, wind)
    if needle is not None:
        _gauge(im, needle)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ───────────── 화면 ─────────────
def _stat_bar(v: int, good_high: bool) -> str:
    return ui.bar(v if good_high else 100 - v, 100, 8)


def archer_card(user, amount: int, archer: A.Archer, chosen: set[str], inv: dict) -> discord.Embed:
    table = "\n".join(f"`{th}점 이상` {A.mult_label(m)}" if th else f"`{A.SCORE_TABLE[-2][0] - 1}점 이하` {A.mult_label(m)}"
                      for th, m in A.SCORE_TABLE)
    xb = " · ".join(f"X {k}개 +{v}배" for k, v in A.X_BONUS.items())
    items = " · ".join(f"{ITEMS[k][0]} {ITEMS[k][1]}" + (" ✅" if k in chosen else "") for k in ITEM_KEYS if inv.get(k))
    e = ui.card(f"🏹 오늘의 궁수 — {archer.flag} {archer.name}",
                f'> 🎙️ *"{archer.intro}!"*\n\n'
                f"`컨디션` {archer.mood}\n"
                f"`손 안정` {_stat_bar(archer.tremor, False)} (손떨림 {archer.tremor})\n"
                f"`바람 적응` {_stat_bar(archer.adapt, True)} ({archer.adapt})\n\n"
                f"**🎯 6발 (2엔드 × 3발)** · `베팅` **{amount:,}원**\n{table}\n"
                f"➕ **X(정중앙) 보너스** {xb}\n"
                + (f"\n🎒 쓸 아이템: {items}" if items else ""),
                ui.INFO, user, SECTION)
    e.set_footer(text=f"바늘이 가운데를 지날 때 [쏘기!] · 착탄은 타이밍 + 손떨림 + 바람 + 운 · {START_TIMEOUT}초 안에 시작하지 않으면 실격")
    return e


class StartView(discord.ui.View):
    """[시작!] + 가진 아이템 켜기/끄기. 연 사람만."""

    def __init__(self, cog: "Archery", user, amount: int, archer: A.Archer, inv: dict):
        super().__init__(timeout=START_TIMEOUT)
        self.cog, self.user, self.amount, self.archer, self.inv = cog, user, amount, archer, inv
        self.chosen: set[str] = set()
        self.message = None
        self.started = False
        self._draw()

    def _draw(self):
        self.clear_items()
        go = discord.ui.Button(label="시작!", emoji="🏹", style=discord.ButtonStyle.success)
        go.callback = self._start
        self.add_item(go)
        for k in ITEM_KEYS:
            if self.inv.get(k):
                on = k in self.chosen
                b = discord.ui.Button(label=f"{ITEMS[k][1]} {'켬' if on else '끔'} ({self.inv[k]})", emoji=ITEMS[k][0],
                                      style=discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary)
                b.callback = self._toggle(k)
                self.add_item(b)

    def _toggle(self, k):
        async def cb(interaction: discord.Interaction):
            self.chosen ^= {k}
            self._draw()
            await interaction.response.edit_message(embed=archer_card(self.user, self.amount, self.archer, self.chosen, self.inv),
                                                    view=self)
        return cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 판을 연 사람만 쏠 수 있어요.", ephemeral=True)
            return False
        return True

    async def _start(self, interaction: discord.Interaction):
        if self.started:
            return await interaction.response.defer()
        self.started = True
        self.stop()
        await self.cog.run(interaction, self)

    async def on_timeout(self):
        if self.started or self.message is None:
            return
        e = ui.card("⌛ 실격", '> 🎙️ *"사대에 아무도 서지 않았습니다! 실격 처리됩니다."*'
                    f"\n\n`정산` **{ui.won(-self.amount)}** · 1배 손실", ui.LOSE, self.user, SECTION)
        try:
            await self.message.edit(embed=e, view=None, attachments=[])
        except discord.HTTPException:
            pass


class ShootView(discord.ui.View):
    """한 발 — [쏘기!]를 누른 시각(디스코드가 받은 시각)을 기록한다."""

    def __init__(self, user):
        super().__init__(timeout=None)
        self.user = user
        self.pressed = asyncio.Event()
        self.press_ts: float | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 사대에 선 사람만 쏠 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="쏘기!", emoji="🏹", style=discord.ButtonStyle.danger)
    async def fire(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.press_ts is None:
            self.press_ts = interaction.created_at.timestamp()
            self.pressed.set()
        await interaction.response.defer()


def _arrow_cell(sc: int, x: bool) -> str:
    return "**X**" if x else ("M" if sc == 0 else str(sc))


def _scoreboard(results: list[tuple[int, bool]]) -> str:
    ends = []
    for e in range(A.ENDS):
        part = results[e * 3:(e + 1) * 3]
        if part:
            ends.append(f"`{e + 1}엔드` " + " · ".join(_arrow_cell(s, x) for s, x in part) + f" = **{sum(s for s, _ in part)}**")
    total = sum(s for s, _ in results)
    xs = sum(x for _, x in results)
    return "\n".join(ends) + f"\n`합계` **{total}점** · `X` **{xs}개**"


def _wind_text(wind) -> str:
    speed, ang = wind
    arrows = "→↗↑↖←↙↓↘"
    return f"{arrows[round(ang / (math.pi / 4)) % 8]} **{speed:g}m/s**"


def _shot_call(sc: int, x: bool, err: float) -> str:
    if x:
        return "정중앙!!! X 텐입니다!!"
    if sc == 10:
        return "텐! 완벽한 한 발!"
    if sc >= 8:
        return "좋아요, 노란 원 가까이!" if sc == 9 else "괜찮은 한 발이에요."
    if sc == 0:
        return "과녁을 벗어났어요… 미스!"
    return "바늘이 한참 비켜 갔어요…" if err > 0.6 else "바람에 밀렸나요? 아쉬운 한 발."


class Archery(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()

    @app_commands.command(name="양궁", description="궁수와 함께 6발! 바늘이 가운데일 때 [쏘기!] · 합계 점수 + X 보너스 배당 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def archery(self, interaction: discord.Interaction, amount: app_commands.Range[int, MIN_BET]):
        user, amount = interaction.user, int(amount)
        await interaction.response.defer()
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "🙅 참가할 수 없어요", f'> 🎙️ *"잔액이 부족합니다!"*\n\n`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**',
                ui.LOSE, user, SECTION))
        archer = A.pick_archer()
        inv, _ = await self.db.inventory(user.id)
        if release.preview_311(interaction.guild_id):   # 3.1.1 양궁 아이템 삭제 — 고를 아이템 없이 바로
            inv = {k: v for k, v in inv.items() if k not in ITEM_KEYS}
        await self.db.add_balance(user.id, -amount)
        view = StartView(self, user, amount, archer, inv)
        try:
            view.message = await interaction.followup.send(embed=archer_card(user, amount, archer, set(), inv), view=view, wait=True)
        except discord.HTTPException:
            await self.db.add_balance(user.id, amount)
            raise

    async def _take_items(self, uid: int, keys: set[str]) -> set[str]:
        """고른 아이템을 1개씩 쓴다 (없어진 건 빼고)."""
        def fn(con):
            used = set()
            for k in keys:
                if con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item=? AND qty>0", (uid, k)).rowcount:
                    used.add(k)
            return used
        return await self.db._tx(fn) if keys else set()

    async def run(self, interaction: discord.Interaction, view: StartView):
        # ponytail: 6발(약 30초) 도중 봇이 재시작되면 정산 전이라 베팅금이 사라진다 — 조정과 같은 한계
        user, amount, archer = view.user, view.amount, view.archer
        used = await self._take_items(user.id, view.chosen)
        bow, windbreak = "bow_pro" in used, "windbreaker" in used
        sweep = A.SWEEP_FOCUS if "focus" in used else A.SWEEP
        gear = " · ".join(f"{ITEMS[k][0]} {ITEMS[k][1]}" for k in ITEM_KEYS if k in used)
        shots: list[tuple[float, float]] = []
        results: list[tuple[int, bool]] = []
        last_call = f"{archer.flag} {archer.name}, 사대에 섭니다!" + (f" ({gear})" if gear else "")
        await interaction.response.edit_message(view=None)
        for i in range(A.ARROWS):
            wind = A.roll_wind()
            sv = ShootView(user)
            gif = await asyncio.to_thread(aim_gif, shots, wind, sweep)
            e = ui.card(f"🏹 {i + 1}/{A.ARROWS}발 — {i // 3 + 1}엔드",
                        f'> 🎙️ *"{last_call}"*\n\n'
                        f"🌬️ 바람 {_wind_text(wind)} · 바늘이 **{sweep:g}초** 동안 왼쪽 → 오른쪽\n"
                        f"🎯 바늘이 **가운데(노란 칸)**일 때 [쏘기!] — 그림이 뜬 뒤 {sweep / 2:g}초쯤\n\n"
                        + (_scoreboard(results) if results else ""), ui.DARK, user, SECTION)
            e.set_image(url="attachment://aim.gif")
            try:
                await interaction.edit_original_response(embed=e, attachments=[discord.File(io.BytesIO(gif), "aim.gif")], view=sv)
            except discord.HTTPException:
                pass
            t0 = time.time()
            try:
                await asyncio.wait_for(sv.pressed.wait(), timeout=sweep + A.LATE)
            except asyncio.TimeoutError:
                pass
            dt = None if sv.press_ts is None else sv.press_ts - t0 - A.DISPLAY_LAG
            err = A.timing_error(dt, sweep)
            x, y = A.shot(err, archer, wind, random, bow, windbreak)
            shots.append((x, y))
            sc, isx = A.score(x, y)
            results.append((sc, isx))
            needle = "안 쐈어요 (자동 발사)" if dt is None else f"바늘 {'가운데' if err < 0.08 else ('왼쪽' if dt < sweep / 2 else '오른쪽')}"
            last_call = f"{i + 1}발째 {_arrow_cell(sc, isx).strip('*')}점 — {_shot_call(sc, isx, err)} ({needle})"

        total, xs = sum(s for s, _ in results), sum(x for _, x in results)
        mult = A.payout(total, xs)
        delta = min(int(amount * mult), A.MAX_PAYOUT)
        new_bal = await self.db.add_balance(user.id, amount + delta)
        place_emoji = "🏆" if mult >= 1.5 else ("🎯" if mult > 0 else ("😐" if mult == 0 else "💨"))
        head = ("X 보너스 대박!!" if xs >= 3 else "고득점!" if mult > 0 else "본전" if mult == 0 else "아쉬워요…")
        e = ui.card(f"{place_emoji} {total}점 — {head}",
                    f'> 🎙️ *"{last_call}"*\n\n{_scoreboard(results)}\n\n'
                    f"`정산` **{ui.won(delta)}** · {A.mult_label(mult)}"
                    + (" (한 판 최대 지급액)" if delta < amount * mult else "")
                    + f"\n`잔액` **{new_bal:,}원**",
                    ui.GOLD if xs >= 3 or mult >= 1.5 else ui.tone(delta), user, SECTION)
        png = await asyncio.to_thread(target_png, shots)
        e.set_image(url="attachment://target.png")
        try:
            await interaction.edit_original_response(embed=e, attachments=[discord.File(io.BytesIO(png), "target.png")], view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Archery(bot))
