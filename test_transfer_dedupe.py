"""이적 트래커 주제 중복 제거 회귀 테스트.

실행: .venv/Scripts/python.exe test_transfer_dedupe.py
"""
import tempfile
from pathlib import Path

import services.transfer_db as tdb
tdb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"

from services.transfer_db import TransferDB, topic_signature as sig  # noqa: E402


def test_same_story_different_outlets_shares_signature():
    """고유명사가 겹치면 매체가 달라도 같은 소식으로 묶인다."""
    a = "Romano gives Liverpool 'here we go' to attacker signing after Cody Gakpo update - Football365"
    b = "Liverpool complete Cody Gakpo swoop as Romano confirms - Sky Sports"
    assert sig(a) == sig(b) == "cody gakpo liverpool"


def test_different_stories_same_club_stay_separate():
    """구단명만 겹치는 서로 다른 소식은 묶이면 안 된다.

    예전엔 서명이 'chelsea' 하나로 떨어져 같은 날 다른 소식까지 눌렸다.
    """
    a = "Romano reveals Chelsea deadline day signing 'here we go' - LiveScore"
    b = "Chelsea 'furious' as Romano confirms deal 'cancelled' - Football365"
    assert sig(a) != sig(b)


def test_identical_headline_is_still_deduped():
    """고유명사가 하나뿐이어도 제목이 사실상 같으면 걸러야 한다."""
    a = "'Here we go' - Fabrizio Romano confirms Liverpool 'agreement' as green light given - TEAMtalk"
    b = "'Here we go' - Fabrizio Romano confirms Liverpool 'agreement' as green light given - LiveScore"
    assert sig(a) == sig(b)
    assert sig(a).startswith("t:")      # 정규화 제목 폴백


def test_outlet_suffix_is_stripped():
    base = "Romano gives Arsenal 'here we go' to Gyokeres deal"
    assert sig(base + " - Football365") == sig(base + " - Sky Sports")


def test_claim_topic_blocks_within_ttl():
    db = TransferDB()
    s = "cody gakpo liverpool"
    assert db.claim_topic(s) is True
    assert db.claim_topic(s) is False          # TTL 안 재시도는 차단
    assert db.claim_topic(s, ttl_seconds=0) is True   # TTL 지나면 다시 허용


def test_empty_signature_never_blocks():
    db = TransferDB()
    assert db.claim_topic("") is True
    assert db.claim_topic("") is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
