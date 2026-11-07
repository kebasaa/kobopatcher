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
            for dest in dests:
                self.l("  ADD  %-35s  TO  %s" % (src, dest))
                if dest.startswith("/"):
                    raise RunError("could not add file: destination must not start with a slash")
                try:
                    with open(src, "rb") as f:
                        buf = f.read()
                except OSError as e:
                    raise wrap(patchfile.os_err("open", src, e), "could not read additional file '%s'", src) from None
                self.out.add_file("./" + dest, buf)
                self.out.expected_size += len(buf)

    def apply_symlinks(self):
        self.d("\n\nKoboPatch::ApplySymlinks")
        if not self.cfg.symlinks:
            return
        self.l("\nAdding additional symlinks")
        declared = {d for dests in self.cfg.files.values() for d in dests}
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
    print(banner)
    d(banner)

    conf = args.config[0] if args.config else "kobopatch.yaml"
    fw = os.path.abspath(args.firmware) if args.firmware else ""

    print("Loading configuration from %s" % conf)
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
