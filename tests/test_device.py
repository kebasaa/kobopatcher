"""kobopatcher device, on copies of a Kobo's .kobo folder (never a real Kobo)."""

import os
import shutil
import sqlite3

import pytest

from kobopatcher import device

ARCHIVE = os.path.expanduser("~/Documents/Github/kobo_aura_h2o/archive/.kobo")

CONF = """[ApplicationPreferences]
SideloadedMode=false

[Reading]
readingFontFamily=Georgia

[PowerOptions]
AutoOffMinutes=60
"""


@pytest.fixture
def kobo(tmp_path):
    """A fake Kobo drive: the archived version file and config if present, else made-up ones."""
    root = tmp_path / "KOBOeReader"
    k = root / ".kobo" / "Kobo"
    k.mkdir(parents=True)
    src_version = os.path.join(ARCHIVE, "version")
    if os.path.isfile(src_version):
        shutil.copyfile(src_version, root / ".kobo" / "version")
    else:
        (root / ".kobo" / "version").write_text("N250000000000,2.6.35.3,4.38.23429,2.6.35.3,2.6.35.3,00000000-0000-0000-0000-000000000370")
    (k / "Kobo eReader.conf").write_text(CONF, encoding="utf-8")
    db = sqlite3.connect(root / ".kobo" / "KoboReader.sqlite")
    db.executescript(
        "CREATE TABLE content (ContentID TEXT, DateLastRead TEXT);"
        "INSERT INTO content VALUES ('a', '2012-05-01T10:00:00Z'), ('b', '2012-05-03T10:00:00Z'), ('c', '2024-01-01T00:00:00Z');"
        "CREATE TABLE AnalyticsEvents (Id TEXT, Mandatory INTEGER);"
        "INSERT INTO AnalyticsEvents VALUES ('1', 1), ('2', 0), ('3', NULL);"
    )
    db.close()
    (root / ".kobo" / "BookReader.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    return root


def test_info(kobo, capsys):
    assert device.main(["--mount", str(kobo), "info"]) == 0
    out = capsys.readouterr().out
    assert "Kobo Aura H2O" in out and "4.38.23429" in out
    assert "N250" in out and "544000" not in out  # no full serial number


def test_conf_editor_roundtrip():
    c = device.KoboConf(CONF)
    assert c.text() == CONF
    c.set("Reading", "disableRegal", "true")
    c.set("ReadingLife", "StatsTrackingEnabled", "false")
    c.remove("PowerOptions", "AutoOffMinutes")
    t = c.text()
    assert "[Reading]\nreadingFontFamily=Georgia\ndisableRegal=true\n" in t
    assert t.endswith("[ReadingLife]\nStatsTrackingEnabled=false\n")
    assert "AutoOffMinutes" not in t and "SideloadedMode=false" in t


@pytest.mark.skipif(not os.path.isfile(os.path.join(ARCHIVE, "Kobo", "Kobo eReader.conf")), reason="no archived config")
def test_conf_editor_on_the_real_config_copy():
    with open(os.path.join(ARCHIVE, "Kobo", "Kobo eReader.conf"), encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
    assert device.KoboConf(text).text() == text  # byte-for-byte when nothing changes


def test_settings_enable_disable_undo(kobo, capsys):
    m = ["--mount", str(kobo)]
    assert device.main(m + ["settings", "--enable", "no-reagl", "--enable", "quiet-ui-logging", "--yes"]) == 0
    conf = (kobo / ".kobo" / "Kobo" / "Kobo eReader.conf").read_text(encoding="utf-8")
    assert "disableRegal=true" in conf and 'Log="ui.debug=false;usertrace.debug=false"' in conf
    assert device.main(m + ["settings", "--list"]) == 0
    assert "[x] no-reagl" in capsys.readouterr().out
    assert device.main(m + ["settings", "--disable", "no-reagl", "--yes"]) == 0
    conf = (kobo / ".kobo" / "Kobo" / "Kobo eReader.conf").read_text(encoding="utf-8")
    assert "disableRegal" not in conf
    assert device.main(m + ["settings", "--enable", "calibre-web-store", "--yes"]) == 1  # needs a value
    assert device.main(m + ["settings", "--set", "calibre-web-store=https://cw.lan/kobo/T", "--yes"]) == 0
    assert "api_endpoint=https://cw.lan/kobo/T" in (kobo / ".kobo" / "Kobo" / "Kobo eReader.conf").read_text(encoding="utf-8")
    assert device.main(m + ["settings", "--undo", "--yes"]) == 0
    assert "api_endpoint" not in (kobo / ".kobo" / "Kobo" / "Kobo eReader.conf").read_text(encoding="utf-8")


def test_db_actions(kobo, tmp_path):
    m = ["--mount", str(kobo)]
    bk = tmp_path / "bk"
    assert device.main(m + ["db", "--run", "analytics-cleanup", "--backup-dir", str(bk), "--yes"]) == 0
    assert (bk / "KoboReader.sqlite").exists()
    db = sqlite3.connect(kobo / ".kobo" / "KoboReader.sqlite")
    assert db.execute("SELECT count(*) FROM AnalyticsEvents").fetchone()[0] == 1
    db.close()
    assert device.main(m + ["db", "--run", "date-shift", "--backup-dir", str(tmp_path / "bk2"), "--yes"]) == 0
    db = sqlite3.connect(kobo / ".kobo" / "KoboReader.sqlite")
    dates = dict(db.execute("SELECT ContentID, DateLastRead FROM content"))
    db.close()
    assert not dates["a"].startswith("2012") and dates["a"] < dates["b"] and dates["c"] == "2024-01-01T00:00:00Z"
    assert device.main(m + ["db", "--run", "compact", "--backup-dir", str(tmp_path / "bk3"), "--yes"]) == 0
    db = sqlite3.connect(kobo / ".kobo" / "KoboReader.sqlite")
    assert db.execute("PRAGMA page_size").fetchone()[0] == 4096
    db.close()
    assert device.main(m + ["db", "--run", "secure-db-rebuild", "--backup-dir", str(tmp_path / "bk4"), "--yes"]) == 0
    assert not (kobo / ".kobo" / "BookReader.sqlite").exists() and (tmp_path / "bk4" / "BookReader.sqlite").exists()


def test_install(kobo, tmp_path):
    tgz = tmp_path / "KoboRoot.tgz"
    tgz.write_bytes(b"\x1f\x8b fake")
    cfg = tmp_path / "kobopatch.yaml"
    cfg.write_text("version: 4.38.23852\nin: x\nout: o\nlog: l\n", encoding="utf-8")
    assert device.main(["--mount", str(kobo), "install", str(tgz), "--config", str(cfg), "--yes"]) == 1  # wrong version
    cfg.write_text("version: 4.38.23429\nin: x\nout: o\nlog: l\n", encoding="utf-8")
    assert device.main(["--mount", str(kobo), "install", str(tgz), "--config", str(cfg), "--yes"]) == 0
    assert (kobo / ".kobo" / "KoboRoot.tgz").read_bytes() == b"\x1f\x8b fake"


def test_no_kobo(tmp_path, capsys):
    assert device.main(["--mount", str(tmp_path), "info"]) == 1
    assert "doesn't look like a Kobo" in capsys.readouterr().err
