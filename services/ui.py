# services/ui.py — 미니게임 임베드 공통 디자인 (색 · 게이지 · 카드)
import discord

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


def card(title: str, desc: str = "", color: int = INFO, user=None, section: str = "") -> discord.Embed:
    """모든 미니게임 임베드의 뼈대: 작성자 줄에 '닉네임 · 섹션'."""
    e = discord.Embed(title=title, description=desc, color=color)
    if user is not None:
        e.set_author(name=f"{user.display_name} · {section}" if section else user.display_name,
                     icon_url=user.display_avatar.url)
    return e
