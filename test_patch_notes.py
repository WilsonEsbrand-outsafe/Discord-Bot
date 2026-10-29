# test_patch_notes.py — 2.8 패치노트: 임베드 2개(새 콘텐츠 · 편의·신규 유저) · 디스코드 글자 한도 · 장부 · 체크리스트 내용 없음
# · 여러 임베드를 한 메시지로 보내고 예전 문자열 패치노트도 그대로
# 실행: venv/Scripts/python.exe test_patch_notes.py
import asyncio
from types import SimpleNamespace

import cogs.patch_notes as P
from patch_notes_data import NOTICES, PATCH_NOTES


def test_28_content():
    v = PATCH_NOTES["2.8"]
    assert [t for t, _ in v] == ["🆕 새 콘텐츠", "🧭 편의 · 신규 유저"]
    assert all(len(b) <= 4096 for _, b in v)
    for word in ("장부", "체크리스트", "가계부"):   # 사적인 기능 — 패치노트에 절대 넣지 않는다
        assert all(word not in b and word not in t for t, b in v), word
    body = "".join(b for _, b in v)
    for cmd in ("/낚시", "/양궁", "/던전", "전술", "유스", "구단 경영", "/튜토리얼", "/오늘", "/게임"):
        assert cmd in body, cmd
    for _, ts, _, content in NOTICES:
        assert isinstance(content, (str, list))


def test_embeds():
    cog = P.PatchNotesCog.__new__(P.PatchNotesCog)
    es = cog.make_embeds("2.8", PATCH_NOTES["2.8"], 1_800_000_000)
    assert len(es) == 2 and es[0].title.endswith("새 콘텐츠") and es[1].footer.text == "v2.8 패치노트"
    assert sum(len(e.title) + len(e.description) + len(e.footer.text or "") for e in es) <= 6000
    old = cog.make_embeds("2.7", PATCH_NOTES["2.7"], 1_800_000_000)
    assert len(old) == 1 and old[0].title == "📢 버전 2.7 업데이트"

    sent = []

    class Ch:
        async def send(self, **k):
            sent.append(k)
    guild = SimpleNamespace(id=1)
    cog.bot = SimpleNamespace(guilds=[guild])
    cog._pick_channel = lambda g: Ch()
    ok, fail = asyncio.run(cog.send_all(es))
    assert (ok, fail) == (1, 0) and len(sent[0]["embeds"]) == 2
    asyncio.run(cog.send_all(old[0]))
    assert len(sent[1]["embeds"]) == 1 and "content" not in sent[1]
    import patch_notes_data as pnd
    import services.economy_db as edb
    assert ("patch-2.81", edb.SHOP_281_TS) in [(k, ts) for k, ts, *_ in pnd.NOTICES]   # 상점이 열리는 시각에 발송
    # 2.81: 패치노트는 @everyone 태그
    asyncio.run(cog.send_all(es, everyone=True))
    assert sent[2]["content"] == "@everyone" and sent[2]["allowed_mentions"].everyone is True


def test_282():
    for ver in ("2.82", "2.82b"):
        for word in ("장부", "체크리스트", "가계부"):
            assert word not in PATCH_NOTES[ver], word
    import release
    keys = {k: ts for k, ts, *_ in NOTICES}
    assert keys["maint-2.82b"] == keys["patch-2.82b"]
    assert release.released(release.MAINTENANCE[0])                            # 2.8 은 공개된 그대로
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-2.82b")
    start, end = release.MAINTENANCE
    assert P.notice_text("maint-2.82", title, body, start - 60) == (title, body)              # 점검 전: 그대로
    t2, b2 = P.notice_text("maint-2.82", title, body, end)                                    # 점검 뒤: 점검 완료
    assert t2 == "✅ 점검 완료 안내" and "점검이 끝났어요" in b2 and "쓸 수 없어요" not in b2
    assert P.notice_text("patch-2.82", "📢", "x", end) == ("📢", "x")


def test_291():
    import calendar
    import release
    for word in ("장부", "체크리스트", "가계부"):
        assert word not in PATCH_NOTES["2.9.1"], word
    keys = {k: ts for k, ts, *_ in NOTICES}
    start = calendar.timegm((2026, 10, 21, 5, 15, 0))                          # 14:15 KST
    assert keys["maint-2.9.1"] == keys["patch-2.9.1"] == calendar.timegm((2026, 10, 21, 5, 0, 0)) < start   # 14:00 KST
    assert release.RELEASE_291_TS == start and not release.released_291(start - 1) and release.released_291(start)
    assert release.preview_291(release.TEST_GUILD, start - 1) and not release.preview_291(1, start - 1)


def test_2101():
    import calendar
    import release
    for word in ("장부", "체크리스트", "가계부"):
        assert word not in PATCH_NOTES["2.10.1"], word
    keys = {k: ts for k, ts, *_ in NOTICES}
    start = calendar.timegm((2026, 10, 22, 1, 0, 0))   # 점검 10:00 ~ 10:05 KST
    assert keys["maint-2.10.1"] == keys["patch-2.10.1"] < start                # 점검 전에 안내 · 패치노트
    assert release.RELEASE_2101_TS == start and not release.released_2101(start - 1) and release.released_2101(start)
    assert release.preview_2101(release.TEST_GUILD, start - 1) and not release.preview_2101(1, start - 1)
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-2.10.1")
    assert title == "🔧 정기점검 안내"


def test_2102():
    import release
    keys = {k: ts for k, ts, *_ in NOTICES}
    assert "UFC" in PATCH_NOTES["2.10.2"] and "장부" not in PATCH_NOTES["2.10.2"]
    assert keys["patch-2.10.2"] == release.RELEASE_2102_TS and release.preview_2102(1, release.RELEASE_2102_TS)
    assert not release.maintenance(release.RELEASE_2102_TS)                    # 무점검


def test_310():
    import release
    note = PATCH_NOTES["3.1.0"]
    assert "키코" in note and "장부" not in note and "체크리스트" not in note and len(note) < 1500
    assert release.preview_310(release.TEST_GUILD)
    import calendar
    keys = {k: ts for k, ts, *_ in NOTICES}
    start = calendar.timegm((2026, 10, 23, 7, 30, 0))   # 3.1.0 점검 16:30 ~ 16:35 KST (지금 MAINTENANCE 는 3.2.0)
    assert keys["maint-3.1.0"] == keys["patch-3.1.0"] == calendar.timegm((2026, 10, 23, 6, 40, 0)) < start   # 15:40 KST 미리 발송


def test_330():
    import calendar
    import release
    note = PATCH_NOTES["3.3.0"]
    assert "3.2.1" not in PATCH_NOTES and "장부" not in note and "KBO" not in note and len(note) < 1000
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = calendar.timegm((2026, 10, 26, 0, 20, 0)), calendar.timegm((2026, 10, 26, 0, 25, 0))   # 3.3.0 점검 09:20 ~ 09:25 KST
    assert keys["maint-3.3.0"] == keys["patch-3.3.0"] < start == release.RELEASE_321_TS   # 미리 발송 · 점검 시작에 공개
    assert "LEGALTOTO" in note
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.3.0")
    assert P.notice_text("maint-3.3.0", title, body, keys["maint-3.3.0"]) == (title, body)


def test_340():
    import calendar
    import release
    from services.economy_db import COUPONS, COUPONS_340
    note = PATCH_NOTES["3.4.0"]
    assert "장부" not in note and "체크리스트" not in note and "%" not in note and len(note) < 1000
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = calendar.timegm((2026, 10, 26, 4, 20, 0)), calendar.timegm((2026, 10, 26, 4, 30, 0))   # 13:20 ~ 13:30 KST
    assert keys["maint-3.4.0"] == keys["patch-3.4.0"] < start == release.RELEASE_340_TS   # 미리 발송 · 점검 시작에 공개
    assert "UPGRADEBOT" in note and COUPONS["UPGRADEBOT"][0] == {"enh_stone": 1} and "UPGRADEBOT" in COUPONS_340
    assert not release.preview_340(123, start - 1) and release.preview_340(123, start)
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.4.0")
    assert "13:20 ~ 13:30" in body
    assert P.notice_text("maint-3.4.0", title, body, keys["maint-3.4.0"]) == (title, body)


def test_341():
    import calendar
    import release
    note = PATCH_NOTES["3.4.1"]
    assert "장부" not in note and "체크리스트" not in note and "%" not in note and len(note) < 600
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = calendar.timegm((2026, 10, 26, 7, 30, 0)), calendar.timegm((2026, 10, 26, 7, 35, 0))   # 16:30 ~ 16:35 KST
    assert keys["maint-3.4.1"] == keys["patch-3.4.1"] < start == release.RELEASE_341_TS   # 미리 발송 · 점검 시작에 공개
    assert not release.preview_341(123, start - 1) and release.preview_341(123, start)
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.4.1")
    assert "16:30 ~ 16:35" in body
    assert P.notice_text("maint-3.4.1", title, body, keys["maint-3.4.1"]) == (title, body)



def test_350():
    import calendar
    import release
    note = PATCH_NOTES["3.5.0"]
    assert "장부" not in note and "체크리스트" not in note and "%" not in note and len(note) < 600
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = calendar.timegm((2026, 10, 27, 3, 20, 0)), calendar.timegm((2026, 10, 27, 3, 25, 0))   # 12:20 ~ 12:25 KST (지금 MAINTENANCE 는 3.6.0)
    assert keys["maint-3.5.0"] == keys["patch-3.5.0"] < start == release.RELEASE_350_TS   # 미리 발송 · 점검 시작에 공개
    assert not release.preview_350(123, start - 1) and release.preview_350(123, start)
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.5.0")
    assert "12:20 ~ 12:25" in body
    assert P.notice_text("maint-3.5.0", title, body, keys["maint-3.5.0"]) == (title, body)


def test_360():
    import calendar
    import release
    note = PATCH_NOTES["3.6.0"]
    assert "장부" not in note and "체크리스트" not in note and "%" not in note and len(note) < 1200
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = calendar.timegm((2026, 10, 29, 4, 25, 0)), calendar.timegm((2026, 10, 29, 4, 30, 0))   # 13:25 ~ 13:30 KST (지금 MAINTENANCE 는 무기한 긴급 점검)
    assert keys["maint-3.6.0"] == keys["patch-3.6.0"] < start == release.RELEASE_360_TS   # 미리 발송 · 점검 시작에 공개
    assert not release.preview_360(123, start - 1) and release.preview_360(123, start)
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.6.0")
    assert "13:25 ~ 13:30" in body and "YOUTHTOKIKO" in note
    assert P.notice_text("maint-3.6.0", title, body, keys["maint-3.6.0"]) == (title, body)
    assert P.notice_text("maint-3.6.0", title, body, keys["maint-3.6.0"]) == (title, body)


def test_360_hold():
    import calendar
    import release
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = release.MAINTENANCE
    assert start == keys["maint-3.6.0-hold"] == calendar.timegm((2026, 10, 29, 6, 58, 0))
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.6.0-hold")
    assert P.notice_text("maint-3.6.0-hold", title, body, start + 60) == (title, body)   # 점검 중에 나간 안내


def test_361():
    import calendar
    import release
    from services.economy_db import COUPONS
    note = PATCH_NOTES["3.6.1"]
    assert "장부" not in note and "%" not in note and len(note) < 600 and "SORRYKIKO" in note
    assert COUPONS["SORRYKIKO"][0] == {"enh_stone": 1}
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = release.MAINTENANCE
    assert keys["maint-3.6.1"] == keys["patch-3.6.1"] == end == calendar.timegm((2026, 10, 29, 7, 44, 0))   # 16:44 KST 점검 끝
    assert release.maintenance(end - 1) and not release.maintenance(end)
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.6.1")
    assert P.notice_text("maint-3.6.1", title, body, end)[0] == "✅ 점검 완료 안내"


def test_320():
    import calendar
    import release
    note = PATCH_NOTES["3.2.0"]
    assert "3.1.1" not in PATCH_NOTES and "장부" not in note and "체크리스트" not in note and len(note) < 1200
    keys = {k: ts for k, ts, *_ in NOTICES}
    start, end = calendar.timegm((2026, 10, 24, 21, 30, 0)), calendar.timegm((2026, 10, 24, 22, 0, 0))   # 3.2.0 점검 06:30 ~ 07:00 KST
    assert keys["maint-3.2.0"] == keys["patch-3.2.0"] == calendar.timegm((2026, 10, 24, 21, 0, 0)) < start   # 06:00 KST 미리 발송
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-3.2.0")
    assert "06:30 ~ 07:00" in body
    assert P.notice_text("maint-3.2.0", title, body, keys["maint-3.2.0"]) == (title, body)
    # 기능은 점검 시작 전엔 테스트 서버만
    assert release.RELEASE_311_TS is None or release.RELEASE_311_TS >= start


if __name__ == "__main__":
    test_28_content()
    test_embeds()
    test_282()
    test_291()
    test_2101()
    test_2102()
    test_310()
    test_320()
    test_330()
    test_340()
    test_341()
    test_350()
    test_360()
    test_360_hold()
    test_361()
    print("OK: patch notes")
