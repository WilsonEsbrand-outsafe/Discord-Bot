# services/usage.py — 3.9.2 명령어 · 버튼 사용 기록 (11/23 규완님 '돈 흐름 기록' + 안 쓰는 명령어 찾기).
# bot.py 가 상호작용마다 record 하고, cogs/money.py 가 1분마다 take 해서 DB(cmd_log)에 넣는다.
import time
import warnings
from typing import Optional

_buf: list[tuple[int, str, int]] = []


def name_of(interaction) -> str:
    """/ 명령어는 이름, 버튼 · 메뉴 · 창은 '버튼:<그 화면을 연 명령어>'."""
    cmd = getattr(interaction, "command", None)
    if cmd is not None and getattr(interaction, "type", None) is not None and interaction.type.name == "application_command":
        return getattr(cmd, "qualified_name", "") or "?"
    msg = getattr(interaction, "message", None)
    with warnings.catch_warnings():   # 명령어 이름은 옛 msg.interaction 에만 있다 (interaction_metadata 엔 없다)
        warnings.simplefilter("ignore", DeprecationWarning)
        meta = getattr(msg, "interaction", None)
    src = getattr(meta, "name", None) or getattr(cmd, "qualified_name", None) or "?"
    return f"버튼:{str(src).split(' ')[0]}"


def record(user_id: int, name: str, now: Optional[float] = None) -> None:
    if len(_buf) < 100_000:
        _buf.append((int(user_id), str(name)[:40], int(time.time() if now is None else now)))


def take() -> list[tuple[int, str, int]]:
    global _buf
    out, _buf = _buf, []
    return out
