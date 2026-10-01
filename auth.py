from __future__ import annotations

import os
from pathlib import Path
import discord
from discord import app_commands
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


# /관리자명령어 드롭다운에서 실행할 관리자 명령어 {이름: 명령어}. cog 리로드에도 살아남도록 여기(auth)에 둔다.
ADMIN_COMMANDS: dict[str, app_commands.Command] = {}


def collect_owner_commands(tree) -> int:
    """관리자(owner_only) 명령어를 / 목록에서 빼서 /관리자명령어 메뉴(ADMIN_COMMANDS)로 옮긴다. 동기화 직전에 부른다.
    cog 를 다시 불러오면 새 명령어가 다시 맨 위에 생기므로, 부를 때마다 옮겨 담는다(같은 이름은 교체)."""
    for cmd in [c for c in tree.get_commands() if isinstance(c, app_commands.Command) and is_owner_command(c)]:
        tree.remove_command(cmd.name)
        ADMIN_COMMANDS[cmd.name] = cmd
    return len(ADMIN_COMMANDS)
