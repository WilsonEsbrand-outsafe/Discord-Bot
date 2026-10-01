# test_admin.py — /관리자명령어: 관리자 명령어를 / 목록에서 빼 드롭다운으로 · 모달 입력(여러 장) · 실행 확인.
# 실행: venv/Scripts/python.exe test_admin.py
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import discord
from discord import app_commands

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"

import auth  # noqa: E402
import cogs.admin as ca  # noqa: E402
from cogs.economy import Economy  # noqa: E402

ran = {}


@app_commands.command(name="입력7", description="입력 7개짜리")
@app_commands.check(auth.owner_only)
async def seven(interaction, a: str, b: int, c: str, d: str, e: str, f: Optional[str] = None, g: Optional[int] = 3):
    ran["입력7"] = (a, b, c, d, e, f, g)
    await interaction.response.send_message("ok")


@app_commands.command(name="초기화테스트", description="입력 없는 위험한 명령어")
@app_commands.check(auth.owner_only)
async def zero(interaction):
    ran["초기화테스트"] = True
    await interaction.response.send_message("ok")


class FakeTree:   # CommandTree 에서 쓰는 부분만
    def __init__(self, cmds):
        self.c = {c.name: c for c in cmds}

    def add_command(self, cmd):
        self.c[cmd.name] = cmd

    def remove_command(self, name):
        return self.c.pop(name, None)

    def get_commands(self):
        return list(self.c.values())


def fill(modal, values: dict):
    """모달 칸에 입력한 것처럼 값을 채운다 (이름 → 값)."""
    for p, label in modal.fields:
        if p.name in values:
            comp = label.component
            if isinstance(comp, discord.ui.TextInput):
                comp._value = values[p.name]
            else:
                comp._values = [values[p.name]]


async def _flow():
    eco = Economy.__new__(Economy)
    eco.db = edb.EconomyDB()
    def fresh():   # cog 가 붙을 때처럼 명령어 사본
        return [c._copy_with(parent=None, binding=eco, set_on_binding=False)
                for c in vars(Economy).values() if isinstance(c, app_commands.Command)]
    cmds = fresh()
    tree = FakeTree(cmds + [seven, zero])
    owner = {c.name for c in tree.get_commands() if auth.is_owner_command(c)}
    assert {"돈지급", "아이템지급", "입력7", "초기화테스트"} <= owner and "지갑" not in owner

    # 동기화: 관리자 명령어는 / 목록에서 빠지고 메뉴로 · cog 리로드 뒤 다시 불러도 같은 이름은 교체
    assert auth.collect_owner_commands(tree) == len(owner)
    assert not {c.name for c in tree.get_commands()} & owner and "지갑" in {c.name for c in tree.get_commands()}
    for c in fresh():   # cog 리로드 → 새 명령어가 다시 맨 위에
        tree.add_command(c)
    assert auth.collect_owner_commands(tree) == len(owner) and set(auth.ADMIN_COMMANDS) == owner

    sent, modals = [], []
    async def rec(*a, **k):
        sent.append((a, k))
    async def modal(m):
        modals.append(m)
    def inter(uid):
        resp = SimpleNamespace(send_message=rec, edit_message=rec, send_modal=modal, defer=rec, is_done=lambda: False)
        return SimpleNamespace(user=SimpleNamespace(id=uid, display_name="주인", display_avatar=SimpleNamespace(url="https://x/a.png")),
                               response=resp, followup=SimpleNamespace(send=rec), guild=None)
    boss, other = inter(auth.OWNER_ID), inter(auth.OWNER_ID + 1)
    cog = ca.Admin.__new__(ca.Admin)

    # /관리자명령어: 모두에게 보이지만(체크 없음) 봇 주인만 실행
    assert not auth.is_owner_command(ca.Admin.admin) and ca.Admin.admin.default_permissions is None
    await ca.Admin.admin.callback(cog, other)
    assert "봇 주인만" in sent[-1][1]["embed"].title and sent[-1][1]["ephemeral"]
    await ca.Admin.admin.callback(cog, boss)
    menu = sent[-1][1]["view"]
    assert sent[-1][1]["ephemeral"] and {o.value for o in menu.menu.options} == owner
    assert not await menu.interaction_check(other) and await menu.interaction_check(boss)

    # 아이템지급: 유저 드롭다운 · 아이템 드롭다운 · 수량 입력 → 실제 명령어가 돈다
    menu.menu._values = ["아이템지급"]
    await menu._pick(boss)
    m = modals[-1]
    kinds = {p.name: type(label.component).__name__ for p, label in m.fields}
    assert kinds == {"user": "UserSelect", "아이템": "Select", "수량": "TextInput"} and m.title == "/아이템지급"
    target = SimpleNamespace(id=55, mention="<@55>")
    fill(m, {"user": target, "아이템": "muffler", "수량": "2"})
    await m.on_submit(boss)
    assert "지급" in sent[-1][1]["embed"].title and (await eco.db.inventory(55))[0] == {"muffler": 2}

    # 숫자 칸에 글자 → 안내만 하고 실행 안 함
    await menu._pick(boss)
    fill(modals[-1], {"user": target, "아이템": "muffler", "수량": "두 개"})
    await modals[-1].on_submit(boss)
    assert "숫자" in sent[-1][0][0] and (await eco.db.inventory(55))[0] == {"muffler": 2}

    # 입력 7개: 필수 5개가 첫 장 → 남은 칸이 모두 선택이면 [바로 실행] (비운 칸은 기본값)
    menu.menu._values = ["입력7"]
    await menu._pick(boss)
    m = modals[-1]
    assert m.title == "/입력7 (1/2)" and [p.name for p, _ in m.fields] == ["a", "b", "c", "d", "e"]
    fill(m, {"a": "A", "b": "1,000", "c": "C", "d": "D", "e": "E"})
    await m.on_submit(boss)
    nxt = sent[-1][1]["view"]
    assert [b.label for b in nxt.children] == ["다음 입력", "바로 실행"]
    await nxt.run_now.callback(boss)
    assert ran["입력7"] == ("A", 1000, "C", "D", "E", None, 3)
    # 두 번째 장까지 채우면 그 값으로
    await menu._pick(boss)
    fill(modals[-1], {"a": "A", "b": "2", "c": "C", "d": "D", "e": "E"})
    await modals[-1].on_submit(boss)
    await sent[-1][1]["view"].next_page.callback(boss)
    assert modals[-1].title == "/입력7 (2/2)"
    fill(modals[-1], {"f": "F", "g": "9"})
    await modals[-1].on_submit(boss)
    assert ran["입력7"] == ("A", 2, "C", "D", "E", "F", 9)

    # 입력 없는 명령어: 바로 실행하지 않고 한 번 더 확인 → 취소 / 실행
    menu.menu._values = ["초기화테스트"]
    await menu._pick(boss)
    confirm = sent[-1][1]["view"]
    assert "실행할까요" in sent[-1][1]["embed"].title and "초기화테스트" not in ran
    await confirm.cancel.callback(boss)
    assert "초기화테스트" not in ran
    await menu._pick(boss)
    await sent[-1][1]["view"].go.callback(boss)
    assert ran["초기화테스트"] is True


def test_admin():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_admin()
    print("OK: admin")
