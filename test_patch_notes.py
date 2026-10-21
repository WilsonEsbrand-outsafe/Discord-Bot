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
    v = PATCH_NOTES["2.82"]
    for word in ("장부", "체크리스트", "가계부"):
        assert word not in v, word
    import release
    keys = {k: ts for k, ts, *_ in NOTICES}
    assert keys["maint-2.82"] == keys["patch-2.82"] < release.MAINTENANCE[0]   # 점검 전에 안내 · 패치노트
    assert release.MAINTENANCE[1] - release.MAINTENANCE[0] == 180
    assert release.released(release.MAINTENANCE[0])                            # 2.8 은 공개된 그대로
    _, _, title, body = next(n for n in NOTICES if n[0] == "maint-2.82")
    start, end = release.MAINTENANCE
    assert P.notice_text("maint-2.82", title, body, start - 60) == (title, body)              # 점검 전: 그대로
    t2, b2 = P.notice_text("maint-2.82", title, body, end)                                    # 점검 뒤: 점검 완료
    assert t2 == "✅ 점검 완료 안내" and "점검이 끝났어요" in b2 and "쓸 수 없어요" not in b2
    assert P.notice_text("patch-2.82", "📢", "x", end) == ("📢", "x")


if __name__ == "__main__":
    test_28_content()
    test_embeds()
    test_282()
    print("OK: patch notes")
