"""Compare the kobopatcher and kobopatch commands end to end.

    cli_diff.py sets    --go KOBOPATCH --patches DIR --testdata DIR [--go-compat] [-j N] [VERSION ...]
    cli_diff.py tests   --go KOBOPATCH --patches DIR --testdata DIR [--go-compat] [-j N] [VERSION ...]
    cli_diff.py errors  --go KOBOPATCH --testdata DIR [--go-compat]
    cli_diff.py tools   --go-symdump BIN --go-cssextract BIN --testdata DIR [--firmware ZIP ...] [--go-compat] [-j N]

sets:   for every patch file of every version, runs both commands with that
        file alone and all of its non-conflicting patches enabled, on the
        kobopatch-testdata tarball, and compares the exit status, the output
        lines and every member of KoboRoot.tgz (except the mtime).
tests:  runs `-t` for every version with all patch files and compares the
        per-patch results.
errors: runs a catalogue of broken configurations and patch files through both
        and compares exit status and messages.
tools:  compares symdump and cssextract output on every ELF patch target in
        the testdata tarballs and firmware zips.

The commands' banners differ by design and are ignored.
"""

import argparse
import hashlib
import io
import json
import lzma
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

PY = [sys.executable, "-m", "kobopatcher"]


def _env(compat):
    e = dict(os.environ)
    if compat:
        e["KOBOPATCHER_GO_COMPAT"] = "1"
    else:
        e.pop("KOBOPATCHER_GO_COMPAT", None)
    return e


_BANNER = re.compile(r"^kobopatch(er)? \S+\nhttps://github.com/\S+\n\n", re.M)


def _norm_out(s, td):
    s = _BANNER.sub("", s, count=1)
    s = s.replace(td, "TMP")
    s = re.sub(r"\(\*([\w.]+)\)\(0x[0-9a-f]+\)", r"(*\1)(PTR)", s)
    return s


def _tgz(path):
    if not os.path.exists(path):
        return None
    out = {}
    with tarfile.open(path, "r:gz") as tf:
        for ti in tf:
            d = tf.extractfile(ti).read() if ti.isreg() else b""
            out[ti.name] = [ti.type.decode("latin-1"), ti.mode, ti.uid, ti.gid, ti.uname, ti.gname, ti.linkname, ti.size, hashlib.sha256(d).hexdigest()]
    return out


def _run(cmd, cwd, env, stdin=None, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


# --- sets -----------------------------------------------------------------------


def _patch_files(patches, testdata, versions):
    vroot = os.path.join(patches, "src", "versions")
    for ver in versions or sorted(os.listdir(vroot)):
        tar = os.path.join(testdata, ver + ".tar.xz")
        if not os.path.isfile(tar):
            continue
        vdir = os.path.join(vroot, ver)
        for b in sorted(os.listdir(vdir)):
            if not os.path.isdir(os.path.join(vdir, b)):
                continue
            for f in sorted(os.listdir(os.path.join(vdir, b))):
                yield ver, tar, os.path.join(vdir, b, f), "usr/local/Kobo/" + os.path.splitext(b)[0]


def _enabled_set(path):
    import kobopatcher.patchfile as pf

    try:
        ps = pf.read_from_file(pf.format_for_file(path), path)
    except Exception:  # noqa: BLE001
        return {}
    groups, en = set(), {}
    for n in ps.names():
        g = set(ps.parsed[n].patch_groups)
        en[n] = not (g & groups)
        if en[n]:
            groups |= g
    return en


def _one_set(job):
    ver, tar, path, target, go, compat = job
    td = tempfile.mkdtemp(prefix="clidiff")
    try:
        res = {}
        en = _enabled_set(path)
        for side, cmd in (("go", [go]), ("py", PY)):
            d = os.path.join(td, side)
            os.makedirs(d)
            cfg = {
                "version": ver,
                "in": tar,
                "out": os.path.join(d, "KoboRoot.tgz"),
                "log": os.path.join(d, "log.txt"),
                "patchFormat": "kobopatch",
                "patches": {path: target},
                "overrides": {path: en} if en else {},
            }
            with open(os.path.join(d, "kobopatch.yaml"), "w", encoding="utf-8") as f:
                json.dump(cfg, f)  # JSON is YAML
            code, out, err = _run(cmd + ["kobopatch.yaml"], d, _env(compat))
            res[side] = {"code": code, "out": _norm_out(out, d), "err": _norm_out(err, d), "tgz": _tgz(cfg["out"])}
        return ver, path, res
    finally:
        shutil.rmtree(td, ignore_errors=True)


def cmd_sets(a):
    jobs = [(v, t, p, tg, a.go, a.go_compat) for v, t, p, tg in _patch_files(a.patches, a.testdata, a.versions)]
    same = diff = 0
    report = []
    with multiprocessing.Pool(a.j) as pool:
        for ver, path, res in pool.imap_unordered(_one_set, jobs):
            g, p = res["go"], res["py"]
            if g == p:
                same += 1
            else:
                diff += 1
                report.append((ver, path, g, p))
    print("sets: %d patch files compared, %d identical, %d different" % (same + diff, same, diff))
    for ver, path, g, p in sorted(report)[: a.max]:
        print("\n== %s %s" % (ver, os.path.relpath(path, a.patches)))
        for k in ("code", "err", "out"):
            if g[k] != p[k]:
                print("  %s:\n    go: %r\n    py: %r" % (k, g[k][-1500:] if isinstance(g[k], str) else g[k], p[k][-1500:] if isinstance(p[k], str) else p[k]))
        if g["tgz"] != p["tgz"]:
            gt, pt = g["tgz"] or {}, p["tgz"] or {}
            for n in sorted(set(gt) | set(pt)):
                if gt.get(n) != pt.get(n):
                    print("  tgz %s:\n    go: %s\n    py: %s" % (n, gt.get(n), pt.get(n)))
    return 1 if diff else 0


# --- tests (-t) -------------------------------------------------------------------


def _one_test(job):
    ver, tar, files, go, compat = job
    td = tempfile.mkdtemp(prefix="clidiff")
    try:
        cfg = "version: %s\nin: %s\nout: %s\nlog: %s\npatchFormat: kobopatch\npatches:\n" % (ver, tar, os.path.join(td, "o.tgz"), os.path.join(td, "l.txt"))
        cfg += "".join("  %s: %s\n" % (p, t) for p, t in files)
        res = {}
        for side, cmd in (("go", [go]), ("py", PY)):
            code, out, err = _run(cmd + ["-"], td, _env(compat), stdin=cfg)
            out = _norm_out(out, td)
            # per-patch lines (" ✔  name" / " ✕  name"), and the error list,
            # independent of the order of the patch files
            marks = sorted(ln.split("\r")[-1] for ln in out.splitlines() if "\r" in ln or ln.startswith(" -  "))
            errs = sorted(ln for ln in out.split("\nErrors:\n", 1)[1].splitlines() if ln.startswith("  ")) if "\nErrors:\n" in out else []
            res[side] = {"code": code, "marks": marks, "errors": errs, "stderr": _norm_out(err, td)}
        return ver, res
    finally:
        shutil.rmtree(td, ignore_errors=True)


def cmd_tests(a):
    byver = {}
    for v, t, p, tg in _patch_files(a.patches, a.testdata, a.versions):
        byver.setdefault((v, t), []).append((p, tg))
    jobs = [(v, t, fs, a.go, a.go_compat) for (v, t), fs in byver.items()]
    same, report = 0, []
    with multiprocessing.Pool(a.j) as pool:
        for ver, res in pool.imap_unordered(_one_test, jobs):
            if res["go"] == res["py"]:
                same += 1
            else:
                report.append((ver, res))
    npatch = 0
    print("tests: %d versions compared, %d identical, %d different" % (len(jobs), same, len(report)))
    for ver, res in sorted(report)[: a.max]:
        g, p = res["go"], res["py"]
        print("\n== %s" % ver)
        for k in ("code", "stderr"):
            if g[k] != p[k]:
                print("  %s: go=%r py=%r" % (k, g[k], p[k]))
        for k in ("marks", "errors"):
            gs, ps = set(g[k]), set(p[k])
            for x in sorted(gs - ps)[:20]:
                print("  %s only go: %s" % (k, x[:400]))
            for x in sorted(ps - gs)[:20]:
                print("  %s only py: %s" % (k, x[:400]))
    return 1 if report else 0


# --- errors -----------------------------------------------------------------------

CATALOGUE = [
    # (name, kobopatch.yaml text with {tar}, {{patch file name: content}}, extra args)
    ("missing config", None, {}, ["nope.yaml"]),
    ("empty config", "", {}, []),
    ("config not a mapping", "- a\n- b\n", {}, []),
    ("config unknown key", "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\nbogus: 1\n", {}, []),
    ("config duplicate key", "version: 1\nversion: 2\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\n", {}, []),
    ("config missing out", "version: 1\nin: {tar}\nlog: l.txt\npatchFormat: kobopatch\n", {}, []),
    ("config bad patchFormat", "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: nope\n", {}, []),
    ("config patches not a map", "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\npatches: [a]\n", {}, []),
    ("config overrides bad bool", "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\noverrides:\n  a.yaml:\n    x: maybe\n", {}, []),
    ("missing firmware", "version: 1\nin: nope.zip\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\n", {}, []),
    ("not a zip", "version: 1\nin: kobopatch.yaml\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\n", {}, []),
    ("missing target", "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\npatches:\n  a.yaml: usr/local/Kobo/nope\n", {"a.yaml": "P:\n  - Enabled: yes\n  - FindZlib: x\n"}, []),
    ("missing patch file", "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\npatches:\n  nope.yaml: usr/local/Kobo/libnickel.so.1.0.0\n", {}, []),
]

_PATCH_ERRORS = {
    "tabs": "P:\n\t- Enabled: yes\n",
    "syntax": "P: [\n",
    "not a mapping": "- a\n",
    "patch not a list": "P: 1\n",
    "instruction not a map": "P:\n  - 1\n",
    "empty instruction": "P:\n  -\n",
    "unknown instruction": "P:\n  - Enabled: yes\n  - Bogus: 1\n",
    "two instructions": "P:\n  - {Enabled: yes, FindZlib: x}\n",
    "unknown field": "P:\n  - Enabled: yes\n  - ReplaceString: {Find: a, Replace: b, Extra: c}\n",
    "duplicate field": "P:\n  - Enabled: yes\n  - ReplaceString: {Find: a, Find: b, Replace: c}\n",
    "duplicate patch": "P:\n  - Enabled: yes\n  - FindZlib: x\nP:\n  - Enabled: yes\n  - FindZlib: y\n",
    "bad int": "P:\n  - Enabled: yes\n  - ReplaceInt: {Offset: 1, Find: 256, Replace: 1}\n",
    "bad bool": "P:\n  - Enabled: maybe\n  - FindZlib: x\n",
    "bytes as string": "P:\n  - Enabled: yes\n  - ReplaceBytes: {Find: abc, Replace: def}\n",
    "no modifying instruction": "P:\n  - Enabled: yes\n",
    "duplicate description": "P:\n  - Description: a\n  - Description: b\n  - FindZlib: x\n",
    "group conflict": "A:\n  - Enabled: yes\n  - PatchGroup: g\n  - FindZlib: x\nB:\n  - Enabled: yes\n  - PatchGroup: g\n  - FindZlib: x\n",
    "must match length": "P:\n  - Enabled: yes\n  - FindReplaceString: {Find: abc, Replace: ab, MustMatchLength: yes}\n",
    "bad hash": "P:\n  - Enabled: yes\n  - FindZlibHash: abc\n",
    "find not found": "P:\n  - Enabled: yes\n  - FindBaseAddressString: \"this text is not in libnickel 12345\"\n",
    "bytes mismatch": "P:\n  - Enabled: yes\n  - ReplaceBytes: {Offset: 0, FindH: 00 00 00 00, ReplaceH: 01 01 01 01}\n",
    "bad hex": "P:\n  - Enabled: yes\n  - ReplaceBytes: {Offset: 0, FindH: 7G, ReplaceH: 00}\n",
    "odd hex": "P:\n  - Enabled: yes\n  - ReplaceBytes: {Offset: 0, FindH: 7F4, ReplaceH: 000}\n",
    "base past end": "P:\n  - Enabled: yes\n  - BaseAddress: 0x7FFFFFFF\n",
    "negative base": "P:\n  - Enabled: yes\n  - BaseAddress: -5\n",
    "unknown symbol": "P:\n  - Enabled: yes\n  - BaseAddress: {Sym: _ZN3Foo3barEv}\n",
    "no plt": "P:\n  - Enabled: yes\n  - BaseAddress: {SymPLT: _ZN3Foo3barEv}\n",
    "two offset methods": "P:\n  - Enabled: yes\n  - BaseAddress: {Offset: 1, Sym: x}\n",
    "replace string too long": "P:\n  - Enabled: yes\n  - FindReplaceString: {Find: \"Library\", Replace: \"Librarys\"}\n",
    "lazy binding twice": "P:\n  - Enabled: yes\n  - ElfLazyBinding: true\nQ:\n  - Enabled: yes\n  - ElfLazyBinding: true\n",
    "stub unknown": "P:\n  - Enabled: yes\n  - StubFunction: {Sym: _ZN3Foo3barEv, Return: void}\n",
    "tombstone wrong path": "P:\n  - Enabled: yes\n  - Tombstone: usr/local/Kobo/libboggle.so\n",
    "override unknown patch": None,
}


def _error_cases(tar):
    for name, cfg, files, args in CATALOGUE:
        yield name, cfg, files, args
    for name, content in _PATCH_ERRORS.items():
        cfg = "version: 1\nin: {tar}\nout: o.tgz\nlog: l.txt\npatchFormat: kobopatch\npatches:\n  a.yaml: usr/local/Kobo/libnickel.so.1.0.0\n"
        if content is None:
            cfg += "overrides:\n  a.yaml:\n    Nope: yes\n"
            content = "P:\n  - Enabled: no\n  - FindZlib: x\n"
        yield "patch: " + name, cfg, {"a.yaml": content}, []


def cmd_errors(a):
    tar = os.path.join(a.testdata, a.version + ".tar.xz")
    same, bad = 0, []
    for name, cfg, files, args in _error_cases(tar):
        res = {}
        for side, cmd in (("go", [a.go]), ("py", PY)):
            with tempfile.TemporaryDirectory(prefix="clidiff") as td:
                if cfg is not None:
                    with open(os.path.join(td, "kobopatch.yaml"), "w", encoding="utf-8") as f:
                        f.write(cfg.replace("{tar}", tar))
                for fn, c in files.items():
                    with open(os.path.join(td, fn), "w", encoding="utf-8") as f:
                        f.write(c)
                code, out, err = _run(cmd + (args or ["kobopatch.yaml"]), td, _env(a.go_compat), timeout=600)
                res[side] = (code, _norm_out(err, td).strip(), _norm_out(out, td).strip().splitlines()[-1:] if out.strip() else [])
        if res["go"] == res["py"]:
            same += 1
        else:
            bad.append((name, res))
    print("errors: %d cases, %d identical, %d different" % (same + len(bad), same, len(bad)))
    for name, res in bad:
        print("\n== %s\n  go: %r\n  py: %r" % (name, res["go"], res["py"]))
    return 1 if bad else 0


# --- tools ------------------------------------------------------------------------

_TARGETS = ("nickel", "libnickel.so.1.0.0", "libadobe.so", "librmsdk.so.1.0.0")


def _tool_inputs(testdata, firmware):
    for f in sorted(os.listdir(testdata)):
        if f.endswith(".tar.xz"):
            yield "tar", os.path.join(testdata, f)
    for z in firmware or []:
        yield "zip", z


def _members(kind, path):
    if kind == "tar":
        with lzma.open(path) as x, tarfile.open(fileobj=x, mode="r|") as tf:
            for ti in tf:
                if os.path.basename(ti.name) in _TARGETS and ti.isreg():
                    yield os.path.basename(ti.name), tf.extractfile(ti).read()
    else:
        with zipfile.ZipFile(path) as zf, zf.open("KoboRoot.tgz") as k, tarfile.open(fileobj=k, mode="r|gz") as tf:
            for ti in tf:
                if os.path.basename(ti.name) in _TARGETS and ti.isreg():
                    yield os.path.basename(ti.name), tf.extractfile(ti).read()


def _one_tool(job):
    kind, path, gosym, gocss, compat = job
    results = []
    for name, data in _members(kind, path):
        td = tempfile.mkdtemp(prefix="clidiff")
        try:
            binp = os.path.join(td, name)
            with open(binp, "wb") as f:
                f.write(data)
            for tool, gobin, mod, outname in (("symdump", gosym, "kobopatcher.symdump", "symdump.out.json"), ("cssextract", gocss, "kobopatcher.cssextract", "cssextract.out.css")):
                r = {}
                for side, cmd in (("go", [gobin]), ("py", [sys.executable, "-m", mod])):
                    d = os.path.join(td, side + tool)
                    os.makedirs(d)
                    code, _, err = _run(cmd + [binp], d, _env(compat), timeout=1800)
                    op = os.path.join(d, outname)
                    h = hashlib.sha256(open(op, "rb").read()).hexdigest() if os.path.exists(op) else None
                    r[side] = (code == 0, h)
                results.append((os.path.basename(path), name, tool, r))
        finally:
            shutil.rmtree(td, ignore_errors=True)
    return results


def cmd_tools(a):
    jobs = [(k, p, a.go_symdump, a.go_cssextract, a.go_compat) for k, p in _tool_inputs(a.testdata, a.firmware)]
    same, bad = 0, []
    with multiprocessing.Pool(a.j) as pool:
        for rs in pool.imap_unordered(_one_tool, jobs):
            for src, name, tool, r in rs:
                if r["go"] == r["py"]:
                    same += 1
                else:
                    bad.append((src, name, tool, r))
    print("tools: %d outputs compared, %d identical, %d different" % (same + len(bad), same, len(bad)))
    for b in sorted(bad):
        print("  %s %s %s go=%s py=%s" % (b[0], b[1], b[2], b[3]["go"], b[3]["py"]))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("sets", "tests", "errors", "tools"):
        p = sub.add_parser(n)
        p.add_argument("--go-compat", action="store_true")
        p.add_argument("-j", type=int, default=os.cpu_count())
        p.add_argument("--max", type=int, default=60)
        p.add_argument("--testdata", required=True)
        if n in ("sets", "tests", "errors"):
            p.add_argument("--go", required=True, help="the Go kobopatch binary")
        if n in ("sets", "tests"):
            p.add_argument("--patches", required=True)
            p.add_argument("versions", nargs="*")
        if n == "errors":
            p.add_argument("--version", default="4.34.20097")
        if n == "tools":
            p.add_argument("--go-symdump", required=True)
            p.add_argument("--go-cssextract", required=True)
            p.add_argument("--firmware", action="append")
    a = ap.parse_args()
    sys.exit({"sets": cmd_sets, "tests": cmd_tests, "errors": cmd_errors, "tools": cmd_tools}[a.cmd](a))


if __name__ == "__main__":
    main()
