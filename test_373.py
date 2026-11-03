"""3.7.3: /스카우트 원석은 한 번에 한 종류 × 여러 개 (테스트 서버 먼저)."""
import random
import sqlite3

import release
from services import gems


def test_single_kind():
    rng = random.Random(5)
    for lv in (1, 3, 5):
        for _ in range(200):
            r = gems.roll_gems(lv, rng, single=True)
            assert len(r) == 1 and lv + 1 <= sum(r.values()) <= lv + 3


def test_scout_switch():
    from cogs.economy import Economy as E
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE wallets (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")
    eco = E.__new__(E)
    assert release.RELEASE_373_TS == release.MAINTENANCE[0]   # 3.7.3 은 연장 점검 중 공개
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        for _ in range(30):
            _, _, info = eco._gem_roll(2, con, 9)
            assert len(info["got"]) <= 1
    finally:
        release.GUILD.reset(tok)


def test_gem_images_exist():
    from cogs.economy import GEM_IMG_DIR
    from services import gems
    for g in gems.ORDER:
        assert (GEM_IMG_DIR / f"{g}.png").is_file()
