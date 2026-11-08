"""kobopatcher device: work with a Kobo connected over USB.

    kobopatcher device [--mount PATH] info [--config kobopatch.yaml]
    kobopatcher device [--mount PATH] install [KoboRoot.tgz] [--config kobopatch.yaml] [--yes]
    kobopatcher device [--mount PATH] settings [--list | --select | --enable ID ... --disable ID ...] [--set ID=VALUE] [--yes] [--undo]
    kobopatcher device [--mount PATH] db [--list | --run ACTION] [--backup-dir DIR] [--yes]

Connect the Kobo, tap "Connect" on it, then run these commands. Everything
that changes the Kobo shows what it will do and asks first (--yes skips the
question). Settings and databases are backed up before they are changed.
"""

import argparse
import datetime
import glob
import os
import shutil
import sqlite3
import string
import sys

import yaml

from .catalog import Entry, Selection
from .errors import KobopatcherError

CONF = os.path.join(".kobo", "Kobo", "Kobo eReader.conf")


# the last field of .kobo/version -> {name, family}
def load_models():
    with open(os.path.join(os.path.dirname(__file__), "data", "models.yaml"), encoding="utf-8") as f:
        return {str(k): v for k, v in (yaml.safe_load(f) or {}).items()}


MODELS = {k: v["name"] for k, v in load_models().items()}


EJECT = """Now eject the Kobo safely (Windows: "Safely Remove Hardware"; macOS: Eject in
Finder; Linux: eject it in the file manager) and unplug the cable. The Kobo then
installs the files: it restarts, unpacks them and restarts once more. This takes
a minute or two; don't switch it off meanwhile."""


class DeviceError(KobopatcherError):
    pass


# --- finding the Kobo ------------------------------------------------------------------------


def candidate_roots():
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    pats = ["/media/%s/*" % user, "/run/media/%s/*" % user, "/media/*", "/mnt/*", "/Volumes/*"]
    roots = []
    for p in pats:
        roots.extend(glob.glob(p))
    if os.name == "nt":
        roots.extend("%s:\\" % d for d in string.ascii_uppercase[3:])
    return roots


def find_kobos():
    found = []
    for r in candidate_roots():
        try:
            if os.path.isfile(os.path.join(r, ".kobo", "version")):
                found.append(os.path.realpath(r))
        except OSError:
            continue
    return sorted(set(found))


def kobo_root(mount):
    if mount:
        if not os.path.isfile(os.path.join(mount, ".kobo", "version")):
            raise DeviceError("%s doesn't look like a Kobo (no .kobo/version)" % mount)
        return mount
    found = find_kobos()
    if not found:
        raise DeviceError(
            "no Kobo found. Connect it over USB, tap Connect on it, and if it still isn't found, "
            "pass the drive with --mount (e.g. --mount /media/$USER/KOBOeReader or --mount E:\\)"
        )
    if len(found) > 1:
        raise DeviceError("several Kobos found (%s); choose one with --mount" % ", ".join(found))
    return found[0]


def read_version(root):
    """Model and firmware from .kobo/version (the serial number isn't kept)."""
    with open(os.path.join(root, ".kobo", "version"), encoding="ascii", errors="replace") as f:
        fields = f.read().strip().split(",")
    if len(fields) < 3:
        raise DeviceError(".kobo/version has an unexpected format")
    model_id = fields[-1][-3:] if fields[-1] else ""
    return {
        "model_code": fields[0][:4],
        "firmware": fields[2],
        "model_id": model_id,
        "model": MODELS.get(model_id, "unknown model (id %s)" % model_id),
    }


def _confirm(question, yes):
    if yes:
        return True
    try:
        return input(question + " (y/n) ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _config_version(path):
    from . import config as configmod

    with open(path, "rb") as f:
        return configmod.load(f.read()).version


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# --- info and install --------------------------------------------------------------------------


def cmd_info(root, a):
    v = read_version(root)
    print("Kobo at %s" % root)
    print("  Model:    %s (%s, model number %s)" % (v["model"], v["model_code"], v["model_id"]))
    print("  Firmware: %s" % v["firmware"])
    try:
        free = shutil.disk_usage(root).free
        print("  Free:     %.0f MB" % (free / 1e6))
    except OSError:
        pass
    if a.config:
        want = _config_version(a.config)
        if want != v["firmware"]:
            print("  WARNING: the patch set is for %s, but the Kobo runs %s. Don't install it." % (want, v["firmware"]))
            return 1
        print("  The patch set matches this firmware.")
    return 0


def cmd_install(root, a):
    src = a.tgz
    if not os.path.isfile(src):
        raise DeviceError("%s not found (run kobopatcher first)" % src)
    v = read_version(root)
    if a.config:
        want = _config_version(a.config)
        if want != v["firmware"]:
            raise DeviceError("the patch set is for %s, but the Kobo runs %s" % (want, v["firmware"]))
    else:
        print("Note: without --config the firmware version can't be checked; the Kobo runs %s." % v["firmware"])
    dest = os.path.join(root, ".kobo", "KoboRoot.tgz")
    if os.path.exists(dest):
        print("There is already a KoboRoot.tgz waiting on the Kobo; it will be replaced.")
    if not _confirm("Copy %s to %s?" % (src, dest), a.yes):
        print("Nothing copied.")
        return 1
    tmp = dest + ".part"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
    print("Copied.")
    print(EJECT)
    print("To undo the patches later, run: kobopatcher restore")
    return 0


# --- Kobo eReader.conf --------------------------------------------------------------------------


class KoboConf:
    """A Qt INI file edited line by line, keeping order, case and unknown keys."""

    def __init__(self, text):
        self.nl = "\r\n" if "\r\n" in text else "\n"
        self.lines = text.splitlines()
        self.trailing_nl = text.endswith(("\n", "\r\n")) or not text

    def _section_range(self, section):
        start = None
        for i, ln in enumerate(self.lines):
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                if start is not None:
                    return start, i
                if s[1:-1] == section:
                    start = i + 1
        return (start, len(self.lines)) if start is not None else (None, None)

    def _find(self, section, key):
        a, b = self._section_range(section)
        if a is None:
            return None
        for i in range(a, b):
            k, sep, _ = self.lines[i].partition("=")
            if sep and k.strip() == key:
                return i
        return None

    def get(self, section, key):
        i = self._find(section, key)
        return None if i is None else self.lines[i].partition("=")[2]

    def set(self, section, key, value):
        i = self._find(section, key)
        if i is not None:
            self.lines[i] = "%s=%s" % (key, value)
            return
        a, b = self._section_range(section)
        if a is None:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines += ["[%s]" % section, "%s=%s" % (key, value)]
            return
        # after the section's last key line
        j = b
        while j > a and not self.lines[j - 1].strip():
            j -= 1
        self.lines.insert(j, "%s=%s" % (key, value))

    def remove(self, section, key):
        i = self._find(section, key)
        if i is not None:
            del self.lines[i]

    def text(self):
        return self.nl.join(self.lines) + (self.nl if self.trailing_nl and self.lines else "")


class SettingEntry(Entry):
    def __init__(self, spec):
        super().__init__(spec["id"], spec["name"], spec.get("category", ""), " ".join(spec.get("description", "").split()))
        self.sets = spec.get("set") or []
        v = spec.get("value")
        self.value_spec = v
        self.value = None
        if v:
            self.sets = [{"section": v["section"], "key": v["key"], "value": None}]

    def is_set_in(self, conf):
        if self.value_spec:
            cur = conf.get(self.value_spec["section"], self.value_spec["key"])
            return cur is not None and cur != ""
        return all(conf.get(s["section"], s["key"]) == s["value"] for s in self.sets)

    def apply(self, conf):
        """Write (enabled) or remove (disabled) the keys; returns change lines."""
        changes = []
        for s in self.sets:
            cur = conf.get(s["section"], s["key"])
            if self.enabled:
                val = self.value if self.value_spec else s["value"]
                if cur != val:
                    conf.set(s["section"], s["key"], val)
                    changes.append("  set    [%s] %s=%s" % (s["section"], s["key"], val))
            elif cur is not None:
                conf.remove(s["section"], s["key"])
                changes.append("  remove [%s] %s (back to the firmware default)" % (s["section"], s["key"]))
        return changes


def load_settings(conf):
    path = os.path.join(os.path.dirname(__file__), "data", "device-settings.yaml")
    with open(path, encoding="utf-8") as f:
        specs = yaml.safe_load(f)
    entries = []
    for spec in specs:
        e = SettingEntry(spec)
        e.default = e.enabled = e.is_set_in(conf)
        if e.value_spec:
            cur = conf.get(e.value_spec["section"], e.value_spec["key"])
            e.value = cur if cur else e.value_spec.get("placeholder", "")
        entries.append(e)
    return Selection(entries, "Settings in Kobo eReader.conf")


def cmd_settings(root, a):
    path = os.path.join(root, CONF)
    if a.undo:
        baks = sorted(glob.glob(path + ".kobopatcher-*.bak"))
        if not baks:
            raise DeviceError("no kobopatcher backup of Kobo eReader.conf found")
        if not _confirm("Restore %s?" % os.path.basename(baks[-1]), a.yes):
            return 1
        shutil.copyfile(baks[-1], path)
        print("Restored %s." % os.path.basename(baks[-1]))
        return 0
    if not os.path.isfile(path):
        raise DeviceError("%s not found" % path)
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
    conf = KoboConf(text)
    sel = load_settings(conf)
    if a.list:
        print(sel.format_list())
        return 0
    for kv in a.set:
        key, sep, val = kv.partition("=")
        e = sel.get(key)
        if not sep or e is None or not e.value_spec:
            raise DeviceError("--set needs ID=VALUE for a setting that takes a value (see --list)")
        e.value, e.enabled = val, True
    if a.select:
        from .tui import run

        if run(sel, path, [("a", "apply", "Write to the Kobo")]) != "apply":
            print("Nothing changed.")
            return 0
    else:
        sel.apply_flags(a.enable, a.disable)
    changes = []
    for e in sel.entries:
        if e.value_spec and e.enabled and (not e.value or e.value == e.value_spec.get("placeholder")):
            raise DeviceError("%s needs a value: use --set %s=VALUE (or e in --select)" % (e.name, e.id))
        changes += e.apply(conf)
    if not changes:
        print("Nothing to change.")
        return 0
    print("Changes to Kobo eReader.conf:")
    print("\n".join(changes))
    if not _confirm("Write them?", a.yes):
        print("Nothing changed.")
        return 1
    bak = "%s.kobopatcher-%s.bak" % (path, _stamp())
    shutil.copyfile(path, bak)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(conf.text())
    print("Written. Backup: %s (undo with: kobopatcher device settings --undo)" % os.path.basename(bak))
    print("Eject the Kobo; the settings take effect when it restarts (or after the next USB disconnect).")
    return 0


# --- databases -----------------------------------------------------------------------------------

DB_ACTIONS = {
    "analytics-cleanup": "Delete the optional analytics events, then VACUUM and check the database (FPB §4.4).",
    "compact": "Checkpoint the write-ahead log, rebuild with 4 KB pages (VACUUM) and check the database (FPB §0.1, step 3).",
    "date-shift": "Move reading dates from 2012 (a reset clock) to now, keeping their order (FPB §0.1, step 2). Set the clock first.",
    "secure-db-rebuild": (
        "Move the broken secure database (BookReader.sqlite*) off the Kobo into the backup folder; the Kobo creates a "
        "new, empty one on the next start. Loses Kobo-store tokens and entitlements (FPB §0.1a)."
    ),
}


def _db_files(root, name):
    base = os.path.join(root, ".kobo", name)
    return [p for p in (base, base + "-wal", base + "-shm") if os.path.exists(p)]


def _backup(files, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    for p in files:
        shutil.copy2(p, os.path.join(backup_dir, os.path.basename(p)))
    return backup_dir


def _run_sql(db, action):
    con = sqlite3.connect(db, isolation_level=None)
    try:
        if action == "analytics-cleanup":
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "AnalyticsEvents" not in tables:
                return "No AnalyticsEvents table; nothing to delete."
            before = con.execute("SELECT count(*) FROM AnalyticsEvents").fetchone()[0]
            con.execute("DELETE FROM AnalyticsEvents WHERE Mandatory IS NOT 1")
            after = con.execute("SELECT count(*) FROM AnalyticsEvents").fetchone()[0]
            con.execute("VACUUM")
            msg = "Deleted %d of %d analytics events." % (before - after, before)
        elif action == "compact":
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() == "wal":
                con.execute("PRAGMA journal_mode=DELETE")  # page_size can't change in WAL mode
            con.execute("PRAGMA page_size=4096")
            con.execute("VACUUM")
            if mode.lower() == "wal":
                con.execute("PRAGMA journal_mode=WAL")
            msg = "Rebuilt with %d-byte pages." % con.execute("PRAGMA page_size").fetchone()[0]
        elif action == "date-shift":
            n = con.execute("SELECT count(*) FROM content WHERE DateLastRead BETWEEN '2012' AND '2013'").fetchone()[0]
            con.execute(
                "WITH d AS (SELECT (julianday('now') - julianday(MAX(DateLastRead))) * 86400 AS s "
                "FROM content WHERE DateLastRead BETWEEN '2012' AND '2013') "
                "UPDATE content SET DateLastRead = strftime('%Y-%m-%dT%H:%M:%SZ', DateLastRead, "
                "'+' || (SELECT CAST(s AS INTEGER) FROM d) || ' seconds') "
                "WHERE DateLastRead BETWEEN '2012' AND '2013'"
            )
            msg = "Moved %d reading dates from 2012 to the present." % n
        else:
            raise DeviceError("unknown action %s" % action)
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise DeviceError("integrity check failed after %s: %s (restore the backup)" % (action, check))
        return msg + " Integrity check: ok."
    finally:
        con.close()


def cmd_db(root, a):
    if a.list or not a.run:
        for k, v in DB_ACTIONS.items():
            print("  %-18s %s" % (k, v))
        print("\nRun one with: kobopatcher device db --run ACTION")
        return 0
    action = a.run
    if action not in DB_ACTIONS:
        raise DeviceError("unknown action %r (see --list)" % action)
    backup_dir = a.backup_dir or os.path.abspath("kobo-backup-%s" % _stamp())
    print(DB_ACTIONS[action])
    if action == "secure-db-rebuild":
        files = _db_files(root, "BookReader.sqlite")
        if not files:
            raise DeviceError("no .kobo/BookReader.sqlite on the Kobo; nothing to rebuild")
        print("Files to move from the Kobo to %s:" % backup_dir)
        for p in files:
            print("  %s (%d bytes)" % (p, os.path.getsize(p)))
        if not a.yes:
            try:
                ok = input('Type "rebuild" to go ahead: ').strip() == "rebuild"
            except EOFError:
                ok = False
            if not ok:
                print("Nothing changed.")
                return 1
        _backup(files, backup_dir)
        for p in files:
            os.remove(p)
        print("Moved. Eject the Kobo; it creates a new secure database when it starts.")
        print("To undo, copy the files from %s back into .kobo." % backup_dir)
        return 0
    files = _db_files(root, "KoboReader.sqlite")
    if not files:
        raise DeviceError("no .kobo/KoboReader.sqlite on the Kobo")
    print("The database is copied to %s first." % backup_dir)
    if not _confirm("Go ahead?", a.yes):
        print("Nothing changed.")
        return 1
    _backup(files, backup_dir)
    print(_run_sql(files[0], action))
    print("Done. Backup: %s. Eject the Kobo before using it." % backup_dir)
    return 0


# --- command line --------------------------------------------------------------------------------


def _parser():
    p = argparse.ArgumentParser(prog="kobopatcher device", description="Work with a Kobo connected over USB.")
    p.add_argument("--mount", help="the Kobo's drive (default: found automatically)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info", help="show the model and firmware of the connected Kobo")
    s.add_argument("--config", help="kobopatch.yaml of a patch set, to check that it matches")
    s = sub.add_parser("install", help="copy a KoboRoot.tgz to the Kobo")
    s.add_argument("tgz", nargs="?", default=os.path.join("out", "KoboRoot.tgz"))
    s.add_argument("--config", help="kobopatch.yaml of the patch set, to check the firmware version")
    s.add_argument("--yes", action="store_true", help="don't ask")
    s = sub.add_parser("settings", help="switch settings in Kobo eReader.conf")
    s.add_argument("--list", action="store_true")
    s.add_argument("--select", action="store_true")
    s.add_argument("--enable", action="append", default=[], metavar="ID")
    s.add_argument("--disable", action="append", default=[], metavar="ID")
    s.add_argument("--set", action="append", default=[], metavar="ID=VALUE")
    s.add_argument("--undo", action="store_true", help="restore the last backup")
    s.add_argument("--yes", action="store_true", help="don't ask")
    s = sub.add_parser("db", help="maintenance of the Kobo's databases")
    s.add_argument("--list", action="store_true")
    s.add_argument("--run", metavar="ACTION")
    s.add_argument("--backup-dir", help="where to put the backup (default: ./kobo-backup-TIMESTAMP)")
    s.add_argument("--yes", action="store_true", help="don't ask")
    return p


def main(argv=None) -> int:
    a = _parser().parse_args(argv)
    try:
        root = kobo_root(a.mount)
        return {"info": cmd_info, "install": cmd_install, "settings": cmd_settings, "db": cmd_db}[a.cmd](root, a)
    except KobopatcherError as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
