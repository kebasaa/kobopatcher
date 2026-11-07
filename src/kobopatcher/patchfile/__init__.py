"""Patch file formats: reading a patch set from a file.

Two formats exist: "kobopatch" (YAML, *.yaml) and "patch32lsb" (the older
text format, *.patch).
"""

import sys

from ..errors import KobopatcherError


class PatchFileError(KobopatcherError):
    pass


def _nolog(msg: str) -> None:
    pass


# Debug log (the log file). Receives already formatted text.
log = _nolog

_silent = False


def set_log(fn) -> None:
    global log
    log = fn or _nolog


def out(msg: str) -> None:
    """Progress output on stdout (kobopatch's fmt.Printf in the formats)."""
    if not _silent:
        sys.stdout.write(msg)
        sys.stdout.flush()


class silenced:
    """Suppress the formats' stdout output (used by the test mode)."""

    def __enter__(self):
        global _silent
        self._old = _silent
        _silent = True

    def __exit__(self, *exc):
        global _silent
        _silent = self._old


FORMATS = ("kobopatch", "patch32lsb")


def get_formats():
    """The names of the patch formats."""
    return list(FORMATS)


def get_format(name):
    """The parse function of a format, or None."""
    if name == "kobopatch":
        from . import kobopatch

        return kobopatch.parse
    if name == "patch32lsb":
        from . import patch32lsb

        return patch32lsb.parse
    return None


def read_from_file(fmt: str, filename: str):
    """Read a patch set from a file (it is not validated)."""
    f = get_format(fmt)
    if f is None:
        raise PatchFileError("no format called '%s'" % fmt)
    try:
        with open(filename, "rb") as fh:
            buf = fh.read()
    except OSError as e:
        raise PatchFileError("could not open patch file: %s" % os_err("open", filename, e)) from None
    try:
        return f(buf)
    except KobopatcherError as e:
        raise PatchFileError("could not parse patch file: %s" % e) from None


def os_err(op: str, name: str, e: OSError) -> str:
    """Go's *PathError text, e.g. "open NAME: no such file or directory"."""
    msg = e.strerror.lower() if e.strerror else str(e)
    return "%s %s: %s" % (op, name, msg)


def format_for_file(filename: str) -> str:
    """The format of a patch file, from its extension (.yaml/.yml or .patch).

    kobopatch derived it with a string replacement on the extension, which
    rejected .yml; kobopatcher accepts both spellings.
    """
    from ..compat import go_compat

    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[1] if "." in base else ""
    if go_compat():
        return ext.replace("patch", "patch32lsb").replace("yaml", "kobopatch")
    return {"yaml": "kobopatch", "yml": "kobopatch", "patch": "patch32lsb"}.get(ext.lower(), ext)
