"""The guided mode: `kobopatcher` without a kobopatch.yaml, and `kobopatcher restore`.

    kobopatcher [start] [--target VERSION] [--model ID] [--mount PATH] [--workdir DIR]
                [--patchsets URL|DIR] [-f FIRMWARE.zip] [--enable/--disable/--only PATCH]
                [--no-menu] [--check-elf] [--yes]
    kobopatcher restore [--target VERSION] [--model ID] [--mount PATH] [--workdir DIR]
                [--patchsets URL|DIR] [-f FIRMWARE.zip] [--full] [--out DIR] [--yes]

Patching: find the connected Kobo and its firmware, download the patch set for
that firmware into ~/kobopatcher/<version>/, download the firmware from Kobo
(after asking), let the user choose patches in the menu, build KoboRoot.tgz and
offer to copy it to the Kobo.

Restoring: put Kobo's own, unmodified KoboRoot.tgz of the installed firmware on
the Kobo (or, with --full, the whole update), which undoes all patches.
"""

import argparse
import glob
import os
import shutil
import sys
import types
import zipfile

from . import __version__
from . import catalog, device, fetch, patchsets
from .errors import KobopatcherError

# the last community patch sets (kobopatch-patches) are for this version
COMMUNITY_LAST = "4.38.21908"
COMMUNITY_URL = "https://github.com/pgaskin/kobopatch-patches/releases"
README_URL = "https://github.com/kebasaa/kobopatcher#readme"

CONNECT_HELP = """No Kobo found. To connect it: plug it into this computer with a USB cable and
tap "Connect" on its screen; it then shows up as a drive called KOBOeReader.
If it's connected but not found, pass the drive with --mount
(e.g. --mount /media/$USER/KOBOeReader, --mount /Volumes/KOBOeReader or --mount E:\\)."""

EJECT = device.EJECT


class GuidedError(KobopatcherError):
    pass


class UI:
    """Questions and messages; --yes answers every question with yes."""

    def __init__(self, yes=False):
        self.yes = yes
        self.n = 0

    def say(self, msg=""):
        print(msg)

    def step(self, msg):
        self.n += 1
        print("\n[%d] %s" % (self.n, msg))

    def ask(self, prompt):
        try:
            return input(prompt).strip()
        except EOFError:
            return None

    def confirm(self, question):
        if self.yes:
            return True
        ans = self.ask("%s [Y/n] " % question)
        if ans is None:
            return False
        return ans.lower() in ("", "y", "yes")

    def choose(self, question, options, default):
        """options: [(key, label)]; returns a key (default on Enter, None on end of input)."""
        if self.yes:
            return default
        print(question)
        for i, (key, label) in enumerate(options, 1):
            print("  %d) %s%s" % (i, label, "  (default)" if key == default else ""))
        while True:
            ans = self.ask("Choose 1-%d: " % len(options))
            if ans is None:
                return None
            if ans == "":
                return default
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return options[int(ans) - 1][0]
            for key, _ in options:
                if ans == key:
                    return key
            print("Please type a number from 1 to %d." % len(options))


def _parser(cmd):
    p = argparse.ArgumentParser(
        prog="kobopatcher" if cmd == "start" else "kobopatcher restore",
        description="Guided mode: patch the firmware of a connected Kobo." if cmd == "start" else
        "Reset a Kobo to its stock firmware (undo all patches).",
    )
    p.add_argument("--target", metavar="VERSION", help="firmware version (default: the one the connected Kobo runs)")
    p.add_argument("--model", metavar="ID", help="model number (last 3 digits of the device id, e.g. 370) when no Kobo is connected")
    p.add_argument("--mount", metavar="PATH", help="the Kobo's drive (default: found automatically)")
    p.add_argument("--workdir", metavar="DIR", help="where patch sets and firmware are kept (default: %s)" % fetch.default_workdir())
    p.add_argument("--patchsets", metavar="URL|DIR", help="where to get patch sets from (default: this project's GitHub release)")
    p.add_argument("-f", "--firmware", metavar="FILE", help="use this firmware zip instead of downloading it")
    p.add_argument("-y", "--yes", action="store_true", help="answer every question with yes (no menu)")
    if cmd == "start":
        p.add_argument("--enable", action="append", default=[], metavar="PATCH")
        p.add_argument("--disable", action="append", default=[], metavar="PATCH")
        p.add_argument("--only", action="append", default=[], metavar="PATCH")
        p.add_argument("--no-menu", action="store_true", help="don't open the menu; use the saved choice")
        p.add_argument("--check-elf", action="store_true", help="check the patched ELF files")
    else:
        p.add_argument("--full", action="store_true", help="unpack the whole firmware update, not only KoboRoot.tgz")
        p.add_argument("--out", metavar="DIR", help="write the stock files into DIR instead of onto a Kobo")
    return p


def wants_guided(argv):
    """Whether `kobopatcher ARGS` means the guided mode: only guided-mode
    options and no kobopatch.yaml in the current folder."""
    if any(a in ("-h", "--help", "--version") for a in argv):
        return False
    if os.path.exists("kobopatch.yaml"):
        return False
    p = _parser("start")

    def error(message):
        raise ValueError(message)

    p.error = error
    try:
        _, rest = p.parse_known_args(argv)
    except ValueError:
        return True  # let the guided parser report the error
    return not rest


def main(cmd, argv):
    a = _parser(cmd).parse_args(argv)
    ui = UI(a.yes)
    cwd = os.getcwd()
    try:
        if cmd == "restore":
            return run_restore(a, ui)
        return run_start(a, ui)
    except KobopatcherError as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
    finally:
        os.chdir(cwd)


# --- shared steps ----------------------------------------------------------------------------


def _find_kobo(ui, a):
    """(root, version info) of the connected Kobo, or (None, None)."""
    if a.mount:
        root = device.kobo_root(a.mount)
    else:
        found = device.find_kobos()
        if not found:
            return None, None
        root = found[0]
        if len(found) > 1:
            if ui.yes:
                raise GuidedError("several Kobos found (%s); choose one with --mount" % ", ".join(found))
            root = ui.choose("Several Kobos are connected. Which one?", [(r, r) for r in found], found[0])
            if root is None:
                raise GuidedError("no Kobo chosen")
    info = device.read_version(root)
    ui.say("Found a %s at %s, running firmware %s." % (info["model"], root, info["firmware"]))
    return root, info


def _workdir(a):
    return os.path.abspath(a.workdir or fetch.default_workdir())


def _known_shas(entry):
    return {str(v.get("sha256", "")).lower() for v in (entry or {}).get("firmware", {}).values() if v.get("sha256")}


def _firmware(ui, a, entry, version, model_id, dest_dir):
    """The path of the firmware zip: -f, the one in the workspace, or downloaded."""
    if a.firmware:
        fw = os.path.abspath(a.firmware)
        if not os.path.isfile(fw):
            raise GuidedError("%s not found" % fw)
        ui.say("Using %s." % fw)
        return fw
    name = "kobo-update-%s.zip" % version
    fw = os.path.join(dest_dir, name)
    shas = _known_shas(entry)
    if os.path.isfile(fw):
        if shas and fetch.sha256_file(fw) not in shas:
            ui.say("%s is there, but isn't the firmware this patch set was checked with." % name)
            ui.say("If it is for another model, that's expected; otherwise delete it and run again.")
        else:
            ui.say("%s is already there." % name)
        return fw
    if not entry or not entry.get("release"):
        raise GuidedError(
            "kobopatcher doesn't know where Kobo publishes firmware %s. Download kobo-update-%s.zip for your model "
            "(links on %s) and pass it with -f" % (version, version, fetch.FIRMWARE_PAGE)
        )
    models = device.load_models()
    if model_id:
        family = fetch.family_for(model_id, models)
    else:
        families = sorted(entry.get("firmware") or {})
        if len(families) != 1:
            raise GuidedError("no Kobo connected: say which model with --model (e.g. --model 370), or pass the firmware with -f")
        family = families[0]
        names = [m["name"] for m in models.values() if m.get("family") == family] or [family]
        ui.say("No Kobo is connected, so this is the firmware for the %s." % " / ".join(names))
        ui.say("For another model, pass --model (e.g. --model 371) or the firmware zip with -f.")
    url = fetch.firmware_url(version, entry["release"], family)
    try:
        size = fetch.head_size(url)
    except fetch.FetchError as e:
        raise GuidedError(
            "%s\nKobo doesn't publish this firmware there for your model. Download kobo-update-%s.zip yourself "
            "(links on %s) and pass it with -f" % (e, version, fetch.FIRMWARE_PAGE)
        ) from None
    sha = ((entry.get("firmware") or {}).get(family) or {}).get("sha256")
    ui.say("The firmware isn't here yet: %s" % url)
    if not ui.confirm("Download %s (%s) from Kobo's server?" % (name, "%.0f MB" % (size / 1e6) if size else "size unknown")):
        raise GuidedError("no firmware. Download it yourself from the address above and pass it with -f, or put it in %s" % dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    fetch.download(url, fw, sha, ui.say)
    if sha:
        ui.say("Downloaded and checked (SHA-256).")
    else:
        ui.say("Downloaded. Its checksum isn't known for this model, so it couldn't be checked; the patches")
        ui.say("still refuse to apply to files that differ from what they expect.")
    return fw


def _index_entry(ui, a, version):
    src = fetch.Source(a.patchsets)
    idx = src.index()
    return src, idx, patchsets.find(idx, version)


# --- patching ------------------------------------------------------------------------------------


def run_start(a, ui):
    ui.say("kobopatcher %s: guided mode. Press Ctrl+C at any time to stop." % __version__)
    ui.step("Looking for a connected Kobo")
    root, info = _find_kobo(ui, a)
    model_id = info["model_id"] if info else (a.model or "")
    if root and not a.target:
        what = ui.choose(
            "What do you want to do?",
            [("patch", "Patch the firmware"), ("restore", "Reset to stock firmware (undo all patches)"),
             ("settings", "Change device settings"), ("quit", "Quit")],
            "patch",
        )
        if what in (None, "quit"):
            return 0
        if what == "restore":
            a.full, a.out = False, None
            return _restore(a, ui, root, info)
        if what == "settings":
            ns = types.SimpleNamespace(undo=False, list=False, set=[], select=True, enable=[], disable=[], yes=False)
            return device.cmd_settings(root, ns)
    if a.target:
        version = a.target
        if info and info["firmware"] != version:
            ui.say("Note: the Kobo runs %s, so the result can't be installed on it." % info["firmware"])
    elif info:
        version = info["firmware"]
    else:
        ui.say(CONNECT_HELP)
        version = _choose_version(ui, a)
        if version is None:
            return 1

    ui.step("Finding the patch set for firmware %s" % version)
    src, idx, entry = _index_entry(ui, a, version)
    available = patchsets.versions(idx)
    if not entry:
        ui.say("There is no patch set for firmware %s." % version)
        ui.say("Patch sets exist for: %s." % ", ".join(available))
        if patchsets.version_key(version) <= patchsets.version_key(COMMUNITY_LAST):
            ui.say("For firmware up to %s, use the community patch sets from %s" % (COMMUNITY_LAST, COMMUNITY_URL))
            ui.say("(unzip one and run kobopatcher in its folder).")
        return 1
    newer = [v for v in available if patchsets.version_key(v) > patchsets.version_key(version)]
    if newer and info and info["firmware"] == version:
        ui.say("A patch set also exists for newer firmware (%s). To use it, update the Kobo first" % ", ".join(newer))
        ui.say("(see \"Updating your Kobo\" in %s). Going on with %s, the version the Kobo runs." % (README_URL, version))
    if model_id and entry.get("checked_models") and model_id not in entry["checked_models"]:
        ui.say("Note: this set was checked with the firmware of the %s only. Patches refuse to apply" %
               " / ".join(device.MODELS.get(m, m) for m in entry["checked_models"]))
        ui.say("if your model's files differ, so nothing is built from a mismatch.")

    ui.step("Setting up the workspace")
    dest = os.path.join(_workdir(a), version)
    state = fetch.set_state(dest, entry)
    if state == "missing":
        ui.say("Downloading the patch set into %s" % dest)
        fetch.install_set(src, entry, dest)
    elif state == "outdated":
        if ui.confirm("A newer version of the patch set for %s is available. Update it? (Your choice of patches is kept.)" % version):
            dropped = fetch.install_set(src, entry, dest)
            ui.say("Updated.")
            for d in dropped:
                ui.say("  dropped from your choice (no longer in the set): %s" % d)
        else:
            ui.say("Keeping the patch set you have.")
    else:
        ui.say("The patch set in %s is up to date." % dest)
    fw = _firmware(ui, a, entry, version, model_id, dest)

    ui.step("Choosing patches")
    conf = os.path.join(dest, "kobopatch.yaml")
    cfg, cat = catalog.load(conf)
    cat.apply_flags(a.enable, a.disable, a.only)
    if not (a.no_menu or ui.yes):
        from . import tui

        ui.say("Opening the menu: Space switches a patch on or off, p builds, s saves and quits, q quits, ? helps.")
        action = tui.run(cat, dest,
                         [("p", "patch", "Build KoboRoot.tgz"), ("s", "save", "Save the choice and quit")])
        if action == "quit":
            ui.say("Stopped; nothing saved or built.")
            return 0
    else:
        action = "patch"
    catalog.save_overrides(conf, cat.overrides())
    on = [e for e in cat.entries if e.enabled]
    ui.say("%d %s selected (%d of them new/experimental). The choice is saved in %s." %
           (len(on), "patch" if len(on) == 1 else "patches", sum(e.experimental for e in on), conf))
    for e in on:
        if e.not_recommended:
            ui.say("Warning: %s is on, but NOT RECOMMENDED: %s" % (e.name, e.not_recommended))
    if action == "save":
        return 0

    ui.step("Building KoboRoot.tgz")
    from . import cli

    args = ["-f", fw]
    if a.check_elf:
        args.append("--check-elf")
    rc = cli.main(args + [conf])
    if rc != 0:
        ui.say("Building failed; the log is in %s." % os.path.join(dest, cfg.log))
        return rc
    out = os.path.join(dest, cfg.out)
    ui.say("Built %s" % out)

    ui.step("Installing on the Kobo")
    if root and info["firmware"] == version:
        if ui.confirm("Copy KoboRoot.tgz to the Kobo now?"):
            device.cmd_install(root, types.SimpleNamespace(tgz=out, config=conf, yes=True))
        else:
            ui.say("Not copied. To do it later, connect the Kobo and run:")
            ui.say("  kobopatcher device install %s --config %s" % (out, conf))
    else:
        ui.say("Copy %s into the hidden .kobo folder of the Kobo (KOBOeReader/.kobo/KoboRoot.tgz)," % out)
        ui.say("or connect a Kobo running %s and run:" % version)
        ui.say("  kobopatcher device install %s --config %s" % (out, conf))
        ui.say("Then eject it; it installs the patches when it restarts.")
    return 0


def _choose_version(ui, a):
    """Without a Kobo: let the user pick a firmware version (or retry the search)."""
    if ui.yes:
        raise GuidedError("no Kobo found; connect it, or say which firmware with --target VERSION")
    src = fetch.Source(a.patchsets)
    vs = patchsets.versions(src.index())
    while True:
        opts = [("retry", "Search for the Kobo again")] + [(v, "Patch firmware %s without a Kobo" % v) for v in vs] + [("quit", "Quit")]
        ans = ui.choose("What now?", opts, "retry")
        if ans in (None, "quit"):
            return None
        if ans != "retry":
            return ans
        found = device.find_kobos()
        if found:
            info = device.read_version(found[0])
            ui.say("Found a %s at %s, running firmware %s." % (info["model"], found[0], info["firmware"]))
            return info["firmware"]
        ui.say("Still no Kobo found.")


# --- resetting to stock ----------------------------------------------------------------------------


def run_restore(a, ui):
    ui.say("kobopatcher %s: reset to stock firmware." % __version__)
    ui.step("Looking for a connected Kobo")
    root, info = (None, None) if a.out and not a.mount else _find_kobo(ui, a)
    if not root and not a.out:
        ui.say(CONNECT_HELP)
        raise GuidedError("no Kobo found (or use --target VERSION --out DIR to write the stock files into a folder)")
    return _restore(a, ui, root, info)


def _restore(a, ui, root, info):
    if info and a.target and a.target != info["firmware"]:
        raise GuidedError(
            "the Kobo runs %s; a reset installs the stock files of the version it runs (leave out --target)" % info["firmware"]
        )
    version = a.target or (info and info["firmware"])
    if not version:
        raise GuidedError("say which firmware with --target VERSION")
    model_id = info["model_id"] if info else (a.model or "")

    ui.step("Getting Kobo's firmware %s" % version)
    dest = os.path.join(_workdir(a), version)
    entry = None
    have = a.firmware or os.path.isfile(os.path.join(dest, "kobo-update-%s.zip" % version))
    if not have:
        _, _, entry = _index_entry(ui, a, version)
    fw = _firmware(ui, a, entry, version, model_id, dest)
    with zipfile.ZipFile(fw) as z:
        if "KoboRoot.tgz" not in z.namelist():
            raise GuidedError("%s has no KoboRoot.tgz; is it a Kobo firmware update?" % fw)

    ui.step("Resetting")
    if a.out:
        target = os.path.abspath(a.out)
        os.makedirs(target, exist_ok=True)
    else:
        target = os.path.join(root, ".kobo")
        waiting = os.path.join(target, "KoboRoot.tgz")
        if os.path.exists(waiting) and not ui.yes:
            raise GuidedError(
                "a KoboRoot.tgz is already waiting on the Kobo (%s). Eject the Kobo and let it install that first, "
                "delete it, or run again with --yes to replace it" % waiting
            )
    if a.full:
        what = "unpack the whole firmware update %s into %s" % (version, target)
        detail = "This reinstalls the system as Kobo's updater does; books, settings and accounts stay."
    else:
        what = "copy Kobo's unmodified KoboRoot.tgz of %s into %s" % (version, target)
        detail = "This replaces every file a patch could have changed; books, settings and accounts stay."
    ui.say(detail)
    if not ui.confirm("%s%s?" % (what[0].upper(), what[1:])):
        ui.say("Nothing changed.")
        return 1
    with zipfile.ZipFile(fw) as z:
        names = z.namelist() if a.full else ["KoboRoot.tgz"]
        for name in names:
            _extract(z, name, target)
    ui.say("Done.")
    _report_extra_files(ui, dest)
    if root and not a.out:
        _offer_settings_undo(ui, root)
        ui.say(EJECT)
    else:
        ui.say("Copy %s into the hidden .kobo folder of the Kobo, then eject it." % ("the files" if a.full else "KoboRoot.tgz"))
    return 0


def _extract(z, name, target):
    parts = name.split("/")
    if name.startswith("/") or "\\" in name or ".." in parts or ":" in parts[0]:
        raise GuidedError("unsafe path %r in the firmware zip" % name)
    if name.endswith("/"):
        os.makedirs(os.path.join(target, *parts[:-1]), exist_ok=True)
        return
    path = os.path.join(target, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    with z.open(name) as src, open(part, "wb") as out:
        shutil.copyfileobj(src, out)
    os.replace(part, path)


def _report_extra_files(ui, dest):
    """Files a patch set added can't be removed by a KoboRoot.tgz; list the ones
    the saved choice of patches adds."""
    conf = os.path.join(dest, "kobopatch.yaml")
    if not os.path.isfile(conf):
        return
    cwd = os.getcwd()
    try:
        cfg, cat = catalog.load(conf)
    except KobopatcherError:
        return
    finally:
        os.chdir(cwd)
    on = {(pf, e.name) for e in cat.entries if e.enabled for pf, _ in e.targets}
    extra = set()
    for dests in (cfg.files or {}).values():
        for d in dests:
            pf, _, name = (d.cond or "").partition(": ")
            if d.cond is None or (pf, name) in on:
                extra.add("/" + d.dest.lstrip("/"))
    if extra:
        ui.say("A KoboRoot.tgz can't delete files, so files your patches added stay on the Kobo.")
        ui.say("Stock firmware doesn't use them: %s" % ", ".join(sorted(extra)))


def _offer_settings_undo(ui, root):
    conf = os.path.join(root, device.CONF)
    baks = sorted(glob.glob(conf + ".kobopatcher-*.bak"))
    if not baks:
        return
    first = baks[0]
    ui.say("kobopatcher changed the Kobo's settings before (Kobo eReader.conf).")
    if ui.confirm("Put the settings back as they were before the first change (%s)?" % os.path.basename(first)):
        bak = "%s.kobopatcher-%s.bak" % (conf, device._stamp())
        shutil.copyfile(conf, bak)
        shutil.copyfile(first, conf)
        ui.say("Settings restored (the current ones are kept in %s)." % os.path.basename(bak))
