# services/dungeon.py — 🏰 던전 (2.8): 하루 1회 · 요일 테마 · 10층 · 층마다 문 2~3개(힌트, 일부 거짓) · 5 · 10층 보스
# 파티 = 내 구단 선발 11명. 스탯 4개: 공격(FW 평균 OVR) · 기동(MF) · 수비(DF) · 운(GK).
# 방: 전투 · 보물 · 함정 · 상인 · 휴식 · 제단 (퀴즈방 없음). 파티 체력 100 — [귀환]하면 보상 전부, 체력 0이면 절반.
# 보상은 돈을 적게, 아이템 위주. 진행 상태는 DB(JSON) — 이어하기 · 30분 방치하면 자동 귀환(보상 전부).
import json
import math
import random
import time
from typing import Optional

from services.club_db import SLOT_GROUP, effective_ovr
from services.economy_db import EconomyDB, give_item, grind_add_xp, _kst_day, _cook_buff

FLOORS = 10
BOSS_FLOORS = (5, 10)
HP_MAX = 100
HP_MEAL = 20                    # 🍱 보양 정식(낚시 요리) 효과
IDLE_RETURN = 1800              # 30분 아무것도 안 하면 자동 귀환
DEATH_KEEP = 0.5                # 쓰러지면 보상 절반
EMPTY_OVR = 20                  # 2.10.1: 빈 자리 (예전엔 그룹이 통째로 비어야 40)
FALSE_HINT = 0.25               # 힌트가 거짓일 확률 (🔦 횃불이면 0)
POTION_HEAL, REST_HEAL, FEATHER_HP = 40, 30, 50

STATS = (("atk", "⚔️ 공격", "FW"), ("mob", "💨 기동", "MF"), ("dfn", "🛡️ 수비", "DF"), ("luck", "🍀 운", "GK"))

# 요일 테마 (월=0): (이모지, 이름, 몬스터, 보스5, 보스10, 함정 이름, 방 비중 보정)
THEMES = (
    ("🔥", "화염 동굴", ("불도마뱀", "용암 골렘", "화염 박쥐"), "불꽃 거인", "화염룡 이그니스", "용암 분출", {"battle": 1.2}),
    ("🌊", "수몰 신전", ("심해 어인", "거대 게", "물귀신"), "해마 기사", "크라켄", "급류", {"treasure": 1.2}),
    ("🌲", "고대 숲", ("독버섯 정령", "늑대 무리", "덩굴 괴물"), "숲의 수호자", "고대 엔트", "독가시 덤불", {"rest": 1.3}),
    ("⚡", "폭풍 탑", ("번개 정령", "가고일", "폭풍 매"), "뇌전 기사", "폭풍의 군주", "낙뢰", {"trap": 1.3}),
    ("💀", "망자의 지하묘지", ("스켈레톤", "구울", "유령"), "묘지기", "리치 왕", "저주의 독안개", {"altar": 1.5}),
    ("💎", "수정 광산", ("수정 골렘", "광부 유령", "두더지 대장"), "수정 거미", "다이아몬드 골렘", "낙석", {"treasure": 1.4}),
    ("🐉", "용의 둥지", ("새끼 용", "용인 전사", "와이번"), "용기사", "고룡 바하무트", "용의 숨결", {"battle": 1.3}),
)
ROOMS = {   # 키: (이모지, 이름, 기본 비중, 힌트)
    "battle": ("⚔️", "전투", 34, ("🔊 으르렁거리는 소리", "🩸 핏자국", "👣 큰 발자국")),
    "treasure": ("📦", "보물", 18, ("✨ 반짝이는 빛", "🪙 금화 냄새", "🗺️ 낡은 지도 표시")),
    "trap": ("🪤", "함정", 16, ("💨 찬바람", "⚙️ 딸깍거리는 소리", "🕳️ 바닥이 이상해요")),
    "merchant": ("🧙", "상인", 8, ("🔔 방울 소리", "🧺 장사꾼 목소리")),
    "rest": ("🏕️", "휴식", 12, ("🔥 따뜻한 기운", "🍲 맛있는 냄새")),
    "altar": ("🕯️", "제단", 12, ("🕯️ 촛불 빛", "🙏 웅얼거리는 기도 소리")),
}
# 보상 표 (아이템 키 → 비중). 층이 깊을수록 좋은 표를 더 자주.
# 2.10.1: 점검 보상 상자(box)는 던전에서 안 나온다 · 스킵권 조각은 확 줄임 (예전 30 · 20)
LOOT_COMMON = {"skip_piece": 6, "bait": 20, "spice": 18, "potion": 10, "torch": 6, "bait_gold": 8, "fish_charm": 4, "dkey": 4}
LOOT_RARE = {"skip_piece": 5, "bow_pro": 9, "windbreaker": 9, "focus": 9, "antidote": 8, "scroll": 8,
             "feather": 5, "dkey": 8, "fish_charm": 8, "dungeon_ticket": 2, "potion": 4, "tactic_note": 6,
             "analysis_report": 6, "special_training": 6, "scout_coupon": 4, "first_team": 2}
BATTLE_DROP = 0.4               # 일반 전투에서 아이템이 나올 확률 (돈은 항상)
# 3.1.1 너프 (판마다 st["v311"] — release.preview_311): 아이템이 덜 나오고 몬스터 · 함정이 더 세다 · 양궁 아이템 삭제
BATTLE_DROP_311, CHEST_ITEM_311 = 0.2, 0.5          # 일반 전투 · 보물 상자에서 아이템이 나올 확률
POWER_311, BOSS_311, TRAP_311 = 1.15, (44, 70), (14, 2.4)   # 몬스터 힘 × · 보스(5층, 10층) · 함정 피해(기본 + 층마다)
LOOT_RARE_311 = {k: v for k, v in LOOT_RARE.items() if k not in ("bow_pro", "windbreaker", "focus")}
COIN = (3_000, 12_000)          # 보물 · 전투에서 나오는 돈 (층 × 이만큼)
XP_PER_FLOOR = 4                # 훈련 경험치 (층마다 진행 보상)
FIRST_CLEAR = {"dungeon_ticket": 1, "skip_piece": 2, "first_team": 1}   # 테마마다 처음 10층을 깨면
# 2.10.1 공개 전 서버(테스트 서버 말고)에서 시작한 판은 예전 값 — 판마다 st["v2101"] 로 고른다 (release.preview_2101)
OLD_LOOT_COMMON = {"skip_piece": 30, "bait": 20, "spice": 18, "potion": 10, "torch": 6, "bait_gold": 8, "fish_charm": 4, "dkey": 4}
OLD_LOOT_RARE = {"skip_piece": 20, "box": 10, **{k: v for k, v in LOOT_RARE.items() if k != "skip_piece"}}
OLD_FIRST_CLEAR = {"box": 1, "dungeon_ticket": 1, "skip_piece": 5, "first_team": 1}
MERCHANT = {"potion": 150_000, "antidote": 200_000, "torch": 250_000, "scroll": 300_000}   # 돈 쓰는 곳
ALTAR_COST = 15                 # 제단: 체력을 바치면 다음 보물 · 전투 보상 2배 (40%는 저주: 다음 방 피해 +50%)


def norm(ovr: float, new: bool = True) -> float:
    """OVR 60 → 0, 95 → 1. 60 아래는 마이너스 (2.10.1: 약한 팀 · 빈 자리는 손해, OVR 25 이하 = -1).
    new=False: 예전 (OVR 50 → 0, 95 → 1, 아래는 0)."""
    if not new:
        return max(0.0, min(1.0, (ovr - 50) / 45))
    return max(-1.0, min(1.0, (ovr - 60) / 35))


def _n(st: dict, ovr: float) -> float:
    return norm(ovr, st.get("v2101", False))


def team_stats(lineup: list[dict], new: bool = True) -> dict[str, int]:
    """선발 11명 → 스탯 4개 (포지션 그룹 평균 OVR). 2.10.1: 빈 자리도 EMPTY_OVR 로 평균에 넣는다.
    new=False: 예전 (빈 자리는 빼고 평균, 그룹이 통째로 비면 40)."""
    out = {}
    for key, _, group in STATS:
        if new:
            vals = [effective_ovr(s["ovr"], s.get("pos", group), s["slot"]) if s.get("player_id") else EMPTY_OVR
                    for s in lineup if SLOT_GROUP.get(s.get("slot")) == group]
            out[key] = round(sum(vals) / len(vals)) if vals else EMPTY_OVR
        else:
            vals = [effective_ovr(s["ovr"], s.get("pos", group), s["slot"]) for s in lineup
                    if s.get("player_id") and SLOT_GROUP.get(s.get("slot")) == group]
            out[key] = round(sum(vals) / len(vals)) if vals else 40
    return out


def theme_of(now_ts: int) -> int:
    """KST 요일 (월=0)."""
    return (_kst_day(now_ts) + 3) % 7


def week_key(ts: int) -> int:
    return (_kst_day(ts) + 3) // 7


def _pick(table: dict, rng) -> str:
    return rng.choices(list(table), weights=list(table.values()))[0]


def make_doors(st: dict, rng) -> list[dict]:
    """이번 층의 문. 보스층은 문 하나(보스). 힌트는 25% 거짓 (횃불이면 전부 참)."""
    f = st["floor"]
    if f in BOSS_FLOORS:
        return [{"room": "boss", "hint": "👑 거대한 기운이 느껴져요"}]
    mods = THEMES[st["theme"]][6]
    weights = {k: v[2] * mods.get(k, 1) for k, v in ROOMS.items()}
    doors = []
    for _ in range(rng.choice((2, 3, 3))):
        room = _pick(weights, rng)
        shown = room
        if not st.get("torch") and rng.random() < FALSE_HINT:
            shown = rng.choice([k for k in ROOMS if k != room])
        doors.append({"room": room, "hint": rng.choice(ROOMS[shown][3])})
    return doors


def new_run(stats: dict, theme: int, rng, torch: bool = False, meal: bool = False, new: bool = True,
            v311: bool = False) -> dict:
    st = {"v2101": new, "v311": v311 and new, "floor": 1, "hp": HP_MAX + (HP_MEAL if meal else 0), "max_hp": HP_MAX + (HP_MEAL if meal else 0),
          "stats": stats, "theme": theme, "torch": torch, "loot": {}, "coins": 0, "xp": 0, "phase": "doors",
          "room": None, "blessed": False, "cursed": False, "revealed": False, "log": [], "used": {}}
    st["doors"] = make_doors(st, rng)
    return st


def _monster(st: dict, rng, boss: bool) -> dict:
    th = THEMES[st["theme"]]
    f = st["floor"]
    name = (th[3] if f == 5 else th[4]) if boss else rng.choice(th[2])
    if st.get("v311"):
        power = (26 + 3.2 * f) * POWER_311 if not boss else BOSS_311[f != 5]
    elif st.get("v2101"):
        power = (26 + 3.2 * f) if not boss else (38 if f == 5 else 60)   # 2.10.1: 더 세게
    else:
        power = (22 + 2.5 * f) if not boss else (30 if f == 5 else 46)
    return {"name": name, "power": power, "boss": boss}


def _damage(st: dict, base: float, rng, stat_cut: float) -> int:
    """기본 피해 × 스탯 감소 × 운 × 저주."""
    d = base * stat_cut * rng.uniform(0.7, 1.3) * (1.5 if st.get("cursed") else 1)
    st["cursed"] = False
    return max(1, round(d))


def _gain(st: dict, item: str, n: int = 1):
    st["loot"][item] = st["loot"].get(item, 0) + n


def _roll_loot(st: dict, rng, rolls: int, rare_p: float) -> list[str]:
    luck = _n(st, st["stats"]["luck"])
    mult = 2 if st.get("blessed") else 1
    st["blessed"] = False
    got = []
    for _ in range(rolls * mult):
        new = st.get("v2101")
        rare = LOOT_RARE_311 if st.get("v311") else LOOT_RARE if new else OLD_LOOT_RARE
        table = rare if rng.random() < rare_p + 0.15 * luck else \
            (LOOT_COMMON if new else OLD_LOOT_COMMON)
        item = _pick(table, rng)
        _gain(st, item)
        got.append(item)
    coins = round(rng.randint(*COIN) * st["floor"] * mult, -2)
    st["coins"] += coins
    return got + [f"coins:{coins}"]


def _hurt(st: dict, dmg: int, log: list[str]) -> None:
    st["hp"] -= dmg
    log.append(f"💔 체력 **-{dmg}**")


def _after_room(st: dict, rng, log: list[str]) -> None:
    """방이 끝나면: 쓰러졌는지 → 아니면 다음 층 (10층 보스를 이기면 클리어)."""
    if st["hp"] <= 0:
        if st.get("inv_feather"):
            st["inv_feather"] = False
            st["used"]["feather"] = st["used"].get("feather", 0) + 1
            st["hp"] = FEATHER_HP
            log.append(f"🪶 **부활 깃털**이 빛나며 체력 {FEATHER_HP}으로 다시 일어섰어요!")
        else:
            st["hp"] = 0
            st["phase"] = "dead"
            return
    st["xp"] += XP_PER_FLOOR
    if st["floor"] >= FLOORS and st.get("room") == "boss":
        st["phase"] = "clear"
        return
    st["floor"] += 1
    st["room"] = None
    st["phase"] = "doors"
    st["revealed"] = False
    st["doors"] = make_doors(st, rng)


def enter(st: dict, door: int, rng) -> list[str]:
    """문 고르기 → 방 상황. 바로 끝나는 방(함정 · 휴식)은 여기서 처리하고 다음 층으로."""
    d = st["doors"][door]
    room = d["room"]
    st["room"] = room
    log: list[str] = []
    s = st["stats"]
    if room in ("battle", "boss"):
        st["monster"] = _monster(st, rng, room == "boss")
        st["phase"] = "battle"
        log.append(f"{'👑' if room == 'boss' else '👹'} **{st['monster']['name']}**이(가) 나타났다!")
    elif room == "treasure":
        st["phase"] = "treasure"
        log.append("📦 낡은 상자가 놓여 있어요.")
    elif room == "trap":
        name = THEMES[st["theme"]][5]
        if rng.random() < 0.25 + 0.5 * _n(st, s["mob"]):
            log.append(f"🪤 **{name}**! …재빠르게 피했어요! (기동)")
        elif st.get("inv_antidote"):
            st["inv_antidote"] = False
            st["used"]["antidote"] = st["used"].get("antidote", 0) + 1
            log.append(f"🪤 **{name}**! 💊 해독제로 피해를 막았어요.")
        else:
            log.append(f"🪤 **{name}**에 걸렸어요!")
            base = (TRAP_311[0] + TRAP_311[1] * st["floor"]) if st.get("v311") else \
                (12 + 2 * st["floor"]) if st.get("v2101") else (10 + 1.5 * st["floor"])
            _hurt(st, _damage(st, base, rng, 1.15 - 0.5 * _n(st, s["dfn"])), log)
        _after_room(st, rng, log)
    elif room == "rest":
        heal = min(REST_HEAL, st["max_hp"] - st["hp"])
        st["hp"] += heal
        log.append(f"🏕️ 모닥불 앞에서 쉬었어요. 체력 **+{heal}**")
        _after_room(st, rng, log)
    elif room == "merchant":
        st["phase"] = "merchant"
        st["offers"] = rng.sample(list(MERCHANT), 2)
        log.append("🧙 수상한 상인이 물건을 펼쳐 놓았어요.")
    elif room == "altar":
        st["phase"] = "altar"
        log.append(f"🕯️ 오래된 제단이에요. 체력 {ALTAR_COST}을 바치면 무슨 일이 생길까요?")
    return log


def act(st: dict, action: str, rng) -> list[str]:
    """방 안에서 고르기. battle: fight / flee · treasure: open / key · merchant: buy:<item> / leave · altar: pray / leave."""
    log: list[str] = []
    s = st["stats"]
    ph = st["phase"]
    if ph == "battle":
        m = st["monster"]
        if action == "flee" and not m["boss"]:
            if rng.random() < 0.3 + 0.55 * _n(st, s["mob"]):
                log.append("🏃 무사히 도망쳤어요! (보상 없음)")
            else:
                log.append("🏃 도망치다 등을 맞았어요!")
                _hurt(st, _damage(st, m["power"] * 0.5, rng, 1.15 - 0.5 * _n(st, s["dfn"])), log)
        else:
            # 공격이 높을수록 빨리 끝나고, 수비가 높을수록 덜 맞는다
            cut = (1.25 - 0.55 * _n(st, s["atk"])) * (1.15 - 0.5 * _n(st, s["dfn"]))
            dmg = _damage(st, m["power"] * 0.62, rng, cut)
            log.append(f"⚔️ **{m['name']}**을(를) 쓰러뜨렸어요!")
            _hurt(st, dmg, log)
            if st["hp"] > 0 or st.get("inv_feather"):
                if st.get("v311"):    # 3.1.1: 보스 1개 · 일반 전투는 더 가끔
                    rolls = 1 if m["boss"] else int(rng.random() < BATTLE_DROP_311)
                elif st.get("v2101"):   # 2.10.1: 보스 3 → 2개 · 일반 전투는 가끔만
                    rolls = 2 if m["boss"] else int(rng.random() < BATTLE_DROP)
                else:
                    rolls = 3 if m["boss"] else 1
                got = _roll_loot(st, rng, rolls, 0.6 if m["boss"] else 0.12 + 0.02 * st["floor"])
                log.append("🎁 " + loot_text(got))
        _after_room(st, rng, log)
    elif ph == "treasure":
        if action == "key" and st.get("inv_dkey"):
            st["inv_dkey"] -= 1
            st["used"]["dkey"] = st["used"].get("dkey", 0) + 1
            got = _roll_loot(st, rng, 1 if st.get("v311") else 2, 0.8)
            log.append("🗝️ 열쇠로 금고를 열었어요! 🎁 " + loot_text(got))
        elif rng.random() < 0.15 * (1 - 0.6 * _n(st, s["luck"])):
            log.append("😱 미믹이었어요!!")
            _hurt(st, _damage(st, 12 + st["floor"], rng, 1.0), log)
        else:
            rolls = int(rng.random() < CHEST_ITEM_311) if st.get("v311") else 1   # 3.1.1: 가끔은 돈만
            got = _roll_loot(st, rng, rolls, 0.2 + 0.02 * st["floor"])
            log.append("📦 상자를 열었어요! 🎁 " + loot_text(got))
        _after_room(st, rng, log)
    elif ph == "merchant":
        if action.startswith("buy:"):
            item = action[4:]
            st.setdefault("buys", []).append(item)   # 돈은 DB 에서 (실패하면 되돌린다)
            log.append(f"🧙 {item_name(item)}을(를) 샀어요.")
        else:
            log.append("🧙 상인에게 인사하고 지나갔어요.")
        _after_room(st, rng, log)
    elif ph == "altar":
        if action == "pray":
            _hurt(st, ALTAR_COST, log)
            if rng.random() < 0.6 + 0.2 * _n(st, s["luck"]):
                st["blessed"] = True
                log.append("✨ 축복! 다음 보물 · 전투 보상이 **2배**")
            else:
                st["cursed"] = True
                log.append("🌑 저주… 다음에 받는 피해 **+50%**")
        else:
            log.append("🕯️ 제단을 지나쳤어요.")
        _after_room(st, rng, log)
    return log


def drink_potion(st: dict) -> int:
    heal = min(POTION_HEAL, st["max_hp"] - st["hp"])
    st["hp"] += heal
    return heal


def reveal(st: dict) -> None:
    """📜 두루마리: 이번 층 문 뒤가 무엇인지 모두 보인다."""
    st["revealed"] = True


def settle(st: dict, rng) -> tuple[dict, int, int]:
    """끝낼 때 받을 것 (아이템, 돈, 훈련 경험치). 쓰러졌으면 절반 (아이템은 개수마다 반올림 랜덤)."""
    keep = DEATH_KEEP if st["phase"] == "dead" else 1.0
    items = {}
    for k, n in st["loot"].items():
        v = n * keep
        q = int(v) + (1 if rng.random() < v - int(v) else 0)
        if q:
            items[k] = q
    return items, int(st["coins"] * keep), int(st["xp"] * keep)


def item_name(key: str) -> str:
    from services.economy_db import ITEMS
    e, n, _ = ITEMS.get(key, ("❔", key, ""))
    return f"{e} {n}"


def loot_text(got: list[str]) -> str:
    out, coins = {}, 0
    for g in got:
        if g.startswith("coins:"):
            coins += int(g[6:])
        else:
            out[g] = out.get(g, 0) + 1
    parts = [item_name(k) + (f" ×{n}" if n > 1 else "") for k, n in out.items()]
    if coins:
        parts.append(f"💰 {coins:,}원")
    return " · ".join(parts) or "아무것도 없었어요"


class DungeonDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS dungeon_runs (user_id INTEGER PRIMARY KEY, state TEXT, started_ts INTEGER, "
                        "last_ts INTEGER, day_key INTEGER)")
            con.execute("CREATE TABLE IF NOT EXISTS dungeon_daily (user_id INTEGER, day_key INTEGER, runs INTEGER, extra INTEGER, "
                        "PRIMARY KEY(user_id, day_key))")
            con.execute("CREATE TABLE IF NOT EXISTS dungeon_best (user_id INTEGER, week INTEGER, floor INTEGER, ts INTEGER, "
                        "PRIMARY KEY(user_id, week))")
            con.execute("CREATE TABLE IF NOT EXISTS dungeon_clears (user_id INTEGER, theme INTEGER, ts INTEGER, "
                        "PRIMARY KEY(user_id, theme))")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _qty(con, uid, item) -> int:
        r = con.execute("SELECT qty FROM inventory WHERE user_id=? AND item=?", (uid, item)).fetchone()
        return int(r[0]) if r else 0

    @staticmethod
    def _daily(con, uid, day) -> tuple[int, int]:
        r = con.execute("SELECT runs, extra FROM dungeon_daily WHERE user_id=? AND day_key=?", (uid, day)).fetchone()
        return (int(r[0]), int(r[1])) if r else (0, 0)

    async def today(self, uid: int, now_ts: int) -> dict:
        """{runs, allowed, active(state or None)}."""
        day = _kst_day(now_ts)

        def fn(con):
            runs, extra = self._daily(con, uid, day)
            row = con.execute("SELECT state, last_ts FROM dungeon_runs WHERE user_id=?", (uid,)).fetchone()
            active = json.loads(row[0]) if row else None
            if active:
                self._load_inv(con, uid, active)
            return {"runs": runs, "allowed": 1 + extra, "active": active,
                    "last_ts": row[1] if row else None, "tickets": self._qty(con, uid, "dungeon_ticket"),
                    "torch": self._qty(con, uid, "torch")}
        return await self._tx(fn)

    async def use_ticket(self, uid: int, now_ts: int) -> bool:
        day = _kst_day(now_ts)

        def fn(con):
            if not con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='dungeon_ticket' AND qty>0",
                               (uid,)).rowcount:
                return False
            con.execute("INSERT INTO dungeon_daily(user_id, day_key, runs, extra) VALUES(?,?,0,1) "
                        "ON CONFLICT(user_id, day_key) DO UPDATE SET extra = extra + 1", (uid, day))
            return True
        return await self._tx(fn)

    async def start(self, uid: int, stats: dict, now_ts: int, use_torch: bool, rng=random, new: bool = True,
                    v311: bool = False) -> dict:
        """입장. reason: active(진행 중) / done(오늘 다 함)."""
        day = _kst_day(now_ts)

        def fn(con):
            if con.execute("SELECT 1 FROM dungeon_runs WHERE user_id=?", (uid,)).fetchone():
                return {"ok": False, "reason": "active"}
            runs, extra = self._daily(con, uid, day)
            if runs >= 1 + extra:
                return {"ok": False, "reason": "done"}
            torch = use_torch and con.execute(
                "UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='torch' AND qty>0", (uid,)).rowcount > 0
            st = new_run(stats, theme_of(now_ts), rng, torch=torch, meal=_cook_buff(con, uid, now_ts, "eel_rice"), new=new,
                         v311=v311)
            if torch:
                st["used"]["torch"] = 1
            self._load_inv(con, uid, st)
            con.execute("INSERT INTO dungeon_daily(user_id, day_key, runs, extra) VALUES(?,?,1,0) "
                        "ON CONFLICT(user_id, day_key) DO UPDATE SET runs = runs + 1", (uid, day))
            con.execute("INSERT INTO dungeon_runs VALUES(?,?,?,?,?)", (uid, json.dumps(st, ensure_ascii=False), now_ts, now_ts, day))
            return {"ok": True, "state": st}
        return await self._tx(fn)

    def _load_inv(self, con, uid, st):
        """가방에 있는 던전 아이템을 상태에 비춰 둔다 (실제 소모는 쓸 때 DB 에서)."""
        st["inv_feather"] = self._qty(con, uid, "feather") > 0
        st["inv_antidote"] = self._qty(con, uid, "antidote") > 0
        st["inv_dkey"] = self._qty(con, uid, "dkey")
        st["inv_potion"] = self._qty(con, uid, "potion")
        st["inv_scroll"] = self._qty(con, uid, "scroll")

    async def step(self, uid: int, now_ts: int, fn_state, rng=random) -> dict:
        """진행 중 상태를 꺼내 fn_state(st, con) → log 를 돌리고 저장. 끝났으면(clear/dead) 정산까지.
        reason: none(진행 중 아님)."""
        def fn(con):
            row = con.execute("SELECT state FROM dungeon_runs WHERE user_id=?", (uid,)).fetchone()
            if not row:
                return {"ok": False, "reason": "none"}
            st = json.loads(row[0])
            self._load_inv(con, uid, st)
            log = fn_state(st, con)
            if log is None:
                return {"ok": False, "reason": "invalid", "state": st}
            self._consume(con, uid, st)
            out = {"ok": True, "state": st, "log": log}
            if st["phase"] in ("clear", "dead"):
                out["result"] = self._finish(con, uid, st, now_ts, rng)
            else:
                con.execute("UPDATE dungeon_runs SET state=?, last_ts=? WHERE user_id=?",
                            (json.dumps(st, ensure_ascii=False), now_ts, uid))
            return out
        return await self._tx(fn)

    def _consume(self, con, uid, st):
        """이번 걸음에서 쓴 아이템을 가방에서 뺀다 (used 에 쌓인 것 중 아직 안 뺀 것)."""
        done = st.setdefault("consumed", {})
        for k, n in st["used"].items():
            extra = n - done.get(k, 0)
            if extra > 0:
                con.execute("UPDATE inventory SET qty = MAX(0, qty - ?) WHERE user_id=? AND item=?", (extra, uid, k))
                done[k] = n

    def _finish(self, con, uid, st, now_ts, rng) -> dict:
        items, coins, xp = settle(st, rng)
        first = False
        if st["phase"] == "clear" and con.execute("INSERT OR IGNORE INTO dungeon_clears VALUES(?,?,?)",
                                                  (uid, st["theme"], now_ts)).rowcount:
            first = True
            for k, n in (FIRST_CLEAR if st.get("v2101") else OLD_FIRST_CLEAR).items():
                items[k] = items.get(k, 0) + n
        for k, n in items.items():
            give_item(con, uid, k, n)
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
        con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (coins, uid))
        if xp:
            con.execute("INSERT OR IGNORE INTO training(user_id, last_play_ts) VALUES(?, 0)", (uid,))
            lv, cur = con.execute("SELECT level, xp FROM training WHERE user_id=?", (uid,)).fetchone()
            lv, cur, _ = grind_add_xp("training", int(lv), int(cur), xp)
            con.execute("UPDATE training SET level=?, xp=? WHERE user_id=?", (lv, cur, uid))
        reached = st["floor"] if st["phase"] != "clear" else FLOORS
        wk = week_key(now_ts)
        con.execute("INSERT INTO dungeon_best VALUES(?,?,?,?) ON CONFLICT(user_id, week) DO UPDATE SET "
                    "floor = MAX(floor, excluded.floor), ts = CASE WHEN excluded.floor > floor THEN excluded.ts ELSE ts END",
                    (uid, wk, reached, now_ts))
        con.execute("DELETE FROM dungeon_runs WHERE user_id=?", (uid,))
        bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])
        return {"items": items, "coins": coins, "xp": xp, "first": first, "floor": reached, "phase": st["phase"],
                "balance": bal}

    async def retreat(self, uid: int, now_ts: int, rng=random) -> dict:
        """[귀환] — 보상 전부. reason: none / busy(방 안에서는 귀환 불가 — 방을 끝내고)."""
        def fn(con):
            row = con.execute("SELECT state FROM dungeon_runs WHERE user_id=?", (uid,)).fetchone()
            if not row:
                return {"ok": False, "reason": "none"}
            st = json.loads(row[0])
            if st["phase"] != "doors":
                return {"ok": False, "reason": "busy"}
            st["phase"] = "return"
            st["floor"] = max(0, st["floor"] - 1)   # 다 깬 층까지가 기록
            return {"ok": True, "state": st, "result": self._finish(con, uid, st, now_ts, rng)}
        return await self._tx(fn)

    async def auto_return(self, now_ts: int, rng=random) -> list[tuple[int, dict]]:
        """IDLE_RETURN 동안 아무것도 안 한 던전은 귀환 처리 (방 안이었으면 그 방은 없던 일로)."""
        def fn(con):
            out = []
            for uid, raw in con.execute("SELECT user_id, state FROM dungeon_runs WHERE last_ts < ?",
                                        (now_ts - IDLE_RETURN,)).fetchall():
                st = json.loads(raw)
                st["phase"] = "return"
                st["floor"] = max(0, st["floor"] - 1)
                out.append((uid, self._finish(con, uid, st, now_ts, rng)))
            return out
        return await self._tx(fn)

    async def merchant_buy(self, uid: int, item: str, now_ts: int, rng=random) -> dict:
        """상인에게 사기 — 돈을 내고 바로 가방에. reason: balance / invalid."""
        price = MERCHANT.get(item)

        def fn(con):
            row = con.execute("SELECT state FROM dungeon_runs WHERE user_id=?", (uid,)).fetchone()
            if not row or price is None:
                return {"ok": False, "reason": "invalid"}
            st = json.loads(row[0])
            if st["phase"] != "merchant" or item not in st.get("offers", []):
                return {"ok": False, "reason": "invalid"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])
            if bal < price:
                return {"ok": False, "reason": "balance", "balance": bal}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (price, uid))
            give_item(con, uid, item)
            st["offers"].remove(item)
            con.execute("UPDATE dungeon_runs SET state=?, last_ts=? WHERE user_id=?", (json.dumps(st, ensure_ascii=False), now_ts, uid))
            return {"ok": True, "balance": bal - price, "state": st}
        return await self._tx(fn)

    async def weekly_top(self, now_ts: int, limit: int = 5) -> list[tuple[int, int]]:
        wk = week_key(now_ts)
        return await self._tx(lambda con: [(u, f) for u, f in con.execute(
            "SELECT user_id, floor FROM dungeon_best WHERE week=? ORDER BY floor DESC, ts ASC LIMIT ?", (wk, limit))])

    async def clears(self, uid: int) -> set[int]:
        return await self._tx(lambda con: {t for (t,) in con.execute("SELECT theme FROM dungeon_clears WHERE user_id=?", (uid,))})


def simulate(stats: dict, n: int = 2000, rng=None, policy: str = "greedy", new: bool = True, v311: bool = False) -> dict:
    """밸런스 확인용: 정해진 방식으로 끝까지(체력 35 아래면 귀환). {clear, dead, avg_floor, avg_items, avg_coins}."""
    rng = rng or random.Random(7)
    res = {"clear": 0, "dead": 0, "floor": 0, "items": 0, "coins": 0}
    for _ in range(n):
        st = new_run(stats, rng.randrange(7), rng, new=new, v311=v311)
        while st["phase"] not in ("clear", "dead", "return"):
            if st["phase"] == "doors":
                if st["hp"] < 35 and st["floor"] not in BOSS_FLOORS:
                    st["phase"] = "return"
                    st["floor"] -= 1
                    break
                enter(st, rng.randrange(len(st["doors"])), rng)
            elif st["phase"] == "battle":
                act(st, "fight", rng)
            elif st["phase"] == "treasure":
                act(st, "open", rng)
            elif st["phase"] == "merchant":
                act(st, "leave", rng)
            elif st["phase"] == "altar":
                act(st, "leave", rng)
        items, coins, _ = settle(st, rng)
        res["clear"] += st["phase"] == "clear"
        res["dead"] += st["phase"] == "dead"
        res["floor"] += FLOORS if st["phase"] == "clear" else st["floor"]
        res["items"] += sum(items.values())
        res["coins"] += coins
    return {k: (v / n) for k, v in res.items()}


def now() -> int:
    return int(time.time())


assert sum(v[2] for v in ROOMS.values()) == 100
