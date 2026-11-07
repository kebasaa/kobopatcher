"""kobopatcher: patch Kobo firmware and build a KoboRoot.tgz.

Usage: kobopatcher [OPTIONS] [CONFIG_FILE]
"""

import argparse
import hashlib
import io
import os
import sys

from . import __version__, config, firmware, patchfile, translations
from .compat import go_compat
from .errors import GoPanic, KobopatcherError
from .patchlib.patcher import Patcher

HOMEPAGE = "https://github.com/kebasaa/kobopatcher"


HELP = """Usage: kobopatcher [OPTIONS] [CONFIG_FILE]

Version: %s

Options:
  -f, --firmware FILE   firmware file to be used, overriding `in` in the config
                        (can also be a testdata tarball from kobopatch-testdata)
  -t, --run-tests       test all patches one at a time (instead of patching)
      --check-elf       re-parse every patched ELF file and check it is still
                        well-formed (dynamic section, program headers, lazy
                        binding vs RELRO, stubs)
      --version         print the version and exit
  -h, --help            show this help text

Choosing patches (each patch of the set is an entry, see --list):
      --list            list the entries by category with their state and id
                        (add --json for JSON) and exit
      --select          choose entries in an interactive menu
      --enable PATCH    switch an entry on (id or name, * and ? globs; repeatable)
      --disable PATCH   switch an entry off (repeatable)
      --only PATCH      switch everything off except these (repeatable)
      --save            write the choice to the overrides: of kobopatch.yaml
                        (otherwise --enable/--disable/--only apply to this run)

Device (a Kobo connected over USB):
  kobopatcher device --help

If CONFIG_FILE is not specified, kobopatcher will use ./kobopatch.yaml.
Use - to read the config from stdin.
"""


class RunError(KobopatcherError):
    pass


def wrap(err, fmt, *a):
    return RunError("%s: %s" % (fmt % a if a else fmt, err))


class KoboPatcher:
    def __init__(self, cfg, log, check_elf=False):
        self.cfg = cfg
        self.check_elf = check_elf
        self.d = log  # debug log (the log file)
        self.out = None
        self.sums = {}
        self.known = set()  # (patch file, patch name)
        self.enabled = set()  # the enabled ones, after overrides

    # user output
    @staticmethod
    def l(msg=""):  # noqa: E743
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    def _entries(self):
        self.d("    KoboPatch::openIn")
        return firmware.open_input(self.cfg.in_, self.l)

    def _targets(self, name):
        """Patch files whose target is this tar entry (in config order)."""
        files = []
        for pf, target in self.cfg.patches.items():
            if name == "./" + target or name == target or os.path.basename(target) == name:
                files.append(pf)
        return files

    def apply_patches(self):
        self.d("\n\nKoboPatch::ApplyPatches")
        matched = set()
        for ent in self._entries():
            name = ent.name
            pfs = self._targets(name)
            if not pfs:
                continue
            for pf in pfs:
                matched.add(pf)
            flat = [pf for pf in pfs if os.path.basename(self.cfg.patches[pf]) == name]
            if flat:
                # from a testdata tarball (flat file names)
                name = "./" + self.cfg.patches[flat[-1]]
                ent.info.name = name
            self.d("    patching entry name:'%s' size:%d mode:'%d' typeflag:'%d' with files: %s" % (name, ent.info.size, ent.info.mode, ord(ent.info.type), ", ".join(pfs)))
            self.l("\nPatching %s" % name)
            if not ent.is_regular():
                raise RunError("could not patch file '%s': not a regular file" % name)
            buf = ent.read()
            orig = bytes(buf) if self.check_elf else None
            pt = Patcher(buf)
            pt.set_path(name)
            for pf in pfs:
                fmt = patchfile.format_for_file(pf)
                self.d("        loading patch file '%s' (detected format %s)" % (pf, fmt))
                try:
                    ps = patchfile.read_from_file(fmt, pf)
                except KobopatcherError as e:
                    raise wrap(e, "could not load patch file '%s'", pf) from None
                ov = self.cfg.overrides.get(pf)
                if ov:
                    self.l("  Applying overrides")
                    for on, enabled in ov.items():
                        self.l(("    ENABLE  `%s`" if enabled else "    DISABLE `%s`") % on)
                        try:
                            ps.set_enabled(on, enabled)
                        except KobopatcherError as e:
                            raise wrap(e, "could not override enabled for patch '%s'", on) from None
                try:
                    ps.validate()
                except KobopatcherError as e:
                    raise wrap(e, "invalid patch file '%s'", pf) from None
                for n in ps.names():
                    self.known.add((pf, n))
                    if getattr(ps, "parsed", None) is not None and ps.parsed[n].enabled:
                        self.enabled.add((pf, n))
                try:
                    ps.apply_to(pt)
                except GoPanic:
                    raise
                except KobopatcherError as e:
                    raise wrap(e, "error applying patch file '%s'", pf) from None
            fbuf = bytes(pt.get_bytes())
            self.out.expected_size += len(fbuf)  # differs from the original size for tombstones
            self.d("        patched file - orig:%d new:%d" % (ent.info.size, len(fbuf)))
            if self.check_elf:
                try:
                    pt.check_elf(orig)
                except KobopatcherError as e:
                    raise wrap(e, "check-elf failed for '%s'", name) from None
                if orig.startswith(b"\x7fELF"):
                    self.l("  CHECK-ELF  tombstone (0 bytes)" if len(fbuf) == 0 else "  CHECK-ELF  ok")
            self.out.add_patched(ent.info, fbuf)
            self.sums[name] = hashlib.sha1(fbuf).hexdigest()
        for pf, target in self.cfg.patches.items():
            if pf not in matched:
                # otherwise a typo in the target would silently skip all its patches
                raise RunError("could not find target '%s' of patch file '%s' in the firmware" % (target, pf))

    def apply_translations(self):
        self.d("\n\nKoboPatch::ApplyTranslations")
        if not self.cfg.translations:
            return
        self.l("\nProcessing translations")
        lr = translations.find_lrelease(self.cfg.lrelease)
        for ts, qm in self.cfg.translations.items():
            self.l("  LRELEASE  %s" % ts)
            if not qm.startswith(translations.QM_PREFIX):
                raise RunError("could not process translation: output for translation must start with %s" % translations.QM_PREFIX)
            try:
                buf = translations.compile_ts(lr, ts, self.d)
            except translations.TranslationError as e:
                if len(e.args) > 1 and e.args[1]:
                    sys.stderr.write(e.args[1] + ("" if e.args[1].endswith("\n") else "\n"))
                raise RunError(e.args[0]) from None
            self.out.add_file("./" + qm, buf)
            self.out.expected_size += len(buf)

    def apply_files(self):
        self.d("\n\nKoboPatch::ApplyFiles")
        if not self.cfg.files:
            return
        self.l("\nAdding additional files")
        for src, dests in self.cfg.files.items():
            for fd in dests:
                dest = fd.dest
                if fd.cond is not None and not self._enabled(fd.cond):
                    self.l("  SKIP %-35s  (%s is disabled)" % (src, fd.cond))
                    continue
                self.l("  ADD  %-35s  TO  %s" % (src, dest))
                if dest.startswith("/"):
                    raise RunError("could not add file: destination must not start with a slash")
                try:
                    with open(src, "rb") as f:
                        buf = f.read()
                except OSError as e:
                    raise wrap(patchfile.os_err("open", src, e), "could not read additional file '%s'", src) from None
                self.out.add_file("./" + dest, buf, fd.mode)
                self.out.expected_size += len(buf)

    def _enabled(self, cond):
        """Whether "PATCHFILE: patch name" was enabled in this run."""
        pf, sep, name = cond.partition(": ")
        if not sep or pf not in self.cfg.patches:
            raise RunError("could not add file: condition %r must be \"PATCHFILE: patch name\" with a patch file from patches" % cond)
        if (pf, name) not in self.known:
            raise RunError("could not add file: condition %r: no such patch in %s" % (cond, pf))
        return (pf, name) in self.enabled

    def apply_symlinks(self):
        self.d("\n\nKoboPatch::ApplySymlinks")
        if not self.cfg.symlinks:
            return
        self.l("\nAdding additional symlinks")
        declared = {d.dest for dests in self.cfg.files.values() for d in dests}
        for src, dest in self.cfg.symlinks.items():
            self.l("  SYMLINK  %-35s  TO  %s" % (src, dest))
            if src.startswith("/") or src.startswith("."):
                raise RunError("could not add symlink: source must not start with a slash/dot")
            if dest.startswith("/"):
                raise RunError("could not add symlink: destination must not start with a slash")
            if src not in declared:
                raise RunError("could not add symlink: target is not declared in files")
            self.out.add_symlink("./" + dest, "/" + src)

    def write_output(self):
        self.d("\n\nKoboPatch::WriteOutput")
        try:
            os.remove(self.cfg.out)
        except OSError:
            pass
        data = self.out.finish()
        try:
            with open(self.cfg.out, "wb") as f:
                f.write(data)
        except OSError as e:
            raise wrap(patchfile.os_err("open", self.cfg.out, e), "could not write output tar.gz") from None
        self.l("\nChecking patched KoboRoot.tgz for consistency")
        firmware.check_output(self.cfg.out, self.out.expected_size)
        self.d("sum == expected:%d" % self.out.expected_size)
        self.d("\nsha1 checksums:")
        for f, s in sorted(self.sums.items()):
            self.d("  %s %s" % (s, f))

    def run_tests(self):
        """Apply each patch on its own; returns {patch file: {name: error or None}}."""
        self.d("\n\nKoboPatch::RunPatchTests")
        res = {}
        for ent in self._entries():
            pfs = self._targets(ent.name)
            if not pfs:
                continue
            self.l("\nPatching %s" % ent.name)
            if not ent.is_regular():
                raise RunError("could not patch file '%s': not a regular file" % ent.name)
            buf = ent.read()
            path = ent.name
            flat = [pf for pf in pfs if os.path.basename(self.cfg.patches[pf]) == ent.name]
            if flat:
                path = "./" + self.cfg.patches[flat[-1]]
            for pf in pfs:
                fmt = patchfile.format_for_file(pf)
                if fmt != "kobopatch":
                    raise RunError("patch testing only works with kobopatch format patches")
                try:
                    ps = patchfile.read_from_file(fmt, pf)
                except KobopatcherError as e:
                    raise wrap(e, "could not load patch file '%s'", pf) from None
                try:
                    ps.validate()
                except KobopatcherError as e:
                    raise wrap(e, "invalid patch file '%s'", pf) from None
                res[pf] = r = {}
                names = ps.names()
                for name in names:
                    sys.stdout.write(" -  %s" % name)
                    sys.stdout.flush()
                    err = None
                    try:
                        for pn in names:
                            ps.set_enabled(pn, pn == name)
                        pt = Patcher(bytearray(buf))
                        if not go_compat():
                            # kobopatch never set the path, so Tombstones always failed
                            pt.set_path(path)
                        with patchfile.silenced():
                            ps.apply_to(pt)
                        ps.set_enabled(name, False)
                    except GoPanic:
                        raise
                    except KobopatcherError as e:
                        err = e
                    sys.stdout.write("\r %s  %s\n" % ("✕" if err else "✔", name))
                    sys.stdout.flush()
                    r[name] = err
        return res


def _build_parser():
    p = argparse.ArgumentParser(
        prog="kobopatcher",
        usage="kobopatcher [OPTIONS] [CONFIG_FILE]",
        description="Patch the binaries of a Kobo firmware update and build a KoboRoot.tgz for the device.",
        epilog="If CONFIG_FILE is not specified, kobopatcher will use ./kobopatch.yaml. Use - to read it from stdin.",
        add_help=False,
    )
    p.add_argument("-h", "--help", action="store_true", help="show this help text")
    p.add_argument("-f", "--firmware", default="", metavar="FILE", help="firmware file to be used (can also use a testdata tarball from kobopatch-patches)")
    p.add_argument("-t", "--run-tests", action="store_true", help="test all patches (instead of running kobopatcher)")
    p.add_argument(
        "--check-elf",
        action="store_true",
        help="re-parse every patched ELF file and check it is still well-formed (dynamic section, program headers, lazy binding vs RELRO, stubs)",
    )
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--select", action="store_true")
    p.add_argument("--enable", action="append", default=[], metavar="PATCH")
    p.add_argument("--disable", action="append", default=[], metavar="PATCH")
    p.add_argument("--only", action="append", default=[], metavar="PATCH")
    p.add_argument("--save", action="store_true")
    p.add_argument("config", nargs="*", metavar="CONFIG_FILE")
    return p


def main(argv=None) -> int:
    try:
        return _main(argv)
    except GoPanic as e:
        sys.stderr.write("panic: %s\n" % e)
        return 2
    except KeyboardInterrupt:
        return 130


def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "device":
        from . import device

        return device.main(argv[1:])
    ap = _build_parser()
    args = ap.parse_args(argv)
    if args.version:
        print("kobopatcher %s" % __version__)
        return 0
    if args.help or len(args.config) > 1:
        sys.stderr.write(HELP % __version__)
        return 1 if go_compat() or len(args.config) > 1 else 0

    tmp = io.StringIO()
    logfile = [tmp]

    def d(msg):
        logfile[0].write(msg + "\n")

    def plog(msg):
        d("          | %s" % msg.rstrip("\n").replace("\n", "\n          | "))

    patchfile.set_log(plog)
    banner = "kobopatcher %s\n%s\n" % (__version__, HOMEPAGE)
    # with --list, stdout carries only the list (e.g. --json)
    say = (lambda m: sys.stderr.write(m + "\n")) if args.list else print
    say(banner)
    d(banner)

    conf = args.config[0] if args.config else "kobopatch.yaml"
    fw = os.path.abspath(args.firmware) if args.firmware else ""

    say("Loading configuration from %s" % conf)
    try:
        if conf == "-":
            buf = sys.stdin.buffer.read()
        else:
            confdir = os.path.dirname(conf)
            if go_compat():
                # kobopatch changed into the directory and then opened the
                # (relative) path again, so "dir/kobopatch.yaml" failed
                if confdir:
                    try:
                        os.chdir(confdir)
                    except OSError:
                        pass
                with open(conf, "rb") as f:
                    buf = f.read()
            else:
                with open(conf, "rb") as f:
                    buf = f.read()
                if confdir:
                    os.chdir(confdir)
        cfg = config.load(buf)
    except OSError as e:
        sys.stderr.write("Error: could not load config file: %s\n" % patchfile.os_err("open", conf, e))
        return 1
    except KobopatcherError as e:
        sys.stderr.write("Error: could not load config file%s: %s\n" % (" from stdin" if conf == "-" else "", e))
        return 1
    d("  | " + cfg.to_json().replace("\n", "\n  | "))

    if args.list or args.select or args.enable or args.disable or args.only or args.save:
        rc = _selection(cfg, args, conf)
        if rc is not None:
            return rc

    try:
        lf = open(cfg.log, "w", encoding="utf-8")
    except OSError as e:
        sys.stderr.write("Error: could not create log file%s\n" % ("" if go_compat() else ": %s" % patchfile.os_err("open", cfg.log, e)))
        return 1
    with lf:
        lf.write(tmp.getvalue())
        logfile[0] = lf
        if fw:
            d("firmware file overridden from command line: %s" % fw)
            cfg.in_ = fw
        return _run(cfg, args, d)


def _selection(cfg, args, conf):
    """--list, --select, --enable/--disable/--only and --save.

    Returns an exit status to stop with, or None to go on patching (with the
    selection put into cfg.overrides).
    """
    from . import catalog

    try:
        cat = catalog.PatchCatalog(cfg)
        cat.apply_flags(args.enable, args.disable, args.only)
    except KobopatcherError as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
    conf_file = os.path.basename(conf)
    if args.select:
        from . import tui

        action = tui.run(cat, conf_file)
        if action == "quit":
            return 0
        if action in ("save", "patch"):
            if conf == "-":
                sys.stderr.write("Error: can't save the selection of a config read from stdin\n")
                return 1
            bak = catalog.save_overrides(conf_file, cat.overrides())
            print("Saved the selection to %s (backup: %s)" % (conf, os.path.basename(bak)))
        if action != "patch":
            return 0
    elif args.save:
        if conf == "-":
            sys.stderr.write("Error: can't save the selection of a config read from stdin\n")
            return 1
        bak = catalog.save_overrides(conf_file, cat.overrides())
        print("Saved the selection to %s (backup: %s)" % (conf, os.path.basename(bak)))
    if args.list:
        print(cat.to_json() if args.json else cat.format_list())
        return 0
    cat.apply_to_config()
    return None


def _run(cfg, args, d) -> int:
    k = KoboPatcher(cfg, d, check_elf=args.check_elf)
    if args.run_tests:
        try:
            res = k.run_tests()
        except KobopatcherError as e:
            sys.stderr.write("Error: could not apply patches: %s\n" % e)
            return 1
        errs = []
        for pf, ps in res.items():
            for pn, err in ps.items():
                if err is not None:
                    errs.append("%s: %s: %s" % (pf, pn, err))
        if errs:
            if not go_compat():
                errs.sort()
            print("\nErrors:\n  %s" % "\n  ".join(errs))
            return 1
        print("\nAll patches applied successfully.")
        return 0

    k.out = firmware.OutputTgz()
    try:
        return _apply_all(k, cfg)
    finally:
        k.out.discard()


def _apply_all(k, cfg) -> int:
    for step, what in (
        (k.apply_patches, "apply patches"),
        (k.apply_translations, "apply translations"),
        (k.apply_files, "apply additional files"),
        (k.apply_symlinks, "apply additional symlinks"),
        (k.write_output, "write output"),
    ):
        try:
            step()
        except GoPanic:
            raise
        except KobopatcherError as e:
            sys.stderr.write("Error: could not %s: %s\n" % (what, e))
            return 1
    print(
        "\nSuccessfully saved patched KoboRoot.tgz to %s. Remember to make sure your kobo is running the target firmware version before patching."
        % cfg.out
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
