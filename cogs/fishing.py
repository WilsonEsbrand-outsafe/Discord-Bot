# cogs/fishing.py — 🎣 /낚시 · /낚시시세 (2.8)
# /낚시 = 낚시터 화면(모두에게): 찌 던지기 → 입질이 오면 봇이 이 메시지를 [🐟 잡기!]로 바꾼다 → 잡은 결과.
#   입질 알림 · 도망 처리는 봇이 시각에 맞춰 직접 고친다 (재시작해도 DB 에서 다시 예약).
# [🧺 어망] = 모두에게 보이는 낚시 메뉴(허브, 2.10.1 · 버튼은 연 사람만): 어망 · 시세 · 도감 · 수족관 · 요리 · 먹이기 · 상점 · 주간 최대어 — ◀ 🏠 ▶.
import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

import release
from cogs.economy import ITEM_HANDLERS
from services import ui
from services.club_db import ClubDB
from services.economy_db import ITEM_PRICES, ITEMS, SHOP_ONE_ONLY, EconomyDB, give_item
from services.fishing import (
    AQUARIUM_MAX, BAG_MAX, CATCH_WINDOW, DAY_FISH_MULT, bag_max, day_fish, CHARM_BONUS, RODS, FISH, FishingDB, GRADE_EMOJI, GRADE_POINTS, GRADES, RECIPES,
    SKIP_PIECES, TRADE_DAILY, by_grade, fish_name, grade_odds, level_need, odds_line,
)
from services.hub import HubView, Page
from services.notifier import send_notify

SECTION = "🎣 낚시터"
SHOP = {k: ITEM_PRICES[k] for k in ("bait", "bait_gold", "rod_carbon", "rod_pro", "rod_legend", "icebox")}   # /아이템상점과 같은 가격
ONE_ONLY = SHOP_ONE_ONLY   # 하나만 가지면 되는 장비


def _clock(sec: int) -> str:
    m, s = divmod(max(0, int(sec)), 60)
    return f"{m}분 {s}초" if m else f"{s}초"


def fish_line(f: dict, price: bool = True) -> str:
    e, n, g, _, _ = FISH[f["species"]]
    s = f"{GRADE_EMOJI[g]} {e} **{n}** {f['size']:g}cm"
    if price and "price" in f:
        s += f" · {f['price']:,}원" + (f" (신선도 {f['fresh']:.0%})" if f.get("fresh", 1) < 1 else "")
    if f.get("displayed"):
        s += " · 🐠 전시 중"
    return s


class CastView(discord.ui.View):
    """낚시터 화면의 버튼 — 찌 던지기(미끼 고르기) · 찌 걷기 · 낚시 메뉴. 연 사람만."""

    def __init__(self, cog: "Fishing", user, state: dict, keep: bool = False):
        super().__init__(timeout=900)
        self.cog, self.user, self.keep = cog, user, keep   # keep: 이 메시지가 잡은 결과면 다시 던질 때 남겨 둔다 (2.9.1)
        if state.get("cast_ts"):
            b = discord.ui.Button(label="찌 걷기", emoji="🪝", style=discord.ButtonStyle.secondary)
            b.callback = self._pull
            self.add_item(b)
        else:
            for bait, label, emoji in ((None, "던지기", "🎣"), ("bait", "일반 미끼로", "🪱"), ("bait_gold", "고급 미끼로", "✨")):
                n = state["bait_qty"].get(bait, 0) if bait else 1
                if not n:
                    continue
                b = discord.ui.Button(label=label + (f" ({n})" if bait else ""), emoji=emoji,
                                      style=discord.ButtonStyle.success if bait is None else discord.ButtonStyle.primary)
                b.callback = self._cast(bait)
                self.add_item(b)
        m = discord.ui.Button(label="어망 · 메뉴", emoji="🧺", style=discord.ButtonStyle.secondary)
        m.callback = self._menu
        self.add_item(m)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 자기 낚시터에서만 던질 수 있어요. `/낚시`를 직접 열어 주세요.", ephemeral=True)
            return False
        return True

    def _cast(self, bait):
        async def cb(interaction: discord.Interaction):
            await self.cog.do_cast(interaction, bait, self.keep)
        return cb

    async def _pull(self, interaction: discord.Interaction):
        await self.cog.do_reel(interaction)

    async def _menu(self, interaction: discord.Interaction):
        await self.cog.open_menu(interaction)


class ReelView(discord.ui.View):
    """입질! — 재시작해도 눌리게 고정 custom_id. 누른 사람의 찌인지 DB 로 확인한다."""

    def __init__(self, cog: "Fishing"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="잡기!", emoji="🐟", style=discord.ButtonStyle.danger, custom_id="fish:reel")
    async def reel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.do_reel(interaction, from_bite=True)


class Fishing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = FishingDB()
        self.clubs = ClubDB()
        self.econ = EconomyDB()   # 2.82 🎣 입질 DM (/알림설정 "낚시 입질")
        self._tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        self.bot.add_view(ReelView(self))
        ITEM_HANDLERS.update({"fish_charm": self._use_charm, "skip_piece": self._use_pieces})
        for c in await self.db.active_casts():
            if c["message_id"] and c["channel_id"]:
                self._schedule(c)

    async def cog_unload(self):
        for t in self._tasks.values():
            t.cancel()
        for k in ("fish_charm", "skip_piece"):
            ITEM_HANDLERS.pop(k, None)

    # ───────────── 화면 ─────────────
    async def _pond_embed(self, user, s: dict, note: str = "") -> discord.Embed:
        lv = f"`낚시` **Lv.{s['level']}** {ui.bar(s['xp'], s['need'], 8)} {s['xp']}/{s['need']}"
        new = self._n(user)
        bag = f"`어망` **{s['bag']}/{bag_max(s['level'], new)}** · `오늘 보내기` **{s['used']}/{s['limit']}**포인트"
        if new:   # 3.1.0 오늘의 물고기
            k = day_fish(int(time.time()))
            bag += f"\n`오늘의 물고기` {FISH[k][0]} **{FISH[k][1]}** — 오늘 팔면 **{DAY_FISH_MULT:g}배**"
        if s.get("cast_ts"):
            body = (f"> 🎙️ *\"찌가 물 위에 둥둥… 입질이 오면 이 메시지가 바뀌어요!\"*\n\n"
                    f"🎣 **<t:{s['cast_ts']}:R>** 던짐 · 입질은 10초 ~ 10분 사이 랜덤\n"   # 2.82: 디스코드 상대시간이라 저절로 늘어난다
                    f"⏳ 오래 기다린 입질일수록 희귀한 물고기가 걸려요")
            color = ui.DARK
        else:
            window = RODS[s["rod"]][1] if s.get("rod") else CATCH_WINDOW
            body = ("> 🎙️ *\"오늘은 뭐가 걸릴까요?\"*\n\n"
                    "🎣 찌를 던지면 **10초 ~ 10분** 사이에 입질이 와요.\n"
                    f"🐟 입질이 오면 **{window // 60}분 안에** [잡기!]를 눌러야 해요. 늦으면 도망가요.")
            color = ui.INFO
        rod = ITEMS[s["rod"]][1] if s.get("rod") else "기본 낚싯대"
        cook = " · ".join(f"{RECIPES[k][0]} {RECIPES[k][1]}" for k in s.get("buffs", []) if k in RECIPES)
        e = ui.card("🎣 낚시터", (note + "\n\n" if note else "") + body + f"\n\n{lv}\n{bag}\n`장비` {rod}"
                    + (f"\n`요리 효과` {cook}" if cook else ""), color, user, SECTION)
        e.set_thumbnail(url=ui.emoji_url("🎣"))
        return e

    async def _show_pond(self, interaction: discord.Interaction, note: str = "", edit: bool = True):
        s = await self.db.status(interaction.user.id, int(time.time()))
        e = await self._pond_embed(interaction.user, s, note)
        view = CastView(self, interaction.user, s)
        if edit:
            await interaction.response.edit_message(embed=e, view=view)
        else:
            await interaction.response.send_message(embed=e, view=view)

    @app_commands.command(name="낚시", description="찌를 던지고 입질을 기다려요 — 오래 기다린 입질일수록 희귀한 물고기 · 어망 · 요리 · 수족관 · 도감")
    async def fish(self, interaction: discord.Interaction):
        s = await self.db.status(interaction.user.id, int(time.time()))
        if s.get("cast_ts") and s.get("message_id"):
            note = "🎣 찌가 이미 물에 있어요! 입질이 오면 처음 던진 메시지가 바뀌어요."
            now = int(time.time())
            if now >= s["bite_ts"]:   # 입질이 왔는데 메시지를 놓친 경우 — 여기서도 잡을 수 있게
                note = "🐟 입질이 와 있어요! 아래 [잡기!]를 누르세요."
                e = await self._pond_embed(interaction.user, s, note)
                await interaction.response.send_message(embed=e, view=ReelView(self))
                return await self.db.set_message(interaction.user.id, (await interaction.original_response()).id)
            e = await self._pond_embed(interaction.user, s, note)
            return await interaction.response.send_message(embed=e, view=CastView(self, interaction.user, s))
        await self._show_pond(interaction, edit=False)

    # ───────────── 던지기 · 입질 · 잡기 ─────────────
    async def do_cast(self, interaction: discord.Interaction, bait, keep: bool = False):
        user = interaction.user
        r = await self.db.cast(user.id, int(time.time()), bait, interaction.channel_id or 0, new=self._n(user))
        if not r["ok"]:
            msg = {"busy": "찌가 이미 물에 있어요.",
                   "full": f"어망이 가득 찼어요 ({r.get('max', BAG_MAX)}마리). 팔거나 먹이거나 놓아주세요."
                           + (" 낚시 레벨이 오르면 어망 칸이 늘어나요." if self._n(user) else ""),
                   "no_bait": "미끼가 없어요."}[r["reason"]]
            return await interaction.response.send_message(msg, ephemeral=True)
        note = "🪱 일반 미끼를 끼웠어요 — 이번엔 잡템이 덜 걸려요." if bait == "bait" else \
            "✨ 고급 미끼를 끼웠어요 — 이번엔 희귀한 물고기가 더 잘 걸려요." if bait == "bait_gold" else ""
        # 2.9.1: 잡은 결과 메시지는 그대로 두고(버튼만 뗀다) 새 낚시터를 아래에 연다 — 뭐가 걸렸는지 채널에 남는다
        await self._show_pond(interaction, note, edit=not keep)
        if keep:
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass
        msg = await interaction.original_response()
        await self.db.set_message(user.id, msg.id)
        self._schedule({"user_id": user.id, "bite_ts": r["bite_ts"], "window": r["window"],
                        "channel_id": interaction.channel_id, "message_id": msg.id})

    def _schedule(self, c: dict):
        old = self._tasks.pop(c["user_id"], None)
        if old:
            old.cancel()
        self._tasks[c["user_id"]] = asyncio.create_task(self._bite_task(c))

    @staticmethod
    def _n(user) -> bool:
        """이 유저가 있는 서버가 3.1.0 공개 서버인가."""
        return release.preview_310(getattr(getattr(user, "guild", None), "id", None))

    def _new(self, c: dict) -> bool:
        """찌를 던진 채널의 서버가 3.1.0 공개 서버인가."""
        return release.preview_310(getattr(getattr(self.bot.get_channel(c["channel_id"]), "guild", None), "id", None))

    async def _message(self, c: dict):
        ch = self.bot.get_channel(c["channel_id"])
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(c["channel_id"])
            except discord.HTTPException:
                return None
        return ch.get_partial_message(c["message_id"])

    async def _bite_task(self, c: dict):
        """입질 시각에 메시지를 [잡기!]로 · 기다려 주는 시간이 끝나면 '도망'으로 (그때까지 안 잡았으면)."""
        try:
            uid = c["user_id"]
            await asyncio.sleep(max(0, c["bite_ts"] - time.time()))
            s = await self.db.status(uid, int(time.time()))
            if s.get("message_id") != c["message_id"]:
                return
            msg = await self._message(c)
            if msg is None:
                return
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            wait = c["bite_ts"] - s["cast_ts"]
            e = ui.card("🐟 입질이다!!", f'> 🎙️ *"찌가 쑥 들어갑니다!! 지금 당겨요!!"*\n\n'
                        f"⏱️ **{_clock(c['window'])}** 안에 [잡기!]를 누르세요 (<t:{c['bite_ts'] + c['window']}:R>)\n"
                        f"⏳ 기다린 시간 **{_clock(wait)}** — {odds_line(grade_odds(wait, new=self._new(c)))}",
                        ui.GOLD, user, SECTION)
            e.set_thumbnail(url=ui.emoji_url("🐟"))
            try:
                await msg.edit(embed=e, view=ReelView(self))
            except discord.HTTPException:
                pass
            await send_notify(self.bot, self.econ, uid, "낚시_입질", self._bite_dm(c, user))
            await asyncio.sleep(max(0, c["bite_ts"] + c["window"] - time.time()) + 1)
            s = await self.db.status(uid, int(time.time()))
            if s.get("message_id") != c["message_id"]:
                return
            await self.db.reel(uid, int(time.time()))   # 너무 늦음 → 도망 처리 (찌 회수)
            e = ui.card("💨 놓쳤어요…", '> 🎙️ *"물고기가 바늘을 털고 도망갔습니다…"*\n\n`/낚시`로 다시 던져 보세요.',
                        ui.EVEN, user, SECTION)
            try:
                await msg.edit(embed=e, view=None)
            except discord.HTTPException:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            print(f"[낚시] 입질 예약 오류: {type(ex).__name__}: {ex}")
        finally:
            if self._tasks.get(c["user_id"]) is asyncio.current_task():
                self._tasks.pop(c["user_id"], None)

    def _bite_dm(self, c: dict, user) -> discord.Embed:
        guild = getattr(self.bot.get_channel(c["channel_id"]), "guild", None)
        link = f"https://discord.com/channels/{guild.id if guild else '@me'}/{c['channel_id']}/{c['message_id']}"
        return ui.card("🎣 입질이 왔어요!", f"⏱️ <t:{c['bite_ts'] + c['window']}:R>까지 [잡기!]를 누르세요.\n[👉 낚시터로 가기]({link})",
                       ui.GOLD, user, SECTION)

    async def do_reel(self, interaction: discord.Interaction, from_bite: bool = False):
        user = interaction.user
        s = await self.db.status(user.id, int(time.time()))
        if from_bite and s.get("message_id") not in (None, interaction.message.id) and s.get("cast_ts"):
            return await interaction.response.send_message("🙅 자기 찌만 당길 수 있어요.", ephemeral=True)
        if from_bite and not s.get("cast_ts"):
            return await interaction.response.send_message("🙅 이 찌는 주인이 아니거나 이미 끝났어요.", ephemeral=True)
        new = release.preview_291(interaction.guild_id)   # 2.9.1: 경험치 하향 · 결과 남기기 (테스트 서버 먼저)
        r = await self.db.reel(user.id, int(time.time()), new_xp=new,
                               new_310=release.preview_310(interaction.guild_id))   # 3.1.0: 새 물고기 15종 · 희귀 확률 하향
        task = self._tasks.pop(user.id, None)
        if task:
            task.cancel()
        if not r["ok"]:
            return await interaction.response.send_message("🎣 물에 던진 찌가 없어요. `/낚시`로 던져 보세요.", ephemeral=True)
        e = self._result_card(user, r)
        s = await self.db.status(user.id, int(time.time()))
        await interaction.response.edit_message(embed=e, view=CastView(self, user, s, keep=new and r["kind"] in ("fish", "junk")))

    def _result_card(self, user, r: dict) -> discord.Embed:
        kind = r["kind"]
        if kind in ("early", "fled"):
            e = (ui.card("🪝 찌를 걷었어요", "아직 입질 전이었어요. 미끼는 물고기 밥이 됐어요.", ui.EVEN, user, SECTION) if kind == "early"
                 else ui.card("💨 놓쳤어요…", "너무 늦게 당겼어요. 물고기가 도망갔습니다.", ui.EVEN, user, SECTION))
            if url := ui.scene_url(f"fish_{kind}", user):   # 3.1.0 키코 그림 (공개 전엔 그림 없음)
                e.set_thumbnail(url=url)
            return e
        if kind == "junk":
            if r["item"] == "old_boot":
                e = ui.card("👢 낡은 장화…", '> 🎙️ *"묵직했는데… 장화네요."*\n\n아무것도 얻지 못했어요.', ui.EVEN, user, SECTION)
                e.set_thumbnail(url=ui.result_thumb("👢", ui.EVEN, user, "fish_boot"))
                return e
            emoji, name, desc = ITEMS[r["item"]]
            e = ui.card(f"{emoji} {name}을(를) 건졌어요!", f'> 🎙️ *"물고기는 아니지만 쓸 만한 게 걸렸어요!"*\n\n*{desc}*\n🎒 `/가방`에 넣었어요.',
                        ui.WIN, user, SECTION)
            e.set_thumbnail(url=ui.result_thumb(emoji, ui.WIN, user, "fish_item"))
            return e
        e_, n, g, avg, _ = FISH[r["species"]]
        tags = (" · 🆕 도감 등록!" if r["new"] else "") + (" · 📏 내 최고 기록!" if r["record"] else "")
        color = (ui.EVEN, ui.WIN, ui.INFO, ui.GOLD, ui.GOLD)[g]
        call = ("손맛이 가볍네요.", "괜찮은 녀석이에요!", "오오, 희귀한 녀석이에요!!", "대어다!!! 낚싯대가 휘어집니다!!",
                "전설의 물고기!!! 낚시터가 술렁입니다!!!")[g]
        desc = (f'> 🎙️ *"{call}"*\n\n{GRADE_EMOJI[g]} **{GRADES[g]}** · {e_} **{n}** · **{r["size"]:g}cm** (평균 {avg}cm){tags}\n'
                f"`오늘 시세` **{r['price']:,}원** · `경험치` +{r['xp']}"
                + (f"\n🆙 낚시 **Lv.{r['level']}** 달성!" if r["leveled"] else "")
                + f"\n⏳ 기다린 시간 {_clock(r['wait'])}")
        e = ui.card(f"{e_} {n}을(를) 낚았다!", desc, color, user, SECTION)
        e.set_thumbnail(url=ui.result_thumb(e_, color, user, f"fish_{g}"))   # 3.1.0 등급별 키코 그림
        return e

    # ───────────── 낚시 메뉴 (허브) ─────────────
    async def open_menu(self, interaction: discord.Interaction, page: int = 0):
        view = self.menu_view(interaction.user, page, release.preview_291(interaction.guild_id))
        await view.send(interaction, ephemeral=not release.preview_2101(interaction.guild_id))   # 2.10.1: 어망 · 메뉴는 모두에게 (버튼은 연 사람만)

    def menu_view(self, user, page: int = 0, new: bool = True) -> HubView:
        pages = [
            Page("🧺", "어망", self._p_bag, "잡은 물고기 · 팔기 · 놓아주기 · 보내기"),
            Page("📈", "오늘 시세", self._p_market, "물고기마다 오늘 값 ▲▼"),
            Page("🍽️", "먹이기", self._p_feed, "유망주 · 선발 선수에게 하루 한 마리"),
            Page("🍳", "요리", self._p_cook, "24시간 효과"),
            Page("🐠", "수족관", self._p_aquarium, f"최대 {AQUARIUM_MAX}마리 전시"),
            Page("📖", "도감", self._p_dex, "40종 모으기"),
            Page("📜", "낚시 기록", self._p_log, "최근에 낚은 물고기 15마리"),
            Page("🏆", "주간 최대어", self._p_weekly, "이번 주 가장 큰 물고기"),
            Page("🛒", "낚시 상점", self._p_shop, "미끼 · 낚싯대 · 아이스박스"),
        ]
        if not new:   # 2.9.1 낚시 기록은 공개 전 서버엔 아직
            pages = [p for p in pages if p.render != self._p_log]
        return HubView(user, "🎣 낚시 메뉴", SECTION, pages, "/낚시", page,
                       ctx={"sel": []},
                       home_note=f"판매는 한도 없음 · 다른 유저에게 보내기는 하루 **{TRADE_DAILY}포인트**까지 (흔함 1 · 보통 1 · 희귀 2 · 영웅 3 · 전설 5).\n"
                                 "먹이기 · 요리 · 수족관 · 도감은 한도가 없어요.")

    async def _bag_select(self, view: HubView, fish: list[dict], placeholder: str, row: int, cb, max_values: int = 25,
                          all_label: str = ""):
        """all_label 이 있으면 맨 위에 '전부' 항목(value 0 — 물고기 id 는 1부터)을 붙인다 (3.1.0 전부 팔기)."""
        opts = [discord.SelectOption(label=f"{FISH[f['species']][1]} {f['size']:g}cm"[:100], value=str(f["id"]),
                                     emoji=GRADE_EMOJI[FISH[f["species"]][2]],
                                     description=(f"{f['price']:,}원 · {GRADE_POINTS[FISH[f['species']][2]]}포인트"
                                                  if "price" in f else None))
                for f in fish[:24 if all_label else 25]]
        if all_label and opts:
            opts.insert(0, discord.SelectOption(label=all_label[:100], value="0", emoji="💰",
                                                description="전시 중인 물고기는 빼고 어망을 한 번에 비워요"))
        if not opts:
            return None
        sel = discord.ui.Select(placeholder=placeholder, options=opts, row=row, min_values=1,
                                max_values=min(max_values, len(opts)))

        async def chosen(interaction: discord.Interaction):
            await cb(interaction, view, [int(v) for v in sel.values])
        sel.callback = chosen
        view.add_item(sel)
        return sel

    async def _p_bag(self, view: HubView) -> discord.Embed:
        user = view.user
        new = self._n(user)
        b = await self.db.bag(user.id, int(time.time()), new)
        fish = sorted(b["fish"], key=lambda f: -f["price"])
        free = [f for f in fish if not f["displayed"]]
        lines = [fish_line(f) for f in fish[:20]]
        more = f"\n…외 {len(fish) - 20}마리" if len(fish) > 20 else ""
        e = ui.card(f"🧺 어망 · {len(fish)}/{b['max']}",
                    ("\n".join(lines) + more if lines else "어망이 비어 있어요. `/낚시`로 낚아 보세요!")
                    + f"\n\n`오늘 보내기` **{b['used']}/{b['limit']}**포인트" + (" · 🧊 아이스박스" if b["icebox"] else ""),
                    ui.INFO, user, SECTION)
        await self._bag_select(view, free, "💰 팔 물고기 고르기 (여러 마리 가능)", 0, self._sell,
                               all_label=f"전부 팔기 · {len(free)}마리 · {sum(f['price'] for f in free):,}원" if new else "")
        await self._bag_select(view, free, "🌊 놓아주기 (돈 · 한도 없이 어망 비우기)", 1, self._release)
        if free:
            us = discord.ui.UserSelect(placeholder="🎁 물고기를 보낼 사람 (그다음 물고기 고르기)", row=2)

            async def picked(interaction: discord.Interaction):
                view.ctx["to"] = us.values[0]
                await view.go(interaction, 0, ui.card("🎁 보낼 물고기를 골라 주세요",
                                                      f"받는 사람: **{us.values[0].display_name}** — 아래 메뉴에서 고르면 바로 보내요.",
                                                      ui.INFO, user, SECTION))
            us.callback = picked
            view.add_item(us)
            if view.ctx.get("to"):
                await self._bag_select(view, free, f"🎁 {view.ctx['to'].display_name}님에게 보낼 물고기", 3, self._give)
        return e

    async def _sell(self, interaction, view: HubView, ids: list[int]):
        r = await self.db.sell(view.user.id, None if 0 in ids else ids, int(time.time()), self._n(view.user))
        if r["ok"]:
            note = ui.card(f"💰 {r['count']}마리 판매!", f"`정산` **+{r['gain']:,}원** · `잔액` **{r['balance']:,}원**",
                           ui.WIN, view.user, SECTION)
        else:
            note = ui.card("🙅 팔 수 없어요", "이미 없거나 수족관에 전시 중인 물고기예요.", ui.LOSE, view.user, SECTION)
        await view.refresh(interaction, note)

    async def _release(self, interaction, view: HubView, ids: list[int]):
        n = await self.db.release_fish(view.user.id, ids)
        await view.refresh(interaction, ui.card(f"🌊 {n}마리를 놓아줬어요", "물고기들이 기뻐하며 헤엄쳐 갑니다.", ui.EVEN, view.user, SECTION))

    async def _give(self, interaction, view: HubView, ids: list[int]):
        to = view.ctx.pop("to", None)
        if to is None or getattr(to, "bot", False):
            return await view.refresh(interaction, ui.card("🙅 받을 사람을 다시 골라 주세요", "", ui.LOSE, view.user, SECTION))
        r = await self.db.give(view.user.id, to.id, ids, int(time.time()), self._n(view.user))
        if r["ok"]:
            note = ui.card(f"🎁 {to.display_name}님에게 {r['count']}마리 보냈어요", ", ".join(r["names"])
                           + f"\n`오늘 보내기` **{r['used']}/{r['limit']}**포인트", ui.WIN, view.user, SECTION)
            await view.refresh(interaction, note)
            if not release.preview_2101(getattr(interaction, "guild_id", None)):
                return
            # 2.10.1: 보낸 건 모두에게 — 채널 맨 아래에 새 메시지로
            shout = ui.card(f"🎁 {view.user.display_name}님이 {to.display_name}님에게 물고기를 보냈어요!",
                            ", ".join(r["names"]) + f"\n총 **{r['count']}마리**", ui.WIN, view.user, SECTION)
            return await interaction.followup.send(content=to.mention, embed=shout, ephemeral=False)
        else:
            msg = {"self": "자기 자신에게는 보낼 수 없어요.", "none": "보낼 물고기가 없어요.",
                   "limit": f"오늘 보내기 한도가 모자라요 (필요 {r.get('need')} · 남음 {r.get('left')}).",
                   "full": "받는 사람의 어망이 가득 찼어요."}[r["reason"]]
            note = ui.card("🙅 보낼 수 없어요", msg, ui.LOSE, view.user, SECTION)
        await view.refresh(interaction, note)

    async def _p_market(self, view: HubView) -> discord.Embed:
        return await self.market_embed(view.user)

    async def market_embed(self, user) -> discord.Embed:
        new = self._n(user)
        m = await self.db.market(int(time.time()), new)
        today = day_fish(int(time.time())) if new else None
        parts = []
        for g in range(5):
            rows = []
            for k in by_grade(new)[g]:
                v = m[k]
                d = v["mult"] / v["prev"] - 1 if v["prev"] else 0
                arrow = "▲" if d > 0.005 else ("▼" if d < -0.005 else "－")
                rows.append(f"{FISH[k][0]} {FISH[k][1]} **{v['price']:,}원** {arrow}{abs(d) * 100:.0f}%"
                            + (" 📉" if v["supply"] < 0.9 else "")
                            + (f" ⭐ **오늘의 물고기 {DAY_FISH_MULT:g}배**" if k == today else ""))
            parts.append(f"**{GRADE_EMOJI[g]} {GRADES[g]}**\n" + "\n".join(rows))
        e = ui.card("📈 오늘의 물고기 시세", "\n\n".join(parts), ui.INFO, user, SECTION)
        e.set_footer(text="평균 크기 · 갓 잡은 기준 · 매일 00시 바뀜 · 📉 = 서버에서 많이 팔려 값이 내려감 (매일 조금씩 회복) · 신선도 하루 -10%")
        return e

    async def _p_feed(self, view: HubView) -> discord.Embed:
        user, now = view.user, int(time.time())
        b = await self.db.bag(user.id, now, self._n(user))
        free = sorted((f for f in b["fish"] if not f["displayed"]), key=lambda f: f["price"])
        fed = await self.db.fed_today(user.id, now)
        team = await self.clubs.get_team(user.id)
        targets = []
        p = (await self.clubs.prospects(user.id, now))["active"]
        if p:
            targets.append((f"P{p['id']}", f"🌟 {p['name']} (유망주)", "성장 경험치 +2~15"))
        if team:
            for s in team["lineup"]:
                if s.get("player_id") and not str(s["player_id"]).startswith("YP"):
                    targets.append((s["player_id"], f"{s['slot']} {s['name']}", "선발 11명 모두 먹이면 오늘 경기 전력 +1"))
        left = [t for t in targets if t[0] not in fed]
        starters = sum(1 for t in fed if not t.startswith("P"))
        e = ui.card("🍽️ 먹이기", "물고기 한 마리를 하루에 한 번씩 먹일 수 있어요. (판매 한도와 상관없음)\n\n"
                    "🌟 **유망주** — 성장 경험치 흔함 +2 · 보통 +3 · 희귀 +5 · 영웅 +8 · 전설 +15 (다음 경기 때 반영)\n"
                    "⚽ **선발 선수** — 오늘 11명을 모두 먹이면 🍽️ 든든한 식단: 오늘 경기 전력 **+1**\n\n"
                    f"오늘 먹인 선발 **{starters}/11** · 남은 대상 **{len(left)}명**", ui.INFO, user, SECTION)
        if not free or not left:
            e.description += "\n\n" + ("먹일 물고기가 없어요." if not free else "오늘은 모두 먹였어요!")
            return e
        tsel = discord.ui.Select(placeholder="① 먹일 대상 고르기", row=0, options=[
            discord.SelectOption(label=name[:100], value=t, description=d, default=view.ctx.get("feed_to") == t)
            for t, name, d in left[:25]])

        async def pick_target(interaction: discord.Interaction):
            view.ctx["feed_to"] = tsel.values[0]
            await view.refresh(interaction)
        tsel.callback = pick_target
        view.add_item(tsel)
        if view.ctx.get("feed_to") in {t for t, _, _ in left}:
            await self._bag_select(view, free, "② 먹일 물고기 고르기 (싼 것부터)", 1, self._feed, max_values=1)
        return e

    async def _feed(self, interaction, view: HubView, ids: list[int]):
        target = view.ctx.pop("feed_to", None)
        r = await self.db.feed(view.user.id, ids[0], target or "", int(time.time()))
        if r["ok"]:
            n = FISH[r["species"]][1]
            if r["kind"] == "prospect":
                msg = f"🌟 **{r['name']}**에게 {n}을(를) 먹였어요! 성장 경험치 **+{r['xp']}**"
            else:
                msg = f"⚽ 선발 선수에게 {n}을(를) 먹였어요. 오늘 먹인 선발 **{r['fed_today']}/11**"
                if r["fed_today"] >= 11:
                    msg += "\n🍽️ **든든한 식단** — 오늘 경기 전력 +1!"
            note = ui.card("🍽️ 냠냠!", msg, ui.WIN, view.user, SECTION)
        else:
            msg = {"none": "그 물고기가 없어요.", "fed": "오늘은 이미 먹였어요.", "not_starter": "선발 선수나 현역 유망주가 아니에요."}[r["reason"]]
            note = ui.card("🙅 먹일 수 없어요", msg, ui.LOSE, view.user, SECTION)
        await view.refresh(interaction, note)

    async def _p_cook(self, view: HubView) -> discord.Embed:
        user, now = view.user, int(time.time())
        active = await self.db.buffs(user.id, now)
        inv, _ = await self.db.inventory(user.id)
        lines = []
        for k, (emoji, name, needs, spice, effect) in RECIPES.items():
            need = " + ".join(f"{GRADE_EMOJI[g]} {GRADES[g]} {n}마리" for g, n in needs) + (f" + 🧂 양념 {spice}" if spice else "")
            on = f" · ✅ <t:{active[k]}:R> 끝" if k in active else ""
            lines.append(f"{emoji} **{name}** — {effect}{on}\n　 재료: {need}")
        e = ui.card("🍳 요리", "요리하면 **24시간** 효과가 생겨요. 같은 요리를 또 하면 시간이 새로 24시간이 돼요.\n"
                    "물고기는 그 등급에서 가장 싼 것부터 써요.\n\n" + "\n".join(lines)
                    + f"\n\n🧂 가진 양념 **{inv.get('spice', 0)}개** — 낚시 잡템 · 던전에서 얻어요", ui.INFO, user, SECTION)
        sel = discord.ui.Select(placeholder="🍳 만들 요리 고르기", row=0, options=[
            discord.SelectOption(label=name, value=k, emoji=emoji, description=effect[:100])
            for k, (emoji, name, _, _, effect) in RECIPES.items()])

        async def cook(interaction: discord.Interaction):
            k = sel.values[0]
            r = await self.db.cook(user.id, k, int(time.time()))
            emoji, name, *_ = RECIPES[k]
            if r["ok"]:
                note = ui.card(f"{emoji} {name} 완성!", f"{RECIPES[k][4]} — <t:{r['until']}:R> 까지\n재료: {', '.join(r['used'])}",
                               ui.GOLD, user, SECTION)
            elif r["reason"] == "fish":
                note = ui.card("🙅 물고기가 모자라요", f"{GRADES[r['grade']]} 물고기 {r['need']}마리가 필요해요 (지금 {r['have']}마리 · 전시 중 제외).",
                               ui.LOSE, user, SECTION)
            else:
                note = ui.card("🙅 양념이 모자라요", f"🧂 양념 {r['need']}개가 필요해요 (지금 {r['have']}개).", ui.LOSE, user, SECTION)
            await view.refresh(interaction, note)
        sel.callback = cook
        view.add_item(sel)
        return e

    async def _p_aquarium(self, view: HubView) -> discord.Embed:
        user = view.user
        b = await self.db.bag(user.id, int(time.time()))
        shown = [f for f in b["fish"] if f["displayed"]]
        score = await self.db.aquarium_score(user.id)
        e = ui.card(f"🐠 수족관 · {len(shown)}/{AQUARIUM_MAX}",
                    ("\n".join(fish_line(f, price=False) for f in shown) if shown else "아직 전시한 물고기가 없어요.")
                    + f"\n\n`수족관 점수` **{score}** — 흔함 1 · 보통 2 · 희귀 4 · 영웅 8 · 전설 16\n"
                    "전시 중인 물고기는 팔거나 먹일 수 없어요. 신선도는 떨어지지 않아요(전시 중엔 값을 매기지 않아요).",
                    ui.INFO, user, SECTION)
        if len(shown) < AQUARIUM_MAX:
            await self._bag_select(view, sorted((f for f in b["fish"] if not f["displayed"]), key=lambda f: -FISH[f["species"]][2]),
                                   "🐠 전시할 물고기", 0, self._show_fish, max_values=1)
        await self._bag_select(view, shown, "↩️ 전시 내리기", 1, self._hide_fish, max_values=1)
        return e

    async def _show_fish(self, interaction, view: HubView, ids: list[int]):
        r = await self.db.display(view.user.id, ids[0], True)
        await view.refresh(interaction, None if r["ok"] else ui.card("🙅 전시할 수 없어요", "수족관이 가득 찼어요.", ui.LOSE, view.user, SECTION))

    async def _hide_fish(self, interaction, view: HubView, ids: list[int]):
        await self.db.display(view.user.id, ids[0], False)
        await view.refresh(interaction)

    async def _p_dex(self, view: HubView) -> discord.Embed:
        dex = await self.db.dex(view.user.id)
        pool = by_grade(release.preview_310(getattr(getattr(view.user, "guild", None), "id", None)))
        parts = []
        for g in range(5):
            cells = [f"{FISH[k][0]} {FISH[k][1]} ×{dex[k][0]} · 최대 {dex[k][1]:g}cm" if k in dex else "❔ ???" for k in pool[g]]
            have = sum(k in dex for k in pool[g])
            parts.append(f"**{GRADE_EMOJI[g]} {GRADES[g]}** {have}/{len(pool[g])}\n" + "\n".join(cells))
        return ui.card(f"📖 도감 · {len(dex)}/{sum(map(len, pool))}", "\n\n".join(parts), ui.INFO, view.user, SECTION)

    async def _p_log(self, view: HubView) -> discord.Embed:
        log = await self.db.recent(view.user.id)
        lines = [f"<t:{f['ts']}:R> {fish_line(f, price=False)}" for f in log]
        return ui.card("📜 낚시 기록", "\n".join(lines) or "아직 낚은 물고기가 없어요. `/낚시`로 던져 보세요!",
                       ui.INFO if lines else ui.EVEN, view.user, SECTION).set_footer(
            text="최근 15마리 · 판 물고기 · 보낸 물고기도 남아요")

    async def _p_weekly(self, view: HubView) -> discord.Embed:
        top = await self.db.weekly_top(int(time.time()))
        lines = []
        for i, t in enumerate(top):
            u = self.bot.get_user(t["user_id"])
            name = u.display_name if u else f"#{t['user_id'] % 10000}"
            lines.append(f"{('🥇', '🥈', '🥉', '4.', '5.')[i]} **{name}** — {fish_name(t['species'])} **{t['size']:g}cm** "
                         f"(평균의 {t['ratio']:.0%})")
        return ui.card("🏆 이번 주 최대어", "\n".join(lines) or "이번 주엔 아직 아무도 낚지 못했어요.",
                       ui.GOLD if lines else ui.EVEN, view.user, SECTION).set_footer(
            text="등급이 높은 물고기 먼저, 같으면 평균 크기 대비 더 큰 쪽 · 매주 월요일 00시 새로 시작")

    async def _p_shop(self, view: HubView) -> discord.Embed:
        user = view.user
        inv, _ = await self.db.inventory(user.id)
        bal = await self.db.get_balance(user.id)
        lines = [f"{ITEMS[k][0]} **{ITEMS[k][1]}** · {p:,}원" + (" · ✅ 보유" if k in ONE_ONLY and inv.get(k) else
                                                               f" · 보유 {inv.get(k, 0)}") + f"\n　 *{ITEMS[k][2]}*"
                 for k, p in SHOP.items()]
        e = ui.card("🛒 낚시 상점", f"`잔액` **{bal:,}원**\n\n" + "\n".join(lines)
                    + "\n\n🍀 낚시 부적 · 🧂 양념 · 🧩 스킵권 조각은 잡템 · 던전에서만 나와요.", ui.INFO, user, SECTION)
        sel = discord.ui.Select(placeholder="🛒 살 물건 고르기", row=0, options=[
            discord.SelectOption(label=f"{ITEMS[k][1]} · {p:,}원", value=k, emoji=ITEMS[k][0])
            for k, p in SHOP.items() if not (k in ONE_ONLY and inv.get(k))])

        async def buy(interaction: discord.Interaction):
            k = sel.values[0]
            r = await self.buy(user.id, k)
            note = (ui.card(f"🛒 {ITEMS[k][0]} {ITEMS[k][1]} 구매!", f"`가격` **-{SHOP[k]:,}원** · `잔액` **{r['balance']:,}원**",
                            ui.WIN, user, SECTION) if r["ok"] else
                    ui.card("🙅 살 수 없어요", "잔액이 부족해요." if r["reason"] == "balance" else "이미 가지고 있어요.",
                            ui.LOSE, user, SECTION))
            await view.refresh(interaction, note)
        sel.callback = buy
        if sel.options:
            view.add_item(sel)
        return e

    async def buy(self, uid: int, item: str) -> dict:
        price = SHOP[item]

        def fn(con):
            if item in ONE_ONLY and self.db._qty(con, uid, item):
                return {"ok": False, "reason": "have"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])
            if bal < price:
                return {"ok": False, "reason": "balance"}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (price, uid))
            give_item(con, uid, item)
            return {"ok": True, "balance": bal - price}
        return await self.db._tx(fn)

    # ───────────── 가방에서 쓰는 아이템 ─────────────
    async def _use_charm(self, interaction: discord.Interaction, user):
        r = await self.db.use_charm(user.id, int(time.time()))
        if not r["ok"]:
            return await interaction.response.send_message("🙅 🍀 낚시 부적이 없어요.", ephemeral=True)
        await interaction.response.send_message(embed=ui.card(
            "🍀 낚시 부적 사용!", f"오늘 물고기 보내기 한도 **+{CHARM_BONUS}** → **{r['used']}/{r['limit']}**포인트",
            ui.GOLD, user, "🎒 아이템"))

    async def _use_pieces(self, interaction: discord.Interaction, user):
        r = await self.db.combine_pieces(user.id)
        if not r["ok"]:
            return await interaction.response.send_message(
                f"🧩 스킵권 조각이 {SKIP_PIECES}개 있어야 해요. (지금 {r['have']}개)", ephemeral=True)
        emoji, name, _ = ITEMS[r["item"]]
        await interaction.response.send_message(embed=ui.card(
            f"🧩 조각 {SKIP_PIECES}개 → {emoji} {name}!", "`/가방`에서 바로 쓸 수 있어요.", ui.GOLD, user, "🎒 아이템"))

    @app_commands.command(name="낚시시세", description="오늘의 물고기 시세 — 종마다 값 · 어제보다 ▲▼")
    async def market_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await self.market_embed(interaction.user))


async def setup(bot: commands.Bot):
    await bot.add_cog(Fishing(bot))
