"""Packing patch sets for download, and reading them back.

A patch set is a folder like src/versions/4.38.23429 with a kobopatch.yaml, its
patch files and a source.yaml saying where Kobo publishes the firmware. For
download, each set is packed into a zip (entries under "<version>/") and listed
in index.json; tools/publish_patchsets.py and the patchsets workflow publish
them on the "patchsets" release of the GitHub repo.
"""

import hashlib
import io
import json
import os
import zipfile

import yaml

from .errors import KobopatcherError

INDEX_FORMAT = 1
# never packed: build output, firmware zips and backups in a set folder
_SKIP_DIRS = {"out", "__pycache__"}
_SKIP_EXT = (".zip", ".bak", ".tgz", ".pyc")


class PatchSetError(KobopatcherError):
    pass


def version_key(v):
    """Sort key for firmware versions like 4.38.23429."""
    out = []
    for part in str(v).split("."):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out)


def set_files(set_dir):
    """The files of a set folder, as sorted relative paths with / separators."""
    files = []
    for dirpath, dirnames, filenames in os.walk(set_dir):
        rel_dir = os.path.relpath(dirpath, set_dir)
        dirnames[:] = sorted(d for d in dirnames if not (rel_dir == "." and d in _SKIP_DIRS) and d != "__pycache__")
        for f in filenames:
            if f.endswith(_SKIP_EXT):
                continue
            rel = f if rel_dir == "." else os.path.join(rel_dir, f)
            files.append(rel.replace(os.sep, "/"))
    if os.path.isfile(os.path.join(set_dir, "out", ".keep")):
        files.append("out/.keep")
    return sorted(files)


def build_zip(set_dir, version):
    """A deterministic zip of a set folder: the same files give the same bytes.

    Stored, not compressed, so the bytes don't depend on the zlib build (the
    sets are small text files).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for rel in set_files(set_dir):
            zi = zipfile.ZipInfo("%s/%s" % (version, rel), date_time=(1980, 1, 1, 0, 0, 0))
            zi.external_attr = 0o644 << 16
            with open(os.path.join(set_dir, rel), "rb") as f:
                z.writestr(zi, f.read())
    return buf.getvalue()


def read_source(set_dir):
    path = os.path.join(set_dir, "source.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            src = yaml.safe_load(f) or {}
    except OSError as e:
        raise PatchSetError("could not read %s: %s" % (path, e)) from None
    if not isinstance(src, dict) or not src.get("version"):
        raise PatchSetError("%s has no version" % path)
    return src


def set_entry(set_dir, data=None, commit=""):
    """The index.json entry of one set (data: its zip, built if not given)."""
    src = read_source(set_dir)
    version = str(src["version"])
    if data is None:
        data = build_zip(set_dir, version)
    return {
        "version": version,
        "file": "kobopatcher-%s.zip" % version,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "commit": commit,
        "release": str(src.get("release", "")),
        "firmware": {str(k): dict(v or {}) for k, v in (src.get("firmware") or {}).items()},
        "checked_models": [str(m) for m in src.get("checked_models") or []],
    }


def set_dirs(versions_dir):
    """The set folders in a folder like src/versions, oldest version first."""
    out = []
    try:
        names = os.listdir(versions_dir)
    except OSError as e:
        raise PatchSetError("could not read %s: %s" % (versions_dir, e)) from None
    for name in names:
        d = os.path.join(versions_dir, name)
        if os.path.isfile(os.path.join(d, "kobopatch.yaml")) and os.path.isfile(os.path.join(d, "source.yaml")):
            out.append(d)
    return sorted(out, key=lambda d: version_key(os.path.basename(d)))


def build_index(sets, commit=""):
    return {"format": INDEX_FORMAT, "commit": commit, "sets": sets}


def check_index(index):
    """Validate a downloaded index.json; returns it."""
    if not isinstance(index, dict) or not isinstance(index.get("sets"), list):
        raise PatchSetError("the patch set index has an unexpected format")
    if index.get("format", 0) > INDEX_FORMAT:
        raise PatchSetError("the patch set index is newer than this kobopatcher; update kobopatcher")
    for s in index["sets"]:
        if not isinstance(s, dict) or not s.get("version") or not s.get("file") or not s.get("sha256"):
            raise PatchSetError("the patch set index has an incomplete entry")
    return index


def find(index, version):
    for s in index["sets"]:
        if s["version"] == version:
            return s
    return None


def versions(index):
    return sorted((s["version"] for s in index["sets"]), key=version_key)


def dumps(index):
    return json.dumps(index, indent=2, sort_keys=True) + "\n"
