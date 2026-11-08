"""End-to-end tests of the kobopatcher command and the tools."""

import gzip
import io
import json
import lzma
import os
import shutil
import tarfile
import zipfile

import pytest

from kobopatcher import apply_cli, cli, cssextract, symdump
from kobopatcher.translations import find_lrelease

PATCH = """
Change otf to _tf:
  - Enabled: no
  - FindBaseAddressString: "*.otf"
  - ReplaceString: {Find: "*", Replace: "_"}
Brightness:
  - Enabled: yes
  - ReplaceInt: {Offset: 0x95DD02, Find: 1, Replace: 2}
"""


def _tar_bytes(members, gz):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name, data, mode in members:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.uname, ti.gname = len(data), mode, "root", "root"
            tf.addfile(ti, io.BytesIO(data))
    raw = bio.getvalue()
    return gzip.compress(raw) if gz else lzma.compress(raw)


@pytest.fixture
def fw(tmp_path, testdata):
    """A firmware zip with libnickel and a plugin."""
    lib = testdata("libnickel.so.1.0.0")
    tgz = _tar_bytes([("./usr/local/Kobo/libnickel.so.1.0.0", lib, 0o755), ("./usr/local/Kobo/libboggle.so", b"\x7fELF fake", 0o644)], True)
    z = tmp_path / "kobo-update-1.2.3.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("manifest.md5sum", "x")
        zf.writestr("KoboRoot.tgz", tgz)
    return z


def _members(path):
    with tarfile.open(path, "r:gz") as tf:
        return {ti.name: (ti, tf.extractfile(ti).read() if ti.isreg() else None) for ti in tf}


def _setup(tmp_path, fw, extra=""):
    d = tmp_path / "set"
    (d / "src").mkdir(parents=True)
    (d / "out").mkdir()
    (d / "src" / "libnickel.yaml").write_text(PATCH)
    (d / "src" / "boggle.yaml").write_text("Tombstone:\n  - Enabled: yes\n  - Tombstone: usr/local/Kobo/libboggle.so\n")
    (d / "kobopatch.yaml").write_text(
        "version: 1.2.3\nin: %s\nout: out/KoboRoot.tgz\nlog: out/log.txt\npatches:\n"
        "  src/libnickel.yaml: usr/local/Kobo/libnickel.so.1.0.0\n  src/boggle.yaml: usr/local/Kobo/libboggle.so\n"
        "overrides:\n  src/libnickel.yaml:\n    Change otf to _tf: yes\n%s" % (fw, extra)
    )
    return d


def test_full_run(tmp_path, fw, testdata, capsys, monkeypatch):
    d = _setup(tmp_path, fw)
    monkeypatch.chdir(tmp_path)
    # a config path with a directory works (kobopatch failed on it)
    assert cli.main(["set/kobopatch.yaml"]) == 0
    out = capsys.readouterr().out
    assert "  APPLY `Brightness`\n  APPLY `Change otf to _tf`\n" in out
    assert "    ENABLE  `Change otf to _tf`\n" in out
    m = _members(d / "out" / "KoboRoot.tgz")
    assert set(m) == {"./usr/local/Kobo/libnickel.so.1.0.0", "./usr/local/Kobo/libboggle.so"}
    ti, data = m["./usr/local/Kobo/libnickel.so.1.0.0"]
    assert ti.mode == 0o755 and ti.uname == "root" and ti.type == tarfile.REGTYPE
    orig = testdata("libnickel.so.1.0.0")
    assert data[0x95DD02] == 2 and len(data) == len(orig)
    i = orig.find(b"*.otf")
    assert data[i : i + 5] == b"_.otf"
    ti, data = m["./usr/local/Kobo/libboggle.so"]
    assert data == b"" and ti.mode == 0o644
    assert (d / "out" / "log.txt").read_text(encoding="utf-8").startswith("kobopatcher ")


def test_check_elf(tmp_path, fw, capsys):
    d = _setup(tmp_path, fw)
    assert cli.main(["--check-elf", str(d / "kobopatch.yaml")]) == 0
    out = capsys.readouterr().out
    # libnickel is an ELF (checked), the fake plugin isn't (but it is tombstoned)
    assert "  CHECK-ELF  ok\n" in out and "  CHECK-ELF  tombstone (0 bytes)\n" in out


def test_files_and_symlinks(tmp_path, fw):
    d = _setup(tmp_path, fw, "files:\n  src/extra.txt:\n    - usr/share/a.txt\n    - usr/share/b.txt\n  src/one.txt: usr/share/c.txt\nsymlinks:\n  usr/share/a.txt: usr/share/link.txt\n")
    (d / "src" / "extra.txt").write_text("hello")
    (d / "src" / "one.txt").write_text("one")
    assert cli.main([str(d / "kobopatch.yaml")]) == 0
    m = _members(d / "out" / "KoboRoot.tgz")
    names = list(m)
    # config order: patched files, then files, then symlinks
    assert names[2:] == ["./usr/share/a.txt", "./usr/share/b.txt", "./usr/share/c.txt", "./usr/share/link.txt"]
    ti, data = m["./usr/share/a.txt"]
    assert data == b"hello" and ti.mode == 0o777 and ti.uid == 0
    ti, _ = m["./usr/share/link.txt"]
    assert ti.issym() and ti.linkname == "/usr/share/a.txt"


def test_symlink_needs_declared_target(tmp_path, fw, capsys):
    d = _setup(tmp_path, fw, "symlinks:\n  usr/share/a.txt: usr/share/link.txt\n")
    assert cli.main([str(d / "kobopatch.yaml")]) == 1
    assert "could not add symlink: target is not declared in files" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("lrelease") and not os.path.exists("/usr/lib/qt6/bin/lrelease") and not os.path.exists("/usr/lib/qt5/bin/lrelease"), reason="lrelease not installed")
def test_translations(tmp_path, fw):
    d = _setup(tmp_path, fw, "translations:\n  src/de.ts: usr/local/Kobo/translations/nickel-de.qm\n")
    (d / "src" / "de.ts").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="de">\n<context>\n'
        "<name>Test</name>\n<message>\n<source>Library</source>\n<translation>Bibliothek</translation>\n"
        "</message>\n</context>\n</TS>\n"
    )
    assert find_lrelease("")
    assert cli.main([str(d / "kobopatch.yaml")]) == 0
    m = _members(d / "out" / "KoboRoot.tgz")
    ti, data = m["./usr/local/Kobo/translations/nickel-de.qm"]
    assert data.startswith(bytes.fromhex("3cb86418caef9c95cd211cbf60a1bddd"))  # the .qm magic
    assert "Bibliothek".encode("utf-16-be") in data


def test_translation_prefix(tmp_path, fw, capsys):
    import sys

    d = _setup(tmp_path, fw, "translations:\n  src/de.ts: usr/local/de.qm\nlrelease: '%s'\n" % sys.executable)
    (d / "src" / "de.ts").write_text("x")
    assert cli.main([str(d / "kobopatch.yaml")]) == 1
    assert "must start with usr/local/Kobo/translations/" in capsys.readouterr().err


def test_testdata_tarball_and_stdin(tmp_path, testdata, capsys, monkeypatch):
    tar = tmp_path / "1.2.3.tar.xz"
    tar.write_bytes(_tar_bytes([("libnickel.so.1.0.0", testdata("libnickel.so.1.0.0"), 0o755)], False))
    (tmp_path / "p.yaml").write_text(PATCH)
    cfg = "version: 1\nin: %s\nout: o.tgz\nlog: l.txt\npatches:\n  p.yaml: usr/local/Kobo/libnickel.so.1.0.0\n" % tar
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(cfg.encode())))
    assert cli.main(["-"]) == 0
    m = _members(tmp_path / "o.tgz")
    assert list(m) == ["./usr/local/Kobo/libnickel.so.1.0.0"]  # flat name rewritten
    assert "Reading input firmware testdata tarball" in capsys.readouterr().out


def test_run_tests(tmp_path, fw, capsys):
    d = _setup(tmp_path, fw)
    assert cli.main(["-t", str(d / "kobopatch.yaml")]) == 0
    out = capsys.readouterr().out
    assert "\r ✔  Brightness\n" in out and "\r ✔  Change otf to _tf\n" in out
    # kobopatcher tests Tombstone patches too (kobopatch never set the path)
    assert "\r ✔  Tombstone\n" in out
    assert out.rstrip().endswith("All patches applied successfully.")


def test_run_tests_failure(tmp_path, fw, capsys):
    d = _setup(tmp_path, fw)
    (d / "src" / "libnickel.yaml").write_text(PATCH + "Broken:\n  - Enabled: no\n  - ReplaceInt: {Offset: 0x95DD02, Find: 9, Replace: 2}\n")
    assert cli.main(["-t", str(d / "kobopatch.yaml")]) == 1
    out = capsys.readouterr().out
    assert "\r ✕  Broken\n" in out
    assert "\nErrors:\n  src/libnickel.yaml: Broken: could not apply patch" in out


def test_errors(tmp_path, fw, capsys):
    d = _setup(tmp_path, fw)
    cfg = (d / "kobopatch.yaml").read_text(encoding="utf-8")
    (d / "kobopatch.yaml").write_text(cfg.replace("usr/local/Kobo/libboggle.so", "usr/local/Kobo/libnope.so"))
    assert cli.main([str(d / "kobopatch.yaml")]) == 1
    assert "could not find target 'usr/local/Kobo/libnope.so' of patch file 'src/boggle.yaml' in the firmware" in capsys.readouterr().err
    (d / "kobopatch.yaml").write_text("version: 1\nin: x\nout: o\n")
    assert cli.main([str(d / "kobopatch.yaml")]) == 1
    assert "version, in, out, and log are required" in capsys.readouterr().err
    (d / "kobopatch.yaml").write_text("version: 1\nin: x\nout: o\nlog: l\nbogus: 1\n")
    assert cli.main([str(d / "kobopatch.yaml")]) == 1
    assert "field bogus not found in type main.Config" in capsys.readouterr().err


def test_help_and_version(capsys):
    assert cli.main(["--help"]) == 0
    assert "Usage: kobopatcher [OPTIONS] [CONFIG_FILE]" in capsys.readouterr().err
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("kobopatcher ")
    assert cli.main(["a.yaml", "b.yaml"]) == 1


def test_apply_cli(tmp_path, testdata, capsys):
    (tmp_path / "in.so").write_bytes(testdata("libnickel.so.1.0.0"))
    (tmp_path / "p.yaml").write_text(PATCH)
    rc = apply_cli.main(["-i", str(tmp_path / "in.so"), "-p", str(tmp_path / "p.yaml"), "-o", str(tmp_path / "out.so")])
    assert rc == 0
    assert (tmp_path / "out.so").read_bytes()[0x95DD02] == 2
    assert "Successfully patched" in capsys.readouterr().out
    assert apply_cli.main(["-i", "x"]) == 1
    assert "input, patch-file, and output flags are required" in capsys.readouterr().err


def test_symdump_and_cssextract(tmp_path, testdata, monkeypatch):
    (tmp_path / "nickel").write_bytes(testdata("nickel"))
    (tmp_path / "lib.so").write_bytes(testdata("libnickel.so.1.0.0"))
    monkeypatch.chdir(tmp_path)
    assert symdump.main(["lib.so"]) == 0
    syms = json.loads((tmp_path / "symdump.out.json").read_text(encoding="utf-8"))
    assert len(syms) == 48035 and set(syms[0]) == {"Name", "Offset", "Index", "Type", "OffsetGOT", "OffsetPLT", "OffsetPLTTail", "Demangled"}
    assert cssextract.main(["nickel"]) == 0
    css = (tmp_path / "cssextract.out.css").read_text(encoding="utf-8")
    assert "offset_int32(4563746)" in css and "#boggleContainer[qApp_deviceIsPika=true]" in css


def _fw_with_images(tmp_path, testdata):
    """A firmware zip whose etc/images has an empty placeholder (overwritten by a later entry) and a symlink."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.GNU_FORMAT) as tf:
        def add(name, data=None, link=None):
            ti = tarfile.TarInfo(name)
            if link is not None:
                ti.type, ti.linkname = tarfile.SYMTYPE, link
                tf.addfile(ti)
            else:
                ti.size, ti.mode = len(data), 0o644
                tf.addfile(ti, io.BytesIO(data))
        add("./usr/local/Kobo/libnickel.so.1.0.0", testdata("libnickel.so.1.0.0"))
        add("./usr/local/Kobo/libboggle.so", b"\x7fELF fake")
        add("./etc/images/dragon-on-0.raw.gz", b"")
        add("./etc/images/snow-on-0.raw.gz", link="dragon-on-0.raw.gz")
        add("./etc/images/empty.raw.gz", b"")
        add("./etc/images/dragon-on-0.raw.gz", b"IMAGE")  # the later entry counts
    z = tmp_path / "kobo-update-1.2.3.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("KoboRoot.tgz", gzip.compress(bio.getvalue()))
    return z


def test_files_from_firmware(tmp_path, testdata):
    fwz = _fw_with_images(tmp_path, testdata)
    d = _setup(tmp_path, fwz, "files:\n  firmware:etc/images/snow-on-0.raw.gz:\n    dest: etc/images/dahlia-on-0.raw.gz\n    mode: 0644\n")
    assert cli.main([str(d / "kobopatch.yaml")]) == 0
    ti, data = _members(d / "out" / "KoboRoot.tgz")["./etc/images/dahlia-on-0.raw.gz"]
    assert data == b"IMAGE" and ti.mode == 0o644  # through the symlink, last entry wins


@pytest.mark.parametrize("src,err", [
    ("firmware:etc/images/missing.raw.gz", "no such file in the firmware"),
    ("firmware:etc/images/empty.raw.gz", "the file is empty in the firmware"),
])
def test_files_from_firmware_errors(tmp_path, testdata, capsys, src, err):
    fwz = _fw_with_images(tmp_path, testdata)
    d = _setup(tmp_path, fwz, "files:\n  %s: etc/images/x.raw.gz\n" % src)
    assert cli.main([str(d / "kobopatch.yaml")]) == 1
    assert err in capsys.readouterr().err
