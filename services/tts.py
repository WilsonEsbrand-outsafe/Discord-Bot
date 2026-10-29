# services/tts.py — /tts 문장 읽어주기: 글 다듬기 · 음성 파일 만들기 (패치노트에 넣지 않는 기능)
# 목소리는 마이크로소프트 엣지 TTS(무료 · 키 없음). 엣지가 안 되면 구글 번역 TTS 로 넘어간다.
import asyncio
import re

MAX_LEN = 200   # 한 번에 읽을 수 있는 글자 수

VOICES = {   # 고르는 이름 → 엣지 목소리
    "선희": "ko-KR-SunHiNeural",
    "인준": "ko-KR-InJoonNeural",
    "현수": "ko-KR-HyunsuMultilingualNeural",
}
DEFAULT_VOICE = "선희"


def clean(text: str, names: dict[int, str] | None = None) -> str:
    """디스코드 표기를 읽을 수 있는 말로: 멘션 → 이름, 링크 → '링크', 서버 이모지 → 이름."""
    names = names or {}
    text = re.sub(r"<@!?(\d+)>", lambda m: names.get(int(m.group(1)), "누군가"), text)
    text = re.sub(r"<@&\d+>|<#\d+>", "", text)
    text = re.sub(r"<a?:(\w+):\d+>", r"\1", text)
    text = re.sub(r"https?://\S+", "링크", text)
    text = re.sub(r"(.)\1{5,}", r"\1\1\1\1\1", text)   # ㅋㅋㅋㅋㅋㅋㅋㅋ… 는 다섯 번까지만
    return re.sub(r"\s+", " ", text).strip()[:MAX_LEN]


async def _edge(text: str, voice: str, path: str) -> None:
    import edge_tts
    await asyncio.wait_for(edge_tts.Communicate(text, VOICES.get(voice, VOICES[DEFAULT_VOICE])).save(path), 20)


def _google(text: str, path: str) -> None:
    from gtts import gTTS
    gTTS(text, lang="ko").save(path)


async def synthesize(text: str, voice: str, path: str) -> str:
    """path 에 mp3 를 만든다. 쓴 엔진 이름('엣지' · '구글')을 돌려준다. 둘 다 안 되면 RuntimeError."""
    try:
        await _edge(text, voice, path)
        return "엣지"
    except Exception as e:
        print("⚠️ 엣지 TTS 실패, 구글로 넘어감:", repr(e))
    try:
        await asyncio.wait_for(asyncio.to_thread(_google, text, path), 20)
        return "구글"
    except Exception as e:
        raise RuntimeError(f"TTS 실패: {e!r}") from e
