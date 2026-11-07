# Port of github.com/ianlancetaylor/demangle at version
# v0.0.0-20250628045327-2d64ad6b7ec5 (BSD-3-Clause, see LICENSE in this
# directory).  This module holds the error types shared by the C++ and
# Rust demanglers.
"""Error types for the demangler."""

from __future__ import annotations

__all__ = ["DemangleError", "NotMangledNameError", "InternalError"]


class DemangleError(Exception):
    """A name could not be demangled.

    This corresponds to every error the Go ``demangle.ToString`` returns
    (the ``demangleErr`` values with an offset, ``ErrNotMangledName`` and
    the plain ``errors.New`` values).  Inputs on which the Go code panics
    (it does so for a handful of malformed names) also raise
    ``DemangleError`` here, since kobopatch treats any failure as "not
    demangleable".
    """

    def __init__(self, err: str, off: int | None = None) -> None:
        self.err = err
        self.off = off
        if off is None:
            super().__init__(err)
        else:
            super().__init__(f"{err} at {off}")


class NotMangledNameError(DemangleError):
    """Go's ``ErrNotMangledName``: the input is not a mangled name."""

    def __init__(self) -> None:
        super().__init__("not a C++ or Rust mangled name")


class InternalError(Exception):
    """Equivalent of a Go ``panic`` that is not a ``demangleErr``.

    In Go these are not recovered by ``ToString`` (the caller would
    crash).  They must not be caught by the parser's own error recovery
    (``demangleCastTemplateArgs``), so this class deliberately does not
    derive from ``DemangleError``; the public entry point converts it.
    """
