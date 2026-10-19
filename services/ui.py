# services/ui.py — 미니게임 임베드 공통 디자인 (색 · 게이지 · 카드)
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


def titled(user) -> str:
    """칭호가 있으면 '🏅 창단 멤버 닉네임' (2.8 — 공개 전엔 테스트 서버에서만)."""
    t = TITLES.get(getattr(user, "id", None))
    guild = getattr(user, "guild", None)
    if t and release.preview(getattr(guild, "id", None)):
        return f"{t} {user.display_name}"
    return user.display_name


def card(title: str, desc: str = "", color: int = INFO, user=None, section: str = "") -> discord.Embed:
    """모든 미니게임 임베드의 뼈대: 작성자 줄에 '닉네임 · 섹션'. 도박 계열이면 이름 옆에 칭호."""
    e = discord.Embed(title=title, description=desc, color=color)
    if user is not None:
        name = titled(user) if section in GAMBLE_SECTIONS else user.display_name
        e.set_author(name=f"{name} · {section}" if section else name, icon_url=user.display_avatar.url)
    return e
