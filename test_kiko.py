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


def test_art_files_exist():
    from pathlib import Path
    for pose in ui.KIKO_POSE.values():
        assert (Path(__file__).parent / "assets" / "kiko" / f"{pose}.png").exists(), pose


def test_other_servers_unchanged_before_release():
    if release.released_2103():
        return
    e = ui.card("t", "", ui.INFO, _member(1), "🎒 아이템")
    assert e.color.value == ui.INFO and e.footer.text is None
    assert ui.card("t").footer.text is None   # 작성자 없는 카드도 그대로
    assert "twemoji" in ui.result_thumb("✅", ui.WIN, _member(1))


if __name__ == "__main__":
    test_test_server_gets_kiko()
    test_art_files_exist()
    test_other_servers_unchanged_before_release()
    print("ok")
