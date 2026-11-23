# cogs/money.py — 3.9.2 돈 관리: 큰 돈 세금(매일 00:00 KST) · 명령어 사용 기록 저장(1분마다) · 기록 30일 정리.
import time

from discord.ext import commands, tasks

import release
from services import lobby, usage
from services.economy_db import EconomyDB, _kst_day
from services.farm import FarmDB


class Money(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.farm = FarmDB()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @tasks.loop(minutes=1)
    async def loop(self):
        now = int(time.time())
        if release.released_392(now) and not getattr(self, "_redenom", False):
            try:
                n = await self.db.redenominate_392()
                self._redenom = True
                if n is not None:
                    print(f"[3.9.2] 리디노미네이션: 지갑 {n}개 1/100")
            except Exception as e:
                print(f"[3.9.2] 리디노미네이션 오류: {e!r}")
        try:
            await self.db.log_commands(usage.take())
        except Exception as e:
            print(f"[사용 기록] 저장 오류: {e!r}")
        # 하루 한 번 (KST 날짜가 바뀐 뒤 첫 루프): 세금 · 기록 정리
        if getattr(self, "_day", None) == _kst_day(now):
            return
        only = None if release.released_392(now) else set(lobby.TESTERS)
        if only is not None and not only:   # 테스트 서버 멤버를 아직 못 읽었으면 다음 루프에
            return
        self._day = _kst_day(now)
        try:
            r = await self.db.wealth_tax(now, only)
            if r is not None:
                print(f"[세금] {r['day']} 큰 돈 세금 {len(r['taxed'])}명 {r['total']:,}원")
            for owner, paid, n in await self.farm.pay_upkeep(now, only):   # 3.9.2 농장 유지비
                print(f"[농장 유지비] {owner} 농장 {n}개 {paid:,} 코인")
            await self.db.prune_logs(now)
        except Exception as e:
            print(f"[세금] 오류: {e!r}")

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Money(bot))
