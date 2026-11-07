"""Exception types shared by kobopatcher."""


class KobopatcherError(Exception):
    """An error reported to the user (kobopatch's `error` values)."""


class GoPanic(KobopatcherError):
    """A situation where kobopatch would have crashed with a Go panic.

    Raised only in go_compat mode (see kobopatcher.compat); the CLI exits with
    status 2 like a Go panic. In the default mode the same situation is a
    normal error.
    """
