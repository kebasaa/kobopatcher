"""Switch between kobopatcher's fixed behaviour and kobopatch's original quirks.

kobopatcher fixes a number of bugs in kobopatch (listed in the README under
"Differences from kobopatch"). To prove that everything else is identical, the
parity harness and the tests can turn the original behaviour back on. This is
not a user-facing option.
"""

import contextlib
import os

_go_compat = os.environ.get("KOBOPATCHER_GO_COMPAT", "") == "1"


def go_compat() -> bool:
    """Whether kobopatch's original (buggy) behaviour is in effect."""
    return _go_compat


def set_go_compat(on: bool) -> None:
    global _go_compat
    _go_compat = bool(on)


@contextlib.contextmanager
def go_compat_mode(on: bool = True):
    """Temporarily enable (or disable) kobopatch's original behaviour."""
    global _go_compat
    old = _go_compat
    _go_compat = bool(on)
    try:
        yield
    finally:
        _go_compat = old
