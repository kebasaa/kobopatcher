"""The patch catalog, the selection flags, saving overrides, the menu,
ReplaceText and conditional extra files."""

import io
import os
import sys

import pytest

from kobopatcher import catalog, cli, config, tui
from kobopatcher.catalog import Entry, Selection
from kobopatcher.patchfile import kobopatch as K
from kobopatcher.patchlib.patcher import Patcher, PatchError

PATCH_A = """
Bigger fonts:
  - Enabled: yes
  - Category: Reading
  - Description: Makes fonts bigger.
  - FindReplaceString: {Find: "small", Replace: "large"}
Left margin:
  - Enabled: no
  - Category: Reading
  - PatchGroup: margins
  - FindReplaceString: {Find: "aaaa", Replace: "bbbb"}
Right margin:
  - Enabled: no
  - Category: Reading
  - PatchGroup: margins
  - FindReplaceString: {Find: "cccc", Replace: "dddd"}
Shared:
  - Enabled: no
  - Category: Speed
  - Experimental: yes
  - FindReplaceString: {Find: "x", Replace: "y"}
"""

PATCH_B = """
Shared:
  - Enabled: no
  - Category: Speed
  - Experimental: yes
  - FindReplaceString: {Find: "x", Replace: "y"}
"""


@pytest.fixture
def patchset(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(PATCH_A, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(PATCH_B, encoding="utf-8")
    conf = tmp_path / "kobopatch.yaml"
    conf.write_text(
        "# my patch set\nversion: 1.2.3\nin: fw.zip\nout: out.tgz\nlog: log.txt\npatches:\n"
        "  a.yaml: usr/local/Kobo/liba.so\n  b.yaml: usr/local/Kobo/libb.so\n"
        "overrides:\n  a.yaml:\n    Bigger fonts: no\n# trailing comment\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return conf


def test_meta_instructions():
    ps = K.parse(PATCH_A.encode())
    assert ps.parsed["Bigger fonts"].category == "Reading"
    assert ps.parsed["Shared"].experimental is True
    ps.validate()


def test_catalog_entries(patchset):
    cfg, cat = catalog.load("kobopatch.yaml")
    names = [e.name for e in cat.entries]
    assert names == ["Bigger fonts", "Left margin", "Right margin", "Shared"]  # experimental last
    shared = cat.get("shared")
    assert len(shared.targets) == 2 and shared.experimental
    big = cat.get("bigger-fonts")
    assert big.default is True and big.enabled is False  # the override
    assert cat.categories() == ["Reading", "Speed"]


def test_group_radio_and_flags(patchset):
    cfg, cat = catalog.load("kobopatch.yaml")
    left, right = cat.get("left-margin"), cat.get("right-margin")
    cat.set(left, True)
    assert cat.set(right, True) == [left] and not left.enabled and right.enabled
    cat.apply_flags(enable=["*margin"], disable=[], only=[])
    assert sum(e.enabled for e in (left, right)) == 1  # a group allows one
    cat.apply_flags(only=["bigger*"])
    assert [e.name for e in cat.entries if e.enabled] == ["Bigger fonts"]
    with pytest.raises(catalog.CatalogError):
        cat.apply_flags(enable=["nothing-like-this"])


def test_overrides_and_saving(patchset):
    cfg, cat = catalog.load("kobopatch.yaml")
    cat.set(cat.get("shared"), True)
    cat.set(cat.get("bigger-fonts"), True)  # back to the file's default
    ov = cat.overrides()
    assert ov == {"a.yaml": {"Shared": True}, "b.yaml": {"Shared": True}}
    bak = catalog.save_overrides("kobopatch.yaml", ov)
    text = open("kobopatch.yaml", encoding="utf-8").read()
    assert text.startswith("# my patch set\n") and "# trailing comment" in text
    assert "Bigger fonts" not in text
    assert open(bak, encoding="utf-8").read().count("Bigger fonts: no") == 1
    cfg2 = config.load(text.encode())
    assert cfg2.overrides == {"a.yaml": {"Shared": True}, "b.yaml": {"Shared": True}}
    # saving again replaces the block instead of adding another
    catalog.save_overrides("kobopatch.yaml", {})
    text = open("kobopatch.yaml", encoding="utf-8").read()
    assert text.count("overrides:") == 1 and "Written by kobopatcher" in text


def test_cli_list(patchset, capsys):
    assert cli.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "[ ] bigger-fonts" in out and "NEW/EXPERIMENTAL" in out
    assert cli.main(["--list", "--json", "--enable", "left-margin"]) == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in data if d["enabled"]] == ["left-margin"]


def test_cli_save(patchset, capsys):
    assert cli.main(["--list", "--save", "--enable", "right-margin"]) == 0
    text = open("kobopatch.yaml", encoding="utf-8").read()
    assert '"Right margin": yes' in text


# --- the menu --------------------------------------------------------------------------------


def _sel():
    es = []
    for i, (name, cat, groups, exp) in enumerate(
        [("One", "A", set(), False), ("Two", "A", {("f", "g")}, False), ("Three", "A", {("f", "g")}, False), ("New", "B", set(), True)]
    ):
        e = Entry("e%d" % i, name, cat, "Description of %s" % name, exp)
        e.groups = groups
        es.append(e)
    v = Entry("val", "With value", "B")
    v.value = "abc"
    es.append(v)
    return Selection(es, "Test")


def test_menu_state():
    sel = _sel()
    m = tui.MenuState(sel)
    assert m.current_entry().name == "One"  # skips the category header
    m.key("space")
    assert sel.entries[0].enabled
    m.key("down")
    m.key("space")
    m.key("down")
    m.key("space")  # Three switches Two off
    assert not sel.entries[1].enabled and sel.entries[2].enabled and "Two" in m.message
    m.key("home")
    m.key("enter")  # collapse A
    assert len([r for r in m.rows if r.kind == "entry"]) == 2
    m.key("C")
    m.key("C")
    m.key("/")
    for ch in "value":
        m.key(ch)
    m.key("enter")
    assert [r.entry.name for r in m.rows if r.kind == "entry"] == ["With value"]
    m.key("e")
    m.key("backspace")
    m.key("Z")
    m.key("enter")
    assert sel.get("val").value == "abZ" and sel.get("val").enabled
    m.key("q")
    assert m.confirm_quit
    m.key("n")
    assert m.result is None
    assert m.key("s") == "save"


def test_menu_reset_and_quit():
    sel = _sel()
    m = tui.MenuState(sel)
    m.key("space")
    m.key("r")
    assert not sel.changed()
    assert m.key("q") == "quit"


def test_plain_menu(monkeypatch):
    sel = _sel()
    inputs = iter(["1", "2 3", "d 1", "x", "s"])
    out = []
    res = tui._run_plain(tui.MenuState(sel), "ctx", inp=lambda p="": next(inputs), out=out.append)
    assert res == "save"
    assert sel.entries[0].enabled and sel.entries[2].enabled and not sel.entries[1].enabled
    assert any("Not understood" in o for o in out)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_curses_in_a_pty():
    pty = pytest.importorskip("pty")
    import select
    import time

    pid, fd = pty.fork()
    if pid == 0:  # child: run the menu and report through the exit status
        os.environ["TERM"] = "xterm"
        os.environ.pop("KOBOPATCHER_NO_TUI", None)
        # pytest replaced these with its capture objects; use the pty
        sys.stdin = open(0, "r", closefd=False)
        sys.stdout = open(1, "w", closefd=False)
        sel = _sel()
        res = tui.run(sel)
        os._exit(10 + (res == "save") * 1 + sel.entries[0].enabled * 2)
    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    out = b""

    def read(t=1.0):
        nonlocal out
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return

    read(1.5)
    os.write(fd, b" ")
    read(0.5)
    os.write(fd, b"s")
    read(1.0)
    _, status = os.waitpid(pid, 0)
    assert b"Test" in out and b"One" in out
    assert os.WEXITSTATUS(status) == 13


# --- ReplaceText and conditional files ------------------------------------------------------------


def test_replace_text():
    p = Patcher(b"#!/bin/sh\necho a\necho b\necho a\n")
    p.replace_text("echo b\n", "echo b &\n")
    assert bytes(p.get_bytes()) == b"#!/bin/sh\necho a\necho b &\necho a\n"
    with pytest.raises(PatchError, match="occurs 2 times"):
        p.replace_text("echo a", "x")
    p.replace_text("echo a", "x", 2)
    with pytest.raises(PatchError, match="could not find"):
        p.replace_text("nope", "x")
    with pytest.raises(PatchError, match="ELF"):
        Patcher(b"\x7fELF....").replace_text("ELF", "XXXXX")


def test_replace_text_instruction():
    ps = K.parse(b"P:\n  - Enabled: yes\n  - ReplaceText: {Find: \"a\\n\", Replace: \"bb\\n\"}\n")
    pt = Patcher(b"x\na\n")
    ps.apply_to(pt)
    assert bytes(pt.get_bytes()) == b"x\nbb\n"
    with pytest.raises(K.KobopatchError, match="Find must not be empty"):
        K.parse(b"P:\n  - Enabled: yes\n  - ReplaceText: {Find: \"\", Replace: x}\n").validate()


def test_conditional_files():
    cfg = config.load(
        b"version: 1\nin: x\nout: o\nlog: l\npatches:\n  a.yaml: t\nfiles:\n"
        b"  s.qss:\n    dest: etc/kobo.qss\n    if: 'a.yaml: Use it'\n    mode: 0644\n  t.txt: [x/a, x/b]\n"
    )
    fd = cfg.files["s.qss"][0]
    assert (fd.dest, fd.cond, fd.mode) == ("etc/kobo.qss", "a.yaml: Use it", 0o644)
    assert [d.dest for d in cfg.files["t.txt"]] == ["x/a", "x/b"]


PATCH_NR = """
Safe tweak:
  - Enabled: no
  - Category: Speed
  - FindReplaceString: {Find: "a", Replace: "b"}
Risky tweak:
  - Enabled: no
  - Category: Speed
  - NotRecommended: it makes every start 20 s slower
  - FindReplaceString: {Find: "c", Replace: "d"}
"""


@pytest.fixture
def nr_set(tmp_path, monkeypatch):
    (tmp_path / "n.yaml").write_text(PATCH_NR, encoding="utf-8")
    (tmp_path / "kobopatch.yaml").write_text(
        "version: 1.2.3\nin: fw.zip\nout: out.tgz\nlog: log.txt\npatches:\n  n.yaml: usr/local/Kobo/libn.so\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_not_recommended_parse():
    ps = K.parse(PATCH_NR.encode())
    assert ps.parsed["Risky tweak"].not_recommended == "it makes every start 20 s slower"
    assert ps.parsed["Safe tweak"].not_recommended == ""
    ps.validate()
    with pytest.raises(Exception, match="NotRecommended needs a reason"):
        K.parse(b'X:\n  - NotRecommended: ""\n  - FindReplaceString: {Find: "a", Replace: "b"}\n')


def test_not_recommended_flags_and_list(nr_set, capsys):
    cfg, cat = catalog.load("kobopatch.yaml")
    risky, safe = cat.get("risky-tweak"), cat.get("safe-tweak")
    assert risky.not_recommended and risky.to_json()["not_recommended"]
    cat.apply_flags(enable=["*"])
    assert safe.enabled and not risky.enabled  # a glob leaves it off
    cat.apply_flags(only=["*tweak"])
    assert safe.enabled and not risky.enabled
    cat.apply_flags(enable=["risky-tweak"])
    assert risky.enabled  # named: switched on
    assert cli.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "risky-tweak" in out and "NOT RECOMMENDED" in out and "advises leaving them off" in out


def test_not_recommended_menu(nr_set):
    cfg, cat = catalog.load("kobopatch.yaml")
    state = tui.MenuState(cat, [])
    risky = cat.get("risky-tweak")
    assert "NOT RECOMMENDED" in state.row_text(next(r for r in state.rows if r.kind != "cat" and r.entry is risky))
    state.cursor = next(i for i, r in enumerate(state.rows) if r.kind == "cat")
    state.key("a")
    assert not risky.enabled and cat.get("safe-tweak").enabled and "NOT RECOMMENDED" in state.message
    state.cursor = next(i for i, r in enumerate(state.rows) if r.kind != "cat" and r.entry is risky)
    assert state.detail_lines(80)[0].startswith("NOT RECOMMENDED: it makes")
    state.key("space")
    assert risky.enabled and state.message.startswith("Not recommended:")
