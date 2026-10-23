# test_kiko.py — 2.10.3 키코 디자인: 테스트 서버에만 로고 파랑 · 키코 푸터 · 화면 푸터를 달아도 아이콘은 키코
# 실행: venv/Scripts/python.exe test_kiko.py
from types import SimpleNamespace

import release
from services import ui


def _member(gid):
    return SimpleNamespace(id=1, display_name="규완", display_avatar=SimpleNamespace(url="https://x/a.png"),
                           guild=SimpleNamespace(id=gid))


def test_test_server_gets_kiko():
    ui.BOT_ICON = "https://x/kiko.png"
    e = ui.card("t", "", ui.INFO, _member(release.TEST_GUILD), "🎒 아이템")
    assert e.color.value == ui.KIKO
    assert e.footer.text.startswith("키코 · ") and e.footer.text[5:] in ui.KIKO_LINES[ui.KIKO]
    assert e.footer.icon_url == "https://x/kiko.png"
    w = ui.card("t", "", ui.WIN, _member(release.TEST_GUILD))
    assert w.color.value == ui.WIN and w.footer.text[5:] in ui.KIKO_LINES[ui.WIN]
    w.set_footer(text="화면 안내")   # 화면이 푸터를 쓰면 문구는 그대로, 아이콘은 키코
    assert w.footer.text == "화면 안내" and w.footer.icon_url == "https://x/kiko.png"

    assert ui.result_thumb("✅", ui.WIN, _member(release.TEST_GUILD)).endswith("/assets/kiko/win.png")
    assert ui.result_thumb("☠️", ui.DOOM, _member(release.TEST_GUILD)).endswith("/assets/kiko/doom.png")
    assert "twemoji" in ui.result_thumb("🏇", ui.DARK, _member(release.TEST_GUILD))   # 연출 중엔 이모지 그대로



def test_scene_thumbs():
    m = _member(release.TEST_GUILD)
    assert ui.result_thumb("✅", ui.WIN, m, "ufc_5").endswith("/assets/kiko/scenes/ufc_5.png")
    assert ui.result_thumb("🐛", ui.LOSE, m, "bat_11").endswith("/assets/kiko/scenes/bat_11.png")
    assert ui.result_thumb("✅", ui.WIN, m, "ufc_99").endswith("/assets/kiko/win.png")   # 장면이 없으면 표정 그림
    assert "twemoji" in ui.result_thumb("✅", ui.WIN, _member(1), "ufc_5") or release.released_2103()


def test_scene_counts_match_tables():
    from cogs.economy import Economy
    tables = {"pk": Economy.PK_TABLE, "bat": Economy.BAT_TABLE, "hoop": Economy.HOOP_TABLE, "ufc": Economy.UFC_TABLE,
              "race": Economy.RACE_RESULTS}
    for game, table in tables.items():
        assert ui.KIKO_SCENES[game] == len(table), game
    from cogs.race import Snail, ROW_N
    from services.fishing import GRADES
    assert ui.KIKO_SCENES["snail"] == len(Snail.results) and ui.KIKO_SCENES["row"] == ROW_N
    assert ui.KIKO_SCENES["fish"] == len(GRADES)


def test_scene_url_named():
    m = _member(release.TEST_GUILD)
    assert ui.scene_url("dg_boss", m).endswith("/assets/kiko/scenes/dg_boss.png")
    assert ui.scene_url("fish_4", m).endswith("/assets/kiko/scenes/fish_4.png")
    assert ui.scene_url("dg_nope", m) is None and ui.scene_url(None, m) is None
    if not release.released_2103():
        assert ui.scene_url("dg_boss", _member(1)) is None   # 공개 전 서버엔 그림 없음


def test_art_files_exist():
    from pathlib import Path
    for pose in ui.KIKO_POSE.values():
        assert (Path(__file__).parent / "assets" / "kiko" / f"{pose}.png").exists(), pose
    for game, n in ui.KIKO_SCENES.items():
        for i in range(n):
            assert (Path(__file__).parent / "assets" / "kiko" / "scenes" / f"{game}_{i}.png").exists(), (game, i)
    for name in ui.KIKO_NAMED:
        assert (Path(__file__).parent / "assets" / "kiko" / "scenes" / f"{name}.png").exists(), name


def test_other_servers_unchanged_before_release():
    if release.released_2103():
        return
    e = ui.card("t", "", ui.INFO, _member(1), "🎒 아이템")
    assert e.color.value == ui.INFO and e.footer.text is None
    assert ui.card("t").footer.text is None   # 작성자 없는 카드도 그대로
    assert "twemoji" in ui.result_thumb("✅", ui.WIN, _member(1))


if __name__ == "__main__":
    test_test_server_gets_kiko()
    test_scene_thumbs()
    test_scene_counts_match_tables()
    test_scene_url_named()
    test_art_files_exist()
    test_other_servers_unchanged_before_release()
    print("ok")
