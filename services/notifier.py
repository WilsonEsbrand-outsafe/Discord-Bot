# services/notifier.py
"""
DM 알림 유틸리티
- send_notify(): 유저 알림 설정 확인 후 DM 전송
"""
from __future__ import annotations
import discord

# 알림 이벤트 키 → 표시 이름
NOTIFY_EVENTS: dict[str, str] = {
    "매물_판매":    "🏷️ 이적시장 매물 판매됨",
    "매물_만료":    "⏰ 이적시장 매물 만료",
    "트레이드_수신": "🤝 트레이드 제안 수신",
    "트레이드_결과": "🤝 트레이드 수락/거절 결과",
    "선수_성장":    "📈 선수 OVR 성장",
    "선수_은퇴":    "💀 선수 은퇴",
    "토토_결과":    "⚽ 토토 정산",
    "송금_수신":    "💸 송금 수신",
}


async def send_notify(
    bot: discord.Client,
    db,               # EconomyDB instance
    user_id: int,
    event_key: str,
    embed: discord.Embed,
) -> None:
    """알림 설정 확인 후 DM 전송. 실패해도 조용히 무시."""
    try:
        if not await db.notify_enabled(user_id, event_key):
            return
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(embed=embed)
    except (discord.Forbidden, discord.NotFound):
        pass
    except Exception:
        pass
