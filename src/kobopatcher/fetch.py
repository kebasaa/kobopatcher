"""Downloading patch sets and firmware, and the workspace they go into.

Patch sets come from the "patchsets" release of the GitHub repo (index.json
plus one zip per set, see patchsets.py), or from a local folder for
development (KOBOPATCHER_PATCHSETS_URL or --patchsets). Firmware comes from
Kobo's own download server.

The workspace is ~/kobopatcher/<version>/ (KOBOPATCHER_HOME or --workdir to
change): the unpacked patch set, the firmware zip and out/KoboRoot.tgz. The
user's choice of patches lives in its kobopatch.yaml and is kept when the set
is updated.
"""

import hashlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import __version__
from . import patchsets
from .errors import KobopatcherError

DEFAULT_INDEX = "https://github.com/kebasaa/kobopatcher/releases/download/patchsets/index.json"
FIRMWARE_URL = "https://ereaderfiles.kobo.com/firmwares/{family}/{release}/kobo-update-{version}.zip"
# a placeholder serial and version: Kobo answers with the newest firmware for the model
UPGRADE_CHECK = "https://api.kobobooks.com/1.0/UpgradeCheck/Device/00000000-0000-0000-0000-000000000{model}/kobo/0.0/N0"
FIRMWARE_PAGE = "https://pgaskin.net/KoboStuff/kobofirmware.html"
MARKER = ".kobopatcher-set.json"
TIMEOUT = 60
# github.com release downloads, and the API route to the same files
_RELEASE_RX = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?#]+)$")
GITHUB_API = "https://api.github.com"


class FetchError(KobopatcherError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# --- HTTP ------------------------------------------------------------------------------------------


def _open(url, method="GET", accept=None):
    headers = {"User-Agent": "kobopatcher/%s" % __version__}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FetchError("%s: not found (404)" % url, 404) from None
        raise FetchError("%s: HTTP error %d %s" % (url, e.code, e.reason), e.code) from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise FetchError(
                "%s: the server's certificate couldn't be checked (%s). Python has no certificate store here; "
                "on macOS with Python from python.org, run 'Install Certificates.command' in its Applications folder" % (url, reason)
            ) from None
        raise FetchError("could not connect to %s: %s (are you online?)" % (url, reason)) from None
    except (OSError, ValueError) as e:
        raise FetchError("could not download %s: %s" % (url, e)) from None


def get_bytes(url, limit=64 << 20):
    """Download a (small) file. A github.com release download that fails with a
    server error (github.com sometimes answers anonymous requests with 504) is
    retried through GitHub's API, which serves the same file."""
    try:
        return _read(url, limit)
    except FetchError as e:
        m = _RELEASE_RX.match(url)
        if not m or not (e.status == 429 or (e.status or 0) >= 500):
            raise
        try:
            return _read(_release_asset_url(*m.groups()), limit, "application/octet-stream")
        except FetchError:
            raise e from None


def _read(url, limit, accept=None):
    with _open(url, accept=accept) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise FetchError("%s is larger than expected" % url)
    return data


def _release_asset_url(owner, repo, tag, name):
    api = "%s/repos/%s/%s/releases/tags/%s" % (GITHUB_API, owner, repo, tag)
    try:
        assets = json.loads(_read(api, 4 << 20, "application/vnd.github+json").decode("utf-8")).get("assets") or []
    except ValueError:
        raise FetchError("GitHub's API gave an unexpected answer for %s" % api) from None
    for a in assets:
        if a.get("name") == name and a.get("url"):
            return a["url"]
    raise FetchError("%s has no file %s" % (api, name), 404)


def head_size(url):
    """The size of a download in bytes, or None if the server doesn't say."""
    with _open(url, "HEAD") as r:
        n = r.headers.get("Content-Length")
    return int(n) if n and n.isdigit() else None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, sha256=None, say=print):
    """Download url to dest (through dest.part), checking the SHA-256 if given."""
    part = dest + ".part"
    h = hashlib.sha256()
    tty = sys.stderr.isatty()
    with _open(url) as r:
        total = r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else 0
        done, shown = 0, -1
        try:
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if tty and pct != shown:
                            sys.stderr.write("\r  %3d%%  %.1f of %.1f MB" % (pct, done / 1e6, total / 1e6))
                            shown = pct
                        elif not tty and pct // 10 != shown // 10:
                            say("  %3d%%" % pct)
                            shown = pct
        except BaseException:
            _remove(part)
            raise
    if tty and total:
        sys.stderr.write("\n")
    if total and done != total:
        _remove(part)
        raise FetchError("the download of %s stopped after %d of %d bytes; try again" % (url, done, total))
    if sha256 and h.hexdigest() != sha256.lower():
        _remove(part)
        raise FetchError("%s doesn't have the expected SHA-256 (got %s, expected %s); nothing was kept" % (url, h.hexdigest(), sha256))
    os.replace(part, dest)
    return h.hexdigest()


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --- patch sets --------------------------------------------------------------------------------


def default_location():
    return os.environ.get("KOBOPATCHER_PATCHSETS_URL") or DEFAULT_INDEX


class Source:
    """Where patch sets come from: an index.json URL, a folder with index.json
    and the zips (as written by tools/publish_patchsets.py), or a folder of set
    folders like src/versions."""

    def __init__(self, location=None):
        self.location = location or default_location()
        self._index = None
        self._dirs = None

    def is_local(self):
        return not re.match(r"^[a-z]+://", self.location)

    def describe(self):
        return self.location

    def index(self):
        if self._index is not None:
            return self._index
        if not self.is_local():
            data = get_bytes(self.location)
            try:
                index = json.loads(data.decode("utf-8"))
            except ValueError:
                raise FetchError("%s is not a patch set index" % self.location) from None
        elif os.path.isfile(os.path.join(self.location, "index.json")):
            with open(os.path.join(self.location, "index.json"), encoding="utf-8") as f:
                index = json.load(f)
        elif os.path.isdir(self.location):
            self._dirs = {}
            sets = []
            for d in patchsets.set_dirs(self.location):
                e = patchsets.set_entry(d)
                self._dirs[e["version"]] = d
                sets.append(e)
            index = patchsets.build_index(sets, "local")
        else:
            raise FetchError("%s is neither a URL nor a folder of patch sets" % self.location)
        self._index = patchsets.check_index(index)
        return self._index

    def set_zip(self, entry):
        """The zip of a set, checked against the index."""
        if self._dirs is not None:
            data = patchsets.build_zip(self._dirs[entry["version"]], entry["version"])
        elif self.is_local():
            with open(os.path.join(self.location, entry["file"]), "rb") as f:
                data = f.read()
        else:
            data = get_bytes(urllib.parse.urljoin(self.location, entry["file"]))
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise FetchError("the download of %s doesn't match the index (SHA-256); try again later" % entry["file"])
        return data


def safe_extract(data, dest, prefix):
    """Unpack the zip bytes into dest, stripping prefix/ and refusing unsafe paths."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = []
        for zi in z.infolist():
            name = zi.filename
            if not name.startswith(prefix + "/"):
                raise FetchError("unexpected file %r in the patch set zip" % name)
            rel = name[len(prefix) + 1:]
            parts = rel.split("/")
            if not rel or rel.startswith("/") or "\\" in rel or ".." in parts or ":" in parts[0]:
                if zi.is_dir() and not rel:
                    continue
                raise FetchError("unsafe path %r in the patch set zip" % name)
            if (zi.external_attr >> 16) & 0o170000 == 0o120000:
                raise FetchError("symlink %r in the patch set zip" % name)
            members.append((zi, rel))
        for zi, rel in members:
            target = os.path.join(dest, *rel.split("/"))
            if zi.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(zi) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def default_workdir():
    return os.environ.get("KOBOPATCHER_HOME") or os.path.join(os.path.expanduser("~"), "kobopatcher")


def installed_set(dest):
    """The marker of the set unpacked in dest ({version, sha256}), or None."""
    try:
        with open(os.path.join(dest, MARKER), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def set_state(dest, entry):
    """Whether dest has the set of the index entry: "missing", "current" or "outdated"."""
    if not os.path.isfile(os.path.join(dest, "kobopatch.yaml")):
        return "missing"
    m = installed_set(dest)
    if m and m.get("sha256") == entry["sha256"]:
        return "current"
    return "outdated"


def install_set(source, entry, dest):
    """Unpack the set into dest. An existing set is replaced; the firmware zips,
    out/ and the choice of patches (overrides: in kobopatch.yaml) are kept.

    Returns a list of overrides that no longer exist in the new set (dropped).
    """
    data = source.set_zip(entry)
    version = entry["version"]
    conf = os.path.join(dest, "kobopatch.yaml")
    old_overrides = _read_overrides(conf) if os.path.isfile(conf) else {}
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        safe_extract(data, tmp, version)
        if os.path.isdir(dest):
            for name in os.listdir(dest):
                p = os.path.join(dest, name)
                if name.endswith(".zip") and os.path.isfile(p):
                    os.replace(p, os.path.join(tmp, name))
                elif name == "out" and os.path.isdir(p):
                    shutil.rmtree(os.path.join(tmp, "out"), ignore_errors=True)
                    os.replace(p, os.path.join(tmp, "out"))
            old = dest + ".old"
            shutil.rmtree(old, ignore_errors=True)
            os.replace(dest, old)
            os.replace(tmp, dest)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            os.replace(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as f:
        json.dump({"version": version, "sha256": entry["sha256"], "commit": entry.get("commit", "")}, f)
        f.write("\n")
    dropped = []
    if old_overrides:
        dropped = _restore_overrides(conf, old_overrides)
    return dropped


def _read_overrides(conf):
    from . import config as configmod

    try:
        with open(conf, "rb") as f:
            return configmod.load(f.read()).overrides or {}
    except (OSError, KobopatcherError):
        return {}


def _restore_overrides(conf, overrides):
    """Write the old overrides into the new kobopatch.yaml, keeping only patches
    that still exist; returns the ones dropped as "file: name"."""
    from . import catalog

    cwd = os.getcwd()
    try:
        cfg, cat = catalog.load(os.path.abspath(conf))
    finally:
        os.chdir(cwd)
    keep, dropped = {}, []
    names = {(pf, e.name) for e in cat.entries for pf, _ in e.targets}
    for pf, ov in overrides.items():
        for name, on in (ov or {}).items():
            if (pf, name) in names:
                keep.setdefault(pf, {})[name] = on
            else:
                dropped.append("%s: %s" % (pf, name))
    if keep:
        catalog.save_overrides(conf, keep)
        _remove(conf + ".bak")
    return dropped


# --- firmware ----------------------------------------------------------------------------------


def firmware_url(version, release, family):
    return FIRMWARE_URL.format(family=family, release=release, version=version)


def upgrade_check(model_id):
    """The newest firmware Kobo offers for a model: {"version", "url", "family"}.

    Sends only the model number (a placeholder serial and version)."""
    if not re.match(r"^\d{3}$", model_id or ""):
        raise FetchError("unknown model id %r" % model_id)
    data = get_bytes(UPGRADE_CHECK.format(model=model_id), 1 << 20)
    try:
        info = json.loads(data.decode("utf-8"))
    except ValueError:
        raise FetchError("Kobo's update service gave an unexpected answer") from None
    url = info.get("UpgradeURL") or ""
    m = re.search(r"/firmwares/([a-z0-9]+)/", url)
    v = re.search(r"kobo-update-([0-9.]+)\.zip", url)
    if not m:
        raise FetchError("Kobo's update service doesn't offer a firmware for model %s" % model_id)
    return {"family": m.group(1), "version": v.group(1) if v else "", "url": url}


def family_for(model_id, models):
    """The download folder of a model: from models.yaml, else from Kobo's update service."""
    fam = (models.get(model_id) or {}).get("family")
    if fam:
        return fam
    return upgrade_check(model_id)["family"]
