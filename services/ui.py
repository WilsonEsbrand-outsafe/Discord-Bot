# services/ui.py — 미니게임 임베드 공통 디자인 (색 · 게이지 · 카드)
import random

import discord

import release

# 칭호 (2.8 창단 멤버 등): user_id → 문구. EconomyDB 가 켜질 때 DB에서 채운다.
TITLES: dict[int, str] = {}
# 도박 계열 결과 화면 — 여기 섹션의 카드에는 이름 옆에 칭호를 항상 붙인다.
GAMBLE_SECTIONS = {
    "🎙️ 경마 중계", "🎙️ 달팽이 레이스 중계", "🎙️ 조정 중계", "🎙️ F1 중계", "🎙️ 양궁 중계",
    "🎙️ 페널티킥 중계", "🎙️ 야구 중계", "🎙️ 농구 중계", "🎙️ UFC 중계", "🎙️ 리그 중계", "🎰 토토",
}

# 결과별 색. 연출 중(DARK)은 선수팩 개봉 화면과 같은 색을 쓴다.
WIN  = 0x2ECC71   # 이득
LOSE = 0xE74C3C   # 손실
GOLD = 0xF1C40F   # 대박 · 대성공 · 레벨업
EVEN = 0x95A5A6   # 본전
INFO = 0x5865F2   # 안내 · 진행
DARK = 0x2B2D31   # 연출 중
DOOM = 0x992D22   # 대참사
KIKO = 0x3D7BE0   # 2.10.3 키코 디자인의 안내 색 (로고 파랑)

# 2.10.3 키코: 축구 봇 로고 속 캐릭터. 푸터 아이콘은 봇 프로필 사진(on_ready 에서 채운다).
BOT_ICON: str | None = None
KIKO_LINES = {
    WIN:  ("좋아요, 이득이에요! ⚽", "역시 감이 좋으시네요!", "이 기세 그대로 가요!"),
    LOSE: ("아깝네요… 다음엔 꼭!", "괜찮아요, 경기는 또 있어요.", "키코도 속상해요 😢"),
    GOLD: ("대박이에요!! 🎉", "와, 이건 하이라이트감이에요!", "골인! 골인이에요!"),
    EVEN: ("본전이면 선방이에요.", "무승부도 소중한 승점이죠."),
    DOOM: ("으악… 이건 키코도 못 막았어요 😱", "오늘은 쉬어 가는 날로 해요…"),
    KIKO: ("키코가 도와드릴게요!", "오늘도 즐거운 축구 해요 ⚽"),
}


def bar(cur: float, total: float, width: int = 10) -> str:
    """▰▰▰▱▱▱ 게이지."""
    filled = round(width * max(0.0, min(cur, total)) / max(total, 1))
    return "▰" * filled + "▱" * (width - filled)


def won(n: int) -> str:
    """부호 붙은 금액: +12,000원 / -3,000원 / ±0원"""
    return f"{n:+,}원" if n else "±0원"


def tone(delta: int) -> int:
    return WIN if delta > 0 else (LOSE if delta < 0 else EVEN)


def emoji_url(emoji: str) -> str:
    """이모지를 큰 그림(Twemoji PNG)으로 — 임베드 썸네일용."""
    code = "-".join(f"{ord(c):x}" for c in emoji if c != "️")
    return f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{code}.png"


KIKO_POSE = {GOLD: "gold", WIN: "win", EVEN: "even", LOSE: "lose", DOOM: "doom"}
KIKO_ART = "https://raw.githubusercontent.com/WilsonEsbrand-outsafe/Discord-Bot/main/assets/kiko/{}.png"
# 결과마다 그 장면을 연기하는 키코 그림이 있는 게임: assets/kiko/scenes/{게임}_{결과 번호}.png (번호 = 결과표 순서)
KIKO_SCENES = {"pk": 10, "bat": 12, "hoop": 12, "ufc": 13, "race": 4}


def result_thumb(emoji: str, color: int, user=None, scene: str | None = None) -> str:
    """게임 결과 썸네일 (2.10.3): 공개 서버에선 그 결과 장면의 키코 그림(scene) → 없으면 결과 색에 맞는 키코 표정 → 아니면 이모지."""
    guild = getattr(user, "guild", None)
    if not release.preview_2103(getattr(guild, "id", None)):
        return emoji_url(emoji)
    game, _, idx = (scene or "").rpartition("_")
    if idx.isdigit() and int(idx) < KIKO_SCENES.get(game, 0):
        return KIKO_ART.format(f"scenes/{scene}")
    return KIKO_ART.format(KIKO_POSE[color]) if color in KIKO_POSE else emoji_url(emoji)


def titled(user) -> str:
    """칭호가 있으면 '🏅 창단 멤버 닉네임' (2.8 — 공개 전엔 테스트 서버에서만)."""
    t = TITLES.get(getattr(user, "id", None))
    guild = getattr(user, "guild", None)
    if t and release.preview(getattr(guild, "id", None)):
        return f"{t} {user.display_name}"
    return user.display_name


class KikoEmbed(discord.Embed):
    """키코 디자인 카드: 따로 푸터를 달아도 아이콘은 키코(봇 프로필)로 남는다."""

    def set_footer(self, *, text=None, icon_url=None):
        return super().set_footer(text=text, icon_url=icon_url or BOT_ICON)


def kiko_line(color: int) -> str:
    return random.choice(KIKO_LINES.get(color, KIKO_LINES[KIKO]))


def card(title: str, desc: str = "", color: int = INFO, user=None, section: str = "") -> discord.Embed:
    """모든 미니게임 임베드의 뼈대: 작성자 줄에 '닉네임 · 섹션'. 도박 계열이면 이름 옆에 칭호.
    2.10.3 공개 서버에선 키코 디자인: 안내 색은 로고 파랑, 아래에 키코 한마디 (화면이 푸터를 쓰면 아이콘만 키코)."""
    guild = getattr(user, "guild", None)
    if release.preview_2103(getattr(guild, "id", None)):
        color = KIKO if color == INFO else color
        e = KikoEmbed(title=title, description=desc, color=color)
        e.set_footer(text=f"키코 · {kiko_line(color)}")
    else:
        e = discord.Embed(title=title, description=desc, color=color)
    if user is not None:
        name = titled(user) if section in GAMBLE_SECTIONS else user.display_name
        e.set_author(name=f"{name} · {section}" if section else name, icon_url=user.display_avatar.url)
    return e
