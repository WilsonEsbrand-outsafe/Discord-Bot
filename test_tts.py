# test_tts.py — 🔊 /tts: 글 다듬기 · 엣지 실패 시 구글로 · 테스트 서버에만 보이기 · 가입 전에도 쓸 수 있음
import asyncio

import release
import services.tts as T


def test_clean():
    assert T.clean("<@123> 안녕 <@!456>", {123: "규완"}) == "규완 안녕 누군가"
    assert T.clean("이거 봐 https://example.com/a?b=1 <:kiko:999> <#1> <@&2>") == "이거 봐 링크 kiko"
    assert T.clean("ㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋ") == "ㅋㅋㅋㅋㅋ"
    assert T.clean("  여러   줄\n\n글  ") == "여러 줄 글"
    assert len(T.clean("가나다" * 200)) == T.MAX_LEN
    assert T.clean("https://x.y") == "링크" and T.clean("<#5>") == ""


def test_fallback(monkeypatch):
    async def bad(*a):
        raise OSError("blocked")
    monkeypatch.setattr(T, "_edge", bad)
    seen = []
    monkeypatch.setattr(T, "_google", lambda text, path: seen.append((text, path)))
    assert asyncio.run(T.synthesize("안녕", "인준", "/tmp/x.mp3")) == "구글" and seen == [("안녕", "/tmp/x.mp3")]

    def worse(text, path):
        raise OSError("also blocked")
    monkeypatch.setattr(T, "_google", worse)
    try:
        asyncio.run(T.synthesize("안녕", "인준", "/tmp/x.mp3"))
        assert False
    except RuntimeError:
        pass


def test_edge_first(monkeypatch):
    used = []

    async def ok(text, voice, path):
        used.append(voice)
    monkeypatch.setattr(T, "_edge", ok)
    assert asyncio.run(T.synthesize("안녕", "현수", "/tmp/x.mp3")) == "엣지" and used == ["현수"]


def test_test_guild_only():
    assert "tts" not in release.hidden_commands(release.TEST_GUILD)
    assert "tts" in release.hidden_commands(757761125403066419)
    assert release.TTS_GUILDS == {release.TEST_GUILD}


def test_gate_free():
    src = open("bot.py", encoding="utf-8").read()
    assert src.count('"cogs.tts"') == 2   # 켤 때 · 리로드 둘 다
    assert '"명령어", "tts"}' in src


def test_cog_loads():
    import cogs.tts as C
    cmd = C.TTS.tts
    assert cmd.name == "tts" and [p.name for p in cmd.parameters] == ["문장", "목소리"]
