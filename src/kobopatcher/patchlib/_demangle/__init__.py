# Port of github.com/ianlancetaylor/demangle at version
# v0.0.0-20250628045327-2d64ad6b7ec5 (BSD-3-Clause, see LICENSE in this
# directory).  This module corresponds to ToString in demangle.go.
"""Pure-Python port of the Go demangle package, as used by kobopatch.

kobopatch calls ``demangle.ToString(name)`` with no options and treats
any error as "not demangleable".  :func:`to_string` returns exactly the
string the Go function returns and raises :class:`DemangleError` where
the Go function returns an error (or panics).

Go strings are byte strings and the demangler indexes them byte by
byte.  To get identical results for non-ASCII input, the name is
encoded as UTF-8 and processed as one character per byte (Latin-1);
the result is decoded back from UTF-8 (with ``surrogateescape`` for
invalid sequences, so the bytes round-trip).

The Go parser and printer are recursive.  A first attempt runs under
the normal recursion limit; if that is exceeded the name is retried in
a helper thread with a large stack and a raised recursion limit.  If
even that is not enough, :class:`DemangleError` is raised.
"""

from __future__ import annotations

import sys
import threading

from .ast import ast_to_string
from .errors import DemangleError, InternalError, NotMangledNameError
from .parser import to_ast
from .rust import old_rust_to_string, rust_to_string

__all__ = ["to_string", "DemangleError", "NotMangledNameError"]

# Limits for the deep-recursion retry.  Since Python 3.11 nested Python
# calls do not use the C stack, so the limit only bounds memory (about
# 200 bytes per frame; a nesting level takes about three frames).  On
# 3.10 every Python frame also uses C stack, so stay well within the
# thread's stack size.
if sys.version_info >= (3, 11):
    _DEEP_RECURSION_LIMIT = 1_000_000
else:  # pragma: no cover
    # Before 3.12 every Python call also uses C stack (more on Windows), so
    # the limit has to fit into _DEEP_STACK_SIZE. Real symbols need far less.
    _DEEP_RECURSION_LIMIT = 40_000 if sys.platform == "win32" else 120_000
_DEEP_STACK_SIZE = 512 * 1024 * 1024


def _to_string_latin1(name: str) -> str:
    """ToString on a one-char-per-byte string.

    May raise DemangleError, InternalError (a Go panic), RecursionError
    or (for inputs on which Go hits a runtime panic) another exception.
    """
    if name.startswith("_R"):
        return rust_to_string(name)

    # Check for an old-style Rust mangled name.
    # It starts with _ZN and ends with "17h" followed by 16 hex digits
    # followed by "E" followed by an optional suffix starting with "."
    # (which we ignore).
    if name.startswith("_ZN"):
        rname = name
        pos = rname.rfind("E.")
        if pos > 0:
            rname = rname[: pos + 1]
        if rname.endswith("E") and len(rname) > 23 and rname[len(rname) - 20 : len(rname) - 17] == "17h":
            s, ok = old_rust_to_string(rname)
            if ok:
                return s

    a = to_ast(name)
    return ast_to_string(a)


def _checked(name: str) -> str:
    """Run the demangler, mapping Go panics to DemangleError."""
    try:
        return _to_string_latin1(name)
    except (DemangleError, RecursionError):
        raise
    except InternalError as e:
        raise DemangleError("internal error: %s" % e) from None
    except MemoryError:
        raise DemangleError("out of memory") from None
    except Exception as e:  # Go runtime panics (nil dereference, index, ...)
        raise DemangleError("internal error: %s: %s" % (type(e).__name__, e)) from None


_deep_lock = threading.Lock()


def _checked_deep(name: str) -> str:
    """Retry in a thread with a big stack and a high recursion limit."""
    result: list = []

    def target() -> None:
        try:
            result.append((True, _checked(name)))
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            result.append((False, e))

    with _deep_lock:
        old_limit = sys.getrecursionlimit()
        try:
            old_size = threading.stack_size()
        except (ValueError, RuntimeError):  # pragma: no cover
            old_size = None
        try:
            try:
                threading.stack_size(_DEEP_STACK_SIZE)
            except (ValueError, RuntimeError):  # pragma: no cover
                pass
            sys.setrecursionlimit(max(old_limit, _DEEP_RECURSION_LIMIT))
            t = threading.Thread(target=target, name="demangle-deep")
            t.start()
            t.join()
        finally:
            sys.setrecursionlimit(old_limit)
            if old_size is not None:
                try:
                    threading.stack_size(old_size)
                except (ValueError, RuntimeError):  # pragma: no cover
                    pass

    ok, val = result[0]
    if ok:
        return val
    if isinstance(val, RecursionError):
        raise DemangleError("recursion too deep") from None
    raise val


def to_string(name: str) -> str:
    """Demangle a C++ or Rust symbol name like Go's demangle.ToString.

    Returns the human-readable name.  Raises DemangleError if the name
    cannot be demangled (NotMangledNameError if it does not look like a
    mangled name at all).
    """
    try:
        raw = name.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = name.encode("utf-8", "surrogatepass")
    s = raw.decode("latin-1")
    try:
        r = _checked(s)
    except RecursionError:
        r = _checked_deep(s)
    return r.encode("latin-1").decode("utf-8", "surrogateescape")
