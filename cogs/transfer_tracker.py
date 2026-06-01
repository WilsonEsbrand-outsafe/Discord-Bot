# cogs/transfer_tracker.py
# 실시간 이적 트래커 — RSS 폴링 후 자동 채널 전송

import os
import re
import time
import asyncio
import logging
from html import unescape

import discord
from discord.ext import commands, tasks
import aiohttp
import feedparser

from services.transfer_db import TransferDB

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 소스 목록
# ─────────────────────────────────────────
FEED_SOURCES = [
    {
        "name": "Fabrizio Romano",
        "url": "https://fabrizio.substack.com/feed",
        "color": 0x1DA1F2,
        "emoji": "🔵",
    },
    {
        "name": "The Guardian · Transfers",
        "url": "https://www.theguardian.com/football/transfers/rss",
        "color": 0x005689,
        "emoji": "🔵",
    },
    {
        "name": "Gianluca Di Marzio",
        "url": "https://www.gianlucadimarzio.com/en/feed",
        "color": 0x009246,
        "emoji": "🟢",
    },
    {
        "name": "Sky Sports · Transfers",
        "url": "https://www.skysports.com/rss/12040",
        "color": 0xE8003D,
        "emoji": "🔴",
    },
    {
        "name": "BBC Sport · Football",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "color": 0xBB1919,
        "emoji": "🟥",
        "filter_keywords": True,   # 일반 경기 기사 제외, 이적 관련만
    },
]

# 이적 관련 키워드 — 하나라도 포함되어야 전송
TRANSFER_KEYWORDS = {
    "transfer", "sign", "signing", "signed", "loan", "deal", "move",
    "join", "joins", "fee", "here we go", "agreement", "medical",
    "contract", "bid", "sell", "depart", "exit", "swap", "permanent",
    "release", "bought", "official", "completed", "announce", "unveiled",
    "done deal", "confirmed", "seal", "sealed",
}

POLL_MINUTES = 7           # 폴링 주기
MAX_PER_SOURCE = 10        # 소스당 한 번에 최대 전송 개수
MAX_AGE_HOURS = 24         # 24시간 이내 기사만 처리
_HTML_TAG = re.compile(r"<[^>]+>")


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────

def _strip_html(text: str) -> str:
    return unescape(_HTML_TAG.sub(" ", text or "")).strip()


def _is_transfer_news(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in TRANSFER_KEYWORDS)


def _is_recent(entry) -> bool:
    """RSS 항목이 MAX_AGE_HOURS 이내인지 확인."""
    published = getattr(entry, "published_parsed", None)
    if not published:
        return True  # 날짜 없으면 허용
    age = time.time() - time.mktime(published)
    return age < MAX_AGE_HOURS * 3600


async def _translate(text: str, session: aiohttp.ClientSession) -> str:
    """Google Translate 비공식 endpoint로 한국어 번역. 실패 시 원문 반환."""
    if not text:
        return ""
    try:
        async with session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text[:500]},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return text
            data = await resp.json(content_type=None)
            return "".join(part[0] for part in data[0] if part[0]).strip()
    except Exception as exc:
        log.debug("번역 실패: %s", exc)
        return text


async def _fetch_entries(session: aiohttp.ClientSession, url: str) -> list:
    """RSS 피드를 가져와 feedparser entries 반환. 실패 시 빈 리스트."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("RSS %s → HTTP %s", url, resp.status)
                return []
            content = await resp.text()
    except Exception as exc:
        log.warning("RSS 요청 실패 %s: %s", url, exc)
        return []
    return feedparser.parse(content).entries


# ─────────────────────────────────────────
# Cog
# ─────────────────────────────────────────

class TransferTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = TransferDB()
        self.channel_id = int(os.getenv("TRANSFER_CHANNEL_ID", 0))
        self._first_run = True
        self._poll.start()

    def cog_unload(self):
        self._poll.cancel()

    # ── 백그라운드 폴링 ──────────────────────
    @tasks.loop(minutes=POLL_MINUTES)
    async def _poll(self):
        if not self.channel_id:
            return

        channel = self.bot.get_channel(self.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (TransferTrackerBot/1.0)"}
        ) as session:
            for source in FEED_SOURCES:
                try:
                    await self._process_source(channel, session, source)
                except Exception as exc:
                    log.error("소스 처리 오류 [%s]: %s", source["name"], exc)
                await asyncio.sleep(2)

        if self._first_run:
            self._first_run = False

    @_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    # ── 소스별 처리 ─────────────────────────
    async def _process_source(
        self,
        channel: discord.TextChannel,
        session: aiohttp.ClientSession,
        source: dict,
    ) -> None:
        entries = await _fetch_entries(session, source["url"])
        to_post = []

        for entry in entries:
            url = entry.get("link", "").strip()
            if not url:
                continue

            # 첫 기동 시 기존 기사는 seen만 등록하고 전송 안 함
            if self._first_run:
                self.db.mark_seen(url)
                continue

            if self.db.is_seen(url):
                continue
            if not _is_recent(entry):
                continue

            title = _strip_html(entry.get("title", ""))
            summary = _strip_html(entry.get("summary", entry.get("description", "")))[:400]

            # BBC Sport은 일반 경기 기사가 섞이므로 최소 필터 유지
            # 나머지 소스는 이미 이적 전문 피드라 필터 없이 전부 수집
            if source.get("filter_keywords") and not _is_transfer_news(title, summary):
                continue

            to_post.append({"url": url, "title": title, "summary": summary})
            if len(to_post) >= MAX_PER_SOURCE:
                break

        for article in to_post:
            await self._send_article(channel, session, source, article)
            await asyncio.sleep(1.5)

    # ── 임베드 전송 ─────────────────────────
    async def _send_article(
        self,
        channel: discord.TextChannel,
        session: aiohttp.ClientSession,
        source: dict,
        article: dict,
    ) -> None:
        title_ko = await _translate(article["title"], session)
        summary_ko = await _translate(article["summary"], session) if article["summary"] else ""

        # 제목이 이미 한국어거나 번역 실패면 원문 그대로
        display_title = title_ko or article["title"]

        desc_parts: list[str] = []
        # 원문 제목 (번역과 다를 때만)
        if title_ko and title_ko != article["title"]:
            desc_parts.append(f"🌐 *{article['title']}*")
        # 한국어 요약
        if summary_ko:
            desc_parts.append(summary_ko)
        # 원문 요약 (번역과 다를 때만, 200자 이내)
        if article["summary"] and summary_ko != article["summary"]:
            desc_parts.append(f"> *{article['summary'][:200]}*")

        embed = discord.Embed(
            title=display_title[:256],
            url=article["url"],
            description="\n\n".join(desc_parts)[:4096],
            color=source["color"],
        )
        embed.set_footer(text=f"{source['emoji']} {source['name']}")

        try:
            await channel.send(embed=embed)
            self.db.mark_seen(article["url"])
            log.info("이적 소식 전송: %s", article["title"][:60])
        except discord.Forbidden:
            log.error("채널 전송 권한 없음: %s", channel.id)
        except Exception as exc:
            log.error("전송 실패: %s", exc)


async def setup(bot: commands.Bot):
    await bot.add_cog(TransferTracker(bot))
