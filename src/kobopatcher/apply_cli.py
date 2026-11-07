"""kobopatcher-apply: apply a single patch file to a single binary."""

import argparse
import sys

from . import __version__, patchfile
from .compat import go_compat
from .errors import GoPanic, KobopatcherError
from .patchlib.patcher import Patcher

HELP = """Usage: kobopatcher-apply [OPTIONS]

Version: %s

Options:
  -i, --input FILE          the file to patch (required)
  -p, --patch-file FILE     the file containing the patches (required)
  -o, --output FILE         the file to write the patched output to (will be
                            overwritten if it exists) (required)
  -f, --patch-format NAME   the patch format (kobopatch or patch32lsb; default:
                            from the patch file's extension, .yaml/.yml or .patch)
  -v, --verbose             show verbose output from the patcher
  -h, --help                show this help text
"""


def _err(msg):
    sys.stderr.write(msg)
    return 1


def main(argv=None) -> int:
    try:
        return _main(argv)
    except GoPanic as e:
        sys.stderr.write("panic: %s\n" % e)
        return 2


def _main(argv):
    ap = argparse.ArgumentParser(prog="kobopatcher-apply", add_help=False)
    ap.add_argument("-i", "--input", default="")
    ap.add_argument("-p", "--patch-file", default="")
    ap.add_argument("-o", "--output", default="")
    ap.add_argument("-f", "--patch-format", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args(argv)
    if a.help or a.rest:
        sys.stderr.write(HELP % __version__)
        return 1 if go_compat() or a.rest else 0
    if not a.input or not a.patch_file or not a.output:
        return _err("Error: input, patch-file, and output flags are required. See --help for more info.\n")
    fmt = a.patch_format or ("kobopatch" if go_compat() else patchfile.format_for_file(a.patch_file))
    if fmt not in patchfile.get_formats():
        return _err("Error: invalid format %s. See --help for more info.\n" % fmt)
    if a.verbose:
        patchfile.set_log(lambda m: (sys.stdout.write(m), sys.stdout.flush()))
    try:
        ps = patchfile.read_from_file(fmt, a.patch_file)
    except KobopatcherError as e:
        return _err("Error: could not read patch file: %s\n" % e)
    try:
        ps.validate()
    except KobopatcherError as e:
        return _err("Error: could not validate patch file: %s\n" % e)
    try:
        with open(a.input, "rb") as f:
            buf = f.read()
    except OSError as e:
        return _err("Error: could not read input file: %s\n" % patchfile.os_err("open", a.input, e))
    pt = Patcher(buf)
    pt.set_path(a.input)
    try:
        ps.apply_to(pt)
    except GoPanic:
        raise
    except KobopatcherError as e:
        return _err("Error: could not apply patch file: %s\n" % e)
    try:
        with open(a.output, "wb") as f:
            f.write(pt.get_bytes())
    except OSError as e:
        return _err("Error: could not create output file: %s\n" % patchfile.os_err("open", a.output, e))
    print("Successfully patched '%s' using '%s' to '%s'" % (a.input, a.patch_file, a.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
