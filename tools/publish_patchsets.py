#!/usr/bin/env python3
"""Pack the patch sets in src/versions for download.

Writes kobopatcher-<version>.zip for every set and index.json into the output
folder. The patchsets workflow uploads them to the "patchsets" release, where
kobopatcher downloads them from.

    python tools/publish_patchsets.py --out dist/patchsets [--commit SHA]
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

from kobopatcher import patchsets  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--versions", default=os.path.join(HERE, "src", "versions"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--commit", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    sets = []
    for d in patchsets.set_dirs(a.versions):
        version = str(patchsets.read_source(d)["version"])
        if version != os.path.basename(d):
            sys.exit("%s: source.yaml says version %s" % (d, version))
        data = patchsets.build_zip(d, version)
        entry = patchsets.set_entry(d, data, a.commit)
        with open(os.path.join(a.out, entry["file"]), "wb") as f:
            f.write(data)
        sets.append(entry)
        print("%s  %s (%d bytes)" % (entry["sha256"], entry["file"], entry["size"]))
    if not sets:
        sys.exit("no patch sets found in %s" % a.versions)
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        f.write(patchsets.dumps(patchsets.build_index(sets, a.commit)))
    print("wrote %s" % os.path.join(a.out, "index.json"))


if __name__ == "__main__":
    main()
