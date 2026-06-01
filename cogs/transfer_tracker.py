# cogs/transfer_tracker.py
# 실시간 이적 트래커 — RSS 폴링 후 채널별 자동 전송
#
# 채널 라우팅:
#   RUMOR   ← 일반 루머/가십/링크
#   HWG     ← Romano 피드에서 "Here We Go" 포함 기사만
#   OFFICIAL← 클럽 공식 발표 RSS + 강한 확정 키워드 기사

import os
import re
import time
import asyncio
import logging
from html import unescape

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import feedparser

from services.transfer_db import TransferDB
from auth import OWNER_ID

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 소스 목록
# ─────────────────────────────────────────
FEED_SOURCES = [
    # ── 1군: 공신력 최상 ──────────────────────────────
    {
        "name": "Fabrizio Romano",
        "url": "https://fabrizio.substack.com/feed",
        "color": 0x1DA1F2,
        "emoji": "🔵",
        "is_romano": True,   # HWG 감지 대상
    },
    {
        "name": "The Guardian · Football",
        "url": "https://www.theguardian.com/football/rss",
        "color": 0x005689,
        "emoji": "🔵",
        "filter_keywords": True,
    },
    {
        "name": "Gianluca Di Marzio",
        "url": "https://www.gianlucadimarzio.com/feed",
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
        "filter_keywords": True,
    },
    # ── 2군: 이적 전문 / 루머 ─────────────────────────
    {
        "name": "TEAMtalk",
        "url": "https://www.teamtalk.com/rss",
        "color": 0xFF6600,
        "emoji": "🟠",
    },
    {
        "name": "Football Insider",
        "url": "https://www.footballinsider247.com/feed/",
        "color": 0x6A0DAD,
        "emoji": "🟣",
    },
    {
        "name": "CaughtOffside",
        "url": "https://www.caughtoffside.com/feed/",
        "color": 0x222222,
        "emoji": "⚫",
    },
    {
        "name": "90min · Transfers",
        "url": "https://www.90min.com/posts.rss",
        "color": 0x00C8FF,
        "emoji": "🔵",
        "filter_keywords": True,
    },
    # ── 3군: 영국 타블로이드 (루머 多) ───────────────────
    {
        "name": "Daily Mail · Football",
        "url": "https://www.dailymail.co.uk/sport/football/index.rss",
        "color": 0x004B87,
        "emoji": "🗞️",
        "filter_keywords": True,
    },
    {
        "name": "Mirror · Football",
        "url": "https://www.mirror.co.uk/sport/football/rss.xml",
        "color": 0xC8102E,
        "emoji": "🗞️",
        "filter_keywords": True,
    },
    {
        "name": "The Sun · Football",
        "url": "https://www.thesun.co.uk/sport/football/feed/",
        "color": 0xFF6600,
        "emoji": "🗞️",
        "filter_keywords": True,
    },
    {
        "name": "The Independent · Football",
        "url": "https://www.independent.co.uk/sport/football/rss",
        "color": 0xD0021B,
        "emoji": "🗞️",
        "filter_keywords": True,
    },
    {
        "name": "talkSPORT",
        "url": "https://talksport.com/football/feed/",
        "color": 0xFF4500,
        "emoji": "📻",
        "filter_keywords": True,
    },
    {
        "name": "GiveMeSport",
        "url": "https://www.givemesport.com/feed/",
        "color": 0x00AAFF,
        "emoji": "⚽",
        "filter_keywords": True,
    },
    # ── 4군: 유럽 현지 ───────────────────────────────
    {
        "name": "Football Italia",
        "url": "https://www.football-italia.net/rss.xml",
        "color": 0x008C45,
        "emoji": "🇮🇹",
    },
    {
        "name": "Marca (EN)",
        "url": "https://www.marca.com/en/rss/football.xml",
        "color": 0xFFCC00,
        "emoji": "🇪🇸",
        "filter_keywords": True,
    },
    {
        "name": "Mundo Deportivo (EN)",
        "url": "https://www.mundodeportivo.com/rss/home.xml",
        "color": 0x004FC3,
        "emoji": "🇪🇸",
        "filter_keywords": True,
    },
]

# ── 이적 관련 키워드 (filter_keywords 소스용) ────────
TRANSFER_KEYWORDS = {
    "transfer", "sign", "signing", "signed", "loan", "deal", "move",
    "join", "joins", "fee", "here we go", "agreement", "medical",
    "contract", "bid", "sell", "depart", "exit", "swap", "permanent",
    "release", "bought", "official", "completed", "announce", "unveiled",
    "done deal", "confirmed", "seal", "sealed", "interest", "target",
    "linked", "approach", "offer", "pursue", "want",
}

# ── 공식 확정 키워드 (OFFICIAL 채널 라우팅용) ─────────
OFFICIAL_KEYWORDS = {
    "officially announces", "officially confirmed", "has signed",
    "signs for", "completes move", "completes transfer", "unveiled as",
    "medical completed", "done deal", "permanent deal", "joins on",
    "seals move", "officially joins", "confirmed signing",
    "agree personal terms", "agreement reached",
}

POLL_MINUTES   = 7
MAX_PER_SOURCE = 10
MAX_AGE_HOURS  = 24
_HTML_TAG = re.compile(r"<[^>]+>")


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────

def _strip_html(text: str) -> str:
    return unescape(_HTML_TAG.sub(" ", text or "")).strip()


def _is_transfer_news(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in TRANSFER_KEYWORDS)


def _is_hwg(title: str, summary: str) -> bool:
    """Romano의 'Here We Go' 기사인지 확인."""
    return "here we go" in (title + " " + summary).lower()


def _is_official_news(title: str, summary: str) -> bool:
    """강한 확정 키워드가 포함된 공식 발표인지 확인."""
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in OFFICIAL_KEYWORDS)


def _is_recent(entry) -> bool:
    published = getattr(entry, "published_parsed", None)
    if not published:
        return True
    return (time.time() - time.mktime(published)) < MAX_AGE_HOURS * 3600


async def _translate(text: str, session: aiohttp.ClientSession) -> str:
    """Google Translate 비공식 endpoint — 실패 시 원문 반환."""
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
    """RSS 피드 → feedparser entries. 실패 시 빈 리스트."""
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
        self.db  = TransferDB()
        # 채널 ID (설정 안 된 경우 0 → 해당 채널 스킵)
        self.rumor_ch_id    = int(os.getenv("TRANSFER_RUMOR_CHANNEL_ID")    or 0)
        self.hwg_ch_id      = int(os.getenv("TRANSFER_HWG_CHANNEL_ID")      or 0)
        self.official_ch_id = int(os.getenv("TRANSFER_OFFICIAL_CHANNEL_ID") or 0)
        self._first_run = True
        self._poll.start()

    def cog_unload(self):
        self._poll.cancel()

    def _get_channel(self, ch_id: int) -> discord.TextChannel | None:
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    def _route(
        self, source: dict, title: str, summary: str
    ) -> discord.TextChannel | None:
        """기사를 어느 채널로 보낼지 결정."""
        # 1순위: Romano + HWG → HWG 채널
        if source.get("is_romano") and _is_hwg(title, summary):
            return self._get_channel(self.hwg_ch_id)
        # 2순위: 클럽 공식 피드 OR 강한 확정 키워드 → OFFICIAL 채널
        if source.get("is_official") or _is_official_news(title, summary):
            return self._get_channel(self.official_ch_id)
        # 기본: RUMOR 채널
        return self._get_channel(self.rumor_ch_id)

    # ── 백그라운드 폴링 ──────────────────────
    @tasks.loop(minutes=POLL_MINUTES)
    async def _poll(self):
        # 채널 하나라도 설정돼 있으면 실행
        if not any([self.rumor_ch_id, self.hwg_ch_id, self.official_ch_id]):
            return

        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (TransferTrackerBot/1.0)"}
        ) as session:
            for source in FEED_SOURCES:
                try:
                    await self._process_source(session, source)
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
        session: aiohttp.ClientSession,
        source: dict,
    ) -> None:
        entries = await _fetch_entries(session, source["url"])
        to_post = []

        for entry in entries:
            url = entry.get("link", "").strip()
            if not url:
                continue

            if self._first_run:
                self.db.mark_seen(url)
                continue

            if self.db.is_seen(url):
                continue
            if not _is_recent(entry):
                continue

            title   = _strip_html(entry.get("title", ""))
            summary = _strip_html(entry.get("summary", entry.get("description", "")))[:400]

            if source.get("filter_keywords") and not _is_transfer_news(title, summary):
                continue

            to_post.append({"url": url, "title": title, "summary": summary})
            if len(to_post) >= MAX_PER_SOURCE:
                break

        for article in to_post:
            channel = self._route(source, article["title"], article["summary"])
            if channel:
                await self._send_article(channel, session, source, article)
                await asyncio.sleep(1.5)
            else:
                # 채널 미설정이면 seen만 등록 (다음 폴링 때 중복 방지)
                self.db.mark_seen(article["url"])

    # ── 임베드 전송 ─────────────────────────
    async def _send_article(
        self,
        channel: discord.TextChannel,
        session: aiohttp.ClientSession,
        source: dict,
        article: dict,
    ) -> None:
        title_ko   = await _translate(article["title"],   session)
        summary_ko = await _translate(article["summary"], session) if article["summary"] else ""

        display_title = title_ko or article["title"]
        is_hwg_post   = source.get("is_romano") and _is_hwg(article["title"], article["summary"])

        desc_parts: list[str] = []
        if title_ko and title_ko != article["title"]:
            desc_parts.append(f"🌐 *{article['title']}*")
        if summary_ko:
            desc_parts.append(summary_ko)
        if article["summary"] and summary_ko != article["summary"]:
            desc_parts.append(f"> *{article['summary'][:200]}*")

        color = 0x00FF85 if is_hwg_post else source["color"]  # HWG는 초록 강조
        embed = discord.Embed(
            title=display_title[:256],
            url=article["url"],
            description="\n\n".join(desc_parts)[:4096],
            color=color,
        )

        if is_hwg_post:
            embed.set_author(name="✅ HERE WE GO! — Fabrizio Romano")
        else:
            embed.set_footer(text=f"{source['emoji']} {source['name']}")

        try:
            await channel.send(embed=embed)
            self.db.mark_seen(article["url"])
            log.info("[%s] %s", channel.name, article["title"][:60])
        except discord.Forbidden:
            log.error("채널 전송 권한 없음: %s", channel.id)
        except Exception as exc:
            log.error("전송 실패: %s", exc)


    # ── 수동 불러오기 ────────────────────────
    @app_commands.command(name="이적불러오기", description="최근 N시간 기사를 강제 수집해 채널에 올립니다. (관리자 전용)")
    @app_commands.describe(시간="몇 시간 전까지 불러올지 (기본 48)")
    async def transfer_fetch(self, interaction: discord.Interaction, 시간: int = 48):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ 관리자 전용 커맨드입니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        total = 0
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (TransferTrackerBot/1.0)"}
        ) as session:
            for source in FEED_SOURCES:
                entries = await _fetch_entries(session, source["url"])
                log.info("[진단] %s → %d개", source["name"], len(entries))
                for entry in entries:
                    url = entry.get("link", "").strip()
                    if not url:
                        continue

                    # seen 무시, 시간 필터만 적용
                    published = getattr(entry, "published_parsed", None)
                    if published:
                        age = time.time() - time.mktime(published)
                        if age > 시간 * 3600:
                            continue

                    title   = _strip_html(entry.get("title", ""))
                    summary = _strip_html(entry.get("summary", entry.get("description", "")))[:400]

                    if source.get("filter_keywords") and not _is_transfer_news(title, summary):
                        continue

                    channel = self._route(source, title, summary)
                    if channel:
                        await self._send_article(channel, session, source, {"url": url, "title": title, "summary": summary})
                        total += 1
                        await asyncio.sleep(1.5)

        await interaction.followup.send(f"✅ {시간}시간 이내 기사 **{total}개** 전송 완료\n소스별 결과는 서버 로그 확인", ephemeral=True)

    # ── 테스트 커맨드 ────────────────────────
    @app_commands.command(name="이적테스트", description="RUMOR·HWG·OFFICIAL 채널에 샘플 임베드를 각각 전송합니다. (관리자 전용)")
    async def transfer_test(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ 관리자 전용 커맨드입니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        samples = [
            {
                "channel_id": self.rumor_ch_id,
                "label": "RUMOR",
                "source": {"name": "TEAMtalk", "color": 0xFF6600, "emoji": "🟠", "is_romano": False},
                "article": {
                    "url": "https://www.teamtalk.com",
                    "title": "[테스트] Man United target £80m striker from Serie A",
                    "title_ko": "[테스트] 맨유, 세리에A 공격수 £8000만에 영입 노린다",
                    "summary_ko": "맨체스터 유나이티드가 이번 여름 이적 시장에서 세리에A 소속 공격수 영입을 위해 접촉을 시작한 것으로 알려졌다.",
                },
            },
            {
                "channel_id": self.hwg_ch_id,
                "label": "HWG",
                "source": {"name": "Fabrizio Romano", "color": 0x00FF85, "emoji": "🔵", "is_romano": True},
                "article": {
                    "url": "https://fabrizio.substack.com",
                    "title": "[테스트] Here We Go! Mbappé to Real Madrid, confirmed!",
                    "title_ko": "[테스트] Here We Go! 음바페, 레알 마드리드 이적 확정!",
                    "summary_ko": "파브리치오 로마노가 공식 확인했다. 킬리안 음바페가 레알 마드리드와 5년 계약에 합의했다.",
                },
            },
            {
                "channel_id": self.official_ch_id,
                "label": "OFFICIAL",
                "source": {"name": "Premier League · Official", "color": 0x3D195B, "emoji": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "is_official": True},
                "article": {
                    "url": "https://www.premierleague.com",
                    "title": "[테스트] Arsenal officially announce signing of midfielder",
                    "title_ko": "[테스트] 아스날, 미드필더 영입 공식 발표",
                    "summary_ko": "아스날 FC가 공식 홈페이지를 통해 미드필더 영입을 공식 발표했다. 선수는 4년 계약에 서명했다.",
                },
            },
        ]

        sent = []
        for s in samples:
            ch = self._get_channel(s["channel_id"])
            if not ch:
                sent.append(f"❌ {s['label']} — 채널 미설정")
                continue

            article = s["article"]
            is_hwg_post = s["source"].get("is_romano") and "here we go" in article["title"].lower()
            color = 0x00FF85 if is_hwg_post else s["source"]["color"]

            desc_parts = [f"🌐 *{article['title']}*", article["summary_ko"]]
            embed = discord.Embed(
                title=article["title_ko"][:256],
                url=article["url"],
                description="\n\n".join(desc_parts),
                color=color,
            )
            if is_hwg_post:
                embed.set_author(name="✅ HERE WE GO! — Fabrizio Romano")
            else:
                embed.set_footer(text=f"{s['source']['emoji']} {s['source']['name']}")

            try:
                await ch.send(embed=embed)
                sent.append(f"✅ {s['label']} → <#{s['channel_id']}>")
            except Exception as exc:
                sent.append(f"❌ {s['label']} — {exc}")

        await interaction.followup.send("\n".join(sent), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TransferTracker(bot))
