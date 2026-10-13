# cogs/checklist.py — 사적인 주간 체크리스트 (명령어 없음 · 패치노트에 안 씀)
# 봇이 정해진 스레드에 주마다 표 하나를 올리고 계속 고쳐 쓴다.
#   갑 · 을: 매크로(dev-mac volva-*.py)가 웹후크로 5분마다 가동 시간을 보고 → 그날 가동 시간에 더한다.
#            보고 값은 웹후크 보낸 사람 이름에 넣는다("가동|을|A90|시작ts|경과초") — 이름은 메시지 내용 권한 없이도 읽힌다.
#   갑: 대리 의뢰 수는 [의뢰 +1] 버튼 · 병: [수정 요청] → [수정 완료] 버튼.
import asyncio
import calendar
import io
import sqlite3
import time
from pathlib import Path
from typing import Optional

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
BUMP_QUIET = 5       # 스레드에 새 메시지가 오고 이만큼(초) 조용해지면 표 · 장부를 맨 아래로 다시 올린다
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
    # 장부: kind egg(source 갑/을 기기) · commission(event_id = 의뢰 +1 체크). amount 는 갑이 등록, settle_id 는 정산 번호
    con.execute("CREATE TABLE IF NOT EXISTS chk_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, "
                "source TEXT, event_id INTEGER, amount INTEGER, amount_ts INTEGER, settle_id INTEGER, note TEXT)")
    for col in ("buyer TEXT", "detail TEXT"):   # 대리 의뢰 · 알 판매: 구매자 · 의뢰 내용 (note = 메모)
        try:
            con.execute(f"ALTER TABLE chk_ledger ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    con.execute("CREATE TABLE IF NOT EXISTS chk_settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, "
                "user_id INTEGER, total INTEGER, gap INTEGER, eul INTEGER, byung INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS chk_kv (key TEXT PRIMARY KEY, value INTEGER)")
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

    widths = [0.8] + [1.0] * 7 + [2.7]
    fig, ax = plt.subplots(figsize=(sum(widths) * 0.95, 3.8), dpi=150)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.set_xlim(0, sum(widths))
    ax.set_ylim(0, 4)
    ax.axis("off")
    xs = [sum(widths[:i]) for i in range(len(widths))]

    def cell(ci, ri, text, color=None, bold=False, fg=_FG, size=17):
        x, y = xs[ci], 3 - ri
        ax.add_patch(plt.Rectangle((x + 0.04, y + 0.06), widths[ci] - 0.08, 0.88, color=color or _GRID,
                                   alpha=1 if color else 0.35, lw=0))
        ax.text(x + widths[ci] / 2, y + 0.5, text, ha="center", va="center", color=fg, fontsize=size,
                fontweight="bold" if bold else "normal", linespacing=1.25)

    for ci, c in enumerate(cols):
        is_today = 1 <= ci <= 7 and week + ci - 1 == today and not final
        cell(ci, 0, c, _TODAY if is_today else None, bold=True, size=15)
    for ri, (p, cs, total, ok) in enumerate(rows, start=1):
        cell(0, ri, p, None, bold=True, size=21)
        for ci, (text, color) in enumerate(cs, start=1):
            cell(ci, ri, text, color)
        cell(8, ri, total + ("\n미달" if final and not ok else ""), _ON if ok else (_OFF if final else None), size=15)
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


# ───────────── 장부 ─────────────
# 볼바 알: 매크로가 뜨자마자 기록(결과 화면 사진과 함께) → 갑이 판매 금액 등록.
# 데삭작 대리: 갑이 [의뢰 +1] → 받은 금액 등록. 정산하면 갑이 을 · 병에게 지분만큼 준다.
SHARES = {   # (종류, 출처 기기) → {사람: %}
    ("egg", "갑"): {"갑": 45, "을": 25, "병": 30},
    ("egg", "을"): {"갑": 30, "을": 40, "병": 30},
    ("commission", "갑"): {"갑": 50, "을": 25, "병": 25},
}
SHOW = 10   # 장부 화면에 종류별로 보여 줄 줄 수


def parse_amount(text: str) -> Optional[int]:
    """'1,500,000' · '150만' · '150만원' → 원. 아니면 None."""
    t = text.replace(",", "").replace(" ", "").replace("원", "")
    mult = 10_000 if t.endswith("만") else 1
    try:
        v = round(float(t.rstrip("만")) * mult)
    except ValueError:
        return None
    return v if v > 0 else None


def split(amount: int, kind: str, source: str) -> dict:
    """지분대로 나누기 — 원 단위 자투리는 갑에게."""
    out = {p: amount * v // 100 for p, v in SHARES[(kind, source)].items() if p != "갑"}
    return {"갑": amount - sum(out.values()), **out}


def entry_label(e: dict) -> str:
    when = time.strftime("%m/%d %H:%M", time.gmtime(e["ts"] + KST))
    what = f"볼바 알 ({e['source']} 기기)" if e["kind"] == "egg" else "데삭작 대리"
    return f"#{e['id']} {what} · {when}"


def entry_text(e: dict) -> str:
    """장부 한 항목: 번호 · 종류 · 시각 + 구매자 · 의뢰 내용 · 메모."""
    out = f"`{entry_label(e)}`" + (f" · 구매자 **{e['buyer']}**" if e.get("buyer") else "")
    if e.get("detail"):
        out += "\n　" + e["detail"].replace("\n", " ")[:120]
    if e.get("note"):
        out += "\n　-# 메모 " + e["note"].replace("\n", " ")[:80]
    return out


def ledger_data(con) -> dict:
    cols = ("id", "ts", "kind", "source", "event_id", "amount", "note", "buyer", "detail")
    rows = [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM chk_ledger WHERE settle_id IS NULL ORDER BY id")]
    owed = {"갑": 0, "을": 0, "병": 0}
    for e in rows:
        if e["amount"] is not None:
            e["split"] = split(e["amount"], e["kind"], e["source"])
            for p, v in e["split"].items():
                owed[p] += v
    last = con.execute("SELECT id, ts FROM chk_settlements ORDER BY id DESC LIMIT 1").fetchone()
    return {"rows": rows, "owed": owed, "last": last}


def settle(con, uid: int, now: float) -> Optional[dict]:
    """금액이 등록된 미정산 항목을 한 번에 정산 → {id, count, total, owed}. 정산할 게 없으면 None."""
    d = ledger_data(con)
    paid = [e for e in d["rows"] if e["amount"] is not None]
    if not paid:
        return None
    o, total = d["owed"], sum(e["amount"] for e in paid)
    cur = con.execute("INSERT INTO chk_settlements(ts, user_id, total, gap, eul, byung) VALUES(?, ?, ?, ?, ?, ?)",
                      (int(now), uid, total, o["갑"], o["을"], o["병"]))
    con.executemany("UPDATE chk_ledger SET settle_id=? WHERE id=?", [(cur.lastrowid, e["id"]) for e in paid])
    return {"id": cur.lastrowid, "count": len(paid), "total": total, "owed": o}


def ledger_embed(d: dict) -> discord.Embed:
    rows = d["rows"]

    def block(title, items, fmt):
        if not items:
            return ""
        lines = [fmt(e) for e in items[-SHOW:]]
        more = f"\n…외 {len(items) - SHOW}건" if len(items) > SHOW else ""
        return f"**{title}**\n" + "\n".join(lines) + more

    eggs = [e for e in rows if e["amount"] is None and e["kind"] == "egg"]
    coms = [e for e in rows if e["amount"] is None and e["kind"] == "commission"]
    paid = [e for e in rows if e["amount"] is not None]
    o = d["owed"]
    parts = [
        block("🥚 판매 대기 (금액 미등록)", eggs, entry_text),
        block("📥 대리 의뢰 (금액 미등록)", coms, entry_text),
        block("💰 정산 대기", paid, lambda e: entry_text(e) + f"\n　**{e['amount']:,}원** → "
              f"갑 {e['split']['갑']:,} · 을 {e['split']['을']:,} · 병 {e['split']['병']:,}"),
    ]
    if paid:
        parts.append(f"**정산하면** 갑 → 을 **{o['을']:,}원** · 갑 → 병 **{o['병']:,}원** (갑 몫 {o['갑']:,}원)")
    desc = "\n\n".join(p for p in parts if p) or "아직 기록이 없어요."
    e = discord.Embed(title="📒 장부", description=desc[:4000] + ("\n…" if len(desc) > 4000 else ""), color=0xF1C40F)
    last = d["last"]
    e.set_footer(text="지분 (갑·을·병) — 갑 기기 알 45·25·30 · 을 기기 알 30·40·30 · 대리 50·25·25"
                      + (f" · 마지막 정산 #{last[0]} {time.strftime('%m/%d', time.gmtime(last[1] + KST))}" if last else ""))
    return e


def _gap_or_owner(uid: int) -> bool:
    return uid == OWNER_ID or "갑" in who(uid)


class EntryModal(discord.ui.Modal):
    """장부 항목 입력 — 대리 의뢰(구매자 · 의뢰 내용 · 금액 · 메모) / 볼바 알(금액 · 구매자 · 메모).
    entry 가 없으면 새 대리 의뢰([의뢰 +1]), 있으면 그 항목 고치기."""

    def __init__(self, cog: "Checklist", kind: str, entry: Optional[dict] = None):
        new = entry is None
        super().__init__(title="데삭작 대리 의뢰" if kind == "commission" else "볼바 알 판매")
        self.cog, self.kind, self.entry = cog, kind, entry
        e = entry or {}
        self.buyer = discord.ui.TextInput(label="구매자 (이름 · 닉네임)", max_length=50,
                                          required=kind == "commission", default=e.get("buyer"))
        self.detail = discord.ui.TextInput(label="의뢰 내용", style=discord.TextStyle.paragraph, max_length=500,
                                           required=False, default=e.get("detail"),
                                           placeholder="캐릭터 · 작업 범위 · 기한 등")
        amount = e.get("amount")
        self.amount = discord.ui.TextInput(
            label="받은 금액" + (" (나중에 등록해도 돼요)" if kind == "commission" else ""), max_length=20,
            required=kind == "egg", default=str(amount) if amount is not None else None, placeholder="예: 300000 · 30만")
        self.note = discord.ui.TextInput(label="메모", style=discord.TextStyle.paragraph, max_length=300,
                                         required=False, default=e.get("note"))
        items = ([self.buyer, self.detail, self.amount, self.note] if kind == "commission"
                 else [self.amount, self.buyer, self.note])
        for it in items:
            self.add_item(it)
        self.new = new

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).strip()
        amount = parse_amount(raw) if raw else None
        if raw and amount is None:
            return await interaction.response.send_message("금액을 숫자로 적어 주세요. (예: 300000 · 30만)", ephemeral=True)
        fields = {"buyer": str(self.buyer.value).strip() or None, "amount": amount,
                  "detail": (str(self.detail.value).strip() or None) if self.kind == "commission" else self.entry.get("detail"),
                  "note": str(self.note.value).strip() or None}
        if self.new:
            eid = await asyncio.to_thread(self.cog.record_commission, interaction.user.id, **fields)
            msg = f"`#{eid}` 데삭작 대리 의뢰를 장부에 적었어요." + ("" if amount else " 금액은 장부의 [금액 · 내용]으로 나중에 등록해 주세요.")
        else:
            ok = await asyncio.to_thread(self.cog.ledger_update, self.entry["id"], **fields)
            msg = f"`#{self.entry['id']}` 고쳤어요." if ok else "이미 정산됐거나 없는 항목이에요."
        await interaction.response.send_message(msg, ephemeral=True)
        if self.new:
            await self.cog.refresh()   # 이번 주 의뢰 수
        await self.cog.refresh_ledger()


class EntryPick(discord.ui.View):
    """미정산 항목 고르기 (나만 보기) — mode: amount(금액 등록 · 고치기) / delete(삭제)."""

    def __init__(self, cog: "Checklist", entries: list[dict], mode: str):
        super().__init__(timeout=180)
        self.cog, self.mode = cog, mode
        self.entries = {e["id"]: e for e in entries}
        sel = discord.ui.Select(placeholder="항목을 골라 주세요", options=[
            discord.SelectOption(label=entry_label(e)[:100], value=str(e["id"]), description=(
                (e["buyer"] or "구매자 미등록") + " · "
                + (f"{e['amount']:,}원" if e["amount"] is not None else "금액 미등록"))[:100])
            for e in reversed(entries[-25:])])
        sel.callback = self._pick
        self.sel = sel
        self.add_item(sel)

    async def _pick(self, interaction: discord.Interaction):
        eid = int(self.sel.values[0])
        if self.mode == "amount":
            e = self.entries[eid]
            return await interaction.response.send_modal(EntryModal(self.cog, e["kind"], e))
        ok = await asyncio.to_thread(self.cog.ledger_delete, eid)
        await interaction.response.edit_message(
            content=f"`#{eid}` 삭제했어요." if ok else "이미 정산됐거나 없는 항목이에요.", view=None)
        await self.cog.refresh()
        await self.cog.refresh_ledger()


class EggSource(discord.ui.View):
    def __init__(self, cog: "Checklist"):
        super().__init__(timeout=180)
        self.cog = cog

    async def _add(self, interaction: discord.Interaction, source: str):
        eid = await asyncio.to_thread(self.cog.ledger_add_egg, source, time.time(), "직접 등록 (매크로가 놓침)")
        await interaction.response.edit_message(content=f"`#{eid}` 볼바 알 ({source} 기기) 등록했어요.", view=None)
        await self.cog.refresh_ledger()

    @discord.ui.button(label="갑 기기", style=discord.ButtonStyle.primary)
    async def gap(self, interaction: discord.Interaction, _):
        await self._add(interaction, "갑")

    @discord.ui.button(label="을 기기", style=discord.ButtonStyle.primary)
    async def eul(self, interaction: discord.Interaction, _):
        await self._add(interaction, "을")


class SettleConfirm(discord.ui.View):
    def __init__(self, cog: "Checklist"):
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="정산 완료 (돈을 보냈어요)", emoji="🧾", style=discord.ButtonStyle.success)
    async def ok(self, interaction: discord.Interaction, _):
        r = await asyncio.to_thread(self.cog.ledger_settle, interaction.user.id)
        if not r:
            return await interaction.response.edit_message(content="정산할 항목이 없어요.", view=None)
        await interaction.response.edit_message(content=f"🧾 정산 #{r['id']} 완료했어요.", view=None)
        o = r["owed"]
        th = await self.cog._thread()
        await th.send(embed=discord.Embed(
            title=f"🧾 정산 #{r['id']}",
            description=f"{r['count']}건 · 합계 **{r['total']:,}원**\n"
                        f"갑 → 을 **{o['을']:,}원** · 갑 → 병 **{o['병']:,}원** (갑 몫 {o['갑']:,}원)\n"
                        f"-# 정산한 사람 {interaction.user.mention}", color=0x2ECC71))
        await self.cog.bump()   # 정산 기록 아래로 표 · 장부를 다시


class LedgerView(discord.ui.View):
    """장부 메시지 버튼 (갑 · 관리자만)."""

    def __init__(self, cog: "Checklist"):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _gap_or_owner(interaction.user.id):
            return True
        await interaction.response.send_message("장부는 갑만 고칠 수 있어요.", ephemeral=True)
        return False

    async def _entries(self, interaction):
        rows = (await asyncio.to_thread(self.cog.ledger))["rows"]
        if not rows:
            await interaction.response.send_message("미정산 항목이 없어요.", ephemeral=True)
        return rows

    @discord.ui.button(label="금액 · 내용", emoji="💰", style=discord.ButtonStyle.success, custom_id="led:amount")
    async def amount(self, interaction: discord.Interaction, _):
        rows = await self._entries(interaction)
        if rows:
            await interaction.response.send_message("금액 · 구매자 · 내용을 적을(또는 고칠) 항목을 골라 주세요.",
                                                    view=EntryPick(self.cog, rows, "amount"), ephemeral=True)

    @discord.ui.button(label="알 직접 등록", emoji="🥚", style=discord.ButtonStyle.secondary, custom_id="led:egg")
    async def egg(self, interaction: discord.Interaction, _):
        await interaction.response.send_message("어느 기기에서 나온 볼바 알인가요? (매크로가 놓친 경우만)",
                                                view=EggSource(self.cog), ephemeral=True)

    @discord.ui.button(label="기록 삭제", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="led:delete")
    async def delete(self, interaction: discord.Interaction, _):
        rows = await self._entries(interaction)
        if rows:
            await interaction.response.send_message("잘못된 기록을 골라 주세요. (매크로가 잘못 본 알 등)",
                                                    view=EntryPick(self.cog, rows, "delete"), ephemeral=True)

    @discord.ui.button(label="정산", emoji="🧾", style=discord.ButtonStyle.primary, custom_id="led:settle")
    async def settle_btn(self, interaction: discord.Interaction, _):
        d = await asyncio.to_thread(self.cog.ledger)
        if not any(e["amount"] is not None for e in d["rows"]):
            return await interaction.response.send_message("금액이 등록된 미정산 항목이 없어요.", ephemeral=True)
        o = d["owed"]
        await interaction.response.send_message(
            f"갑 → 을 **{o['을']:,}원** · 갑 → 병 **{o['병']:,}원** (갑 몫 {o['갑']:,}원)\n"
            "돈을 보낸 뒤에 눌러 주세요. 금액 미등록 항목은 다음 정산으로 넘어가요.",
            view=SettleConfirm(self.cog), ephemeral=True)


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
        if not _gap_or_owner(interaction.user.id):
            return await interaction.response.send_message("갑만 누를 수 있어요.", ephemeral=True)
        await interaction.response.send_modal(EntryModal(self.cog, "commission"))   # 구매자 · 의뢰 내용 → 장부

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
        self._bump_at, self._bumper = 0.0, None
        con = connect()
        if con.execute("INSERT OR IGNORE INTO chk_kv(key, value) VALUES('manual_20261012', 1)").rowcount:
            day = kst_day(calendar.timegm((2026, 10, 12, 3, 0, 0)))   # 2026-10-12 (월) KST
            for p, h in (("갑", 1.5), ("을", 6.3)):                    # 사용자 요청 — 보고 연결 전에 돌린 시간
                con.execute("INSERT INTO chk_hours(person, day, secs) VALUES(?, ?, ?) ON CONFLICT(person, day) "
                            "DO UPDATE SET secs = secs + excluded.secs", (p, day, h * 3600))
        con.commit()
        con.close()

    async def cog_load(self):
        self.bot.add_view(ChecklistView(self))
        self.bot.add_view(LedgerView(self))
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
                led = con.execute("SELECT amount, settle_id FROM chk_ledger WHERE event_id=?", (row[0],)).fetchone()
                if led and (led[0] is not None or led[1] is not None):
                    return "금액이 등록된 의뢰는 장부의 [기록 삭제]로 지워 주세요."
                con.execute("DELETE FROM chk_events WHERE id=?", (row[0],))
                con.execute("DELETE FROM chk_ledger WHERE event_id=?", (row[0],))
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

    def _db(self, fn):
        con = connect()
        try:
            out = fn(con)
            con.commit()
            return out
        finally:
            con.close()

    def ledger(self) -> dict:
        return self._db(ledger_data)

    def ledger_add_egg(self, source: str, ts: float, note: Optional[str] = None) -> int:
        return self._db(lambda con: con.execute(
            "INSERT INTO chk_ledger(ts, kind, source, note) VALUES(?, 'egg', ?, ?)", (int(ts), source, note)).lastrowid)

    def record_commission(self, uid: int, buyer: Optional[str], detail: Optional[str], amount: Optional[int],
                          note: Optional[str]) -> int:
        """데삭작 대리 의뢰: 이번 주 의뢰 수(체크) + 장부 항목 → 장부 번호."""
        now = int(time.time())

        def fn(con):
            ev = con.execute("INSERT INTO chk_events(ts, kind, user_id) VALUES(?, 'commission', ?)", (now, uid)).lastrowid
            return con.execute(
                "INSERT INTO chk_ledger(ts, kind, source, event_id, buyer, detail, amount, amount_ts, note) "
                "VALUES(?, 'commission', '갑', ?, ?, ?, ?, ?, ?)",
                (now, ev, buyer, detail, amount, now if amount else None, note)).lastrowid
        return self._db(fn)

    def ledger_update(self, eid: int, buyer: Optional[str], detail: Optional[str], amount: Optional[int],
                      note: Optional[str]) -> bool:
        """미정산 항목의 구매자 · 의뢰 내용 · 금액 · 메모를 고친다."""
        return self._db(lambda con: con.execute(
            "UPDATE chk_ledger SET buyer=?, detail=?, amount=?, amount_ts=?, note=? WHERE id=? AND settle_id IS NULL",
            (buyer, detail, amount, int(time.time()) if amount else None, note, eid)).rowcount > 0)

    def ledger_delete(self, eid: int) -> bool:
        """미정산 항목 삭제 — 대리 의뢰면 그 체크(주간 의뢰 수)도 같이 지운다."""
        def fn(con):
            row = con.execute("SELECT event_id FROM chk_ledger WHERE id=? AND settle_id IS NULL", (eid,)).fetchone()
            if not row:
                return False
            con.execute("DELETE FROM chk_ledger WHERE id=?", (eid,))
            if row[0]:
                con.execute("DELETE FROM chk_events WHERE id=?", (row[0],))
            return True
        return self._db(fn)

    def ledger_settle(self, uid: int) -> Optional[dict]:
        return self._db(lambda con: settle(con, uid, time.time()))

    def _egg(self, name: str, now: float) -> bool:
        """웹후크 보고 '알|갑|A34|ts' → 장부에 볼바 알."""
        parts = name.split("|")
        if len(parts) != 4 or parts[0] != "알" or parts[1] not in ("갑", "을"):
            return False
        self.ledger_add_egg(parts[1], now)
        return True

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

    async def _ledger(self, repost: bool = False):
        """장부 메시지를 고친다 (없거나 repost 면 지우고 맨 아래에 새로)."""
        d = await asyncio.to_thread(self.ledger)
        mid = await asyncio.to_thread(self._db, lambda con: (con.execute(
            "SELECT value FROM chk_kv WHERE key='ledger_msg'").fetchone() or [None])[0])
        th = await self._thread()
        msg = None
        if mid:
            try:
                msg = await th.fetch_message(mid)
                if repost:
                    await msg.delete()
                    msg = None
                else:
                    await msg.edit(embed=ledger_embed(d), view=LedgerView(self))
            except discord.NotFound:
                msg = None
        if msg is None:
            msg = await th.send(embed=ledger_embed(d), view=LedgerView(self))
            await asyncio.to_thread(self._db, lambda con: con.execute(
                "INSERT OR REPLACE INTO chk_kv(key, value) VALUES('ledger_msg', ?)", (msg.id,)))

    async def refresh_ledger(self, repost: bool = False):
        async with self._lock:
            await self._ledger(repost)

    async def _post_or_edit(self, week: int, final: bool, repost: bool = False) -> bool:
        """표를 고치거나 올린다 (repost 면 지우고 맨 아래에 새로) → 새로 올렸으면 True."""
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
                if repost:
                    await msg.delete()
                    msg = None
                else:
                    await msg.edit(embed=make_embed(w, final), attachments=[file], view=view)
            except discord.NotFound:
                msg = None
        created = msg is None
        if created:
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
        return created

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
            startup = self._rendered_day is None          # 봇이 켜질 때(배포 · 재시작) 표 · 장부를 맨 아래로
            created = await self._post_or_edit(week, final=False, repost=startup)
            if created or startup:                         # 새 주 표 아래로 장부를 옮긴다
                await self._ledger(repost=True)
            self._rendered_day = kst_day(now)

    async def bump(self):
        """이번 주 표 · 장부를 스레드 맨 아래로 (지우고 다시 올린다)."""
        async with self._lock:
            await self._post_or_edit(week_of(kst_day(time.time())), final=False, repost=True)
            await self._ledger(repost=True)
        print("📌 [체크리스트] 표 · 장부를 맨 아래로 옮김")

    def bump_soon(self):
        """새 메시지가 오면 BUMP_QUIET 초 조용해질 때 한 번만 맨 아래로 (연달아 오는 채팅마다 다시 올리지 않게)."""
        self._bump_at = time.monotonic() + BUMP_QUIET
        if self._bumper is None or self._bumper.done():
            self._bumper = asyncio.create_task(self._bump_later())

    async def _bump_later(self):
        while (left := self._bump_at - time.monotonic()) > 0:
            await asyncio.sleep(left)
        try:
            await self.bump()
        except Exception as e:
            print("❌ [체크리스트] 맨 아래로 옮기기 실패:", repr(e))

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
        """스레드에 새 메시지(채팅 · 알 보고 · 정산 등)가 남으면 표 · 장부를 맨 아래로 옮긴다.
        봇이 직접 올린 표 · 장부는 무시하고, 가동 보고는 바로 지우니 옮길 필요가 없다."""
        if message.channel.id != THREAD_ID or message.author.id == self.bot.user.id:
            return
        if message.webhook_id == WEBHOOK_ID:
            if await asyncio.to_thread(self._report, message.author.name, time.time()):
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                return await self.refresh()
            await asyncio.to_thread(self._egg, message.author.name, time.time())   # 알 보고는 사진이 있어 증거로 남겨 둔다
        self.bump_soon()


async def setup(bot: commands.Bot):
    await bot.add_cog(Checklist(bot))
