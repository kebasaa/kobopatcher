"""Compiling Qt translations (.ts) to .qm files with lrelease."""

import os
import shutil
import subprocess
import tempfile

from .errors import KobopatcherError

QM_PREFIX = "usr/local/Kobo/translations/"

# where distributions put lrelease when it is not in PATH
_EXTRA_DIRS = ["/usr/lib/qt6/bin", "/usr/lib/qt5/bin", "/usr/lib64/qt6/bin", "/usr/lib64/qt5/bin", "/opt/homebrew/opt/qt/bin", "/usr/local/opt/qt/bin"]


class TranslationError(KobopatcherError):
    pass


def find_lrelease(configured: str) -> str:
    """The lrelease executable: the configured one, else from PATH."""
    if configured:
        p = shutil.which(configured)
        if p is None:
            raise TranslationError('could not find lrelease (part of QT Linguist): exec: "%s": executable file not found in $PATH' % configured)
        return p
    for name in ("lrelease", "lrelease-qt6", "lrelease-qt5", "lrelease.exe"):
        p = shutil.which(name)
        if p:
            return p
    for d in _EXTRA_DIRS:
        p = os.path.join(d, "lrelease")
        if os.access(p, os.X_OK):
            return p
    raise TranslationError('could not find lrelease (part of QT Linguist): exec: "lrelease.exe": executable file not found in $PATH')


def compile_ts(lrelease: str, ts: str, log) -> bytes:
    """Run `lrelease TS -qm OUT` and return the .qm data."""
    td = tempfile.mkdtemp(prefix="lrelease-qm")
    try:
        out = os.path.join(td, "out.qm")
        r = subprocess.run([lrelease, ts, "-qm", out], capture_output=True)
        log("          | lrelease stdout: %s" % r.stdout.decode("utf-8", "replace").replace("\n", "\n          | "))
        log("          | lrelease stderr: %s" % r.stderr.decode("utf-8", "replace").replace("\n", "\n          | "))
        if r.returncode != 0:
            raise TranslationError("error running lrelease: exit status %d" % r.returncode, r.stderr.decode("utf-8", "replace"))
        try:
            with open(out, "rb") as f:
                return f.read()
        except OSError as e:
            raise TranslationError("could not read generated qm file: open %s: %s" % (out, (e.strerror or str(e)).lower())) from None
    finally:
        shutil.rmtree(td, ignore_errors=True)
