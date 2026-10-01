from __future__ import annotations

import os
from pathlib import Path
import discord
from dotenv import load_dotenv

# .env 로드
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OWNER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))

async def owner_only(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID

async def owner_only_error(interaction: discord.Interaction, error: Exception):
    raise error


def is_owner_command(cmd) -> bool:
    """@app_commands.check(owner_only) 가 걸린 명령어인지."""
    return any(getattr(c, "__name__", "") == "owner_only" for c in getattr(cmd, "checks", []))


def hide_owner_commands(tree) -> int:
    """관리자(owner_only) 명령어를 / 목록에서 숨긴다 — 서버 관리자 권한이 있는 사람에게만 보이고,
    실행은 여전히 봇 주인만 된다. 동기화 직전에 부른다. 숨긴 개수를 돌려준다."""
    hidden = [cmd for cmd in tree.get_commands() if is_owner_command(cmd)]
    for cmd in hidden:
        cmd.default_permissions = discord.Permissions(administrator=True)
    return len(hidden)
