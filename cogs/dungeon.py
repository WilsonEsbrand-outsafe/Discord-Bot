# cogs/dungeon.py — 🏰 /던전 (2.8)
# 입구 화면(모두에게) → [입장] → 층마다 문 고르기 → 방(전투 · 보물 · 함정 · 상인 · 휴식 · 제단) → 10층 보스.
# 한 메시지를 계속 고쳐 쓴다. 진행 상태는 DB 라서 버튼이 만료돼도 /던전으로 이어할 수 있고, 30분 방치하면 자동 귀환.
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.economy import ITEM_HANDLERS
import release
from services import ui
from services.club_db import ClubDB
from services.dungeon import (
    BOSS_FLOORS, DEATH_KEEP, FLOORS, IDLE_RETURN, MERCHANT, POTION_HEAL, ROOMS, STATS, THEMES, ALTAR_COST,
    DungeonDB, act, drink_potion, enter, item_name, loot_text, reveal, team_stats, theme_of,
)

SECTION = "🏰 던전"
VIEW_TIMEOUT = IDLE_RETURN


def _hp_line(st: dict) -> str:
    return f"`체력` {ui.bar(st['hp'], st['max_hp'], 10)} **{st['hp']}/{st['max_hp']}**"


def _floor_line(st: dict) -> str:
    marks = "".join("👑" if f in BOSS_FLOORS and f >= st["floor"] else ("🟩" if f < st["floor"] else ("📍" if f == st["floor"] else "⬛"))
                    for f in range(1, FLOORS + 1))
    return f"`층` {marks} **{st['floor']}/{FLOORS}**"


def _loot_line(st: dict) -> str:
    items = " · ".join(item_name(k) + (f" ×{n}" if n > 1 else "") for k, n in st["loot"].items())
    return f"`모은 것` {items or '아직 없음'}" + (f" · 💰 {st['coins']:,}원" if st["coins"] else "")


def _stats_line(stats: dict) -> str:
    return " · ".join(f"{label} **{stats[k]}**" for k, label, _ in STATS)


def run_embed(user, st: dict, log: list[str] | None = None) -> discord.Embed:
    th = THEMES[st["theme"]]
    ph = st["phase"]
    parts = []
    if log:
        parts.append("\n".join(log))
    if ph == "doors":
        if st["floor"] in BOSS_FLOORS:
            parts.append("👑 **보스층** — 도망칠 수 없어요")
        else:
            rows = []
            for i, d in enumerate(st["doors"]):
                real = f" → {ROOMS[d['room']][0]} **{ROOMS[d['room']][1]}**" if st.get("revealed") else ""
                rows.append(f"🚪 **{i + 1}번 문** — {d['hint']}{real}")
            parts.append("\n".join(rows) + ("\n-# 🔦 힌트가 모두 진짜예요" if st.get("torch") else ""))
    elif ph == "battle":
        m = st["monster"]
        parts.append(f"{'👑' if m['boss'] else '👹'} **{m['name']}** — 위험도 {'🔥' * min(5, round(m['power'] / 10))}"
                     )
    elif ph == "treasure":
        parts.append("📦 열어 볼까요? (가끔 미믹)" + (f" · 🗝️ 열쇠 {st['inv_dkey']}개" if st.get("inv_dkey") else ""))
    elif ph == "merchant":
        parts.append("\n".join(f"{item_name(k)} — **{MERCHANT[k]:,}원**" for k in st.get("offers", []))
                     or "🧙 \"오늘은 다 팔렸다네.\"")
    elif ph == "altar":
        parts.append(f"🙏 체력 **-{ALTAR_COST}** 바치고 기도 — 대개 축복(보상 2배), 가끔 저주")
    parts.append(f"{_hp_line(st)}\n{_floor_line(st)}\n{_loot_line(st)}")
    color = ui.DOOM if ph == "battle" and st["monster"]["boss"] else (ui.DARK if ph == "battle" else ui.INFO)
    e = ui.card(f"{th[0]} {th[1]} · {st['floor']}층", "\n\n".join(parts), color, user, SECTION)
    e.set_footer(text="힌트는 가끔 거짓 · 30분 방치하면 자동 귀환(보상 전부)")
    scene = "dg_boss" if ph == "battle" and st["monster"]["boss"] else f"dg_{ph}"
    if url := ui.scene_url(scene, user):   # 3.1.0 장면별 키코 그림 (공개 전 서버는 그림 없음)
        e.set_thumbnail(url=url)
    return e


def result_embed(user, r: dict, log: list[str] | None = None) -> discord.Embed:
    th = THEMES[r.get("theme", 0)]
    if r["phase"] == "clear":
        title, color, scene = f"🏆 {th[1]} 클리어!", ui.GOLD, "dg_clear"
    elif r["phase"] == "dead":
        title, color, scene = f"💀 {r['floor']}층에서 쓰러졌어요… (보상 {DEATH_KEEP:.0%})", ui.LOSE, "dg_dead"
    else:
        title, color, scene = f"🏃 귀환 · {r['floor']}층까지", ui.WIN, "dg_return"
    got = " · ".join(item_name(k) + (f" ×{n}" if n > 1 else "") for k, n in r["items"].items()) or "없음"
    lines = ([*log, ""] if log else []) + [
        f"`아이템` {got}",
        f"`돈` 💰 {r['coins']:,}원 · `경험치` +{r['xp']}",
    ]
    if r.get("first"):
        lines.append("✨ 첫 클리어 보너스 포함")
    lines.append(f"`잔액` {r['balance']:,}원")
    e = ui.card(title, "\n".join(lines), color, user, SECTION)
    e.set_footer(text="보상은 가방에 들어갔어요 · 내일 00시에 다시 열려요")
    if url := ui.scene_url(scene, user):
        e.set_thumbnail(url=url)
    return e


class EntryView(discord.ui.View):
    def __init__(self, cog: "Dungeon", user, info: dict):
        super().__init__(timeout=600)
        self.cog, self.user = cog, user
        if info["active"]:
            self._btn("이어하기", "▶️", discord.ButtonStyle.success, self._resume)
        elif info["runs"] < info["allowed"]:
            self._btn("입장", "⚔️", discord.ButtonStyle.success, self._enter(False))
            if info["torch"]:
                self._btn(f"횃불 켜고 입장 ({info['torch']})", "🔦", discord.ButtonStyle.primary, self._enter(True))
        elif info["tickets"]:
            self._btn(f"추가 입장권 쓰기 ({info['tickets']})", "🎫", discord.ButtonStyle.primary, self._ticket)
        self._btn("주간 랭킹", "🏆", discord.ButtonStyle.secondary, self._rank, f"dungeon:{user.id}:rank")

    def _btn(self, label, emoji, style, cb, custom_id=None):
        b = discord.ui.Button(label=label, emoji=emoji, style=style, **({"custom_id": custom_id} if custom_id else {}))
        b.callback = cb
        self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cid = str((getattr(interaction, "data", None) or {}).get("custom_id", ""))
        if interaction.user.id != self.user.id and not cid.endswith(":rank"):   # 랭킹은 누구나
            await interaction.response.send_message("🙅 자기 던전만 들어갈 수 있어요. `/던전`을 직접 열어 주세요.", ephemeral=True)
            return False
        return True

    def _enter(self, torch: bool):
        async def cb(interaction: discord.Interaction):
            await self.cog.do_start(interaction, torch)
        return cb

    async def _resume(self, interaction: discord.Interaction):
        st = (await self.cog.db.today(self.user.id, int(time.time())))["active"]
        if not st:
            return await self.cog.show_entry(interaction, edit=True)
        await interaction.response.edit_message(embed=run_embed(self.user, st, ["▶️ 이어서 들어갑니다."]),
                                                view=RunView(self.cog, self.user, st))

    async def _ticket(self, interaction: discord.Interaction):
        await self.cog.db.use_ticket(self.user.id, int(time.time()))
        await self.cog.show_entry(interaction, edit=True, note="🎫 추가 입장권을 썼어요. 오늘 한 번 더 들어갈 수 있어요!")

    async def _rank(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await self.cog.rank_embed(interaction.user), ephemeral=True)


class RunView(discord.ui.View):
    """던전 안 — 지금 단계(문 · 전투 · 보물 · 상인 · 제단)에 맞는 버튼."""

    def __init__(self, cog: "Dungeon", user, st: dict):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog, self.user = cog, user
        self.message = None
        ph = st["phase"]
        if ph == "doors":
            for i, d in enumerate(st["doors"]):
                label = f"{i + 1}번 문" + (f" · {ROOMS[d['room']][1]}" if st.get("revealed") and d["room"] in ROOMS else "")
                self._btn(label, "👑" if d["room"] == "boss" else "🚪", discord.ButtonStyle.primary, self._act("door", i), 0)
            if st["floor"] not in BOSS_FLOORS and not st.get("revealed") and st.get("inv_scroll"):
                self._btn(f"두루마리 ({st['inv_scroll']})", "📜", discord.ButtonStyle.secondary, self._act("scroll"), 1)
            self._btn("귀환 (보상 전부)", "🏃", discord.ButtonStyle.danger, self._retreat, 1)
        elif ph == "battle":
            self._btn("싸우기", "⚔️", discord.ButtonStyle.danger, self._act("fight"), 0)
            if not st["monster"]["boss"]:
                self._btn("도망치기", "🏃", discord.ButtonStyle.secondary, self._act("flee"), 0)
        elif ph == "treasure":
            self._btn("상자 열기", "📦", discord.ButtonStyle.success, self._act("open"), 0)
            if st.get("inv_dkey"):
                self._btn(f"열쇠로 금고 열기 ({st['inv_dkey']})", "🗝️", discord.ButtonStyle.primary, self._act("key"), 0)
        elif ph == "merchant":
            for k in st.get("offers", []):
                self._btn(f"{item_name(k).split(' ', 1)[1]} {MERCHANT[k] // 10_000}만원", item_name(k).split(" ")[0],
                          discord.ButtonStyle.primary, self._buy(k), 0)
            self._btn("지나가기", "👋", discord.ButtonStyle.secondary, self._act("leave"), 0)
        elif ph == "altar":
            self._btn(f"기도하기 (체력 -{ALTAR_COST})", "🙏", discord.ButtonStyle.primary, self._act("pray"), 0)
            self._btn("지나가기", "👋", discord.ButtonStyle.secondary, self._act("leave"), 0)
        if st.get("inv_potion") and st["hp"] < st["max_hp"]:
            self._btn(f"포션 +{POTION_HEAL} ({st['inv_potion']})", "🩹", discord.ButtonStyle.success, self._act("potion"), 1)

    def _btn(self, label, emoji, style, cb, row):
        b = discord.ui.Button(label=label[:80], emoji=emoji, style=style, row=row)
        b.callback = cb
        self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 자기 던전만 진행할 수 있어요. `/던전`을 직접 열어 주세요.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                e = self.message.embeds[0]
                e.set_footer(text="⏰ 버튼이 만료됐어요 — /던전 으로 이어할 수 있어요 (30분 방치하면 자동 귀환)")
                await self.message.edit(embed=e, view=None)
            except Exception:
                pass

    def _act(self, kind: str, arg=None):
        async def cb(interaction: discord.Interaction):
            await self.cog.do_step(interaction, kind, arg)
        return cb

    def _buy(self, item: str):
        async def cb(interaction: discord.Interaction):
            await self.cog.do_buy(interaction, item)
        return cb

    async def _retreat(self, interaction: discord.Interaction):
        await self.cog.do_retreat(interaction)


class Dungeon(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = DungeonDB()
        self.clubs = ClubDB()
        self.rng = random.Random()

    async def cog_load(self):
        ITEM_HANDLERS["dungeon_ticket"] = self._use_ticket
        self.idle_loop.start()

    async def cog_unload(self):
        ITEM_HANDLERS.pop("dungeon_ticket", None)
        self.idle_loop.cancel()

    @tasks.loop(minutes=5)
    async def idle_loop(self):
        await self.db.auto_return(int(time.time()), self.rng)

    # ───────────── 입구 ─────────────
    async def entry_embed(self, user, info: dict, note: str = "", new: bool = True) -> discord.Embed:
        now = int(time.time())
        th = THEMES[theme_of(now)]
        team = await self.clubs.get_team(user.id, fill_injured=True)
        stats = team_stats(team["lineup"], new) if team else None
        clears = await self.db.clears(user.id)
        lines = [f"{FLOORS}층 · {'·'.join(map(str, BOSS_FLOORS))}층 보스 · [귀환]하면 보상 전부, 쓰러지면 절반", ""]
        if stats:
            lines.append(f"`파티` {_stats_line(stats)}")
        lines.append(f"`오늘 입장` **{info['runs']}/{info['allowed']}** · 🎫 {info['tickets']} · 🔦 {info['torch']}")
        lines.append(f"`첫 클리어` {'✅ 완료' if theme_of(now) in clears else '아직 (보너스 있음)'}")
        if info["active"]:
            st = info["active"]
            lines += ["", f"⏸️ **진행 중인 던전이 있어요** — {st['floor']}층 · 체력 {st['hp']}/{st['max_hp']}"]
        elif info["runs"] >= info["allowed"]:
            lines += ["", "😴 오늘은 다 들어갔어요. 내일 00시에 다시 열려요."]
        if note:
            lines += ["", note]
        return ui.card(f"{th[0]} 던전 입구 — {th[1]}", "\n".join(lines), ui.INFO, user, SECTION)

    async def show_entry(self, interaction: discord.Interaction, edit: bool = False, note: str = ""):
        info = await self.db.today(interaction.user.id, int(time.time()))
        e = await self.entry_embed(interaction.user, info, note, release.preview_2101(interaction.guild_id))
        v = EntryView(self, interaction.user, info)
        if edit:
            await interaction.response.edit_message(embed=e, view=v)
        else:
            await interaction.response.send_message(embed=e, view=v)

    async def rank_embed(self, user) -> discord.Embed:
        top = await self.db.weekly_top(int(time.time()), 10)
        medals = ("🥇", "🥈", "🥉")
        rows = []
        for i, (uid, fl) in enumerate(top):
            m = self.bot.get_user(uid) if self.bot else None
            name = m.display_name if m else f"<@{uid}>"
            rows.append(f"{medals[i] if i < 3 else f'`{i + 1}.`'} {name} — **{'🏆 클리어' if fl >= FLOORS else f'{fl}층'}**")
        e = ui.card("🏆 이번 주 던전 최고 층", "\n".join(rows) or "아직 아무도 들어가지 않았어요.", ui.GOLD, user, SECTION)
        e.set_footer(text="월요일 00시 초기화 · 같은 층이면 먼저 도달한 사람이 위")
        return e

    @app_commands.command(name="던전", description="🏰 하루 한 번, 내 선발 11명으로 10층 던전을 탐험해요")
    async def dungeon(self, interaction: discord.Interaction):
        if not await self.clubs.get_team(interaction.user.id):
            return await interaction.response.send_message("🙅 구단이 있어야 던전에 들어갈 수 있어요. `/구단생성`부터 해 주세요.",
                                                           ephemeral=True)
        await self.show_entry(interaction)

    # ───────────── 진행 ─────────────
    async def _show(self, interaction: discord.Interaction, r: dict, log: list[str]):
        if "result" in r:
            res = r["result"]
            res["theme"] = r["state"]["theme"]
            return await interaction.response.edit_message(embed=result_embed(interaction.user, res, log), view=None)
        v = RunView(self, interaction.user, r["state"])
        await interaction.response.edit_message(embed=run_embed(interaction.user, r["state"], log), view=v)
        v.message = interaction.message

    async def do_start(self, interaction: discord.Interaction, torch: bool):
        if release.preview_311(interaction.guild_id):   # 3.1.1: 빈자리는 벤치 선수로 자동으로 채운다
            await self.clubs.fill_empty(interaction.user.id)
        team = await self.clubs.get_team(interaction.user.id, fill_injured=True)
        if not team:
            return await interaction.response.send_message("🙅 구단이 없어요.", ephemeral=True)
        new = release.preview_2101(interaction.guild_id)   # 2.10.1 던전 난이도 · 보상 (테스트 서버 먼저)
        r = await self.db.start(interaction.user.id, team_stats(team["lineup"], new), int(time.time()), torch, self.rng, new)
        if not r["ok"]:
            return await self.show_entry(interaction, edit=True,
                                         note="⏸️ 이미 진행 중이에요." if r["reason"] == "active" else "😴 오늘은 다 들어갔어요.")
        th = THEMES[r["state"]["theme"]]
        log = [f"{th[0]} **{th[1]}**에 들어섰어요." + (" 🔦 횃불을 켰어요!" if r["state"].get("torch") else "")]
        if r["state"]["max_hp"] > 100:
            log.append("🍱 보양 정식 덕분에 체력이 넉넉해요!")
        await self._show(interaction, r, log)

    async def do_step(self, interaction: discord.Interaction, kind: str, arg=None):
        rng = self.rng

        def fn(st, con):
            ph = st["phase"]
            if kind == "door":
                if ph != "doors" or arg >= len(st["doors"]):
                    return None
                return enter(st, arg, rng)
            if kind == "potion":
                if not st.get("inv_potion") or st["hp"] >= st["max_hp"]:
                    return None
                st["used"]["potion"] = st["used"].get("potion", 0) + 1
                return [f"🩹 포션을 마셨어요. 체력 **+{drink_potion(st)}**"]
            if kind == "scroll":
                if ph != "doors" or not st.get("inv_scroll") or st.get("revealed"):
                    return None
                st["used"]["scroll"] = st["used"].get("scroll", 0) + 1
                reveal(st)
                return ["📜 두루마리를 펼치자 문 뒤가 훤히 보여요!"]
            want = {"fight": "battle", "flee": "battle", "open": "treasure", "key": "treasure", "leave": ("merchant", "altar"),
                    "pray": "altar"}[kind]
            if ph not in (want if isinstance(want, tuple) else (want,)):
                return None
            return act(st, kind, rng)

        r = await self.db.step(interaction.user.id, int(time.time()), fn, rng)
        if not r["ok"]:
            if r["reason"] == "none":
                return await self.show_entry(interaction, edit=True, note="⏹️ 진행 중인 던전이 없어요 (자동 귀환됐을 수 있어요).")
            st = r["state"]
            v = RunView(self, interaction.user, st)
            return await interaction.response.edit_message(embed=run_embed(interaction.user, st, ["🙅 지금은 할 수 없어요."]), view=v)
        await self._show(interaction, r, r["log"])

    async def do_buy(self, interaction: discord.Interaction, item: str):
        r = await self.db.merchant_buy(interaction.user.id, item, int(time.time()))
        info = await self.db.today(interaction.user.id, int(time.time()))
        st = info["active"]
        if not st:
            return await self.show_entry(interaction, edit=True)
        if r["ok"]:
            log = [f"🧙 {item_name(item)}을(를) 샀어요! 💰 잔액 {r['balance']:,}원"]
        else:
            log = [f"🙅 돈이 모자라요. (잔액 {r.get('balance', 0):,}원)" if r["reason"] == "balance" else "🙅 살 수 없어요."]
        v = RunView(self, interaction.user, st)
        await interaction.response.edit_message(embed=run_embed(interaction.user, st, log), view=v)
        v.message = interaction.message

    async def do_retreat(self, interaction: discord.Interaction):
        r = await self.db.retreat(interaction.user.id, int(time.time()), self.rng)
        if not r["ok"]:
            return await self.show_entry(interaction, edit=True, note="⏹️ 진행 중인 던전이 없어요.")
        res = r["result"]
        res["theme"] = r["state"]["theme"]
        await interaction.response.edit_message(embed=result_embed(interaction.user, res, ["🏃 무사히 던전을 빠져나왔어요."]),
                                                view=None)

    async def _use_ticket(self, interaction: discord.Interaction, user):
        if not await self.db.use_ticket(user.id, int(time.time())):
            return await interaction.response.send_message("🙅 🎫 던전 추가 입장권이 없어요.", ephemeral=True)
        await interaction.response.send_message(embed=ui.card(
            "🎫 던전 추가 입장권 사용!", "오늘 `/던전`에 한 번 더 들어갈 수 있어요.", ui.GOLD, user, "🎒 아이템"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Dungeon(bot))
