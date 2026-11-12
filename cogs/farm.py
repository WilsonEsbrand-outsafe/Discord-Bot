# cogs/farm.py — /농사 (3.8.1, 테스트 서버 먼저)
# /농사 → 땅 고르기 → 그 채널에 내 농장 스레드(공개 · 조작은 주인만)가 열리고, 농장 화면의 버튼 · 메뉴로 논다.
# 다른 사람 농장 스레드에서는 [인수 제안]만 누를 수 있다 (명문구단처럼 제안 → 주인이 수락 · 적정 인수 금액 이상이면 바로).
# 농장은 여러 개 가질 수 있다 — 스레드 하나가 농장 하나 (farm_threads).
# 씨앗 · 동물은 서버마다 하나 있는 '농사 상점' 스레드에서 산다 (farm_shops) — 공개되면 모든 서버의 패치노트 채널에 생긴다.
# 농장 화면 · 상점의 버튼은 custom_id 가 고정(farm:…)이라 봇이 재시작해도 계속 눌린다. 규칙 · 수치는 services/farm.py.
import asyncio
import time
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import release
from cogs.patch_notes import PATCH_THREADS
from services import farm as F
from services import ui

SECTION = "농사"
ROW = 6   # 땅 그림 한 줄 칸 수
SHOP_NAME = "농사 상점"
STICKY_WAIT = 3     # 농장 스레드에 글이 올라오면 이만큼(초) 조용해진 뒤 밭을 맨 밑으로 다시 올린다
STICKY_SCAN = 50    # 지울 옛 밭 화면을 찾는 최근 글 수
SHOPS: dict[int, int] = {}   # 서버 → 농사 상점 스레드 (farm_shops 표를 읽어 둔 것)
SHOP_MSGS: dict[int, tuple[Optional[int], int]] = {}   # 서버 → (상점 글, 상점 화면 버전)


def _name(key: str) -> str:
    if key in F.TOOLS:
        return F.TOOLS[key][0]
    return (F.CROPS.get(key) or F.ANIMALS[key])[1]


def _shop_link(user) -> str:
    tid = SHOPS.get(getattr(getattr(user, "guild", None), "id", None))
    return f"<#{tid}>" if tid else f"**{SHOP_NAME}** 스레드"


def _dur(sec: float) -> str:
    s = max(1, round(sec))
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}시간" + (f" {m}분" if m else "")
    return f"{m}분" + (f" {s}초" if s else "")


def _item(key: str, tier: int = 1) -> str:
    return F.ITEMS[key][1] + (f" ★{tier}" if tier > 1 else "")


def _res_rows(res: dict) -> str:
    """땅의 세부 자원 — 한 줄에 하나 · 11/11 규완님: 별을 색 있는 별로 (채운 별만 노란 별)."""
    return "\n".join(f"{F.RES[k][1]} {'⭐' * res[k]}" for k in F.RES)


def land_embed(user, pick: Optional[str] = None, more: bool = False) -> discord.Embed:
    guide = "\n".join(f"{n}: {d}" for _, n, d in F.RES.values())
    head = "새 밭의 땅을 골라 주세요" if more else "농사를 시작할 땅을 골라 주세요"
    cost = f"\n새 밭을 열면 이 농장 코인에서 **{F.NEW_FARM_COST:,}**이 빠져요." if more else ""
    e = ui.card(head, f"별이 많을수록 좋아요.\n{guide}{cost}\n-# 땅은 한 번 고르면 바꿀 수 없어요.",
                ui.INFO, user, SECTION)
    for i, (k, (_, n, desc, res)) in enumerate(F.LANDS.items()):
        e.add_field(name=n + (" (선택)" if k == pick else ""), value=f"{desc}\n{_res_rows(res)}")
        if i % 2:   # 두 칸씩 (오른쪽 빈칸)
            e.add_field(name="\u200b", value="\u200b")
    return e


def _qty(key: str, n: int) -> str:
    return f"{n:,}마리" if key in F.ANIMALS else f"{n:,}개"


def _left(ts: int, now: int) -> str:
    """남은 시간 글자 (2분 30초) — 화면을 그릴 때 정해진다.
    11/11 '모바일에서 시간 움직이는 거' → 초마다 바뀌는 <t:…:R> 를 뺐고, 11/12 '바뀐 시간 표시 방식은 너무 구려' → 시각(<t:…:T>) 대신 남은 시간."""
    return _dur(max(1, int(ts) - int(now)))


def _grid_fields(e: discord.Embed, fields: list[tuple[str, str]]):
    """칸(field)을 한 줄에 세 개씩 — 마지막 줄은 빈칸으로 채워서 다음 묶음이 새 줄에서 시작하게."""
    for name, value in fields:
        e.add_field(name=name, value=value)
    for _ in range(-len(fields) % 3):
        e.add_field(name="\u200b", value="\u200b")


def panel_embed(user, f: dict, now: int, note: str = "") -> discord.Embed:
    """11/11 규완님 '가독성이 너무 별로야' — 값은 칸마다 하나, 목록은 한 줄에 하나 (· 로 이어 붙이지 않는다)."""
    _, ln, _, res = F.LANDS[f["land"]]
    cells, ready, growing, animals = [], Counter(), {}, {}
    for p in f["fields"]:
        if p is None:
            cells.append("🟫")
        elif p["kind"] == "crop":
            cells.append(F.CROPS[p["key"]][0] if p["ready"] else "🌱")
            if p["ready"]:
                ready[p["key"]] += 1
            else:
                n, ts = growing.get(p["key"], (0, p["ready_ts"]))
                growing[p["key"]] = (n + 1, min(ts, p["ready_ts"]))
        else:
            prod = F.ANIMALS[p["key"]][3]
            cells.append(F.PRODUCTS[prod][0] if p["ready"] else F.ANIMALS[p["key"]][0])
            if p["ready"]:
                ready[prod] += 1
            n, ts = animals.get(p["key"], (0, None))
            animals[p["key"]] = (n + 1, ts if p["ready"] else min(ts or p["ready_ts"], p["ready_ts"]))
    grid = "\n".join("".join(cells[i:i + ROW]) for i in range(0, len(cells), ROW))
    desc = (f"{note}\n\n" if note else "") + grid
    if not (ready or growing or animals or f["items"]):
        desc += "\n\n아래 메뉴에서 당근을 골라 심어 보세요. 당근은 씨앗이 필요 없어요."
    desc += f"\n-# 씨앗과 동물은 {_shop_link(user)}에서 사요"
    e = ui.card(f"{user.display_name}의 농장", desc, ui.WIN, user, SECTION)

    chests = sum(f["chests"].values())
    stats = [("에너지", f"**{f['energy']}** / {F.ENERGY_MAX}"), ("코인", f"**{f['coins']:,}**"), ("낫", f"**+{f['sickle']}**"),
             (ln, _res_rows(res))]
    if chests:
        stats.append(("상자", f"**{chests}**개"))
    if f["whet"]:
        stats.append(("숫돌", f"**{f['whet']}**개"))
    _grid_fields(e, stats)

    lists = []
    if ready:
        lists.append(("거둘 수 있어요", "\n".join(f"{_item(k)} {n}개" for k, n in ready.items())))
    if growing:
        lists.append(("자라는 중", "\n".join(f"{_item(k)} {n}개 ({_left(ts, now)} 남음)" for k, (n, ts) in growing.items())))
    if animals:
        lists.append((f"동물 ({f['animals']}/{f['animal_cap']})", "\n".join(
            f"{F.ANIMALS[k][1]} {n}마리" + (f" ({_left(ts, now)} 남음)" if ts else "") for k, (n, ts) in animals.items())))
    if f["items"]:
        worth = sum(F.value(k, t) * q for k, t, q in f["items"])
        shown = [f"{_item(k, t)} {q}개" for k, t, q in f["items"][:12]]
        if len(f["items"]) > 12:
            shown.append(f"그 밖에 {len(f['items']) - 12}가지")
        lists.append(("창고", "\n".join(shown) + f"\n-# 모두 팔면 {worth:,} 코인"))
    if f["seeds"]:
        lists.append(("사 둔 씨앗과 동물", "\n".join(f"{_name(k)} {_qty(k, q)}" for k, q in f["seeds"].items())))
    tools = _tool_rows(f, now)
    if tools:
        lists.append(("아이템", "\n".join(tools)))
    if lists:
        _grid_fields(e, lists)
    e.set_footer(text="농장 주인만 누를 수 있어요. 인수 제안은 누구나 할 수 있어요.\n심기, 거두기, 합치기는 한 번에 에너지 1을 써요.")
    return e


def _tool_rows(f: dict, now: int) -> list[str]:
    """농장 화면 · 아이템 화면: 가진 아이템 한 줄에 하나 + 세워 둔 허수아비."""
    rows = [f"{F.TOOLS[k][0]} {q:,}개" + (" (심을 때 안 써요)" if k == "fert" and not f["fert_on"] else "")
            for k, q in f["tools"].items()]
    if f["scare_until"] > now:
        rows.append(f"허수아비 세워 둠 ({_left(f['scare_until'], now)} 남음)")
    return rows


def help_embed(user) -> discord.Embed:
    """주제마다 칸 하나 (11/11 규완님: 모든 농사 화면 가독성)."""
    e = ui.card("농사 도움말", "작물과 동물을 길러 농장 코인을 벌어요.", ui.INFO, user, SECTION)
    for name, value in (
            ("심기", "메뉴에서 작물을 고르면 빈 땅에 심어요.\n시간이 지나면 다 자라요."),
            ("농사 상점", f"씨앗과 동물은 {_shop_link(user)}에서 농장 코인으로 사요.\n당근은 씨앗 없이 바로 심을 수 있어요."),
            ("동물", f"입양하면 땅 한 칸에 살면서 달걀, 양털, 우유 같은 것을 계속 만들어요.\n땅 {F.ANIMAL_PER_PLOTS}칸마다 1마리까지 기를 수 있어요."),
            ("거두기", "다 자란 작물과 동물이 만든 것을 창고로 옮겨요."),
            ("합치기", f"같은 물건 {F.MERGE_NEED}개를 합치면 한 등급 위 1개가 돼요.\n★{F.TIER_MAX}까지 올릴 수 있고, 등급이 높을수록 훨씬 비싸요."),
            ("창고", "물건을 농장 코인으로 팔아요."),
            ("아이템", "\n".join(f"**{n}**: {d}." for n, _, d in F.TOOLS.values()) + f"\n{_shop_link(user)}에서 사고 **아이템**에서 써요."),
            ("에너지", f"심기, 거두기, 합치기 한 번에 1을 써요.\n시간이 지나면 {F.ENERGY_MAX}까지 다시 차요."),
            ("땅 넓히기", "코인으로 한 칸씩 넓혀요.\n넓힐 때마다 은 상자를 하나 발견해요."),
            ("낫 강화", "강화할수록 거둘 때 하나 더 나오거나 ★2로 나올 확률이 올라요."),
            ("상자", "거두다 가끔 나오고, 하루 첫 거두기에 하나를 받아요.\n코인, 에너지, 숫돌, 높은 등급 물건이 들어 있어요."),
            ("환전", f"코인 1개는 축구 봇 돈 {F.RATE:,}원이에요.\n환전하지 않아도 농사는 코인만으로 다 할 수 있어요."),
            ("행운", "축구 봇 돈을 쓰면 상자 보상과 낫 강화 확률이 조금 좋아져요.\n안 써도 괜찮아요."),
            ("새 밭 만들기", f"농장 코인이 {F.NEW_FARM_COST:,}개 모이면 그 코인을 내고 새 밭을 하나 더 열 수 있어요."),
            ("인수", "다른 사람 농장 스레드에서 **인수 제안**을 눌러 내 농장 코인으로 금액을 제안해요.\n"
                    "적정 인수 금액 이상이면 바로 인수되고, 그보다 적으면 주인이 수락해야 해요.\n농장은 여러 개 가질 수 있어요.")):
        e.add_field(name=name, value=value, inline=False)
    return e


# ───────────── 농장 화면 (봇이 재시작해도 눌리는 버튼) ─────────────
class FarmPanel(discord.ui.View):
    def __init__(self, cog: "Farm", f: Optional[dict] = None):
        super().__init__(timeout=None)
        self.cog = cog
        have = f["seeds"] if f else {}
        opts = [discord.SelectOption(label=f"{n} 심기", value=k,
                                     description=f"사 둔 씨앗 {have.get(k, 0)}개" if price else "씨앗 없이 바로 심어요")
                for k, (e, n, price, sec, sell) in F.CROPS.items()]
        opts += [discord.SelectOption(label=f"{n} 입양", value=k, description=f"사 둔 {n} {have.get(k, 0)}마리")
                 for k, (e, n, price, prod, sec) in F.ANIMALS.items()]
        plant = discord.ui.Select(custom_id="farm:plant", placeholder="심을 작물이나 입양할 동물 고르기", row=0, options=opts)
        plant.callback = self._plant
        self.add_item(plant)
        n = f["plant_n"] if f else 0
        qty = discord.ui.Select(custom_id="farm:qty", placeholder="한 번에 심을 칸 수", row=1, options=[
            discord.SelectOption(label="한 번에 빈 땅 모두 심기" if v == 0 else f"한 번에 {v}칸씩 심기", value=str(v), default=v == n)
            for v in (0, 1, 2, 3, 6)])
        qty.callback = self._qty
        self.add_item(qty)
        chests = sum(f["chests"].values()) if f else 0
        cost = F.expand_cost(f["plots"]) if f and f["plots"] < F.PLOTS_MAX else None
        offers = f.get("offers", 0) if f else 0
        for cid, label, style, row, cb in (
                ("harvest", "거두기", discord.ButtonStyle.success, 2, self._harvest),
                ("merge", "합치기", discord.ButtonStyle.primary, 2, self._merge),
                ("store", "창고", discord.ButtonStyle.primary, 2, self._store),
                ("chest", f"상자 ({chests})" if chests else "상자", discord.ButtonStyle.primary, 2, self._chest),
                ("refresh", "새로고침", discord.ButtonStyle.secondary, 2, self._reload),
                ("expand", f"땅 넓히기 ({cost:,} 코인)" if cost else "땅 넓히기", discord.ButtonStyle.secondary, 3, self._expand),
                ("sickle", "낫 강화", discord.ButtonStyle.secondary, 3, self._sickle),
                ("items", "아이템", discord.ButtonStyle.secondary, 3, self._items),
                ("exchange", "환전", discord.ButtonStyle.secondary, 3, self._exchange),
                ("help", "도움말", discord.ButtonStyle.secondary, 3, self._help),
                ("offer", "인수 제안", discord.ButtonStyle.secondary, 4, self._offer),
                ("offers", f"받은 인수 제안 ({offers})" if offers else "받은 인수 제안", discord.ButtonStyle.secondary, 4,
                 self._offers),
                ("newfarm", "새 밭 만들기", discord.ButtonStyle.secondary, 4, self._newfarm)):
            b = discord.ui.Button(custom_id=f"farm:{cid}", label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not release.preview_381(interaction.guild_id):   # 공개 전엔 테스트 서버의 농장만
            await interaction.response.send_message("/농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
            return False
        owner = await self.cog.db.thread_owner(interaction.channel_id)
        if (interaction.data or {}).get("custom_id") == "farm:offer":   # 인수 제안은 주인 말고 누구나
            if owner is None:
                await interaction.response.send_message("이 스레드에는 농장이 없어요.", ephemeral=True)
                return False
            if owner == interaction.user.id:
                await interaction.response.send_message("내 농장이에요. 들어온 제안은 **받은 인수 제안**에서 볼 수 있어요.", ephemeral=True)
                return False
            return True
        if owner != interaction.user.id:
            await interaction.response.send_message("농장 주인만 누를 수 있어요. `/농사`로 내 농장을 열어 보세요.", ephemeral=True)
            return False
        return True

    async def _fid(self, interaction: discord.Interaction) -> int:
        """누른 스레드의 농장 번호."""
        return await self.cog.db.thread_farm(interaction.channel_id) or 0

    async def _plant(self, interaction: discord.Interaction):
        key, fid, now = interaction.data["values"][0], await self._fid(interaction), int(time.time())
        r = await self.cog.db.plant(interaction.user.id, fid, key, now)
        if not r["ok"]:
            what = f"사 둔 {_name(key)}이(가)" if key in F.ANIMALS else f"{_name(key)} 씨앗이"
            note = {"full": "빈 땅이 없어요. 다 자란 것을 거두거나 땅을 넓혀 보세요.",
                    "energy": "에너지가 없어요. 조금 기다리면 다시 차요.",
                    "seeds": f"{what} 없어요. {_shop_link(interaction.user)}에서 사 주세요. 당근은 씨앗 없이 심을 수 있어요.",
                    "cap": f"동물은 땅 {F.ANIMAL_PER_PLOTS}칸당 1마리까지예요 (지금 {r.get('cap', 0)}마리까지). 땅을 넓혀 보세요.",
                    }.get(r["reason"], "심을 수 없어요.")
        elif r["animal"]:
            _, n, _, prod, _ = F.ANIMALS[key]
            note = f"**{n}**을(를) 입양했어요.\n첫 {_item(prod)}은(는) {_left(r['ready_ts'], now)} 뒤에 나와요."
        elif r.get("fert"):
            rest = r["count"] - r["fert"]
            note = (f"{_item(key)} **{r['count']}칸**을 심었어요.\n"
                    f"비료를 {r['fert']}개 써서 {r['fert']}칸은 {_left(r['fert_ready'], now)} 뒤에 다 자라요."
                    + (f"\n나머지 {rest}칸은 {_left(r['ready_ts'], now)} 뒤에 다 자라요." if rest else "")
                    + (f"\n남은 씨앗 {r['left']}개" if r["left"] is not None else "") + f"\n남은 비료 {r['fert_left']}개")
        else:
            note = (f"{_item(key)} **{r['count']}칸**을 심었어요.\n{_left(r['ready_ts'], now)} 뒤에 다 자라요."
                    + (f"\n남은 씨앗 {r['left']}개" if r["left"] is not None else ""))
        await self.cog.show(interaction, fid, note)

    async def _qty(self, interaction: discord.Interaction):
        fid = await self._fid(interaction)
        await self.cog.db.set_plant_n(interaction.user.id, fid, int(interaction.data["values"][0]))
        await self.cog.show(interaction, fid)

    async def _harvest(self, interaction: discord.Interaction):
        fid = await self._fid(interaction)
        r = await self.cog.db.harvest(interaction.user.id, fid, int(time.time()))
        if not r["ok"]:
            note = "에너지가 없어요. 조금 기다리면 다시 차요." if r["reason"] == "energy" else "아직 거둘 것이 없어요."
        else:
            note = f"**{r['count']}칸**을 거뒀어요.\n" + "\n".join(f"{_item(k, t)} {q}개" for (k, t), q in r["got"].items())
            if r["extra"]:
                note += f"\n낫 덕분에 하나 더: {r['extra']}개"
            if r["high"]:
                note += f"\n{'낫과 허수아비' if r.get('scare') else '낫'} 덕분에 ★2: {r['high']}개"
            for c, n in Counter(r["chests"]).items():
                note += f"\n{F.CHESTS[c][1]} {n}개를 발견했어요"
            if r["daily"]:
                c = F.CHESTS[F.DAILY_CHEST]
                note += f"\n오늘 첫 수확 선물: {c[1]}"
            if r["left"]:
                note += f"\n에너지가 모자라서 {r['left']}칸은 남았어요."
        await self.cog.show(interaction, fid, note)

    async def _reload(self, interaction: discord.Interaction):  # View._refresh 는 discord.py 내부용이라 이름을 피한다
        await self.cog.show(interaction, await self._fid(interaction))

    async def _expand(self, interaction: discord.Interaction):
        fid = await self._fid(interaction)
        r = await self.cog.db.expand(interaction.user.id, fid, int(time.time()))
        if r["ok"]:
            c = F.CHESTS[r["chest"]]
            note = f"땅이 **{r['plots']}칸**으로 넓어졌어요 (코인 -{r['cost']:,}).\n{c[1]}를 발견했어요."
        else:
            note = (f"땅은 {F.PLOTS_MAX}칸이 끝이에요." if r["reason"] == "max"
                    else f"코인이 모자라요 (땅 넓히기 {r.get('cost', 0):,} 코인).")
        await self.cog.show(interaction, fid, note)

    async def _sub(self, interaction: discord.Interaction, cls):
        f = await self.cog.db.get(await self._fid(interaction), int(time.time()))
        view = cls(self.cog, interaction.user, interaction.message, f)
        await interaction.response.send_message(embed=view.embed(f), view=view, ephemeral=True)

    async def _merge(self, interaction: discord.Interaction):
        await self._sub(interaction, MergeView)

    async def _store(self, interaction: discord.Interaction):
        await self._sub(interaction, StoreView)

    async def _chest(self, interaction: discord.Interaction):
        await self._sub(interaction, ChestView)

    async def _sickle(self, interaction: discord.Interaction):
        await self._sub(interaction, SickleView)

    async def _items(self, interaction: discord.Interaction):
        await self._sub(interaction, ItemView)

    async def _newfarm(self, interaction: discord.Interaction):
        """이 농장 코인이 NEW_FARM_COST 이상이면 땅을 골라 새 밭(농장)을 하나 더 연다."""
        f = await self.cog.db.get(await self._fid(interaction), int(time.time()))
        if f["coins"] < F.NEW_FARM_COST:
            return await interaction.response.send_message(
                f"이 농장 코인이 **{F.NEW_FARM_COST:,}**개 모이면 새 밭을 하나 더 만들 수 있어요.\n지금 코인 **{f['coins']:,}**",
                ephemeral=True)
        view = LandPick(self.cog, interaction.user, f["id"], interaction.message)
        await interaction.response.send_message(embed=land_embed(interaction.user, None, True), view=view, ephemeral=True)

    async def _exchange(self, interaction: discord.Interaction):
        f = await self.cog.db.get(await self._fid(interaction), int(time.time()))
        await interaction.response.send_modal(ExchangeModal(self.cog, f["id"], f["coins"]))

    async def _help(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=help_embed(interaction.user), ephemeral=True)

    async def _offer(self, interaction: discord.Interaction):
        now = int(time.time())
        f = await self.cog.db.get(await self._fid(interaction), now)
        mine = await self.cog.db.farms_of(interaction.user.id, now)
        if not mine:   # 인수 금액은 내 농장 코인으로 낸다
            return await interaction.response.send_message(
                "인수 금액은 내 농장 코인으로 내요. 먼저 `/농사`로 내 농장을 열어 주세요.", ephemeral=True)
        view = OfferStart(self.cog, interaction.user, f, interaction.message, mine)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def _offers(self, interaction: discord.Interaction):
        now = int(time.time())
        f = await self.cog.db.get(await self._fid(interaction), now)
        offers = await self.cog.db.farm_offers(interaction.user.id, f["id"], now)
        view = OwnerOffers(self.cog, interaction.user, f, offers, interaction.message)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


# ───────────── 작은 화면들 (누른 사람에게만 보인다 — 끝나면 농장 화면도 고친다) ─────────────
class _Sub(discord.ui.View):
    def __init__(self, cog: "Farm", user, panel, f: dict):
        super().__init__(timeout=180)
        self.cog, self.user, self.panel, self.note, self.win = cog, user, panel, "", False
        self.fid = f["id"]
        self.build(f)

    def build(self, f: dict):
        raise NotImplementedError

    def embed(self, f: dict) -> discord.Embed:
        raise NotImplementedError

    async def redraw(self, interaction: discord.Interaction, note: str = "", panel_note: str = "", win: bool = False):
        """이 화면과 농장 화면을 지금 상태로 다시 그린다."""
        now = int(time.time())
        f = await self.cog.db.get(self.fid, now)
        self.note, self.win = note, win
        self.clear_items()
        self.build(f)
        await interaction.response.edit_message(embed=self.embed(f), view=self)
        try:
            await self.panel.edit(embed=panel_embed(self.user, f, now, panel_note), view=FarmPanel(self.cog, f))
        except discord.HTTPException:
            pass


class MergeView(_Sub):
    def build(self, f: dict):
        able = [(k, t, q) for k, t, q in f["items"] if q >= F.MERGE_NEED and t < F.TIER_MAX][:25]
        if able:
            sel = discord.ui.Select(placeholder="합칠 물건 고르기", options=[
                discord.SelectOption(label=f"{F.ITEMS[k][1]} ★{t} {q}개를 ★{t + 1} {q // F.MERGE_NEED}개로", value=f"{k}:{t}",
                                     description=f"★{t + 1} 하나에 {F.value(k, t + 1):,} 코인")
                for k, t, q in able])
            sel.callback = self._pick
            self.add_item(sel)

    def embed(self, f: dict) -> discord.Embed:
        able = [(k, t, q) for k, t, q in f["items"] if q >= F.MERGE_NEED and t < F.TIER_MAX]
        body = ("\n".join(f"{_item(k, t)} {q}개" for k, t, q in able) if able
                else f"지금은 합칠 수 있는 것이 없어요. 같은 물건, 같은 등급이 {F.MERGE_NEED}개 있어야 해요.")
        e = ui.card("합치기", (f"{self.note}\n\n" if self.note else "")
                    + f"같은 물건 {F.MERGE_NEED}개를 합치면 한 등급 위 1개가 돼요.\n"
                    f"한 번에 에너지 1을 쓰고, ★{F.TIER_MAX}까지 올릴 수 있어요.", ui.INFO, self.user, SECTION)
        e.add_field(name="합칠 수 있는 것", value=body)
        e.add_field(name="에너지", value=f"**{f['energy']}** / {F.ENERGY_MAX}")
        return e

    async def _pick(self, interaction: discord.Interaction):
        k, t = interaction.data["values"][0].split(":")
        r = await self.cog.db.merge(self.user.id, self.fid, k, int(t), int(time.time()))
        note = (f"{_item(k, int(t))} {r['used']}개를 합쳐서 **{_item(k, r['tier'])} {r['made']}개**가 됐어요." if r["ok"] else
                {"energy": "에너지가 없어요.", "few": f"{F.MERGE_NEED}개가 있어야 합칠 수 있어요."}.get(r["reason"], "합칠 수 없어요."))
        await self.redraw(interaction, note, note if r["ok"] else "")


class SellModal(discord.ui.Modal):
    """11/12 규완님 '팔 개수는 본인이 입력하게': 창고에서 물건을 고르면 팔 개수를 직접 적는다."""

    def __init__(self, view: "StoreView", key: str, tier: int, have: int):
        super().__init__(title=f"{F.ITEMS[key][1]} ★{tier} 팔기"[:45])
        self.view, self.key, self.tier = view, key, tier
        self.count = discord.ui.TextInput(label=f"팔 개수 (가진 것 {have:,}개)"[:45], max_length=9,
                                          placeholder="'전부'라고 적으면 모두 팔아요")
        self.add_item(self.count)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.count.value).replace(",", "").replace("개", "").strip()
        if raw in ("전부", "모두", "all"):
            n = None
        elif raw.isdigit() and int(raw) > 0:
            n = int(raw)
        else:
            return await interaction.response.send_message("팔 개수는 1 이상의 숫자로 적어 주세요.", ephemeral=True)
        v = self.view
        await v._done(interaction, await v.cog.db.sell(v.user.id, v.fid, int(time.time()), [(self.key, self.tier)], n))


class StoreView(_Sub):
    def build(self, f: dict):
        items = f["items"][:25]
        if items:
            sel = discord.ui.Select(placeholder="팔 물건 고르기 (고르면 팔 개수를 적어요)", row=0, options=[
                discord.SelectOption(label=f"{F.ITEMS[k][1]} ★{t} {q}개", value=f"{k}:{t}:{q}",
                                     description=f"하나에 {F.value(k, t):,} 코인") for k, t, q in items])
            sel.callback = self._sell
            self.add_item(sel)
            b = discord.ui.Button(label="창고 모두 팔기", style=discord.ButtonStyle.success, row=2)
            b.callback = self._sell_all
            self.add_item(b)
        pets = [(i, p) for i, p in enumerate(f["fields"]) if p and p["kind"] == "animal"][:25]
        if pets:
            sel = discord.ui.Select(placeholder="내보낼 동물 고르기 (산 값의 절반을 돌려받아요)", row=1, options=[
                discord.SelectOption(label=f"{i + 1}번 땅 {F.ANIMALS[p['key']][1]}", value=str(i),
                                     description=f"돌려받는 코인 {int(F.ANIMALS[p['key']][2] * F.ANIMAL_REFUND):,}") for i, p in pets])
            sel.callback = self._release
            self.add_item(sel)

    def embed(self, f: dict) -> discord.Embed:
        rows = [f"{_item(k, t)} {q}개 (하나에 {F.value(k, t):,} 코인)" for k, t, q in f["items"]]
        worth = sum(F.value(k, t) * q for k, t, q in f["items"])
        e = ui.card("창고와 판매", (f"{self.note}\n\n" if self.note else "") + ("\n".join(rows) if rows else "창고가 비어 있어요.")
                    + f"\n-# {F.MERGE_NEED}개를 모아 합치면 더 비싸게 팔 수 있어요.", ui.INFO, self.user, SECTION)
        if rows:
            e.add_field(name="모두 팔면", value=f"**{worth:,}** 코인")
        e.add_field(name="가진 코인", value=f"**{f['coins']:,}**")
        return e

    async def _done(self, interaction: discord.Interaction, r: dict):
        note = (f"**+{r['coins']:,} 코인**을 받았어요.\n" + "\n".join(f"{_item(k, t)} {q}개를 팔았어요" for k, t, q in r["sold"]) if r["ok"]
                else "팔 물건이 없어요.")
        await self.redraw(interaction, note, note if r["ok"] else "")

    async def _sell(self, interaction: discord.Interaction):
        k, t, q = interaction.data["values"][0].split(":")
        await interaction.response.send_modal(SellModal(self, k, int(t), int(q)))

    async def _sell_all(self, interaction: discord.Interaction):
        await self._done(interaction, await self.cog.db.sell(self.user.id, self.fid, int(time.time())))

    async def _release(self, interaction: discord.Interaction):
        r = await self.cog.db.sell_animal(self.user.id, self.fid, int(interaction.data["values"][0]), int(time.time()))
        note = (f"{F.ANIMALS[r['key']][1]}을(를) 내보냈어요 (코인 +{r['coins']:,})" if r["ok"]
                else "그 땅에는 동물이 없어요.")
        await self.redraw(interaction, note, note if r["ok"] else "")


class ItemView(_Sub):
    """11/12 농사 상점 아이템 쓰기 — 비료는 켜 두면 심을 때 쓰고, 나머지는 눌러서 하나씩 쓴다."""

    def build(self, f: dict):
        have = f["tools"]
        b = discord.ui.Button(label="심을 때 비료 쓰기: " + ("켜짐" if f["fert_on"] else "꺼짐"),
                              style=discord.ButtonStyle.success if f["fert_on"] else discord.ButtonStyle.secondary, row=0)
        b.callback = self._fert
        self.add_item(b)
        for key, label in (("drink", "에너지 음료 마시기"), ("feed", "동물 사료 주기"), ("scarecrow", "허수아비 세우기")):
            b = discord.ui.Button(label=f"{label} ({have.get(key, 0)})", style=discord.ButtonStyle.primary,
                                  disabled=not have.get(key), row=1)
            b.callback = self._use(key)
            self.add_item(b)

    def embed(self, f: dict) -> discord.Embed:
        now = int(time.time())
        desc = "\n".join(f"**{n}**: {d}." for n, _, d in F.TOOLS.values())
        e = ui.card("아이템", (f"{self.note}\n\n" if self.note else "") + desc
                    + f"\n-# 아이템은 {_shop_link(self.user)}에서 사요.", ui.INFO, self.user, SECTION)
        _grid_fields(e, [(n, f"**{f['tools'].get(k, 0):,}**개") for k, (n, _, _) in F.TOOLS.items()]
                     + [("에너지", f"**{f['energy']}** / {F.ENERGY_MAX}"),
                        ("허수아비", f"{_left(f['scare_until'], now)} 남음" if f["scare_until"] > now else "세워 둔 것 없음")])
        return e

    async def _fert(self, interaction: discord.Interaction):
        f = await self.cog.db.get(self.fid, int(time.time()))
        on = not f["fert_on"]
        await self.cog.db.set_fert(self.user.id, self.fid, on, int(time.time()))
        await self.redraw(interaction, "이제 심을 때 비료를 써요." if on else "이제 심을 때 비료를 쓰지 않아요.")

    def _use(self, key: str):
        async def cb(interaction: discord.Interaction):
            now = int(time.time())
            r = await self.cog.db.use_tool(self.user.id, self.fid, key, now)
            if r["ok"]:
                note = {"drink": lambda: f"에너지 음료를 마셨어요.\n에너지 +{r.get('gained', 0)} (지금 {r.get('energy', 0)})",
                        "feed": lambda: f"동물 {r.get('count', 0)}마리에게 사료를 줬어요.\n다음 생산품이 절반 시간에 나와요.",
                        "scarecrow": lambda: f"허수아비를 세웠어요.\n{_left(r.get('until', now), now)} 동안 ★2가 더 잘 나와요."}[key]()
            else:
                note = {"have": f"{_name(key)}이(가) 없어요. {_shop_link(self.user)}에서 사 주세요.",
                        "energy": "에너지가 이미 가득 찼어요.",
                        "animals": "기다리는 동물이 없어요."}.get(r["reason"], "쓸 수 없어요.")
            await self.redraw(interaction, note, note if r["ok"] else "")
        return cb


class ChestView(_Sub):
    lucky = False

    def build(self, f: dict):
        for tier, (_, n) in F.CHESTS.items():
            have = f["chests"][tier]
            label = f"{n} 열기 ({have})" + (f", {F.LUCKY_FEE[tier]:,}원" if self.lucky else "")
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.success, disabled=have <= 0, row=0)
            b.callback = self._open(tier)
            self.add_item(b)
        t = discord.ui.Button(label="행운 열기: 켜짐" if self.lucky else "행운 열기: 꺼짐", row=1,
                              style=discord.ButtonStyle.primary if self.lucky else discord.ButtonStyle.secondary)
        t.callback = self._toggle
        self.add_item(t)

    def embed(self, f: dict) -> discord.Embed:
        e = ui.card("상자", (f"{self.note}\n\n" if self.note else "")
                    + "상자에는 코인, 에너지, 숫돌, 높은 등급 물건이 들어 있어요. 좋은 상자일수록 보상이 많아요.\n"
                    "**행운 열기**: 축구 봇 돈을 내면 보상마다 두 번 뽑아 더 좋은 쪽을 받아요.",
                    ui.GOLD if self.win else ui.INFO, self.user, SECTION)
        _grid_fields(e, [(n, f"**{f['chests'][k]}**개\n-# 행운 열기 {F.LUCKY_FEE[k]:,}원") for k, (_, n) in F.CHESTS.items()])
        e.add_field(name="내 잔액", value=f"{f['balance']:,}원")
        return e

    def _open(self, tier: str):
        async def cb(interaction: discord.Interaction):
            r = await self.cog.db.open_chest(self.user.id, self.fid, tier, int(time.time()), lucky=self.lucky)
            if not r["ok"]:
                note = (f"축구 봇 돈이 모자라요 (행운 열기 {r.get('fee', 0):,}원). 행운 열기를 끄면 그냥 열 수 있어요."
                        if r["reason"] == "money" else "그 상자가 없어요.")
                return await self.redraw(interaction, note)
            got = []
            for x in r["rewards"]:
                got.append({"coins": f"코인 +{x['n']:,}", "energy": f"에너지 +{x['n']}", "whet": f"숫돌 +{x['n']}"}.get(x["kind"])
                           or f"{_item(x['key'], x['tier'])} {x['n']}개")
            n = F.CHESTS[tier][1]
            note = f"**{n}**를 열었어요" + (f" (행운 -{r['fee']:,}원)" if r["fee"] else "") + ".\n" + "\n".join(f"**{g}**" for g in got)
            await self.redraw(interaction, note, note, win=True)
        return cb

    async def _toggle(self, interaction: discord.Interaction):
        self.lucky = not self.lucky
        await self.redraw(interaction)


class SickleView(_Sub):
    charm = False

    def build(self, f: dict):
        lv = f["sickle"]
        done = lv >= F.SICKLE_MAX
        for label, whet in ((f"코인으로 강화 ({F.sickle_cost(lv):,})" if not done else "코인으로 강화", False),
                            (f"숫돌로 강화 ({F.sickle_whet(lv)}개)" if not done else "숫돌로 강화", True)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.success, disabled=done, row=0)
            b.callback = self._go(whet)
            self.add_item(b)
        fee = "" if done else f", {F.CHARM_FEE * (lv + 1):,}원"
        t = discord.ui.Button(label=("행운 부적: 켜짐" if self.charm else "행운 부적: 꺼짐") + (fee if self.charm else ""),
                              style=discord.ButtonStyle.primary if self.charm else discord.ButtonStyle.secondary, disabled=done, row=1)
        t.callback = self._toggle
        self.add_item(t)

    def embed(self, f: dict) -> discord.Embed:
        lv = f["sickle"]
        def luck(x):
            return f"하나 더 **{F.extra_p(x):.0%}**\n★2 로 **{F.high_p(x):.0%}**"
        if lv >= F.SICKLE_MAX:
            body, fields = f"낫 **+{lv}** (최고 단계)", [("지금 거둘 때", luck(lv))]
        else:
            p = min(1.0, F.SICKLE_CHANCE[lv] + (F.CHARM_BONUS if self.charm else 0))
            body = (f"낫 **+{lv}**을 **+{lv + 1}**로 강화해요. 실패해도 단계는 내려가지 않아요.\n"
                    f"**행운 부적**: 축구 봇 돈 {F.CHARM_FEE * (lv + 1):,}원을 내면 성공 확률 +{F.CHARM_BONUS:.0%}p")
            fields = [("지금 거둘 때", luck(lv)), ("강화하면", luck(lv + 1)),
                      ("성공 확률", f"**{p:.0%}**" + ("\n-# 부적 포함" if self.charm else "")),
                      ("재료", f"{F.sickle_cost(lv):,} 코인\n또는 숫돌 {F.sickle_whet(lv)}개")]
        e = ui.card("낫 강화", (f"{self.note}\n\n" if self.note else "") + body, ui.GOLD if self.win else ui.INFO, self.user, SECTION)
        _grid_fields(e, fields + [("코인", f"**{f['coins']:,}**"), ("숫돌", f"**{f['whet']}**개"), ("내 잔액", f"{f['balance']:,}원")])
        return e

    def _go(self, whet: bool):
        async def cb(interaction: discord.Interaction):
            r = await self.cog.db.enhance(self.user.id, self.fid, int(time.time()), use_whet=whet, charm=self.charm)
            if not r["ok"]:
                note = {"coins": f"코인이 모자라요 ({r.get('cost', 0):,} 코인).",
                        "whet": f"숫돌이 모자라요 ({r.get('need', 0)}개). 숫돌은 상자에서 나와요.",
                        "money": f"축구 봇 돈이 모자라요 (행운 부적 {r.get('fee', 0):,}원). 부적을 끄면 그냥 강화할 수 있어요.",
                        "max": "이미 최고 단계예요."}.get(r["reason"], "강화할 수 없어요.")
                return await self.redraw(interaction, note)
            note = (f"강화 성공! 낫이 **+{r['level']}**이 됐어요." if r["success"]
                    else f"강화 실패… 낫은 **+{r['level']}** 그대로예요.")
            await self.redraw(interaction, note, note if r["success"] else "", win=r["success"])
        return cb

    async def _toggle(self, interaction: discord.Interaction):
        self.charm = not self.charm
        await self.redraw(interaction)


class ExchangeModal(discord.ui.Modal):
    def __init__(self, cog: "Farm", fid: int, coins: int):
        super().__init__(title="농장 코인 환전")
        self.cog, self.fid = cog, fid
        self.amount = discord.ui.TextInput(label=f"바꿀 코인 (코인 1 = {F.RATE:,}원)"[:45], max_length=12,
                                           placeholder=f"가진 코인 {coins:,}, '전부'라고 적으면 모두 바꿔요")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(",", "").replace("코인", "").strip()
        now = int(time.time())
        if raw in ("전부", "모두", "all"):
            raw = str((await self.cog.db.get(self.fid, now))["coins"])
        if not raw.isdigit():
            return await interaction.response.send_message("바꿀 코인은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.cog.db.exchange(interaction.user.id, self.fid, int(raw), now)
        if not r["ok"] and r["reason"] == "none":   # 그 사이 농장 주인이 바뀌었다
            return await interaction.response.send_message("내 농장이 아니에요.", ephemeral=True)
        note = (f"{r['coins']:,} 코인을 **{r['won']:,}원**으로 바꿨어요.\n잔액 {r['balance']:,}원" if r["ok"] else
                f"코인이 모자라요 (가진 코인 {r.get('have', 0):,})." if r["reason"] == "coins" else "1 코인 이상 적어 주세요.")
        await self.cog.show(interaction, self.fid, note)


# ───────────── 인수 (명문구단처럼: 제안 → 주인이 수락 · 적정 인수 금액 이상이면 바로) ─────────────
def _farm_line(f: dict) -> str:
    return f"**{F.LANDS[f['land']][1]}** 농장"


def _num(raw) -> Optional[int]:
    raw = str(raw).replace(",", "").replace("코인", "").replace("원", "").strip()
    return int(raw) if raw.isdigit() else None


_OFFER_FAIL = {"mine": "이미 내 농장이에요.", "amount": "제안 금액은 1 코인 이상이어야 해요.",
               "gone": "없어졌거나 만료된 제안이에요."}


def _offer_fail(r: dict) -> str:
    if r["reason"] == "coins":
        return f"낼 농장의 코인이 모자라요.\n필요한 코인 **{r.get('cost', 0):,}**\n가진 코인 **{r.get('have', 0):,}**"
    if r["reason"] == "pay":
        return "인수 금액은 내 농장의 코인으로 내야 해요."
    return _OFFER_FAIL.get(r["reason"], "인수할 수 없어요.")


def _got_line(r: dict) -> str:
    """판 사람이 받은 인수 대금(코인)이 어디로 갔는지."""
    if r.get("to_fid"):
        return f"받은 코인은 내 {F.LANDS[r['to_land']][1]} 농장에 들어갔어요.\n그 농장 코인 **{r['to_coins']:,}**"
    return ("남은 농장이 없어서 받은 코인을 맡아 뒀어요.\n`/농사`로 새 농장을 열면 그 농장에 들어가요.\n"
            f"맡아 둔 코인 **{r['to_coins']:,}**")


class OfferModal(discord.ui.Modal):
    """인수 금액 제안 (농장 코인) — 적정 인수 금액 이상이면 바로 인수."""

    def __init__(self, cog: "Farm", user, f: dict, panel, pay_fid: int):
        super().__init__(title="농장 인수 제안")
        self.cog, self.user, self.f, self.panel, self.pay_fid = cog, user, f, panel, pay_fid
        self.amount = discord.ui.TextInput(label="제안 금액 (코인)", max_length=15,
                                           placeholder=f"적정 인수 금액 {f['fair']:,} 코인 이상이면 바로 인수")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        amount = _num(self.amount.value)
        if amount is None:
            return await interaction.response.send_message("제안 금액은 숫자로 적어 주세요.", ephemeral=True)
        await self.cog.make_offer(interaction, self.user, self.f["id"], amount, self.panel, self.pay_fid)


class OfferStart(discord.ui.View):
    """다른 사람 농장: 적정 인수 금액으로 바로 인수하거나, 금액을 제안한다 (누른 사람에게만 보인다).
    11/11: 금액은 농장 코인 — 내 농장 중 하나의 코인으로 낸다 (여러 개면 고른다)."""

    def __init__(self, cog: "Farm", user, f: dict, panel, mine: list[dict]):
        super().__init__(timeout=180)
        self.cog, self.user, self.f, self.panel, self.mine = cog, user, f, panel, mine
        self.pay = max(mine, key=lambda x: x["coins"])["id"]   # 처음엔 코인이 가장 많은 농장
        self._draw()

    @property
    def pay_farm(self) -> dict:
        return next(x for x in self.mine if x["id"] == self.pay)

    def _draw(self):
        self.clear_items()
        now = discord.ui.Button(label=f"{self.f['fair']:,} 코인에 바로 인수"[:80], style=discord.ButtonStyle.success, row=0)
        now.callback = self._now
        self.add_item(now)
        propose = discord.ui.Button(label="금액 제안", style=discord.ButtonStyle.primary, row=0)
        propose.callback = self._propose
        self.add_item(propose)
        if len(self.mine) > 1:
            sel = discord.ui.Select(placeholder="어느 농장의 코인으로 낼지 고르기", row=1, options=[
                discord.SelectOption(label=f"{F.LANDS[x['land']][1]} 농장"[:100], description=f"코인 {x['coins']:,}",
                                     value=str(x["id"]), default=x["id"] == self.pay) for x in self.mine[:25]])
            sel.callback = self._pick
            self.add_item(sel)

    def embed(self) -> discord.Embed:
        f, p = self.f, self.pay_farm
        e = ui.card("농장 인수 제안", (
            f"<@{f['owner']}>님의 {_farm_line(f)}을 인수해요.\n"
            "적정 인수 금액 이상이면 바로 인수돼요. 그보다 적은 금액은 주인이 수락해야 인수돼요.\n"
            "인수 금액은 내 농장 코인으로 내고, 판 사람도 같은 코인을 받아요.\n"
            "-# 인수하면 코인, 창고, 밭, 동물, 낫, 상자가 모두 넘어와요."), ui.INFO, self.user, SECTION)
        _grid_fields(e, [("땅", f"**{f['plots']}**칸"), ("코인", f"**{f['coins']:,}**"), ("낫", f"**+{f['sickle']}**")]
                     + ([("동물", f"**{f['animals']}**마리")] if f["animals"] else [])
                     + [("적정 인수 금액", f"**{f['fair']:,}** 코인"),
                        (f"내 {F.LANDS[p['land']][1]} 농장 코인", f"**{p['coins']:,}**")])
        return e

    async def _pick(self, interaction: discord.Interaction):
        self.pay = int(interaction.data["values"][0])
        self._draw()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _now(self, interaction: discord.Interaction):
        self.stop()
        await self.cog.make_offer(interaction, self.user, self.f["id"], self.f["fair"], self.panel, self.pay, edit=True)

    async def _propose(self, interaction: discord.Interaction):
        await interaction.response.send_modal(OfferModal(self.cog, self.user, self.f, self.panel, self.pay))


class AskModal(discord.ui.Modal):
    """주인이 적정 인수 금액을 정한다 — 이 금액 이상 제안이 오면 바로 인수된다."""

    def __init__(self, cog: "Farm", user, f: dict):
        super().__init__(title="적정 인수 금액 정하기")
        self.cog, self.user, self.fid = cog, user, f["id"]
        lo, hi = F.farm_ask_range(f["worth"])
        self.amount = discord.ui.TextInput(label="적정 인수 금액 (코인)", default=str(f["fair"]), max_length=15,
                                           placeholder=f"{lo:,} ~ {hi:,}"[:100])
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        amount = _num(self.amount.value)
        if amount is None:
            return await interaction.response.send_message("금액은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.cog.db.set_farm_ask(self.user.id, self.fid, amount, int(time.time()))
        if r["ok"]:
            e = ui.card("적정 인수 금액", f"**{r['ask']:,} 코인**으로 정했어요.\n"
                        "이 금액 이상을 제안하면 바로 인수되고, 그보다 적은 제안은 수락하거나 거절할 수 있어요.", ui.WIN, self.user, SECTION)
        else:
            e = ui.card("적정 인수 금액", "내 농장이 아니에요." if r["reason"] == "none" else
                        f"**{r['lo']:,} 코인**부터 **{r['hi']:,} 코인**까지 정할 수 있어요.", ui.LOSE, self.user, SECTION)
        await interaction.response.send_message(embed=e, ephemeral=True)


class OwnerOffers(discord.ui.View):
    """내 농장에 들어온 인수 제안 — 골라서 [수락] · [거절], 적정 인수 금액 정하기 (주인에게만 보인다)."""

    def __init__(self, cog: "Farm", user, f: dict, offers: list[dict], panel):
        super().__init__(timeout=300)
        self.cog, self.user, self.f, self.offers, self.panel = cog, user, f, offers, panel
        self.pick: Optional[int] = offers[0]["id"] if offers else None
        if offers:
            sel = discord.ui.Select(placeholder="받은 인수 제안 고르기", row=0, options=[
                discord.SelectOption(label=f"{o['amount']:,} 코인 — {cog.name_of(user, o['buyer_id'])}"[:100], value=str(o["id"]),
                                     default=o["id"] == self.pick) for o in offers[:25]])
            sel.callback = self._picked
            self.add_item(sel)
            for label, style, ok in (("수락", discord.ButtonStyle.success, True), ("거절", discord.ButtonStyle.danger, False)):
                b = discord.ui.Button(label=label, style=style, row=1)
                b.callback = self._answer(ok)
                self.add_item(b)
        ask = discord.ui.Button(label="적정 인수 금액 정하기", style=discord.ButtonStyle.primary, row=2)
        ask.callback = self._ask
        self.add_item(ask)

    def embed(self) -> discord.Embed:
        f = self.f
        lo, hi = F.farm_ask_range(f["worth"])
        rows = "\n".join(f"<@{o['buyer_id']}> **{o['amount']:,} 코인** (<t:{o['ts'] + F.FARM_OFFER_HOURS * 3600}:R> 만료)"
                         for o in self.offers[:25]) or "받은 인수 제안이 없어요."
        e = ui.card("받은 인수 제안", (
            f"{rows}\n\n적정 인수 금액 이상 제안이 오면 바로 인수돼요.\n"
            "수락하면 농장이 통째로 넘어가고, 제안 금액을 코인으로 받아요.\n"
            "-# 받은 코인은 남은 내 농장에 들어가요. 남은 농장이 없으면 다음에 여는 농장에 들어가요."),
            ui.INFO, self.user, SECTION)
        e.add_field(name="적정 인수 금액", value=f"**{f['fair']:,}** 코인")
        e.add_field(name="정할 수 있는 금액", value=f"{lo:,} 코인부터\n{hi:,} 코인까지")
        return e

    async def _picked(self, interaction: discord.Interaction):
        self.pick = int(interaction.data["values"][0])
        await interaction.response.defer()

    def _answer(self, accept: bool):
        async def cb(interaction: discord.Interaction):
            self.stop()
            await self.cog.answer_offer(interaction, self.user, self.pick, accept, self.panel)
        return cb

    async def _ask(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AskModal(self.cog, self.user, self.f))


# ───────────── 농사 상점 (서버마다 스레드 하나 — 봇이 재시작해도 눌리는 버튼) ─────────────
SHOP_VER = 5   # 상점 화면을 바꾸면 올린다 — 이미 올라간 상점 글도 새 화면으로 고친다


def shop_embeds() -> list[discord.Embed]:
    """11/12 규완님 '농사상점 살짝 보기 불편해졌는데 확실하게 분류해줘' → 씨앗 · 동물 · 아이템을 카드(임베드) 하나씩으로 나누고,
    아래 메뉴도 종류마다 하나. 11/11: 한 줄에 값을 몰아 쓰지 말 것 → 물건마다 칸 하나 (이름, 값)."""
    head = ui.card(SHOP_NAME, "씨앗, 동물, 아이템을 농장 코인으로 사요.\n아래 메뉴에서 종류별로 골라 사요.\n"
                              "**내 코인**을 누르면 내가 가진 코인이 보여요.", ui.INFO, None, SECTION)
    seeds = discord.Embed(title="씨앗", description="당근은 씨앗 없이 바로 심을 수 있어요.", color=ui.WIN)
    for _, n, price, _, _ in F.CROPS.values():
        if price:
            seeds.add_field(name=n, value=f"{price:,} 코인")
    animals = discord.Embed(title="동물", description=f"땅 {F.ANIMAL_PER_PLOTS}칸마다 한 마리 기를 수 있어요.", color=ui.INFO)
    for _, n, price, _, _ in F.ANIMALS.values():
        animals.add_field(name=n, value=f"{price:,} 코인")
    tools = discord.Embed(title="아이템", description="농장 화면의 **아이템**에서 써요.", color=ui.GOLD)
    for n, price, what in F.TOOLS.values():
        tools.add_field(name=n, value=f"**{price:,} 코인**\n{what}")
    return [head, seeds, animals, tools]


def coins_embed(user, farms: list[dict]) -> discord.Embed:
    """내 농장마다 코인 한 칸씩."""
    e = ui.card("내 코인", "농장마다 가진 코인이에요.", ui.INFO, user, SECTION)
    _grid_fields(e, [(f"{F.LANDS[f['land']][1]} 농장", f"{f['coins']:,} 코인") for f in farms[:24]])
    return e


SHOP_MENUS = (   # (custom_id, 메뉴 이름, 물건들) — 종류마다 메뉴 하나 (11/12 '확실하게 분류')
    ("farm:shop:seed", "씨앗 사기", [k for k, v in F.CROPS.items() if v[2]]),
    ("farm:shop:animal", "동물 사기", list(F.ANIMALS)),
    ("farm:shop:tool", "아이템 사기", list(F.TOOLS)),
)


class ShopPanel(discord.ui.View):
    def __init__(self, cog: "Farm"):
        super().__init__(timeout=None)
        self.cog = cog
        for row, (cid, name, keys) in enumerate(SHOP_MENUS):
            sel = discord.ui.Select(custom_id=cid, placeholder=name, row=row, options=[
                discord.SelectOption(label=_name(k), value=k, description=f"{F.shop_price(k):,} 코인") for k in keys])
            sel.callback = self._menu
            self.add_item(sel)
        b = discord.ui.Button(custom_id="farm:shop:coins", label="내 코인", style=discord.ButtonStyle.secondary, row=len(SHOP_MENUS))
        b.callback = self._coins
        self.add_item(b)

    async def _menu(self, interaction: discord.Interaction):
        await self._pick(interaction.data["values"][0])(interaction)
        try:   # 같이 쓰는 상점 글이라 고른 것을 비워 둔다 (같은 것을 다시 고를 수 있게)
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _coins(self, interaction: discord.Interaction):
        """11/11 규완님: 농사 상점에서 내가 가진 코인이 보이게 (누른 사람에게만)."""
        if not release.preview_381(interaction.guild_id):
            return await interaction.response.send_message("농사 상점은 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        farms = await self.cog.db.farms_of(interaction.user.id, int(time.time()))
        if not farms:
            return await interaction.response.send_message("먼저 `/농사`로 농장을 열어 주세요.", ephemeral=True)
        await interaction.response.send_message(embed=coins_embed(interaction.user, farms), ephemeral=True)

    def _pick(self, key: str):
        async def cb(interaction: discord.Interaction):
            if not release.preview_381(interaction.guild_id):
                return await interaction.response.send_message("농사 상점은 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
            farms = await self.cog.db.farms_of(interaction.user.id, int(time.time()))
            if not farms:
                return await interaction.response.send_message("먼저 `/농사`로 농장을 열어 주세요.", ephemeral=True)
            view = BuyView(self.cog, interaction.user, farms, key)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
        return cb


class BuyView(discord.ui.View):
    """농사 상점에서 고른 씨앗 · 동물 · 아이템을 내 농장 코인으로 산다 (누른 사람에게만 보인다)."""

    def __init__(self, cog: "Farm", user, farms: list[dict], key: str):
        super().__init__(timeout=180)
        self.cog, self.user, self.farms, self.key, self.note = cog, user, farms, key, ""
        self.fid = farms[0]["id"]
        self.unit = "마리" if key in F.ANIMALS else "개"
        self._draw()

    @property
    def farm(self) -> dict:
        return next((f for f in self.farms if f["id"] == self.fid), self.farms[0])

    def _draw(self):
        self.clear_items()
        f, price = self.farm, F.shop_price(self.key)
        most = f["coins"] // price
        for n in ((1,) if self.key in F.ANIMALS else (1, 5, 10)):
            b = discord.ui.Button(label=f"{n}{self.unit} ({price * n:,} 코인)", style=discord.ButtonStyle.success,
                                  disabled=most < n, row=0)
            b.callback = self._buy(n)
            self.add_item(b)
        if self.key not in F.ANIMALS:
            b = discord.ui.Button(label=f"살 수 있는 만큼 ({most:,}개)" if most else "살 수 있는 만큼",
                                  style=discord.ButtonStyle.primary, disabled=most < 1, row=0)
            b.callback = self._buy(0)
            self.add_item(b)
        if len(self.farms) > 1:
            sel = discord.ui.Select(placeholder="어느 농장의 코인으로 살지 고르기", row=1, options=[
                discord.SelectOption(label=f"{F.LANDS[x['land']][1]} 농장"[:100], description=f"코인 {x['coins']:,}",
                                     value=str(x["id"]), default=x["id"] == self.fid) for x in self.farms[:25]])
            sel.callback = self._farm
            self.add_item(sel)

    def embed(self) -> discord.Embed:
        """한 줄에 하나씩 (11/11 규완님)."""
        f, k = self.farm, self.key
        if k in F.ANIMALS:
            _, n, price, prod, sec = F.ANIMALS[k]
            info = [f"값 **{price:,} 코인**", f"만드는 것 **{F.PRODUCTS[prod][1]}**",
                    f"{F.PRODUCTS[prod][1]} 하나를 팔면 **{F.PRODUCTS[prod][2]:,} 코인**", f"{_dur(sec)}마다 하나씩 만들어요"]
        elif k in F.TOOLS:
            n, price, what = F.TOOLS[k]
            info = [f"값 **{price:,} 코인**", f"{what}."]
        else:
            _, n, price, sec, sell = F.CROPS[k]
            info = [f"값 **{price:,} 코인**", f"다 자라는 데 **{_dur(sec)}**", f"다 자라면 팔 때 **{sell:,} 코인**"]
        have = (f["tools"] if k in F.TOOLS else f["seeds"]).get(k, 0)
        mine = [f"내 {F.LANDS[f['land']][1]} 농장 코인 **{f['coins']:,}**", f"사 둔 {n} **{have}{self.unit}**"]
        where = "농장 화면의 아이템에서" if k in F.TOOLS else "내 농장의 심기 메뉴에서"
        return ui.card(f"{n} 사기", (f"{self.note}\n\n" if self.note else "") + "\n".join(info) + "\n\n" + "\n".join(mine)
                       + f"\n-# 산 것은 {where} 써요", ui.INFO, self.user, SECTION)

    async def _redraw(self, interaction: discord.Interaction, note: str = ""):
        self.farms = await self.cog.db.farms_of(self.user.id, int(time.time())) or self.farms
        self.note = note
        self._draw()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def _buy(self, n: int):
        async def cb(interaction: discord.Interaction):
            now = int(time.time())
            count = n
            if not count:   # 살 수 있는 만큼
                f = await self.cog.db.get(self.fid, now)
                count = (f["coins"] // F.shop_price(self.key)) if f else 0
            r = await self.cog.db.buy(self.user.id, self.fid, self.key, count, now)
            if r["ok"]:
                note = f"**{_name(self.key)} {r['count']:,}{self.unit}**를 샀어요 (코인 -{r['cost']:,})"
            else:
                note = {"coins": f"코인이 모자라요 ({r.get('cost', 0):,} 코인이 필요해요).", "amount": "코인이 모자라요.",
                        "none": "내 농장이 아니에요."}.get(r["reason"], "살 수 없어요.")
            await self._redraw(interaction, note)
        return cb

    async def _farm(self, interaction: discord.Interaction):
        self.fid = int(interaction.data["values"][0])
        await self._redraw(interaction)


class LandPick(discord.ui.View):
    """처음 한 번: 땅을 고르고 [시작]. pay_fid 가 있으면 그 농장 코인 NEW_FARM_COST 로 새 밭(농장)을 하나 더 연다."""

    def __init__(self, cog: "Farm", user, pay_fid: Optional[int] = None, panel=None):
        super().__init__(timeout=300)
        self.cog, self.user, self.pick, self.pay_fid, self.panel = cog, user, None, pay_fid, panel
        self._draw()

    def _draw(self):
        self.clear_items()
        sel = discord.ui.Select(placeholder="땅 고르기", options=[
            discord.SelectOption(label=n, value=k, description=desc[:100], default=k == self.pick)
            for k, (_, n, desc, _) in F.LANDS.items()])
        sel.callback = self._picked
        self.add_item(sel)
        go = discord.ui.Button(label=(f"{F.LANDS[self.pick][1]}에 새 밭 만들기" if self.pay_fid else f"{F.LANDS[self.pick][1]}에서 시작하기")
                               if self.pick else "땅을 먼저 골라 주세요",
                               style=discord.ButtonStyle.success, disabled=not self.pick)
        go.callback = self._start
        self.add_item(go)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    async def _picked(self, interaction: discord.Interaction):
        self.pick = interaction.data["values"][0]
        self._draw()
        await interaction.response.edit_message(embed=land_embed(self.user, self.pick, bool(self.pay_fid)), view=self)

    async def _start(self, interaction: discord.Interaction):
        if self.pay_fid:
            return await self._more(interaction)
        self.stop()
        r = await self.cog.db.create(self.user.id, self.pick, int(time.time()))
        _, n, desc, _ = F.LANDS[self.pick]
        msg = f"**{n}**에 농장을 열었어요. {desc}." if r["ok"] else "이미 농장이 있어요."
        if r.get("held"):
            msg += f"\n맡아 둔 인수 대금 **{r['held']:,} 코인**이 이 농장에 들어왔어요."
        await interaction.response.edit_message(embed=ui.card("농사 시작", msg, ui.GOLD, self.user, SECTION), view=None)
        if r.get("id"):
            await self.cog.open(interaction, self.user, r["id"])

    async def _more(self, interaction: discord.Interaction):
        """11/12 규완님 '100만 코인 모으면 밭 하나 더'."""
        now = int(time.time())
        r = await self.cog.db.create_more(self.user.id, self.pick, self.pay_fid, now)
        if not r["ok"]:
            msg = (f"이 농장 코인이 모자라요.\n필요한 코인 **{r.get('cost', 0):,}**\n가진 코인 **{r.get('have', 0):,}**"
                   if r["reason"] == "coins" else "내 농장이 아니에요.")
            return await interaction.response.edit_message(embed=ui.card("새 밭 만들기", msg, ui.LOSE, self.user, SECTION), view=None)
        self.stop()
        _, n, desc, _ = F.LANDS[self.pick]
        await interaction.response.edit_message(embed=ui.card("새 밭 만들기", (
            f"**{n}**에 새 밭을 열었어요. {desc}.\n코인 **-{r['cost']:,}**\n이 농장에 남은 코인 **{r['left']:,}**"),
            ui.GOLD, self.user, SECTION), view=None)
        if self.panel is not None:   # 코인을 낸 농장 화면도 고친다
            try:
                f = await self.cog.db.get(self.pay_fid, now)
                await self.panel.edit(embed=panel_embed(self.user, f, now), view=FarmPanel(self.cog, f))
            except discord.HTTPException:
                pass
        await self.cog.open(interaction, self.user, r["id"])


class FarmPick(discord.ui.View):
    """농장이 여러 개일 때: 열 농장 고르기."""

    def __init__(self, cog: "Farm", user, farms: list[dict]):
        super().__init__(timeout=180)
        self.cog, self.user = cog, user
        sel = discord.ui.Select(placeholder="열 농장 고르기", options=[
            discord.SelectOption(label=f"{F.LANDS[f['land']][1]} 농장"[:100], description=f"코인 {f['coins']:,}", value=str(f["id"]))
            for f in farms[:25]])
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await self.cog.open(interaction, self.user, int(interaction.data["values"][0]))


class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = F.FarmDB()
        self.panel = FarmPanel(self)
        bot.add_view(self.panel)   # 재시작 전에 올라간 농장 화면의 버튼도 받는다
        self.shop = ShopPanel(self)
        bot.add_view(self.shop)    # 농사 상점 버튼도
        self._shop_lock = asyncio.Lock()
        self._sticky: dict[int, asyncio.Task] = {}   # 농장 스레드 → 밭을 다시 올릴 예약
        self._sticky_lock = asyncio.Lock()

    async def cog_load(self):
        rows = await self.db.shop_threads()
        SHOPS.clear()
        SHOPS.update({g: t for g, (t, _, _) in rows.items()})
        SHOP_MSGS.clear()
        SHOP_MSGS.update({g: (m, v) for g, (_, m, v) in rows.items()})
        self.shop_loop.start()

    def cog_unload(self):
        self.panel.stop()
        self.shop.stop()
        self.shop_loop.cancel()
        for t in self._sticky.values():
            t.cancel()

    # ───────────── 농사 상점 스레드 ─────────────
    @tasks.loop(minutes=5)
    async def shop_loop(self):
        """/농사 가 열린 서버(공개 전엔 테스트 서버, 공개되면 모든 서버)에 농사 상점 스레드가 없으면 만든다."""
        for guild in list(self.bot.guilds):
            if not release.preview_381(guild.id):
                continue
            try:
                await self.ensure_shop(guild)
            except Exception as ex:   # 한 서버가 안 돼도 나머지는 계속
                print(f"[농사 상점] {guild.id} 오류: {type(ex).__name__}: {ex}")

    @shop_loop.before_loop
    async def _before_shop(self):
        await self.bot.wait_until_ready()

    async def _shop_parents(self, guild, near=None) -> list:
        """상점 스레드를 만들 채널 후보: 패치노트가 가는 채널 (패치노트가 스레드로 가는 서버는 그 스레드의 채널) → near."""
        out = []
        tid = PATCH_THREADS.get(guild.id)
        if tid:
            th = guild.get_channel_or_thread(tid)
            if th is None:
                try:
                    th = await guild.fetch_channel(tid)
                except discord.HTTPException:
                    th = None
            out.append(getattr(th, "parent", None))
        notes = self.bot.get_cog("PatchNotesCog")
        if notes is not None:
            out.append(await asyncio.to_thread(notes._pick_channel, guild))
        out.append(near)
        return [c for n, c in enumerate(out) if isinstance(c, discord.TextChannel) and c not in out[:n]]

    async def ensure_shop(self, guild, near=None):
        """이 서버의 농사 상점 스레드 (없으면 만든다 · 보관됐으면 다시 연다). 만들 수 없으면 None."""
        async with self._shop_lock:
            tid = SHOPS.get(guild.id)
            if tid:
                th = guild.get_thread(tid)
                if th is None:
                    try:
                        th = await guild.fetch_channel(tid)
                    except discord.NotFound:          # 지워진 스레드 → 새로 만든다
                        th = None
                    except discord.HTTPException:     # 잠깐 안 되는 것일 수 있다 — 새로 만들지 않는다
                        return None
                if isinstance(th, discord.Thread):
                    await self._open_shop(guild, th)
                    if SHOP_MSGS.get(guild.id, (None, 0))[1] != SHOP_VER:
                        await self._refresh_shop(guild, th)
                    return th
            for parent in await self._shop_parents(guild, near):
                perms = parent.permissions_for(guild.me)
                if not (perms.view_channel and perms.create_public_threads and perms.send_messages_in_threads):
                    continue
                try:
                    if parent.is_news():   # 공지 채널의 스레드는 글에서만 열 수 있다
                        first = await parent.send(f"**{SHOP_NAME}**이 열렸어요. 씨앗과 동물은 이 글의 스레드에서 사요.")
                        th = await first.create_thread(name=SHOP_NAME, auto_archive_duration=10080, reason=SHOP_NAME)
                    else:
                        th = await parent.create_thread(name=SHOP_NAME, type=discord.ChannelType.public_thread,
                                                        auto_archive_duration=10080, reason=SHOP_NAME)
                    msg = await th.send(embeds=shop_embeds(), view=ShopPanel(self))
                except discord.HTTPException as ex:
                    print(f"[농사 상점] {guild.id} 스레드 만들기 오류: {type(ex).__name__}: {ex}")
                    continue
                await self.db.set_shop_thread(guild.id, th.id, msg.id, SHOP_VER)
                SHOPS[guild.id], SHOP_MSGS[guild.id] = th.id, (msg.id, SHOP_VER)
                print(f"[농사 상점] {guild.id} 스레드 {th.id} 만듦 (#{parent.name})")
                await self._open_shop(guild, th)
                return th
            return None

    async def _open_shop(self, guild, th):
        """보관된 상점 스레드는 다시 열고, 잠겨 있으면 푼다.
        11/11: 잠그면 '스레드 관리하기' 권한이 없는 사람은 디스코드가 상점 버튼을 막아서 살 수 없다 (#134 를 되돌림)."""
        kw = {}
        if th.archived:
            kw["archived"] = False
        if th.locked:
            kw["locked"] = False
        if not kw:
            return
        try:
            await th.edit(**kw)
            if "locked" in kw:
                print(f"[농사 상점] {guild.id} 스레드 {th.id} 잠금 풂")
        except discord.HTTPException as ex:
            if "locked" in kw:
                print(f"[농사 상점] {guild.id} 스레드 잠금 풀기 오류: {type(ex).__name__}: {ex}")

    async def _refresh_shop(self, guild, th):
        """상점 화면이 바뀌었으면 이미 올라간 상점 글을 새 화면으로 고친다 (못 찾으면 새로 올린다)."""
        mid = SHOP_MSGS.get(guild.id, (None, 0))[0]
        msg = None
        try:
            if mid:
                msg = await th.fetch_message(mid)
        except discord.HTTPException:
            msg = None
        try:
            if msg is None:   # 예전 상점 글: 스레드 맨 처음의 봇 글
                async for m in th.history(limit=10, oldest_first=True):
                    if m.author.id == self.bot.user.id and m.embeds:
                        msg = m
                        break
            if msg is not None:
                await msg.edit(embeds=shop_embeds(), view=ShopPanel(self))
            else:
                msg = await th.send(embeds=shop_embeds(), view=ShopPanel(self))
        except discord.HTTPException as ex:
            print(f"[농사 상점] {guild.id} 상점 글 고치기 오류: {type(ex).__name__}: {ex}")
            return
        await self.db.set_shop_thread(guild.id, th.id, msg.id, SHOP_VER)
        SHOP_MSGS[guild.id] = (msg.id, SHOP_VER)
        print(f"[농사 상점] {guild.id} 상점 글 {msg.id} 새 화면으로 (v{SHOP_VER})")

    async def show(self, interaction: discord.Interaction, fid: int, note: str = ""):
        """누른 농장 화면을 지금 상태로 다시 그린다."""
        now = int(time.time())
        f = await self.db.get(fid, now)
        await interaction.response.edit_message(embed=panel_embed(interaction.user, f, now, note), view=FarmPanel(self, f))

    # ───────────── 밭을 맨 밑에 (11/11 규완님: 누가 채팅을 치면 밭이 위로 올라간다) ─────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """농장 스레드에 다른 글이 올라오면, 잠깐 조용해진 뒤 밭 화면을 맨 밑으로 다시 올린다."""
        th = message.channel
        if not isinstance(th, discord.Thread) or message.author.id == self.bot.user.id:
            return
        if not release.preview_381(th.guild.id):
            return
        fid = await self.db.thread_farm(th.id)
        if fid is None:
            return
        old = self._sticky.pop(th.id, None)
        if old:
            old.cancel()   # 글이 이어지면 마지막 글 뒤에 한 번만
        self._sticky[th.id] = asyncio.create_task(self._sticky_later(th, fid))

    async def _sticky_later(self, th: discord.Thread, fid: int):
        try:
            await asyncio.sleep(STICKY_WAIT)
        except asyncio.CancelledError:
            return
        if self._sticky.get(th.id) is asyncio.current_task():
            del self._sticky[th.id]   # 여기부터는 새 글이 와도 끊지 않는다 (새 예약이 따로 잡힌다)
        try:
            await self.repost(th, fid)
        except Exception as ex:
            print(f"[농사] {th.id} 밭 다시 올리기 오류: {type(ex).__name__}: {ex}")

    @staticmethod
    def _is_panel(m: discord.Message) -> bool:
        return any(getattr(c, "custom_id", None) == "farm:harvest"
                   for row in m.components for c in getattr(row, "children", []))

    async def repost(self, th: discord.Thread, fid: int) -> bool:
        """밭 화면을 스레드 맨 밑에 새로 올리고 위에 남은 옛 밭 화면은 지운다. 이미 맨 밑이면 그대로."""
        async with self._sticky_lock:
            old, first = [], True
            async for m in th.history(limit=STICKY_SCAN):
                if m.author.id == self.bot.user.id and self._is_panel(m):
                    if first:
                        return False   # 맨 밑 글이 이미 밭
                    old.append(m)
                first = False
            now = int(time.time())
            f = await self.db.get(fid, now)
            if f is None:
                return False
            owner = th.guild.get_member(f["owner"]) or await self.bot.fetch_user(f["owner"])
            await th.send(embed=panel_embed(owner, f, now), view=FarmPanel(self, f))
            for m in old:
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass
            return True

    async def open(self, interaction: discord.Interaction, user, fid: int):
        """내 농장 fid 의 스레드를 안내한다 (없으면 이 채널에 만들고 농장 화면을 올린다).
        그 스레드 안에서 부를 때만 농장 화면을 새로 올린다."""
        try:
            await self._open(interaction, user, fid)
        finally:
            guild, ch = interaction.guild, interaction.channel
            if guild.id not in SHOPS:   # 패치노트 채널에 상점을 못 만든 서버 — 농장을 여는 이 채널에라도 만든다
                try:
                    await self.ensure_shop(guild, ch.parent if isinstance(ch, discord.Thread) else ch)
                except Exception as ex:
                    print(f"[농사 상점] {guild.id} 오류: {type(ex).__name__}: {ex}")

    async def _open(self, interaction: discord.Interaction, user, fid: int):
        guild, ch, now = interaction.guild, interaction.channel, int(time.time())
        f = await self.db.get(fid, now)
        tid = await self.db.get_thread(fid, guild.id)
        if tid and ch.id == tid and not interaction.response.is_done():
            return await interaction.response.send_message(embed=panel_embed(user, f, now), view=FarmPanel(self, f))
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        thread = None
        if tid:
            thread = guild.get_thread(tid)
            if thread is None:
                try:
                    thread = await guild.fetch_channel(tid)
                except discord.HTTPException:   # 지워진 스레드
                    thread = None
            if not isinstance(thread, discord.Thread):
                thread = None
        created = thread is None
        try:
            if created:
                parent = ch.parent if isinstance(ch, discord.Thread) else ch
                if not isinstance(parent, discord.TextChannel):
                    return await interaction.followup.send(
                        "농장 스레드는 일반 채팅 채널에서만 열 수 있어요. 채팅 채널에서 `/농사`를 써 주세요.", ephemeral=True)
                thread = await parent.create_thread(name=f"{user.display_name}의 농장"[:100],
                                                    type=discord.ChannelType.public_thread, auto_archive_duration=10080,
                                                    reason=f"/농사 — {user}")
                await self.db.set_thread(fid, guild.id, thread.id)
                await thread.send(content=user.mention, embed=panel_embed(user, f, now), view=FarmPanel(self, f))
            elif thread.archived:   # 이미 있는 스레드엔 농장 화면을 다시 올리지 않는다 (스레드 안에서 /농사 를 쓰면 올린다)
                await thread.edit(archived=False)
        except discord.Forbidden:
            return await interaction.followup.send(
                "봇이 이 채널에 스레드를 만들거나 글을 쓸 수 없어요. 서버 관리자가 봇 역할에 **공개 스레드 만들기**와 "
                "**스레드에서 메시지 보내기** 권한을 켜 주세요.", ephemeral=True)
        except discord.HTTPException as ex:
            print(f"[농사] 스레드 열기 오류: {type(ex).__name__}: {ex}")
            return await interaction.followup.send("농장 스레드를 열지 못했어요. 잠시 뒤에 다시 해 주세요.", ephemeral=True)
        await interaction.followup.send(f"{'농장이 열렸어요' if created else '내 농장은 여기예요'}: {thread.mention}", ephemeral=True)

    # ───────────── 인수 ─────────────
    def name_of(self, near, uid: int) -> str:
        """uid 의 이름 (near 와 같은 서버의 별명 → 봇이 아는 이름 → id)."""
        guild = getattr(near, "guild", None)
        m = (guild.get_member(uid) if guild else None) or self.bot.get_user(uid)
        return m.display_name if m else str(uid)

    async def _user(self, guild, uid: int):
        m = guild.get_member(uid) if guild else None
        if m is None:
            try:
                m = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            except discord.HTTPException:
                m = None
        return m

    async def _dm(self, uid: int, e: discord.Embed):
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(embed=e)
        except discord.HTTPException:
            pass

    async def _moved(self, interaction: discord.Interaction, fid: int, owner_id: int, panel, note: str):
        """농장 주인이 바뀐 뒤: 농장 화면을 새 주인 것으로 고치고, 그 농장의 스레드 이름도 새 주인으로."""
        now = int(time.time())
        owner = await self._user(interaction.guild, owner_id)
        if owner is None:
            return
        f = await self.db.get(fid, now)
        try:
            if panel is not None and f:
                await panel.edit(embed=panel_embed(owner, f, now, note), view=FarmPanel(self, f))
        except discord.HTTPException:
            pass
        for tid in await self.db.threads_of(fid):
            th = self.bot.get_channel(tid)
            try:
                if isinstance(th, discord.Thread):
                    await th.edit(name=f"{owner.display_name}의 농장"[:100])
            except discord.HTTPException:
                pass

    async def make_offer(self, interaction: discord.Interaction, user, fid: int, amount: int, panel, pay_fid: int,
                         edit: bool = False):
        """인수 제안 (농장 코인 · pay_fid 농장에서 낸다)."""
        r = await self.db.offer_farm(user.id, fid, amount, int(time.time()), pay_fid)
        place = getattr(interaction.channel, "mention", "농장 스레드")
        if not r["ok"]:
            e = ui.card("인수 제안 실패", _offer_fail(r), ui.LOSE, user, SECTION)
        elif r["auto"]:
            e = ui.card("농장 인수 완료", (
                "적정 인수 금액 이상을 제안해서 바로 인수했어요.\n"
                f"인수 금액 **{r['cost']:,} 코인**\n코인을 낸 농장에 남은 코인 **{r['left']:,}**\n\n"
                f"전 주인 <@{r['prev_owner']}>님이 인수 대금을 코인으로 받았어요.\n"
                "이 스레드의 농장이 이제 내 농장이에요. `/농사`로도 열 수 있어요."), ui.GOLD, user, SECTION)
            await self._dm(r["prev_owner"], ui.card("농장이 인수됐어요", (
                f"{user.mention}님이 적정 인수 금액 이상을 제안해 {place}의 농장을 바로 인수했어요.\n"
                f"받은 코인 **+{r['cost']:,}**\n" + _got_line(r)), ui.INFO, None, SECTION))
        else:
            e = ui.card("인수 제안을 보냈어요", (
                f"제안 금액 **{r['amount']:,} 코인**\n적정 인수 금액 {r['fair']:,} 코인\n\n"
                f"주인 <@{r['owner_id']}>님이 수락하면 인수돼요.\n제안은 <t:{r['expires']}:R> 만료돼요.\n"
                "-# 코인은 수락될 때 빠져나가요. 그때 코인이 모자라면 인수되지 않아요."), ui.INFO, user, SECTION)
            await self._dm(r["owner_id"], ui.card("농장 인수 제안", (
                f"{user.mention}님이 {place}의 농장을 **{r['amount']:,} 코인**에 인수하고 싶어 해요.\n"
                f"수락하면 **{r['amount']:,} 코인**을 받아요.\n"
                "농장 화면의 **받은 인수 제안**에서 수락하거나 거절할 수 있어요.\n"
                f"제안은 <t:{r['expires']}:R> 만료돼요."),
                ui.INFO, None, SECTION))
        if edit:
            await interaction.response.edit_message(embed=e, view=None)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
        if r["ok"] and r["auto"]:
            await self._moved(interaction, fid, user.id, panel, f"{user.mention}님이 이 농장을 인수했어요.")

    async def answer_offer(self, interaction: discord.Interaction, user, offer_id: Optional[int], accept: bool, panel):
        r = await self.db.answer_farm_offer(user.id, offer_id or 0, accept, int(time.time()))
        place = getattr(interaction.channel, "mention", "농장 스레드")
        if r["ok"] and r["accepted"]:
            e = ui.card("농장을 넘겼어요", (
                f"<@{r['buyer_id']}>님의 제안을 수락했어요.\n받은 코인 **+{r['amount']:,}**\n" + _got_line(r)),
                ui.WIN, user, SECTION)
            await self._dm(r["buyer_id"], ui.card("농장 인수 완료", (
                f"주인이 **{r['amount']:,} 코인** 제안을 수락했어요. {place}의 농장이 이제 내 농장이에요.\n"
                "`/농사`로도 열 수 있어요."), ui.GOLD, None, SECTION))
        elif r["ok"]:
            e = ui.card("인수 제안 거절", f"<@{r['buyer_id']}>님의 **{r['amount']:,} 코인** 제안을 거절했어요.", ui.EVEN, user, SECTION)
            await self._dm(r["buyer_id"], ui.card("농장 인수 제안 거절", (
                f"{place}의 농장 주인이 **{r['amount']:,} 코인** 제안을 거절했어요."), ui.EVEN, None, SECTION))
        else:
            msg = (_OFFER_FAIL["gone"] if r["reason"] == "gone" else
                   "제안한 사람이 지금 인수할 수 없어서 제안이 닫혔어요.\n" + _offer_fail(r))
            e = ui.card("처리할 수 없어요", msg, ui.LOSE, user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)
        if r["ok"] and r["accepted"]:
            await self._moved(interaction, r["id"], r["buyer_id"], panel, f"<@{r['buyer_id']}>님이 이 농장을 인수했어요.")

    @app_commands.command(name="농사", description="내 농장 — 땅을 골라 작물과 동물을 기르고, 합치고, 넓혀요")
    async def farm(self, interaction: discord.Interaction):
        if not release.preview_381(interaction.guild_id):
            return await interaction.response.send_message("/농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message("/농사 는 서버 채널에서 써 주세요.", ephemeral=True)
        user = interaction.user
        farms = await self.db.farms_of(user.id, int(time.time()))
        if not farms:
            return await interaction.response.send_message(embed=land_embed(user), view=LandPick(self, user), ephemeral=True)
        here = await self.db.thread_farm(interaction.channel_id)     # 내 농장 스레드 안이면 그 농장
        if len(farms) == 1 or here in [f["id"] for f in farms]:
            return await self.open(interaction, user, here if here in [f["id"] for f in farms] else farms[0]["id"])
        lines = "\n".join(f"{n}. {_farm_line(f)} (땅 {f['plots']}칸)" for n, f in enumerate(farms, 1))
        await interaction.response.send_message(
            embed=ui.card("내 농장", f"{lines}\n\n열 농장을 골라 주세요.", ui.INFO, user, SECTION),
            view=FarmPick(self, user, farms), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
