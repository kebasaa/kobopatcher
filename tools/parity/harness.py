"""Differential test of kobopatcher against Go kobopatch on real patch sets.

    harness.py jobs  --patches DIR --testdata DIR [--rootfs-set VER=ROOTFS:SETDIR ...] -o jobs.jsonl
    harness.py py    jobs.jsonl results.jsonl [--go-compat] [-j N]
    harness.py go    GOREF jobs.jsonl results.jsonl [-j N]
    harness.py compare go.jsonl py.jsonl [--fixes]

`jobs` builds one job per patch (that patch alone enabled), one with every
patch enabled that doesn't conflict (one per PatchGroup), and one with the
file's own Enabled states, for every patch file of every version of
kobopatch-patches (DIR/src/versions/VER/BINARY.yaml/AUTHOR.yaml) whose
binaries are in kobopatch-testdata (DIR/VER.tar.xz). GOREF is the helper in
tools/parity/goref built against the kobopatch to compare with.

Result lines hold the parse/validate/apply error texts or the SHA256 of the
patched file. `compare` normalises Go pointer addresses and reports every
difference.
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
import time

# --- job generation ----------------------------------------------------------------


def gen_jobs(args):
    import kobopatcher.patchfile as pf

    jobs = []
    vroot = os.path.join(args.patches, "src", "versions")
    for ver in sorted(os.listdir(vroot)):
        tar = os.path.join(args.testdata, ver + ".tar.xz")
        if not os.path.isfile(tar):
            print("skip %s: no testdata" % ver, file=sys.stderr)
            continue
        vdir = os.path.join(vroot, ver)
        for bindir in sorted(os.listdir(vdir)):
            if not os.path.isdir(os.path.join(vdir, bindir)):
                continue
            member = bindir[: -len(".yaml")] if bindir.endswith(".yaml") else bindir
            for f in sorted(os.listdir(os.path.join(vdir, bindir))):
                path = os.path.join(vdir, bindir, f)
                jobs.extend(_jobs_for(ver, path, {"tar": tar, "member": member}, pf))
    for spec in args.rootfs_set or []:
        ver, rest = spec.split("=", 1)
        rootfs, setdir = rest.split(":", 1)
        import yaml

        with open(os.path.join(setdir, "kobopatch.yaml")) as fh:
            cfg = yaml.safe_load(fh)
        for pfile, target in cfg["patches"].items():
            jobs.extend(_jobs_for(ver, os.path.join(setdir, pfile), {"bin": os.path.join(rootfs, target), "member": ""}, pf))
    with open(args.o, "w") as out:
        for j in jobs:
            out.write(json.dumps(j) + "\n")
    print("%d jobs" % len(jobs), file=sys.stderr)


def _jobs_for(ver, path, src, pf):
    fmt = pf.format_for_file(path)
    base = dict(src, patch=path, format=fmt)
    jid = "%s|%s" % (ver, os.path.relpath(path))
    out = [dict(base, id=jid + "|asis")]
    try:
        ps = pf.read_from_file(fmt, path)
    except Exception:  # noqa: BLE001 - both implementations must report it
        return out
    names = ps.names()
    for n in names:
        out.append(dict(base, id=jid + "|only|" + n, enable=[n]))
    groups, chosen = set(), []
    for n in names:
        g = set(ps.parsed[n].patch_groups) if hasattr(ps, "parsed") else set()
        if g & groups:
            continue
        groups |= g
        chosen.append(n)
    out.append(dict(base, id=jid + "|all", enable=chosen))
    return out


# --- running ----------------------------------------------------------------------

_cache = {}


def _load(j):
    key = (j.get("bin", ""), j.get("tar", ""), j.get("member", ""))
    if key in _cache:
        return _cache[key]
    _cache.clear()
    if j.get("bin"):
        with open(j["bin"], "rb") as f:
            buf = f.read()
    else:
        import lzma
        import tarfile

        buf = None
        with lzma.open(j["tar"]) as x, tarfile.open(fileobj=x, mode="r|") as tf:
            for ti in tf:
                if ti.name == j["member"] or os.path.basename(ti.name) == j["member"]:
                    buf = tf.extractfile(ti).read()
                    break
        if buf is None:
            raise FileNotFoundError("member %s not found" % j["member"])
    _cache[key] = buf
    return buf


def run_py(j):
    import kobopatcher.patchfile as pf
    from kobopatcher.errors import GoPanic, KobopatcherError
    from kobopatcher.patchlib.patcher import Patcher

    r = {"id": j["id"], "size": 0}
    try:
        try:
            orig = _load(j)
        except Exception as e:  # noqa: BLE001
            r["parse_err"] = "load: %s" % e
            return r
        try:
            ps = pf.read_from_file(j["format"], j["patch"])
        except GoPanic:
            raise
        except KobopatcherError as e:
            r["parse_err"] = str(e)
            return r
        if j["format"] == "kobopatch":
            r["names"] = ps.names()
        if "enable" in j:
            for n in r.get("names", []):
                ps.set_enabled(n, False)
            for n in j["enable"]:
                try:
                    ps.set_enabled(n, True)
                except KobopatcherError as e:
                    r["validate_err"] = "enable: %s" % e
                    return r
        try:
            ps.validate()
        except KobopatcherError as e:
            r["validate_err"] = str(e)
            return r
        pt = Patcher(bytearray(orig))
        pt.set_path("./usr/local/Kobo/" + os.path.basename(j.get("member", "") + j.get("bin", "")))
        try:
            with pf.silenced():
                ps.apply_to(pt)
        except GoPanic:
            raise
        except KobopatcherError as e:
            r["apply_err"] = str(e)
            return r
        out = bytes(pt.get_bytes())
        r["sha256"] = hashlib.sha256(out).hexdigest()
        r["size"] = len(out)
    except GoPanic as e:
        r["panic"] = str(e)
    except Exception as e:  # noqa: BLE001 - a crash of the port is a finding
        r["panic"] = "PYTHON EXCEPTION %s: %s" % (type(e).__name__, e)
    return r


def _init(compat):
    from kobopatcher.compat import set_go_compat

    set_go_compat(compat)


def _groups(jobs):
    gs = {}
    for j in jobs:
        gs.setdefault((j.get("bin", ""), j.get("tar", ""), j.get("member", "")), []).append(j)
    return list(gs.values())


def _run_group(g):
    return [run_py(j) for j in g]


def cmd_py(args):
    jobs = [json.loads(line) for line in open(args.jobs)]
    t = time.time()
    with multiprocessing.Pool(args.j, initializer=_init, initargs=(args.go_compat,)) as pool, open(args.results, "w") as out:
        for rs in pool.imap_unordered(_run_group, _groups(jobs)):
            for r in rs:
                out.write(json.dumps(r) + "\n")
    print("%d jobs in %.0fs" % (len(jobs), time.time() - t), file=sys.stderr)


def cmd_go(args):
    jobs = [json.loads(line) for line in open(args.jobs)]
    groups = _groups(jobs)
    chunks = [[] for _ in range(args.j)]
    for i, g in enumerate(sorted(groups, key=len, reverse=True)):
        min(chunks, key=len).extend(g)
    t = time.time()
    with tempfile.TemporaryDirectory() as td:
        procs = []
        for i, c in enumerate(chunks):
            if not c:
                continue
            jf, rf = os.path.join(td, "j%d" % i), os.path.join(td, "r%d" % i)
            with open(jf, "w") as f:
                for j in c:
                    f.write(json.dumps(j) + "\n")
            procs.append((subprocess.Popen([args.goref, jf, rf], stdout=subprocess.DEVNULL), rf))
        with open(args.results, "w") as out:
            for p, rf in procs:
                if p.wait() != 0:
                    sys.exit("goref failed")
                with open(rf) as f:
                    out.write(f.read())
    print("%d jobs in %.0fs" % (len(jobs), time.time() - t), file=sys.stderr)


# --- comparison -------------------------------------------------------------------

_PTR = re.compile(r"\(\*([\w.]+)\)\(0x[0-9a-f]+\)")


def _norm(r):
    out = {}
    for k in ("names", "parse_err", "validate_err", "apply_err", "panic", "sha256", "size"):
        v = r.get(k)
        if isinstance(v, str):
            v = _PTR.sub(r"(*\1)(PTR)", v)
        if v not in (None, "", 0, []):
            out[k] = v
    return out


def cmd_compare(args):
    a = {r["id"]: _norm(r) for r in map(json.loads, open(args.a))}
    b = {r["id"]: _norm(r) for r in map(json.loads, open(args.b))}
    missing = sorted(set(a) ^ set(b))
    diffs = [i for i in sorted(set(a) & set(b)) if a[i] != b[i]]
    ok = sum(1 for i in a if i in b and a[i] == b[i] and "sha256" in a[i])
    errs = sum(1 for i in a if i in b and a[i] == b[i] and "sha256" not in a[i])
    print("compared %d jobs: %d identical (%d patched outputs, %d identical errors), %d different, %d missing" % (len(set(a) & set(b)), ok + errs, ok, errs, len(diffs), len(missing)))
    for i in diffs[: args.max]:
        print("\n== %s\n  A: %s\n  B: %s" % (i, json.dumps(a[i])[:600], json.dumps(b[i])[:600]))
    for i in missing[:20]:
        print("missing in one side: %s" % i)
    return 1 if diffs or missing else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("jobs")
    p.add_argument("--patches", required=True)
    p.add_argument("--testdata", required=True)
    p.add_argument("--rootfs-set", action="append", help="VER=ROOTFS:SETDIR (a patch set with a kobopatch.yaml, applied to an extracted rootfs)")
    p.add_argument("-o", required=True)
    p = sub.add_parser("py")
    p.add_argument("jobs")
    p.add_argument("results")
    p.add_argument("--go-compat", action="store_true")
    p.add_argument("-j", type=int, default=os.cpu_count())
    p = sub.add_parser("go")
    p.add_argument("goref")
    p.add_argument("jobs")
    p.add_argument("results")
    p.add_argument("-j", type=int, default=os.cpu_count())
    p = sub.add_parser("compare")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--max", type=int, default=50)
    a = ap.parse_args()
    if a.cmd == "jobs":
        gen_jobs(a)
    elif a.cmd == "py":
        cmd_py(a)
    elif a.cmd == "go":
        cmd_go(a)
    else:
        sys.exit(cmd_compare(a))


if __name__ == "__main__":
    main()
