# services/undot.py — 3.8.2 규완님 'AI 티 나는 더러운 · 모양 전체적으로 없애기'
# 봇이 디스코드로 보내는 모든 글(메시지 · 임베드 · 버튼 · 메뉴 · 창 · / 명령어 설명)에서 가운뎃점 '·' 을 지운다.
# 코드 곳곳의 글을 하나하나 고치는 대신 보내기 직전에 한 번에 바꾼다 — 새로 쓰는 글도 자동으로 깨끗하다.
#   'A · B · C' → 'A, B, C' / 줄 맨 앞 · 맨 끝의 '·' 은 지운다 / 'A·B' → 'A, B'
# 공개 전엔 테스트 서버로 가는 글만 (release.preview_382). 서버를 모르는 글(DM 등)은 공개된 뒤부터.
import re
import time
from typing import Any, Callable, Optional

import release

DOT = "·"
_LEAD = re.compile(r"(^|\n)([ \t　]*)·[ \t]*")      # 줄 맨 앞 '· ' (들여쓰기는 둔다)
_TRAIL = re.compile(r"[ \t]*·[ \t]*(?=\n|$)")          # 줄 맨 끝 ' ·'
_OPEN = re.compile(r"([(\[])[ \t]*·[ \t]*")             # '(· KBO)' → '(KBO)'
_MID = re.compile(r"[ \t]*·[ \t]*")                     # 나머지 → ', '
_DUP = re.compile(r",(?:[ \t]*,)+")


def undot(text: str) -> str:
    if not isinstance(text, str) or DOT not in text:
        return text
    text = _LEAD.sub(r"\1\2", text)
    text = _TRAIL.sub("", text)
    text = _OPEN.sub(r"\1", text)
    text = _MID.sub(", ", text)
    return _DUP.sub(",", text)


# 바꾸는 열쇠: 글자로 보이는 것만. custom_id · url · 메뉴 선택지 value(봇이 읽는 값)는 건드리지 않는다.
_TEXT_KEYS = {"content", "title", "description", "name", "text", "label", "placeholder"}


def clean(obj: Any) -> Any:
    """디스코드로 보내는 JSON 묶음에서 보이는 글만 undot (제자리에서 고치고 그대로 돌려준다)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                # 임베드 칸(field)의 value 는 글이다 (discord.py 는 칸마다 inline 을 같이 보낸다)
                if k in _TEXT_KEYS or (k == "value" and "inline" in obj):
                    obj[k] = undot(v)
            elif isinstance(v, (dict, list)):
                clean(v)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                clean(v)
    return obj


# ───────────── 디스코드로 보내기 직전에 끼운다 (bot.py 가 install 을 부른다) ─────────────
_TOKENS: dict[str, tuple[Optional[int], float]] = {}   # 상호작용 토큰 → (서버, 시각) — 응답 · 후속 메시지가 어느 서버로 가는지
_TOKEN_TTL = 16 * 60                                    # 디스코드 상호작용 토큰은 15분


def remember(token: str, guild_id: Optional[int]):
    now = time.time()
    _TOKENS[token] = (guild_id, now)
    if len(_TOKENS) > 5000:
        for t, (_, ts) in list(_TOKENS.items()):
            if now - ts > _TOKEN_TTL:
                _TOKENS.pop(t, None)


def guild_of(route, get_channel: Callable[[int], Any]) -> tuple[bool, Optional[int]]:
    """(서버를 알았나, 서버)."""
    gid = getattr(route, "guild_id", None)
    if gid:
        return True, int(gid)
    tok = getattr(route, "webhook_token", None)
    if tok and tok in _TOKENS:
        return True, _TOKENS[tok][0]
    cid = getattr(route, "channel_id", None)
    if cid:
        ch = get_channel(int(cid))
        g = getattr(ch, "guild", None)
        if g is not None:
            return True, g.id
    return False, None


def on_for(route, get_channel: Callable[[int], Any]) -> bool:
    known, gid = guild_of(route, get_channel)
    return release.preview_382(gid) if known and gid else release.released_382()


def _clean_multipart(parts):
    import json
    for p in parts or []:
        if isinstance(p, dict) and p.get("name") == "payload_json" and isinstance(p.get("value"), str):
            p["value"] = json.dumps(clean(json.loads(p["value"])), separators=(",", ":"), ensure_ascii=True)


def install(get_channel: Callable[[int], Any]):
    """discord.py 의 HTTP 보내기 두 곳(봇 · 웹훅/상호작용)과 Interaction 생성에 끼운다. 두 번 불러도 한 번만."""
    import discord
    from discord import http as dhttp
    from discord.webhook import async_ as wh

    if getattr(dhttp.HTTPClient.request, "_undot", False):
        return

    orig_init = discord.Interaction.__init__

    def init(self, *a, **kw):
        orig_init(self, *a, **kw)
        try:
            remember(self.token, self.guild_id)
        except Exception:
            pass
    discord.Interaction.__init__ = init

    orig_http = dhttp.HTTPClient.request

    async def http_request(self, route, *, files=None, form=None, **kw):
        try:
            if on_for(route, get_channel):
                if isinstance(kw.get("json"), (dict, list)):
                    clean(kw["json"])
                if form:
                    _clean_multipart(form)
        except Exception as ex:   # 글 고치기가 실패해도 보내는 건 막지 않는다
            print(f"[undot] {type(ex).__name__}: {ex}")
        return await orig_http(self, route, files=files, form=form, **kw)
    http_request._undot = True
    dhttp.HTTPClient.request = http_request

    orig_wh = wh.AsyncWebhookAdapter.request

    async def wh_request(self, route, session, *, payload=None, multipart=None, **kw):
        try:
            if on_for(route, get_channel):
                if isinstance(payload, (dict, list)):
                    clean(payload)
                if multipart:
                    _clean_multipart(multipart)
        except Exception as ex:
            print(f"[undot] {type(ex).__name__}: {ex}")
        return await orig_wh(self, route, session, payload=payload, multipart=multipart, **kw)
    wh.AsyncWebhookAdapter.request = wh_request
