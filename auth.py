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


ADMIN_GROUP = "관리자명령어"


def group_owner_commands(tree) -> app_commands.Group:
    """관리자(owner_only) 명령어를 /관리자명령어 <이름> 하나로 묶는다. 동기화 직전에 부른다.
    그룹은 서버 관리자에게만 / 목록에 보이고, 하위 명령어 실행은 여전히 봇 주인만 된다.
    cog 를 다시 불러오면 새 명령어가 다시 맨 위에 생기므로, 부를 때마다 옮겨 담는다(같은 이름은 교체)."""
    group = tree.get_command(ADMIN_GROUP)
    if group is None:
        group = app_commands.Group(name=ADMIN_GROUP, description="(봇 주인 전용) 관리자 명령어 모음",
                                   default_permissions=discord.Permissions(administrator=True), guild_only=True)
        tree.add_command(group)
    for cmd in [c for c in tree.get_commands() if isinstance(c, app_commands.Command) and is_owner_command(c)]:
        tree.remove_command(cmd.name)
        group.add_command(cmd, override=True)
    return group
