"""Test every patch of kobopatch-patches with kobopatcher -t.

    python tools/test_patches.py --patches KOBOPATCH-PATCHES --testdata KOBOPATCH-TESTDATA [VERSION ...]
    python tools/test_patches.py --own src/versions --testdata KOBOPATCH-TESTDATA [VERSION ...]

For each version (default: all with testdata), generates a config mapping
every patch file of src/versions/VERSION/BINARY.yaml/ to usr/local/Kobo/BINARY
and runs `kobopatcher -t -f TESTDATA/VERSION.tar.xz -` on it, like
kobopatch-patches' scripts/test/test.go.

With --own, runs `kobopatcher -t` on each of kobopatcher's own patch sets
(src/versions/VERSION/kobopatch.yaml) with the matching testdata tarball, or,
when there is none, only checks that every patch file loads (`--list`).
"""

import argparse
import os
import subprocess
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patches")
    ap.add_argument("--own", help="kobopatcher's src/versions directory")
    ap.add_argument("--testdata", required=True)
    ap.add_argument("--kobopatcher", default=None, help="command to run (default: this Python's kobopatcher)")
    ap.add_argument("versions", nargs="*")
    a = ap.parse_args()
    if a.own:
        sys.exit(_own(a))
    if not a.patches:
        ap.error("--patches or --own is required")
    vroot = os.path.join(a.patches, "src", "versions")
    versions = a.versions or sorted(os.listdir(vroot))
    cmd = a.kobopatcher.split() if a.kobopatcher else [sys.executable, "-m", "kobopatcher"]
    failed = []
    with tempfile.TemporaryDirectory() as td:
        for ver in versions:
            tar = os.path.abspath(os.path.join(a.testdata, ver + ".tar.xz"))
            if not os.path.isfile(tar):
                print("skip %s: no testdata" % ver)
                continue
            lines = ["version: %s" % ver, "in: %s" % tar, "out: %s" % os.path.join(td, "out.tgz"), "log: %s" % os.path.join(td, "log.txt"), "patches:"]
            vdir = os.path.join(vroot, ver)
            for b in sorted(os.listdir(vdir)):
                if not os.path.isdir(os.path.join(vdir, b)):
                    continue
                for f in sorted(os.listdir(os.path.join(vdir, b))):
                    target = "usr/local/Kobo/" + os.path.splitext(b)[0]
                    lines.append("  %s: %s" % (os.path.abspath(os.path.join(vdir, b, f)), target))
            print("\n### %s" % ver, flush=True)
            r = subprocess.run(cmd + ["-t", "-f", tar, "-"], input="\n".join(lines) + "\n", text=True)
            if r.returncode != 0:
                failed.append(ver)
    if failed:
        print("\nFailed: %s" % ", ".join(failed))
        sys.exit(1)
    print("\nAll versions passed.")


def _own(a):
    cmd = a.kobopatcher.split() if a.kobopatcher else [sys.executable, "-m", "kobopatcher"]
    failed = []
    for ver in a.versions or sorted(os.listdir(a.own)):
        cfg = os.path.join(a.own, ver, "kobopatch.yaml")
        if not os.path.isfile(cfg):
            continue
        tar = os.path.abspath(os.path.join(a.testdata, ver + ".tar.xz"))
        print("\n### %s (kobopatcher)" % ver, flush=True)
        if os.path.isfile(tar):
            r = subprocess.run(cmd + ["-t", "-f", tar, cfg])
        else:
            print("no testdata for %s: only loading the patch files" % ver)
            r = subprocess.run(cmd + ["--list", cfg])
        if r.returncode != 0:
            failed.append(ver)
    if failed:
        print("\nFailed: %s" % ", ".join(failed))
        return 1
    print("\nAll own patch sets passed.")
    return 0


if __name__ == "__main__":
    main()
