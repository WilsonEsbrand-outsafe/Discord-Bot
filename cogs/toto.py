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
from services import ui
from services.ufc_db import UfcDB
from cogs.ufc_toto import place_ufc_bet, upcoming_fights
from typing import Optional

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
    # WC(월드컵)는 4년에 한 번이라 자동 등록에서 뺐다. 비시즌 대회는
    # OddsAPI가 무료 /sports 조회로 걸러내므로 유료 호출을 먹지 않는다.
    AUTO_IMPORT_COMPETITIONS = ["PL", "PD", "CL"]  # 자동 등록할 대회 코드
    AUTO_IMPORT_FETCH        = 10        # 대회당 한 번에 가져올 최대 경기 수
    SETTLE_MIN_ELAPSED       = 2 * 3600  # 킥오프 후 이 시간이 지나야 정산 조회
    SETTLE_BATCH             = 10        # 한 번에 확인할 최대 경기 수
    # 크레딧 계산: 대회 3개 x 지역 2개 x 하루 2회 = 12크레딧/일 (약 360/월).
    # 무료 플랜이 월 500이므로 주기를 줄이면 월 한도를 넘긴다.
    AUTO_IMPORT_INTERVAL     = 3600 * 12 # 체크 주기 (12시간)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.ufc = UfcDB()
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

        async def _fetch(season_year: int | None, silent_404: bool = False) -> list[dict]:
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
                    is_404 = "404" in str(e)
                    is_429 = "429" in str(e)
                    if is_429:
                        print(f"[IMPORT] {competition} rate limit(429) — 30초 대기 후 재시도")
                        await asyncio.sleep(30)
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
                        except Exception as e2:
                            print(f"[IMPORT] {competition} {status} 재시도 실패: {e2}")
                    elif not (silent_404 and is_404):
                        print(f"[IMPORT] {competition} {status} 조회 실패(season={season_year}): {e}")
                await asyncio.sleep(7)  # 분당 10요청 제한 — 요청 간 7초 간격
            return sorted(out, key=lambda m: m.get("utcDate") or "")

        # 1차: season 없이 조회 (API가 현재 시즌 자동 선택)
        matches = await _fetch(None)

        # 2차: 결과가 없으면 작년·올해 시즌만 재시도 (year+1은 미개막이라 404 필연)
        if not matches:
            year = now_utc.year
            for season_year in [year - 1, year]:
                matches = await _fetch(season_year, silent_404=True)
                if matches:
                    print(f"[IMPORT] {competition}: season={season_year} 폴백으로 {len(matches)}경기 수신")
                    break

        result["fetched"] = len(matches)

        # ── 1단계: 경기 등록 ──────────────────────────────────────────
        # 배당 조회보다 먼저 등록해야, 어떤 경기에 배당이 필요한지 알 수 있다.
        registered: list[tuple] = []   # (match_id, home, away, kickoff_ts)
        for m in matches:
            if result["added"] >= limit:
                break
            mid     = str(m.get("id"))
            home    = (m.get("homeTeam") or {}).get("name") or "HOME"
            away    = (m.get("awayTeam") or {}).get("name") or "AWAY"
            utc_iso = m.get("utcDate")
            if not utc_iso:
                continue
            kickoff_ts = int(datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).timestamp())

            await self.db.toto_upsert_match(
                match_id=mid, home=home, away=away, kickoff_ts=kickoff_ts,
                base_home=self.BASE_HOME, base_draw=self.BASE_DRAW, base_away=self.BASE_AWAY,
            )
            result["added"] += 1
            registered.append((mid, home, away, kickoff_ts))
            result["notes"].append(f"`{home}` vs `{away}`")

        # ── 2단계: 배당이 필요한 경기가 있을 때만 유료 호출 ───────────
        # 예전엔 이미 배당이 다 붙어 있어도 매번 The Odds API 를 호출해
        # 재시작마다 크레딧을 태웠다. (대회당 지역 2개 = 2크레딧)
        need_odds = await self.db.toto_missing_odds([r[0] for r in registered])
        if not need_odds:
            if registered:
                print(f"[ODDS] {competition}: 등록된 {len(registered)}경기 모두 배당 보유 — 호출 스킵")
            print(f"[IMPORT] {competition}: API {result['fetched']}개 → 등록 {result['added']}개 / 배당 0개(기보유)")
            return result

        odds_events: list[dict] = []
        if self.odds:
            try:
                odds_events = await self.odds.get_events(competition)
                result["odds_events"] = len(odds_events)
            except Exception as e:
                print(f"[ODDS] {competition} 배당 조회 실패: {e}")

        for idx, (mid, home, away, kickoff_ts) in enumerate(registered):
            note_i = len(result["notes"]) - len(registered) + idx
            if mid not in need_odds:
                result["notes"][note_i] += " → 배당 이미 반영됨"
                continue
            if not odds_events or not self.odds:
                result["notes"][note_i] += " → Odds API 이벤트 없음"
                continue

            ev = OddsAPI.find_match(home, away, kickoff_ts, odds_events)
            if ev:
                h2h = OddsAPI.extract_h2h(ev)
                if h2h:
                    await self.db.toto_update_base_odds(mid, *h2h)
                    result["odds_applied"] += 1
                    result["notes"][note_i] += f" → 배당 {h2h[0]}/{h2h[1]}/{h2h[2]}"
                else:
                    result["notes"][note_i] += (
                        f" → 이벤트 매칭됨, h2h 추출 실패 "
                        f"(`{ev.get('home_team')}` vs `{ev.get('away_team')}`)"
                    )
            else:
                hint = odds_events[0] if odds_events else None
                if hint:
                    result["notes"][note_i] += (
                        f" → 매칭 실패 (Odds API 예시: "
                        f"`{hint.get('home_team')}` vs `{hint.get('away_team')}`)"
                    )

        print(f"[IMPORT] {competition}: API {result['fetched']}개 → 등록 {result['added']}개 / 배당 {result['odds_applied']}개")
        return result

    async def _auto_import_loop(self):
        """
        AUTO_IMPORT_INTERVAL초(기본 6시간)마다 설정된 모든 대회의
        예정 경기를 자동 등록합니다. upsert로 중복을 처리하므로
        이미 등록된 경기는 건드리지 않습니다.
        """
        await asyncio.sleep(120)  # 부팅 직후 부하가 겹치지 않게 정산 루프보다 더 뒤에서 시작

        while True:
            try:
                if self.api:
                    total = 0
                    for comp in self.AUTO_IMPORT_COMPETITIONS:
                        r = await self._do_import(competition=comp, limit=self.AUTO_IMPORT_FETCH)
                        total += r["added"]
                        await asyncio.sleep(20)  # 대회 간 rate limit 여유 확보
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
        await asyncio.sleep(45)  # 부팅 직후 명령 동기화·시장 부트스트랩과 겹치지 않게

        while True:
            try:
                if not self.api:
                    await asyncio.sleep(30)
                    continue

                now_ts = int(time.time())

                # 시작한 open 경기 -> closed로 전환
                await self.db.toto_close_started(now_ts)

                # 정산 후보. 킥오프 직후 경기는 끝났을 리가 없는데도 매분
                # 조회해 분당 10회 한도를 통째로 태우고 있었다. 경기 시간
                # (연장·추가시간 포함)이 지난 것만 본다.
                candidates = await self.db.toto_list_candidates_for_settle(
                    now_ts - self.SETTLE_MIN_ELAPSED, limit=self.SETTLE_BATCH
                )
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

                    except Exception as e:
                        # 예전엔 대기 없이 continue 해서, 한도가 소진되면
                        # 남은 후보를 연달아 때려 429를 계속 재발생시켰다.
                        # 호출 간격 자체는 FootballAPI 의 전역 리미터가
                        # 책임지고, 여기서는 오류 시 추가로 쉬어준다.
                        print(f"❌ [AUTO-SETTLE] match_id={mid} error:", repr(e))
                        await asyncio.sleep(30 if "429" in str(e) else 3)
                        continue

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print("❌ [AUTO-SETTLE] loop error:", repr(e))
                await asyncio.sleep(60)

    # ───────────── 공통 ─────────────
    async def _odds(self, match_id: str, base_h: float, base_d: float, base_a: float) -> dict[str, float]:
        """현재 동적 배당 {1, X, 2}."""
        pool = await self.db.toto_get_match_pool(match_id)
        oh, od, oa = self.db.toto_compute_dynamic_odds(
            base_home=base_h, base_draw=base_d, base_away=base_a,
            pool_home=pool["1"], pool_draw=pool["X"], pool_away=pool["2"],
            alpha=self.ALPHA, smoothing=self.SMOOTHING, cap_pct=self.CAP_PCT,
        )
        return {"1": oh, "X": od, "2": oa}

    async def place_soccer_bet(self, user_id: int, match_id: str, pick: str, amount: int):
        """축구 베팅. (실패 이유 또는 None, 성공 정보)"""
        m = await self.db.toto_get_match(match_id)
        if not m:
            return "경기를 찾을 수 없습니다.", None
        _, home, away, kickoff_ts, status, _, base_h, base_d, base_a = m
        if status != "open":
            return "이미 마감된 경기입니다.", None
        now_ts = int(time.time())
        if now_ts >= int(kickoff_ts) - 600:
            return "경기 시작 10분 전부터 베팅이 마감됩니다.", None
        odds = (await self._odds(match_id, base_h, base_d, base_a))[pick]
        err = await self.db.toto_place_bet(user_id=user_id, match_id=match_id, pick=pick, amount=int(amount),
                                           odds_locked=float(odds), now_ts=now_ts)
        if err:
            return err, None
        return None, {"home": home, "away": away, "odds": float(odds)}

    async def show_pick(self, interaction: discord.Interaction, game: dict):
        """경기를 고르면: 본인에게만 결과 버튼(현재 배당)을 보여준다."""
        if game["kind"] == "soccer":
            m = await self.db.toto_get_match(game["match_id"])
            if not m or m[4] != "open" or time.time() >= int(m[3]) - 600:
                return await interaction.response.send_message("❌ 베팅이 마감된 경기입니다.", ephemeral=True)
            odds = await self._odds(game["match_id"], *m[6:9])
            picks = [("1", f"{game['home']} 승", odds["1"]), ("X", "무승부", odds["X"]), ("2", f"{game['away']} 승", odds["2"])]
            title = f"⚽ {game['home']} vs {game['away']}"
        else:
            f = game["fight"]
            picks = [(f["home"], f["home"], f["home_odds"]), (f["away"], f["away"], f["away_odds"])]
            title = f"🥊 {f['home']} vs {f['away']}"
        e = ui.card(title, f"킥오프 <t:{game['ts']}:f> (<t:{game['ts']}:R>)\n\n"
                    + "\n".join(f"• {label} **{o:.2f}배**" for _, label, o in picks)
                    + "\n\n아래 버튼을 누르고 금액을 입력하면 바로 베팅됩니다. 배당은 베팅 순간 고정돼요.",
                    ui.INFO, interaction.user, SECTION)
        await interaction.response.send_message(embed=e, view=PickView(self, game, picks), ephemeral=True)

    # ───────────── 유저 ─────────────
    @app_commands.command(name="토토", description="축구·UFC 경기 목록과 배당 — 메뉴에서 골라 바로 베팅")
    async def toto_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        now_ts = int(time.time())
        rows = await self.db.toto_list_open_matches(now_ts, limit=15)
        live = await self.db.toto_list_in_progress(now_ts, limit=10)
        fights = (await upcoming_fights())[:8]

        games, options, parts = {}, [], []
        n = 0
        if rows:
            lines = []
            for match_id, home, away, kickoff_ts, base_h, base_d, base_a in rows:
                n += 1
                o = await self._odds(match_id, base_h, base_d, base_a)
                games[f"s:{match_id}"] = {"kind": "soccer", "match_id": match_id, "home": home, "away": away,
                                          "ts": int(kickoff_ts)}
                options.append(discord.SelectOption(label=f"{n}. {home} vs {away}"[:100], value=f"s:{match_id}",
                                                    emoji="⚽", description=_short_ts(kickoff_ts)))
                lines.append(f"`{n}` **{home} vs {away}** · <t:{int(kickoff_ts)}:R>\n"
                             f"　 홈 **{o['1']}** · 무 **{o['X']}** · 원정 **{o['2']}**")
            parts.append("**⚽ 축구**\n" + "\n".join(lines))
        if fights:
            lines = []
            for f in fights:
                n += 1
                ts = int(datetime.fromisoformat(f["commence_time"].replace("Z", "+00:00")).timestamp())
                games[f"u:{f['event_id']}"] = {"kind": "ufc", "fight": f, "ts": ts}
                options.append(discord.SelectOption(label=f"{n}. {f['home']} vs {f['away']}"[:100],
                                                    value=f"u:{f['event_id']}", emoji="🥊", description=_short_ts(ts)))
                lines.append(f"`{n}` **{f['home']} vs {f['away']}** · <t:{ts}:R>\n"
                             f"　 {f['home']} **{f['home_odds']:.2f}** · {f['away']} **{f['away_odds']:.2f}**")
            parts.append("**🥊 UFC**\n" + "\n".join(lines))
        if live:
            parts.append("**🔴 진행 중** (베팅 마감 · 끝나면 자동 정산)\n" + "\n".join(
                f"**{home} vs {away}** · 킥오프 <t:{int(k)}:R>" for _, home, away, k, *_ in live))

        if not options:
            e = ui.card("🎰 토토", "지금은 베팅할 수 있는 경기가 없어요." + ("\n\n" + parts[0] if parts else ""),
                        ui.EVEN, interaction.user, SECTION)
            return await interaction.followup.send(embed=e)
        e = ui.card("🎰 토토", "\n\n".join(parts), ui.INFO, interaction.user, SECTION)
        e.set_footer(text="아래 메뉴에서 경기를 고르면 바로 베팅 · 배당은 베팅 순간 고정 · 내역·취소는 /내베팅")
        await interaction.followup.send(embed=e, view=GameMenu(self, games, options[:25]))

    @app_commands.command(name="내베팅", description="축구·UFC 베팅 내역 — 경기 시작 전 축구 베팅은 여기서 취소")
    async def my_bets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user, now_ts = interaction.user, int(time.time())
        soccer = await self.db.toto_list_user_bets(user.id, limit=10)
        ufc = await self.ufc.list_recent_for_user(user.id, limit=10)
        if not soccer and not ufc:
            return await interaction.followup.send(
                embed=ui.card("🧾 내 베팅", "베팅 내역이 없어요. `/토토`에서 시작해 보세요!", ui.EVEN, user, SECTION),
                ephemeral=True)

        total_bet = total_pay = 0
        parts, cancellable = [], []
        if soccer:
            lines = []
            for match_id, home, away, kickoff_ts, status, result, pick, amount, odds, settled, payout in soccer:
                total_bet += int(amount)
                total_pay += int(payout)
                if int(settled):
                    state = f"✅ 적중 **+{int(payout):,}원**" if int(payout) > 0 else "❌ 미적중"
                elif status == "closed":
                    state = "🟡 경기 중 · 정산 대기"
                else:
                    state = f"🟢 킥오프 <t:{int(kickoff_ts)}:R>"
                    if now_ts < int(kickoff_ts) - 600:
                        cancellable.append((match_id, f"{home} vs {away}", int(amount)))
                lines.append(f"**{home} vs {away}** · {_pick_name(pick)} · {int(amount):,}원 × {odds}\n　 {state}")
            parts.append("**⚽ 축구**\n" + "\n".join(lines))
        if ufc:
            lines = []
            for r in ufc:
                home, away = r["match_id"].split("|", 1)
                total_bet += r["amount"]
                if r["settled"]:
                    pay = int(r["amount"] * r["odds"]) if r["won"] else 0
                    total_pay += pay
                    state = f"✅ 적중 **+{pay:,}원**" if r["won"] else "❌ 미적중"
                else:
                    state = f"🟢 진행 중 · 적중 시 **{int(r['amount'] * r['odds']):,}원**"
                lines.append(f"**{home} vs {away}** · {r['fighter']} · {r['amount']:,}원 × {r['odds']:.2f}\n　 {state}")
            parts.append("**🥊 UFC**\n" + "\n".join(lines))

        e = ui.card("🧾 내 베팅", "\n\n".join(parts), ui.INFO, user, SECTION)
        e.set_footer(text=f"총 베팅 {total_bet:,}원 · 총 수령 {total_pay:,}원"
                          + (" · 아래 메뉴로 축구 베팅 취소(전액 환불)" if cancellable else ""))
        view = CancelMenu(self, user.id, cancellable) if cancellable else discord.utils.MISSING
        await interaction.followup.send(embed=e, view=view, ephemeral=True)

    # ───────────── 관리자 ─────────────
    @app_commands.command(name="토토관리", description="(관리자) 경기 불러오기 · 등록 · 삭제 · 결과 입력 · 배당 확인")
    @app_commands.describe(작업="할 작업", 경기="경기 ID (등록 · 삭제 · 결과)", 결과="경기 결과 (결과 입력)",
                           홈="홈팀 (등록)", 원정="원정팀 (등록)", 킥오프="킥오프 유닉스 타임(초) (등록)",
                           대회="대회 코드 (불러오기, 기본 PL)", 개수="불러올 경기 수 1~20 (불러오기)")
    @app_commands.choices(
        작업=[app_commands.Choice(name="📥 경기 불러오기", value="import"),
              app_commands.Choice(name="➕ 경기 등록", value="add"),
              app_commands.Choice(name="🗑️ 경기 삭제 (환불)", value="delete"),
              app_commands.Choice(name="🏁 결과 입력 · 정산", value="result"),
              app_commands.Choice(name="📊 배당 대회 확인", value="odds")],
        결과=[app_commands.Choice(name="1 - 홈승", value="1"), app_commands.Choice(name="X - 무승부", value="X"),
              app_commands.Choice(name="2 - 원정승", value="2")],
    )
    @app_commands.autocomplete(경기=match_autocomplete)
    @app_commands.check(owner_only)
    async def manage(self, interaction: discord.Interaction, 작업: str, 경기: Optional[str] = None,
                     결과: Optional[str] = None, 홈: Optional[str] = None, 원정: Optional[str] = None,
                     킥오프: Optional[int] = None, 대회: str = "PL", 개수: int = 10):
        await interaction.response.defer(ephemeral=True)
        say = lambda msg: interaction.followup.send(msg, ephemeral=True)   # noqa: E731
        need = {"add": (경기, 홈, 원정, 킥오프), "delete": (경기,), "result": (경기, 결과)}.get(작업, ())
        if any(v is None for v in need):
            return await say("❌ 이 작업에 필요한 값이 빠졌어요. (등록: 경기·홈·원정·킥오프 / 삭제: 경기 / 결과: 경기·결과)")

        if 작업 == "import":
            if not self.api:
                return await say("❌ API가 아직 초기화되지 않았습니다. 봇을 재시작해 주세요.")
            competition = (대회 or "PL").strip().upper()
            r = await self._do_import(competition=competition, limit=max(1, min(20, int(개수))))
            lines = [
                f"**대회**: {competition}",
                f"**API 수신**: {r['fetched']}경기",
                f"**DB 등록**: {r['added']}경기",
                f"**배당 이벤트**: {r['odds_events']}개 (The Odds API)",
                f"**배당 적용**: {r['odds_applied']}/{r['added']} " + ("✅" if r['odds_applied'] > 0 else "❌ (기본값 사용)"),
            ]
            if r["error"]:
                lines.append(f"⚠️ 오류: `{r['error']}`")
            lines += [f"• {note}" for note in r.get("notes", [])]
            return await say("\n".join(lines))

        if 작업 == "add":
            await self.db.toto_upsert_match(match_id=경기.strip(), home=홈.strip(), away=원정.strip(),
                                            kickoff_ts=int(킥오프), base_home=self.BASE_HOME,
                                            base_draw=self.BASE_DRAW, base_away=self.BASE_AWAY)
            return await say("✅ 경기 등록/갱신 완료")

        if 작업 == "delete":
            ok, msg = await self.db.toto_refund_and_delete_open_match(경기.strip())
            return await say(f"{'✅' if ok else '❌'} {msg}")

        if 작업 == "result":
            ok, msg = await self.db.toto_set_result_and_settle(경기.strip(), 결과, int(time.time()))
            await say(f"{'✅' if ok else '❌'} {msg}")
            if ok:
                await self._notify_settle_dm(경기.strip())
            return

        # odds
        if not self.odds:
            return await say("❌ Odds API 미초기화")
        soccer = [s for s in await self.odds.list_active_sports() if "soccer" in s.get("key", "")]
        if not soccer:
            return await say("현재 배당 있는 축구 대회가 없습니다. (ODDS_API_KEY 확인 필요)")
        text = "\n".join(f"`{s['key']}` — {s.get('title', '')}" for s in soccer)
        await say(f"**현재 배당 있는 축구 대회:**\n{text[:1800]}" + ("\n…(생략)" if len(text) > 1800 else ""))


# ───────────── 화면 구성 요소 ─────────────
SECTION = "🎰 토토"


def _short_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d %H:%M KST")


class GameMenu(discord.ui.View):
    """/토토 아래 경기 선택 메뉴 — 누구나 골라서 각자 베팅."""

    def __init__(self, cog: "Toto", games: dict, options: list[discord.SelectOption]):
        super().__init__(timeout=900)
        self.cog, self.games = cog, games
        self.select = discord.ui.Select(placeholder="🎯 베팅할 경기를 고르세요", options=options)
        self.select.callback = self._chosen
        self.add_item(self.select)

    async def _chosen(self, interaction: discord.Interaction):
        game = self.games.get(self.select.values[0])
        if game is None:
            return await interaction.response.send_message("❌ 목록이 오래됐어요. `/토토`를 다시 열어 주세요.", ephemeral=True)
        await self.cog.show_pick(interaction, game)


class PickView(discord.ui.View):
    def __init__(self, cog: "Toto", game: dict, picks: list[tuple[str, str, float]]):
        super().__init__(timeout=300)
        for i, (pick, label, odds) in enumerate(picks):
            b = discord.ui.Button(label=f"{label} · {odds:.2f}배"[:80],
                                  style=(discord.ButtonStyle.primary, discord.ButtonStyle.secondary,
                                         discord.ButtonStyle.danger)[i if len(picks) == 3 else i * 2])
            b.callback = self._open(cog, game, pick, label, odds)
            self.add_item(b)

    @staticmethod
    def _open(cog, game, pick, label, odds):
        async def cb(interaction: discord.Interaction):
            await interaction.response.send_modal(BetModal(cog, game, pick, label, odds))
        return cb


class BetModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="베팅 금액 (원)", placeholder="예: 10000", max_length=12)

    def __init__(self, cog: "Toto", game: dict, pick: str, label: str, odds: float):
        super().__init__(title=f"{label} 베팅"[:45])
        self.cog, self.game, self.pick, self.label, self.odds = cog, game, pick, label, odds

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.replace(",", "").replace("원", "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            return await interaction.response.send_message("❌ 금액은 1 이상의 숫자로 입력해 주세요.", ephemeral=True)
        amount, user = int(raw), interaction.user
        if self.game["kind"] == "soccer":
            err, info = await self.cog.place_soccer_bet(user.id, self.game["match_id"], self.pick, amount)
            odds, title = (info or {}).get("odds"), f"⚽ {self.game['home']} vs {self.game['away']}"
        else:
            f = self.game["fight"]
            err = await place_ufc_bet(user.id, f, self.pick, self.odds, amount, self.cog.db, self.cog.ufc)
            odds, title = self.odds, f"🥊 {f['home']} vs {f['away']}"
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        e = ui.card("✅ 베팅 완료",
                    f"`경기` **{title}**\n`픽` **{self.label}**\n"
                    f"`베팅` **{amount:,}원** · `배당` **{odds:.2f}배** (고정)\n"
                    f"`적중 시` **{int(amount * odds):,}원**",
                    ui.WIN, user, SECTION)
        await interaction.response.send_message(embed=e)


class CancelMenu(discord.ui.View):
    """/내베팅 — 경기 시작 10분 전까지 축구 베팅 취소(전액 환불)."""

    def __init__(self, cog: "Toto", user_id: int, bets: list[tuple[str, str, int]]):
        super().__init__(timeout=300)
        self.cog, self.user_id = cog, user_id
        self.select = discord.ui.Select(
            placeholder="↩️ 취소할 축구 베팅 (전액 환불)",
            options=[discord.SelectOption(label=f"{name} · {amt:,}원"[:100], value=mid) for mid, name, amt in bets[:25]])
        self.select.callback = self._cancel
        self.add_item(self.select)

    async def _cancel(self, interaction: discord.Interaction):
        ok, msg = await self.cog.db.toto_cancel_bet(user_id=self.user_id, match_id=self.select.values[0],
                                                    now_ts=int(time.time()))
        await interaction.response.send_message(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Toto(bot))
