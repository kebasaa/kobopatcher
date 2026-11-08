"""The guided mode, patch-set downloads and `kobopatcher restore`, against a
local HTTP server and a fake Kobo folder (never the network or a real Kobo)."""

import builtins
import functools
import gzip
import hashlib
import http.server
import io
import os
import tarfile
import threading
import zipfile

import pytest

from kobopatcher import cli, fetch, guided, patchsets

V1, V2 = "4.38.90001", "4.38.90002"
RCS = b"#!/bin/sh\necho hello\n"


def _tgz(members):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.uname, ti.gname = len(data), 0o755, "root", "root"
            tf.addfile(ti, io.BytesIO(data))
    return gzip.compress(bio.getvalue())


def _firmware(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("KoboRoot.tgz", _tgz([("./etc/init.d/rcS", RCS)]))
        z.writestr("manifest.md5sum", "x\n")
        z.writestr("upgrade/upgrade.sh", "#!/bin/sh\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_set(versions_dir, version, fw_sha, extra_patch=""):
    d = versions_dir / version
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "out").mkdir(exist_ok=True)
    (d / "out" / ".keep").write_text("")
    (d / "src" / "boot.yaml").write_text(
        "Shout hello:\n  - Enabled: yes\n  - Category: Boot\n  - ReplaceText: {Find: \"echo hello\\n\", Replace: \"echo HELLO\\n\"}\n"
        "Say bye too:\n  - Enabled: no\n  - Category: Boot\n  - ReplaceText: {Find: \"#!/bin/sh\\n\", Replace: \"#!/bin/sh\\necho bye\\n\"}\n"
        + extra_patch
    )
    (d / "src" / "extra.css").write_text("x{}\n")
    (d / "kobopatch.yaml").write_text(
        "version: %s\nin: kobo-update-%s.zip\nout: out/KoboRoot.tgz\nlog: out/log.txt\n"
        "patches:\n  src/boot.yaml: etc/init.d/rcS\n"
        "files:\n  src/extra.css:\n    dest: etc/extra.css\n    if: 'src/boot.yaml: Say bye too'\n" % (version, version)
    )
    (d / "source.yaml").write_text(
        "version: %s\nrelease: Jan2030\nfirmware:\n  kobo5: {sha256: %s}\nchecked_models: [\"370\"]\n" % (version, fw_sha)
    )
    return d


def _publish(versions_dir, out):
    out.mkdir(exist_ok=True)
    sets = []
    for d in patchsets.set_dirs(str(versions_dir)):
        v = os.path.basename(d)
        data = patchsets.build_zip(d, v)
        e = patchsets.set_entry(d, data)
        (out / e["file"]).write_bytes(data)
        sets.append(e)
    (out / "index.json").write_text(patchsets.dumps(patchsets.build_index(sets)))


@pytest.fixture
def server(tmp_path):
    """A local HTTP server for tmp_path/www; yields its base URL."""
    www = tmp_path / "www"
    www.mkdir()
    handler = functools.partial(_QuietHandler, directory=str(www))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d/" % httpd.server_address[1], www
    httpd.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def world(tmp_path, server, monkeypatch):
    """Two published patch sets, Kobo's firmware server and a fake Kobo running V1."""
    base, www = server
    fwdir = www / "firmwares" / "kobo5" / "Jan2030"
    fwdir.mkdir(parents=True)
    sha1 = _firmware(fwdir / ("kobo-update-%s.zip" % V1))
    sha2 = _firmware(fwdir / ("kobo-update-%s.zip" % V2))
    versions = tmp_path / "versions"
    _make_set(versions, V1, sha1)
    _make_set(versions, V2, sha2)
    _publish(versions, www / "patchsets")
    monkeypatch.setattr(fetch, "FIRMWARE_URL", base + "firmwares/{family}/{release}/kobo-update-{version}.zip")
    kobo = tmp_path / "KOBOeReader"
    (kobo / ".kobo" / "Kobo").mkdir(parents=True)
    (kobo / ".kobo" / "version").write_text("N250000000000,2.6.35.3,%s,2.6.35.3,2.6.35.3,00000000-0000-0000-0000-000000000370" % V1)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.chdir(run)
    monkeypatch.delenv("KOBOPATCHER_PATCHSETS_URL", raising=False)
    return {
        "index": base + "patchsets/index.json",
        "www": www,
        "versions": versions,
        "kobo": kobo,
        "work": tmp_path / "work",
        "fw_sha": sha1,
    }


def _args(w, *more):
    return ["--patchsets", w["index"], "--mount", str(w["kobo"]), "--workdir", str(w["work"])] + list(more)


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))


def _rcs_on_kobo(w):
    with tarfile.open(w["kobo"] / ".kobo" / "KoboRoot.tgz", "r:gz") as tf:
        return {ti.name: tf.extractfile(ti).read() for ti in tf if ti.isreg()}


# --- packing --------------------------------------------------------------------------------------


def test_pack_is_deterministic(world):
    d = str(world["versions"] / V1)
    (world["versions"] / V1 / "out" / "KoboRoot.tgz").write_bytes(b"build output")
    (world["versions"] / V1 / ("kobo-update-%s.zip" % V1)).write_bytes(b"firmware")
    assert patchsets.build_zip(d, V1) == patchsets.build_zip(d, V1)
    names = zipfile.ZipFile(io.BytesIO(patchsets.build_zip(d, V1))).namelist()
    assert "%s/out/.keep" % V1 in names and "%s/source.yaml" % V1 in names
    assert not any(n.endswith((".tgz", ".zip")) for n in names)


def test_safe_extract_refuses_bad_paths(tmp_path):
    for bad in ["v/../x", "v//etc/x", "other/x"]:
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as z:
            z.writestr(bad, "x")
        with pytest.raises(fetch.FetchError):
            fetch.safe_extract(bio.getvalue(), str(tmp_path / "out"), "v")


def test_wants_guided(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert guided.wants_guided([]) and guided.wants_guided(["--target", "1", "--yes"])
    assert not guided.wants_guided(["other.yaml"]) and not guided.wants_guided(["-t"])
    assert not guided.wants_guided(["--help"])
    (tmp_path / "kobopatch.yaml").write_text("x")
    assert not guided.wants_guided([])


def test_upgrade_check(monkeypatch):
    monkeypatch.setattr(fetch, "get_bytes", lambda url, limit=0: b'{"UpgradeURL": "https://ereaderfiles.kobo.com/firmwares/kobo7/Oct2026/kobo-update-4.38.23852.zip"}')
    assert fetch.upgrade_check("371") == {"family": "kobo7", "version": "4.38.23852", "url": "https://ereaderfiles.kobo.com/firmwares/kobo7/Oct2026/kobo-update-4.38.23852.zip"}
    assert fetch.family_for("370", {"370": {"family": "kobo5"}}) == "kobo5"
    assert fetch.family_for("371", {}) == "kobo7"


# --- patching -----------------------------------------------------------------------------------------


def test_guided_end_to_end(world, capsys):
    assert cli.main(_args(world, "--yes")) == 0
    out = capsys.readouterr().out
    assert "running firmware %s" % V1 in out and "newer firmware (%s)" % V2 in out
    assert "Downloaded and checked (SHA-256)" in out
    ws = world["work"] / V1
    assert (ws / ("kobo-update-%s.zip" % V1)).is_file() and not list(ws.glob("*.part"))
    assert _rcs_on_kobo(world)["./etc/init.d/rcS"] == b"#!/bin/sh\necho HELLO\n"
    assert "kobopatcher restore" in out
    # a second run finds everything in place
    assert cli.main(_args(world, "--yes", "--enable", "say-bye-too")) == 0
    out = capsys.readouterr().out
    assert "is up to date" in out and "is already there" in out
    files = _rcs_on_kobo(world)
    assert files["./etc/init.d/rcS"] == b"#!/bin/sh\necho bye\necho HELLO\n" and "./etc/extra.css" in files
    assert '"Say bye too": yes' in (ws / "kobopatch.yaml").read_text()


def test_download_needs_a_yes(world, monkeypatch, capsys):
    _answers(monkeypatch, "1", "n")  # patch; don't download
    assert cli.main(_args(world, "--no-menu")) == 1
    assert "no firmware" in capsys.readouterr().err
    assert not (world["work"] / V1 / ("kobo-update-%s.zip" % V1)).exists()


def test_firmware_checksum_mismatch(world, capsys):
    p = world["www"] / "firmwares" / "kobo5" / "Jan2030" / ("kobo-update-%s.zip" % V1)
    p.write_bytes(p.read_bytes() + b"tampered")
    assert cli.main(_args(world, "--yes")) == 1
    assert "doesn't have the expected SHA-256" in capsys.readouterr().err
    assert not list((world["work"] / V1).glob("kobo-update-*"))


def test_set_update_keeps_the_choice(world, capsys):
    assert cli.main(_args(world, "--yes", "--no-menu", "--enable", "say-bye-too")) == 0
    ws = world["work"] / V1
    (ws / "out" / "mine.txt").write_text("kept")
    # publish a changed set: one more patch
    _make_set(world["versions"], V1, world["fw_sha"],
              "Extra:\n  - Enabled: no\n  - ReplaceText: {Find: \"hello\", Replace: \"howdy\"}\n")
    _publish(world["versions"], world["www"] / "patchsets")
    capsys.readouterr()
    assert cli.main(_args(world, "--yes", "--no-menu")) == 0
    assert "Updated." in capsys.readouterr().out
    text = (ws / "kobopatch.yaml").read_text()
    assert '"Say bye too": yes' in text and "Extra:" in (ws / "src" / "boot.yaml").read_text()
    assert (ws / "out" / "mine.txt").read_text() == "kept" and (ws / ("kobo-update-%s.zip" % V1)).is_file()
    assert fetch.installed_set(str(ws))["sha256"] == patchsets.find(fetch.Source(world["index"]).index(), V1)["sha256"]


def test_no_set_for_this_firmware(world, capsys):
    (world["kobo"] / ".kobo" / "version").write_text("N250,x,4.38.21000,x,x,00000000-0000-0000-0000-000000000370")
    assert cli.main(_args(world, "--yes")) == 1
    out = capsys.readouterr().out
    assert "no patch set for firmware 4.38.21000" in out and V1 in out and "kobopatch-patches" in out


def test_without_a_kobo(world, monkeypatch, capsys):
    a = ["--patchsets", world["index"], "--workdir", str(world["work"])]
    monkeypatch.setattr(guided.device, "find_kobos", lambda: [])
    assert cli.main(a + ["--yes"]) == 1
    assert "--target" in capsys.readouterr().err
    assert cli.main(a + ["--yes", "--target", V2]) == 0
    out = capsys.readouterr().out
    assert "No Kobo is connected, so this is the firmware for the Kobo Aura H2O" in out
    assert (world["work"] / V2 / "out" / "KoboRoot.tgz").is_file()
    assert not (world["kobo"] / ".kobo" / "KoboRoot.tgz").exists()


def test_local_patchsets_folder(world, capsys):
    a = ["--patchsets", str(world["versions"]), "--mount", str(world["kobo"]), "--workdir", str(world["work"]), "--yes"]
    assert cli.main(a) == 0
    assert fetch.installed_set(str(world["work"] / V1))["version"] == V1


# --- restore ---------------------------------------------------------------------------------------


def _stock_tgz(w, version=V1):
    p = w["www"] / "firmwares" / "kobo5" / "Jan2030" / ("kobo-update-%s.zip" % version)
    with zipfile.ZipFile(p) as z:
        return z.read("KoboRoot.tgz")


def test_restore(world, monkeypatch, capsys):
    kobo_dir = world["kobo"] / ".kobo"
    (kobo_dir / "KoboRoot.tgz").write_bytes(b"waiting")
    assert cli.main(["restore"] + _args(world, "--yes")) == 0  # --yes replaces a waiting file
    assert (kobo_dir / "KoboRoot.tgz").read_bytes() == _stock_tgz(world)
    capsys.readouterr()
    _answers(monkeypatch, "y")
    assert cli.main(["restore"] + _args(world)) == 1  # without --yes, a waiting file blocks
    assert "already waiting" in capsys.readouterr().err


def test_restore_full_to_a_folder_and_settings(world, monkeypatch, capsys):
    out = world["work"] / "stock"
    assert cli.main(["restore", "--patchsets", world["index"], "--workdir", str(world["work"]), "--target", V1, "--full",
                     "--out", str(out), "--yes"]) == 0
    assert sorted(os.listdir(out)) == ["KoboRoot.tgz", "manifest.md5sum", "upgrade"]
    conf = world["kobo"] / ".kobo" / "Kobo" / "Kobo eReader.conf"
    conf.write_text("[Reading]\nchanged=1\n")
    (world["kobo"] / ".kobo" / "Kobo" / "Kobo eReader.conf.kobopatcher-20300101-000000.bak").write_text("[Reading]\n")
    _answers(monkeypatch, "y", "y")  # copy KoboRoot.tgz; restore the settings
    assert cli.main(["restore"] + _args(world)) == 0
    assert conf.read_text() == "[Reading]\n"
    assert "Settings restored" in capsys.readouterr().out


def test_restore_from_the_guided_menu_lists_extra_files(world, monkeypatch, capsys):
    assert cli.main(_args(world, "--yes", "--enable", "say-bye-too")) == 0
    (world["kobo"] / ".kobo" / "KoboRoot.tgz").unlink()
    capsys.readouterr()
    _answers(monkeypatch, "2", "y")  # reset to stock; yes
    assert cli.main(_args(world)) == 0
    out = capsys.readouterr().out
    assert "/etc/extra.css" in out
    assert (world["kobo"] / ".kobo" / "KoboRoot.tgz").read_bytes() == _stock_tgz(world)


def test_restore_refuses_another_version(world, capsys):
    assert cli.main(["restore"] + _args(world, "--yes", "--target", V2)) == 1
    assert "a reset installs the stock files of the version it runs" in capsys.readouterr().err
