# cogs/checklist.py — 사적인 주간 체크리스트 (명령어 없음 · 패치노트에 안 씀)
# 봇이 정해진 스레드에 주마다 표 하나를 올리고 계속 고쳐 쓴다.
#   갑 · 을: 매크로(dev-mac volva-*.py)가 웹후크로 5분마다 가동 시간을 보고 → 그날 가동 시간에 더한다.
#            보고 값은 웹후크 보낸 사람 이름에 넣는다("가동|을|A90|시작ts|경과초") — 이름은 메시지 내용 권한 없이도 읽힌다.
#   갑: 대리 의뢰 수는 [의뢰 +1] 버튼 · 병: [수정 요청] → [수정 완료] 버튼.
import asyncio
import io
import sqlite3
import time
from pathlib import Path

import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from discord.ext import commands, tasks  # noqa: E402

from auth import OWNER_ID  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"
matplotlib.rcParams["font.family"] = "NanumGothic"

THREAD_ID = 1558976894559322212
WEBHOOK_ID = 1558986340702363738
MEMBERS = {
    "갑": {1146659618790051960, 733634487073636352},
    "을": {673864024030445569, 867801225507897395},
    "병": {1194489261693612043, 1513740056555290706},
}
GAP_WEEKDAY_H, GAP_COMMISSIONS, EUL_WEEK_H = 30, 4, 80
SLACK = 120          # 보고 간격 오차 허용(초) — 실제로 흐른 시간보다 많이 늘어난 가동 시간은 인정하지 않는다
DAYS = "월화수목금토일"
KST = 9 * 3600


def kst_day(ts: float) -> int:
    return int(ts + KST) // 86400


def week_of(day: int) -> int:
    """그 주 월요일의 day 번호 (1970-01-01 은 목요일)."""
    return day - (day + 3) % 7


def day_label(day: int) -> str:
    t = time.gmtime(day * 86400)
    return f"{t.tm_mon}/{t.tm_mday}"


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS chk_hours (person TEXT, day INTEGER, secs REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY(person, day))")
    con.execute("CREATE TABLE IF NOT EXISTS chk_sessions (person TEXT, sid INTEGER, elapsed REAL, ts REAL, "
                "PRIMARY KEY(person, sid))")
    con.execute("CREATE TABLE IF NOT EXISTS chk_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, "
                "user_id INTEGER)")   # kind: commission(갑 의뢰) · fix_req · fix_done
    con.execute("CREATE TABLE IF NOT EXISTS chk_messages (week INTEGER PRIMARY KEY, message_id INTEGER, "
                "final INTEGER NOT NULL DEFAULT 0)")
    return con


def add_runtime(con, person: str, sid: int, elapsed: float, now: float) -> float:
    """매크로 보고 반영 → 이번에 더한 초. 경과는 시작 이후 실제로 흐른 시간을 넘을 수 없고,
    늘어난 만큼은 지난 보고 이후 흐른 시간을 넘을 수 없다. 더한 시간은 [now-증가분, now] 를 KST 날짜별로 나눠 넣는다."""
    elapsed = max(0.0, min(float(elapsed), now - sid + SLACK))
    row = con.execute("SELECT elapsed, ts FROM chk_sessions WHERE person=? AND sid=?", (person, sid)).fetchone()
    prev, since = (row[0], row[1]) if row else (0.0, float(sid))
    inc = max(0.0, min(elapsed - prev, now - since + SLACK))
    con.execute("INSERT INTO chk_sessions(person, sid, elapsed, ts) VALUES(?, ?, ?, ?) ON CONFLICT(person, sid) "
                "DO UPDATE SET elapsed=MAX(elapsed, excluded.elapsed), ts=excluded.ts", (person, sid, elapsed, now))
    t0, t1 = now - inc, now
    while t0 < t1:
        d = kst_day(t0)
        end = min(t1, (d + 1) * 86400 - KST)
        con.execute("INSERT INTO chk_hours(person, day, secs) VALUES(?, ?, ?) ON CONFLICT(person, day) "
                    "DO UPDATE SET secs = secs + excluded.secs", (person, d, end - t0))
        t0 = end
    return inc


def who(user_id: int) -> set[str]:
    return {p for p, ids in MEMBERS.items() if user_id in ids}


def week_data(con, week: int, now: float) -> dict:
    days = list(range(week, week + 7))
    hours = {p: [0.0] * 7 for p in ("갑", "을")}
    for p, d, s in con.execute("SELECT person, day, secs FROM chk_hours WHERE day BETWEEN ? AND ?", (week, week + 6)):
        if p in hours:
            hours[p][d - week] = s / 3600
    ev = con.execute("SELECT ts, kind FROM chk_events ORDER BY ts").fetchall()
    in_week = [(kst_day(ts), k) for ts, k in ev if week <= kst_day(ts) <= week + 6]
    fix = []   # 병 칸: 그날 요청/완료가 있었으면 그날 끝 기준 대기 0 → done, 남음 → pending
    for d in days:
        if not any(dd == d and k in ("fix_req", "fix_done") for dd, k in in_week):
            fix.append(None)
            continue
        req = sum(k == "fix_req" and kst_day(ts) <= d for ts, k in ev)
        done = sum(k == "fix_done" and kst_day(ts) <= d for ts, k in ev)
        fix.append("done" if req <= done else "pending")
    req_all = sum(k == "fix_req" for _, k in ev)
    done_all = sum(k == "fix_done" for _, k in ev)
    return {"week": week, "today": kst_day(now), "hours": hours,
            "gap_weekday": sum(hours["갑"][:5]), "eul_week": sum(hours["을"]),
            "commissions": sum(k == "commission" for _, k in in_week),
            "fix_req": sum(k == "fix_req" for _, k in in_week), "fix_done": sum(k == "fix_done" for _, k in in_week),
            "pending": max(0, req_all - done_all), "fix": fix}


# ───────────── 그림 ─────────────
_BG, _GRID, _FG, _SUB = "#2b2d31", "#3f4147", "#f2f3f5", "#949ba4"
_ON, _OFF, _WAIT, _TODAY = "#2d7d46", "#4a2c2c", "#8a6d1f", "#5865f2"


def render_png(w: dict, final: bool = False) -> bytes:
    week, today = w["week"], w["today"]
    cols = [""] + [f"{DAYS[i]}\n{day_label(week + i)}" for i in range(7)] + ["이번 주"]
    gap_ok = w["gap_weekday"] >= GAP_WEEKDAY_H
    rows = []
    for p in ("갑", "을"):
        cells = []
        for i, h in enumerate(w["hours"][p]):
            d = week + i
            if d > today:
                cells.append(("", None))
            else:
                cells.append((f"{h:.1f}h" if h >= 0.05 else "-", _ON if h >= 0.05 else (_OFF if d < today else None)))
        if p == "갑":
            com_ok = w["commissions"] >= GAP_COMMISSIONS
            total = (f"평일 {w['gap_weekday']:.1f}/{GAP_WEEKDAY_H}h" + (" 달성" if gap_ok else "")
                     + f"\n의뢰 {w['commissions']}/{GAP_COMMISSIONS}" + (" 달성" if com_ok else ""))
            ok = gap_ok and com_ok
        else:
            ok = w["eul_week"] >= EUL_WEEK_H
            total = f"{w['eul_week']:.1f}/{EUL_WEEK_H}h" + (" 달성" if ok else "")
        rows.append((p, cells, total, ok))
    cells = []
    for i, f in enumerate(w["fix"]):
        cells.append({"done": ("완료", _ON), "pending": ("대기", _WAIT), None: ("-" if week + i <= today else "", None)}[f])
    rows.append(("병", cells, f"요청 {w['fix_req']} · 완료 {w['fix_done']}"
                 + (f"\n대기 {w['pending']}" if w["pending"] else ""), w["pending"] == 0))

    widths = [0.7] + [1.0] * 7 + [1.9]
    fig, ax = plt.subplots(figsize=(sum(widths) * 0.95, 3.1), dpi=150)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.set_xlim(0, sum(widths))
    ax.set_ylim(0, 4)
    ax.axis("off")
    xs = [sum(widths[:i]) for i in range(len(widths))]

    def cell(ci, ri, text, color=None, bold=False, fg=_FG, size=11):
        x, y = xs[ci], 3 - ri
        ax.add_patch(plt.Rectangle((x + 0.04, y + 0.06), widths[ci] - 0.08, 0.88, color=color or _GRID,
                                   alpha=1 if color else 0.35, lw=0))
        ax.text(x + widths[ci] / 2, y + 0.5, text, ha="center", va="center", color=fg, fontsize=size,
                fontweight="bold" if bold else "normal", linespacing=1.25)

    for ci, c in enumerate(cols):
        is_today = 1 <= ci <= 7 and week + ci - 1 == today and not final
        cell(ci, 0, c, _TODAY if is_today else None, bold=True, size=10)
    for ri, (p, cs, total, ok) in enumerate(rows, start=1):
        cell(0, ri, p, None, bold=True, size=13)
        for ci, (text, color) in enumerate(cs, start=1):
            cell(ci, ri, text, color)
        cell(8, ri, total + ("\n미달" if final and not ok else ""), _ON if ok else (_OFF if final else None), size=10)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    return buf.getvalue()


def make_embed(w: dict, final: bool = False) -> discord.Embed:
    week = w["week"]
    title = f"📋 주간 체크리스트 · {day_label(week)}(월) ~ {day_label(week + 6)}(일)" + (" · 마감" if final else "")
    desc = (f"**갑** 평일 {GAP_WEEKDAY_H}시간 · 대리 의뢰 {GAP_COMMISSIONS}건\n"
            f"**을** 일주일 {EUL_WEEK_H}시간\n"
            "**병** 코드 수정 요청이 오면 수정")
    e = discord.Embed(title=title, description=desc, color=0x5865F2 if not final else 0x4E5058)
    e.set_image(url="attachment://checklist.png")
    e.set_footer(text="가동 시간은 매크로가 5분마다 자동 보고" + ("" if final else " · 버튼은 해당 줄 사람만"))
    return e


class ChecklistView(discord.ui.View):
    def __init__(self, cog: "Checklist"):
        super().__init__(timeout=None)
        self.cog = cog

    async def _act(self, interaction: discord.Interaction, kind: str, allowed: set[str], deny: str):
        uid = interaction.user.id
        if uid != OWNER_ID and not (who(uid) & allowed):
            return await interaction.response.send_message(deny, ephemeral=True)
        msg = await asyncio.to_thread(self.cog.record_event, kind, uid)
        if msg:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer()
        await self.cog.refresh()

    @discord.ui.button(label="의뢰 +1", emoji="📥", style=discord.ButtonStyle.primary, custom_id="chk:commission")
    async def commission(self, interaction: discord.Interaction, _):
        await self._act(interaction, "commission", {"갑"}, "갑만 누를 수 있어요.")

    @discord.ui.button(label="수정 요청", emoji="🛠️", style=discord.ButtonStyle.secondary, custom_id="chk:fix_req")
    async def fix_req(self, interaction: discord.Interaction, _):
        await self._act(interaction, "fix_req", {"갑", "을", "병"}, "체크리스트 멤버만 누를 수 있어요.")

    @discord.ui.button(label="수정 완료", emoji="✅", style=discord.ButtonStyle.success, custom_id="chk:fix_done")
    async def fix_done(self, interaction: discord.Interaction, _):
        await self._act(interaction, "fix_done", {"병"}, "병만 누를 수 있어요.")

    @discord.ui.button(label="내 마지막 체크 취소", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="chk:undo")
    async def undo(self, interaction: discord.Interaction, _):
        await self._act(interaction, "undo", {"갑", "을", "병"}, "체크리스트 멤버만 누를 수 있어요.")


class Checklist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._rendered_day = None
        con = connect()
        con.commit()
        con.close()

    async def cog_load(self):
        self.bot.add_view(ChecklistView(self))
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    # ───────────── DB ─────────────
    def record_event(self, kind: str, uid: int) -> str:
        """버튼 기록 → 실패하면 안내 문구, 성공하면 ""."""
        now = time.time()
        con = connect()
        try:
            if kind == "undo":
                row = con.execute("SELECT id, ts FROM chk_events WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
                if not row or week_of(kst_day(row[1])) != week_of(kst_day(now)):
                    return "이번 주에 취소할 내 체크가 없어요."
                con.execute("DELETE FROM chk_events WHERE id=?", (row[0],))
            else:
                if kind == "fix_done":
                    req = con.execute("SELECT COUNT(*) FROM chk_events WHERE kind='fix_req'").fetchone()[0]
                    done = con.execute("SELECT COUNT(*) FROM chk_events WHERE kind='fix_done'").fetchone()[0]
                    if done >= req:
                        return "대기 중인 수정 요청이 없어요."
                con.execute("INSERT INTO chk_events(ts, kind, user_id) VALUES(?, ?, ?)", (int(now), kind, uid))
            con.commit()
            return ""
        finally:
            con.close()

    def _report(self, name: str, now: float) -> bool:
        """웹후크 보고 '가동|을|A90|시작ts|경과초' 반영."""
        parts = name.split("|")
        if len(parts) != 5 or parts[0] != "가동" or parts[1] not in ("갑", "을"):
            return False
        try:
            sid, elapsed = int(parts[3]), float(parts[4])
        except ValueError:
            return False
        con = connect()
        try:
            add_runtime(con, parts[1], sid, elapsed, now)
            con.commit()
        finally:
            con.close()
        return True

    # ───────────── 스레드 ─────────────
    async def _thread(self):
        th = self.bot.get_channel(THREAD_ID) or await self.bot.fetch_channel(THREAD_ID)
        if isinstance(th, discord.Thread) and th.archived:
            await th.edit(archived=False)
        return th

    async def _post_or_edit(self, week: int, final: bool):
        def load():
            con = connect()
            try:
                row = con.execute("SELECT message_id, final FROM chk_messages WHERE week=?", (week,)).fetchone()
                return row, week_data(con, week, time.time())
            finally:
                con.close()
        row, w = await asyncio.to_thread(load)
        png = await asyncio.to_thread(render_png, w, final)
        file = discord.File(io.BytesIO(png), filename="checklist.png")
        th = await self._thread()
        view = None if final else ChecklistView(self)
        msg = None
        if row:
            try:
                msg = await th.fetch_message(row[0])
                await msg.edit(embed=make_embed(w, final), attachments=[file], view=view)
            except discord.NotFound:
                msg = None
        if msg is None:
            msg = await th.send(embed=make_embed(w, final), file=file, view=view)

        def save():
            con = connect()
            try:
                con.execute("INSERT INTO chk_messages(week, message_id, final) VALUES(?, ?, ?) ON CONFLICT(week) "
                            "DO UPDATE SET message_id=excluded.message_id, final=excluded.final", (week, msg.id, int(final)))
                con.commit()
            finally:
                con.close()
        await asyncio.to_thread(save)

    async def refresh(self):
        """이번 주 표를 새로 그린다 (없으면 올린다). 지난주 표가 아직 마감 전이면 마감으로 바꾼다."""
        async with self._lock:
            now = time.time()
            week = week_of(kst_day(now))

            def unfinished():
                con = connect()
                try:
                    return [w for (w,) in con.execute("SELECT week FROM chk_messages WHERE final=0 AND week<?", (week,))]
                finally:
                    con.close()
            for old in await asyncio.to_thread(unfinished):
                await self._post_or_edit(old, final=True)
            await self._post_or_edit(week, final=False)
            self._rendered_day = kst_day(now)

    @tasks.loop(minutes=1)
    async def tick(self):
        if self._rendered_day != kst_day(time.time()):   # 처음 · 날이 바뀌면 (오늘 칸 · 주 바뀜)
            try:
                await self.refresh()
            except Exception as e:
                print("❌ [체크리스트] 갱신 실패:", repr(e))

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.webhook_id != WEBHOOK_ID or message.channel.id != THREAD_ID:
            return
        if await asyncio.to_thread(self._report, message.author.name, time.time()):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await self.refresh()


async def setup(bot: commands.Bot):
    await bot.add_cog(Checklist(bot))
