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
