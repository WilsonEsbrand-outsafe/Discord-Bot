# cogs/toto.py
import time
import discord
import aiohttp
import os
import asyncio
import datetime as dt
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from auth import owner_only
from services.football_api import FootballAPI
from services.odds_api import OddsAPI


from services.economy_db import EconomyDB
from services.notifier import send_notify

def _fmt_ts(ts: int) -> str:
    # 디스코드 타임스탬프(로컬 표시)
    return f"<t:{int(ts)}:f>"

def _pick_name(p: str) -> str:
    return {"1": "홈승(1)", "X": "무(X)", "2": "원정승(2)"}.get(p, p)

class Toto(commands.Cog):
    BASE_HOME = 1.4
    BASE_DRAW = 2.9
    BASE_AWAY = 2.1

    ALPHA = 0.25
    SMOOTHING = 50
    CAP_PCT = 0.20

    # 자동 경기 등록 설정
    AUTO_IMPORT_COMPETITIONS = ["PL", "CL", "PD", "WC"]  # 자동 등록할 대회 코드
    AUTO_IMPORT_FETCH        = 10       # 대회당 한 번에 가져올 최대 경기 수
    AUTO_IMPORT_INTERVAL     = 3600 * 6 # 체크 주기 (6시간)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.session: aiohttp.ClientSession | None = None
        self.api: FootballAPI | None = None
        self.odds: OddsAPI | None = None
        self._auto_task: asyncio.Task | None = None
        self._import_task: asyncio.Task | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.api  = FootballAPI(self.session)
        self.odds = OddsAPI(self.session)
        # 자동정산 루프 시작
        self._auto_task = asyncio.create_task(self._auto_settle_loop())
        # 자동 경기 등록 루프 시작
        self._import_task = asyncio.create_task(self._auto_import_loop())

    # ───────────── 자동완성 ─────────────
    async def match_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            now_ts = int(time.time())
            rows = await self.db.toto_list_open_matches(now_ts, limit=15)
            choices = []
            for match_id, home, away, kickoff_ts, *_ in rows:
                local_dt = dt.datetime.fromtimestamp(int(kickoff_ts))
                label = f"{home} vs {away} ({local_dt.strftime('%m/%d %H:%M')})"
                if current and current.lower() not in label.lower() and current not in str(match_id):
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=str(match_id)))
            return choices[:10]
        except Exception:
            return []

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()

        if self._import_task and not self._import_task.done():
            self._import_task.cancel()

    async def _notify_settle_dm(self, match_id: str):
        """
        해당 경기 정산 완료 시, 베팅한 유저들에게 DM 알림
        """
        try:
            m = await self.db.toto_get_match_brief(match_id)
            if not m:
                return

            _, home, away, kickoff_ts, status, result = m
            if status != "settled" or (result not in ("1", "X", "2")):
                return

            bets = await self.db.toto_list_bets_for_dm(match_id)

            result_name = {"1": "홈승(1)", "X": "무(X)", "2": "원정승(2)"}[result]
            kickoff_text = f"<t:{int(kickoff_ts)}:f>"

            for user_id, pick, amount, odds_locked, settled, payout in bets:
                # 혹시라도 미정산이면 스킵
                if int(settled) != 1:
                    continue

                pick_name = {"1": "홈승(1)", "X": "무(X)", "2": "원정승(2)"}.get(str(pick), str(pick))

                win = (int(payout) > 0)
                title = "✅ 토토 정산 완료 (적중)" if win else "❌ 토토 정산 완료 (미적중)"

                e = discord.Embed(
                    title=title,
                    description=f"경기: **{home} vs {away}**\n킥오프: {kickoff_text}\n결과: **{result_name}**",
                )
                e.add_field(name="내 픽", value=f"{pick_name}", inline=True)
                e.add_field(name="베팅", value=f"{int(amount):,}원", inline=True)
                e.add_field(name="고정 배당", value=f"{float(odds_locked)}", inline=True)
                e.add_field(
                    name="지급",
                    value=f"{int(payout):,}원" if win else "0원",
                    inline=True,
                )

                await send_notify(self.bot, self.db, int(user_id), "토토_결과", e)

        except Exception:
            pass

    async def _do_import(self, competition: str = "PL", limit: int = 10) -> dict:
        """
        football-data.org에서 예정 경기를 가져와 DB에 등록합니다.
        반환값: {"fetched": int, "added": int, "odds_events": int, "odds_applied": int, "error": str|None}
        """
        result: dict = {"fetched": 0, "added": 0, "odds_events": 0, "odds_applied": 0, "error": None, "notes": []}

        if not self.api:
            result["error"] = "API 미초기화"
            return result

        now_utc   = datetime.now(tz=timezone.utc)
        today_str = now_utc.date().isoformat()
        far_str   = now_utc.replace(year=now_utc.year + 1).date().isoformat()
        fetch     = limit * 3

        async def _fetch(season_year: int | None) -> list[dict]:
            """SCHEDULED + TIMED 병합해서 반환. 실패 시 빈 리스트."""
            out: list[dict] = []
            for status in ("SCHEDULED", "TIMED"):
                try:
                    rows = await self.api.competition_matches(
                        competition_code=competition,
                        season_year=season_year,
                        status=status,
                        date_from=today_str,
                        date_to=far_str,
                        limit=fetch,
                    )
                    by_id = {m["id"]: m for m in out}
                    for r in rows:
                        by_id.setdefault(r["id"], r)
                    out = list(by_id.values())
                except Exception as e:
                    print(f"[IMPORT] {competition} {status} 조회 실패(season={season_year}): {e}")
            return sorted(out, key=lambda m: m.get("utcDate") or "")

        # 1차: season 없이 조회 (API가 현재 시즌 자동 선택)
        matches = await _fetch(None)

        # 2차: 결과가 없으면 작년·올해·내년 시즌 순서로 재시도
        if not matches:
            year = now_utc.year
            for season_year in [year - 1, year, year + 1]:
                matches = await _fetch(season_year)
                if matches:
                    print(f"[IMPORT] {competition}: season={season_year} 폴백으로 {len(matches)}경기 수신")
                    break

        result["fetched"] = len(matches)

        # The Odds API 배당 가져오기
        odds_events: list[dict] = []
        if self.odds:
            try:
                odds_events = await self.odds.get_events(competition)
                result["odds_events"] = len(odds_events)
            except Exception as e:
                print(f"[ODDS] {competition} 배당 조회 실패: {e}")

        for m in matches:
            if result["added"] >= limit:
                break
            mid      = str(m.get("id"))
            home     = (m.get("homeTeam") or {}).get("name") or "HOME"
            away     = (m.get("awayTeam") or {}).get("name") or "AWAY"
            utc_iso  = m.get("utcDate")
            if not utc_iso:
                continue
            kickoff_ts = int(datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).timestamp())

            await self.db.toto_upsert_match(
                match_id=mid, home=home, away=away, kickoff_ts=kickoff_ts,
                base_home=self.BASE_HOME, base_draw=self.BASE_DRAW, base_away=self.BASE_AWAY,
            )
            result["added"] += 1

            result["notes"].append(f"`{home}` vs `{away}`")
            if odds_events and self.odds:
                ev = OddsAPI.find_match(home, away, kickoff_ts, odds_events)
                if ev:
                    h2h = OddsAPI.extract_h2h(ev)
                    if h2h:
                        await self.db.toto_update_base_odds(mid, *h2h)
                        result["odds_applied"] += 1
                        result["notes"][-1] += f" → 배당 {h2h[0]}/{h2h[1]}/{h2h[2]}"
                    else:
                        result["notes"][-1] += f" → 이벤트 매칭됨, h2h 추출 실패 (`{ev.get('home_team')}` vs `{ev.get('away_team')}`)"
                else:
                    # 가장 유사한 이벤트 이름 힌트
                    hint = odds_events[0] if odds_events else None
                    if hint:
                        result["notes"][-1] += f" → 매칭 실패 (Odds API 예시: `{hint.get('home_team')}` vs `{hint.get('away_team')}`)"
            elif not odds_events:
                result["notes"][-1] += " → Odds API 이벤트 없음"

        print(f"[IMPORT] {competition}: API {result['fetched']}개 → 등록 {result['added']}개 / 배당 {result['odds_applied']}개")
        return result

    async def _auto_import_loop(self):
        """
        AUTO_IMPORT_INTERVAL초(기본 6시간)마다 설정된 모든 대회의
        예정 경기를 자동 등록합니다. upsert로 중복을 처리하므로
        이미 등록된 경기는 건드리지 않습니다.
        """
        await asyncio.sleep(15)  # 정산 루프 뒤에 살짝 늦게 시작

        while True:
            try:
                if self.api:
                    total = 0
                    for comp in self.AUTO_IMPORT_COMPETITIONS:
                        r = await self._do_import(competition=comp, limit=self.AUTO_IMPORT_FETCH)
                        total += r["added"]
                        await asyncio.sleep(1)
                    print(f"[AUTO-IMPORT] 완료: 총 {total}경기 등록")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[AUTO-IMPORT] 오류: {e}")

            await asyncio.sleep(self.AUTO_IMPORT_INTERVAL)

    async def _auto_settle_loop(self):
        """
        1) 킥오프 지난 open 경기는 closed로 전환
        2) 킥오프 지난 미정산 경기들을 football-data.org로 조회
        3) FINISHED면 1/X/2 판정 후 DB 정산
        """
        await asyncio.sleep(5)  # 봇 부팅 직후 약간 대기

        while True:
            try:
                if not self.api:
                    await asyncio.sleep(30)
                    continue

                now_ts = int(time.time())

                # 시작한 open 경기 -> closed로 전환
                await self.db.toto_close_started(now_ts)

                # 정산 후보
                candidates = await self.db.toto_list_candidates_for_settle(now_ts, limit=30)
                if not candidates:
                    await asyncio.sleep(60)
                    continue

                for mid in candidates:
                    try:
                        data = await self.api.match(str(mid))
                        m = data.get("match") or data  # 응답 형태 대비

                        status = (m.get("status") or "").upper()
                        if status not in ("FINISHED", "AWARDED"):
                            continue

                        score = (m.get("score") or {})
                        ft = (score.get("fullTime") or {})
                        home_goals = ft.get("home")
                        away_goals = ft.get("away")

                        # fullTime이 없으면(간혹) winner로 판단 시도
                        if home_goals is None or away_goals is None:
                            winner = (score.get("winner") or "").upper()  # HOME_TEAM/AWAY_TEAM/DRAW
                            if winner == "HOME_TEAM":
                                result = "1"
                            elif winner == "AWAY_TEAM":
                                result = "2"
                            elif winner == "DRAW":
                                result = "X"
                            else:
                                continue
                        else:
                            home_goals = int(home_goals)
                            away_goals = int(away_goals)
                            if home_goals > away_goals:
                                result = "1"
                            elif home_goals < away_goals:
                                result = "2"
                            else:
                                result = "X"

                        ok, msg = await self.db.toto_set_result_and_settle(str(mid), result, now_ts)
                        if ok:
                            print(f"✅ [AUTO-SETTLE] match_id={mid} result={result} :: {msg}")
                            await self._notify_settle_dm(str(mid))
                        else:
                            print(f"⚠️ [AUTO-SETTLE] match_id={mid} skipped :: {msg}")

                        # API 과호출 방지
                        await asyncio.sleep(0.6)

                    except Exception as e:
                        print(f"❌ [AUTO-SETTLE] match_id={mid} error:", repr(e))
                        continue

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print("❌ [AUTO-SETTLE] loop error:", repr(e))
                await asyncio.sleep(60)

    # ───────────── 유저 ─────────────

    @app_commands.command(name="토토", description="오픈된 경기 목록과 현재 배당을 보여줍니다.")
    async def toto_list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        now_ts = int(time.time())
        rows = await self.db.toto_list_open_matches(now_ts, limit=20)

        if not rows:
            return await interaction.followup.send("현재 오픈된 경기가 없습니다.")

        e = discord.Embed(title="📋 토토 경기 목록 (오픈)", description="베팅 시점 배당이 고정됩니다.")
        for match_id, home, away, kickoff_ts, base_h, base_d, base_a in rows:
            pool = await self.db.toto_get_match_pool(match_id)
            oh, od, oa = self.db.toto_compute_dynamic_odds(
                base_home=base_h,
                base_draw=base_d,
                base_away=base_a,
                pool_home=pool["1"],
                pool_draw=pool["X"],
                pool_away=pool["2"],
                alpha=self.ALPHA,
                smoothing=self.SMOOTHING,
                cap_pct=self.CAP_PCT,
            )

            e.add_field(
                name=f"{home} vs {away}",
                value=(
                    f"킥오프: {_fmt_ts(kickoff_ts)}\n"
                    f"배당: **1** {oh} · **X** {od} · **2** {oa}\n"
                    f"ID: `{match_id}`"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=e)

    @app_commands.command(name="베팅", description="경기에 베팅합니다.")
    @app_commands.describe(match_id="경기 선택", pick="베팅할 결과", amount="베팅 금액")
    @app_commands.autocomplete(match_id=match_autocomplete)
    @app_commands.choices(pick=[
        app_commands.Choice(name="1 - 홈승", value="1"),
        app_commands.Choice(name="X - 무승부", value="X"),
        app_commands.Choice(name="2 - 원정승", value="2"),
    ])
    async def bet(self, interaction: discord.Interaction, match_id: str, pick: str, amount: int):
        await interaction.response.defer()

        pick = (pick or "").strip().upper()
        if pick == "0":
            pick = "X"

        m = await self.db.toto_get_match(match_id)
        if not m:
            return await interaction.followup.send("❌ 경기를 찾을 수 없습니다.")
        _, home, away, kickoff_ts, status, _, base_h, base_d, base_a = m
        if status != "open":
            return await interaction.followup.send("❌ 이미 마감된 경기입니다.")
        now_ts = int(time.time())
        if now_ts >= int(kickoff_ts) - 600:
            return await interaction.followup.send("❌ 경기 시작 10분 전부터 베팅이 마감됩니다.")


        # 현재 동적 배당 계산
        pool = await self.db.toto_get_match_pool(match_id)
        oh, od, oa = self.db.toto_compute_dynamic_odds(
            base_home=base_h,
            base_draw=base_d,
            base_away=base_a,
            pool_home=pool["1"],
            pool_draw=pool["X"],
            pool_away=pool["2"],
            alpha=self.ALPHA,
            smoothing=self.SMOOTHING,
            cap_pct=self.CAP_PCT,
        )
        odds = {"1": oh, "X": od, "2": oa}.get(pick)
        if odds is None:
            return await interaction.followup.send("❌ 픽은 1 / X / 2 중 하나여야 합니다.")

        now_ts = int(time.time())
        err = await self.db.toto_place_bet(
            user_id=interaction.user.id,
            match_id=match_id,
            pick=pick,
            amount=int(amount),
            odds_locked=float(odds),
            now_ts=now_ts,
        )
        if err:
            return await interaction.followup.send(f"❌ {err}")

        e = discord.Embed(title="✅ 베팅 완료", description=f"{interaction.user.mention}")
        e.add_field(name="경기", value=f"{home} vs {away}", inline=False)
        e.add_field(name="픽", value=_pick_name(pick), inline=True)
        e.add_field(name="베팅", value=f"{int(amount):,}원", inline=True)
        e.add_field(name="고정 배당", value=f"{odds}", inline=True)
        e.add_field(name="예상 지급(적중 시)", value=f"{int(int(amount)*float(odds)):,}원", inline=False)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="내베팅", description="최근 베팅 내역을 확인합니다. (정산 완료 포함)")
    async def my_bets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        rows = await self.db.toto_list_user_bets(interaction.user.id, limit=20)
        if not rows:
            return await interaction.followup.send("베팅 내역이 없습니다.", ephemeral=True)

        e = discord.Embed(title="🧾 내 베팅 내역 (최근 20건)")
        total_bet = 0
        total_payout = 0

        for match_id, home, away, kickoff_ts, status, result, pick, amount, odds_locked, settled, payout in rows:
            total_bet += int(amount)
            total_payout += int(payout)

            if int(settled) == 1:
                win = int(payout) > 0
                result_text = f"{'✅ 적중' if win else '❌ 미적중'} | 지급: **{int(payout):,}원**"
            elif status == "closed":
                result_text = "🟡 경기 중 (정산 대기)"
            else:
                result_text = f"🟢 베팅 진행중 | 킥오프: {_fmt_ts(kickoff_ts)}"

            e.add_field(
                name=f"{home} vs {away}",
                value=(
                    f"픽: **{_pick_name(pick)}** | 베팅: **{int(amount):,}원** | 배당: {odds_locked}\n"
                    f"{result_text}"
                ),
                inline=False,
            )

        profit = total_payout - total_bet
        sign = "+" if profit >= 0 else ""
        e.set_footer(text=f"총 베팅: {total_bet:,}원 | 총 수령: {total_payout:,}원 | 손익: {sign}{profit:,}원")
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="베팅취소", description="내 베팅을 취소하고 전액 환불받습니다. (경기 시작 전만)")
    @app_commands.describe(match_id="경기 선택")
    @app_commands.autocomplete(match_id=match_autocomplete)
    async def cancel_bet(self, interaction: discord.Interaction, match_id: str):
        await interaction.response.defer(ephemeral=True)

        ok, msg = await self.db.toto_cancel_bet(
            user_id=interaction.user.id,
            match_id=match_id.strip(),
            now_ts=int(time.time()),
        )
        if not ok:
            return await interaction.followup.send(f"❌ {msg}", ephemeral=True)

        await interaction.followup.send(f"✅ {msg}", ephemeral=True)

    @app_commands.command(name="진행중", description="현재 진행 중인 토토 경기를 확인합니다.")
    async def live_matches(self, interaction: discord.Interaction):
        await interaction.response.defer()

        now_ts = int(time.time())
        rows = await self.db.toto_list_in_progress(now_ts, limit=20)

        if not rows:
            return await interaction.followup.send("현재 진행 중인 경기가 없습니다.")

        e = discord.Embed(title="⚽ 진행 중인 경기")
        for match_id, home, away, kickoff_ts, base_h, base_d, base_a in rows:
            elapsed_min = max(0, (int(time.time()) - int(kickoff_ts)) // 60)  # ✅ 추가

            pool = await self.db.toto_get_match_pool(match_id)
            oh, od, oa = self.db.toto_compute_dynamic_odds(
                base_home=base_h,
                base_draw=base_d,
                base_away=base_a,
                pool_home=pool["1"],
                pool_draw=pool["X"],
                pool_away=pool["2"],
                alpha=self.ALPHA,
                smoothing=self.SMOOTHING,
                cap_pct=self.CAP_PCT,
            )

            e.add_field(
                name=f"{home} vs {away}",
                value=(
                    f"시작: {_fmt_ts(kickoff_ts)} (경과 {elapsed_min}분)\n"
                    f"배당: **1** {oh} · **X** {od} · **2** {oa}\n"
                    f"ID: `{match_id}`"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=e)

    # ───────────── 나만 사용 명령어 ─────────────

    @app_commands.command(name="토토불러오기", description="(관리자) football-data.org에서 다음 경기들을 자동 등록합니다.")
    @app_commands.describe(competition="대회 코드(기본 PL)", limit="가져올 경기 수(1~20)")
    @app_commands.check(owner_only)
    async def import_matches(self, interaction: discord.Interaction, competition: str = "PL", limit: int = 10):
        await interaction.response.defer(ephemeral=True)

        if not self.api:
            return await interaction.followup.send("❌ API가 아직 초기화되지 않았습니다. 봇을 재시작해 주세요.", ephemeral=True)

        limit       = max(1, min(20, int(limit)))
        competition = (competition or "PL").strip().upper()

        r = await self._do_import(competition=competition, limit=limit)

        lines = [
            f"**대회**: {competition}",
            f"**API 수신**: {r['fetched']}경기",
            f"**DB 등록**: {r['added']}경기",
            f"**배당 이벤트**: {r['odds_events']}개 (The Odds API)",
            f"**배당 적용**: {r['odds_applied']}/{r['added']} "
            + ("✅" if r['odds_applied'] > 0 else "❌ (기본값 사용)"),
        ]
        if r["error"]:
            lines.append(f"⚠️ 오류: `{r['error']}`")
        for note in r.get("notes", []):
            lines.append(f"• {note}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="경기삭제", description="(관리자) 오픈된 토토 경기를 환불 후 삭제합니다.")
    @app_commands.describe(match_id="경기 ID")
    @app_commands.check(owner_only)
    async def delete_match(self, interaction: discord.Interaction, match_id: str):
        
        await interaction.response.defer(ephemeral=True)

        ok, msg = await self.db.toto_refund_and_delete_open_match(match_id.strip())
        if not ok:
            return await interaction.followup.send(f"❌ {msg}", ephemeral=True)

        await interaction.followup.send(f"✅ {msg}", ephemeral=True)

    @app_commands.command(name="경기등록", description="(관리자) 토토 경기를 수동 등록합니다.")
    @app_commands.describe(match_id="고유 ID", home="홈팀", away="원정팀", kickoff_ts="킥오프 유닉스 타임(초)")
    @app_commands.check(owner_only)
    async def add_match(self, interaction: discord.Interaction, match_id: str, home: str, away: str, kickoff_ts: int):
   
        await interaction.response.defer(ephemeral=True)

        await self.db.toto_upsert_match(
            match_id=match_id.strip(),
            home=home.strip(),
            away=away.strip(),
            kickoff_ts=int(kickoff_ts),
            base_home=self.BASE_HOME,
            base_draw=self.BASE_DRAW,
            base_away=self.BASE_AWAY,
        )
        await interaction.followup.send("✅ 경기 등록/갱신 완료", ephemeral=True)

    @app_commands.command(name="결과", description="(관리자) 경기 결과를 입력하고 정산합니다. (1/X/2)")
    @app_commands.describe(match_id="경기 ID", result="1 / X / 2")
    @app_commands.check(owner_only)
    async def set_result(self, interaction: discord.Interaction, match_id: str, result: str):

        await interaction.response.defer(ephemeral=True)

        ok, msg = await self.db.toto_set_result_and_settle(match_id.strip(), result.strip().upper(), int(time.time()))
        if not ok:
            return await interaction.followup.send(f"❌ {msg}", ephemeral=True)

        await interaction.followup.send(f"✅ {msg}", ephemeral=True)
        await self._notify_settle_dm(match_id.strip())

    @app_commands.command(name="배당확인", description="(관리자) The Odds API에서 현재 배당 가능한 축구 대회 목록을 확인합니다.")
    @app_commands.check(owner_only)
    async def check_odds_sports(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.odds:
            return await interaction.followup.send("❌ Odds API 미초기화", ephemeral=True)

        sports = await self.odds.list_active_sports()
        soccer = [s for s in sports if "soccer" in s.get("key", "")]

        if not soccer:
            return await interaction.followup.send(
                "현재 배당 있는 축구 대회가 없습니다. (ODDS_API_KEY 확인 필요)", ephemeral=True
            )

        lines = [f"`{s['key']}` — {s.get('title', '')}" for s in soccer]
        text = "\n".join(lines)
        # 2000자 제한 대비
        if len(text) > 1800:
            text = text[:1800] + "\n…(생략)"

        await interaction.followup.send(f"**현재 배당 있는 축구 대회:**\n{text}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Toto(bot))
