import os
import pathlib

import pytest

from kobopatcher.compat import go_compat_mode

TESTDATA = pathlib.Path(__file__).parent / "testdata"


def default_rootfs():
    """The extracted rootfs of firmware 4.38.23429 (the ELF oracle tests).

    Set KOBOPATCHER_TEST_ROOTFS to point elsewhere.
    """
    env = os.environ.get("KOBOPATCHER_TEST_ROOTFS")
    cands = [env] if env else []
    cands.append(os.path.expanduser("~/Documents/Github/kobo_aura_h2o/archive/fw-4.38.23429/x/rootfs"))
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "usr/local/Kobo/libnickel.so.1.0.0")):
            return c
    return None


@pytest.fixture
def rootfs():
    r = default_rootfs()
    if r is None:
        pytest.skip("firmware 4.38.23429 rootfs not found (set KOBOPATCHER_TEST_ROOTFS)")
    return r


@pytest.fixture
def fwfile(rootfs):
    def read(name):
        p = os.path.join(rootfs, name)
        if not os.path.isfile(p):
            pytest.skip("%s not in the rootfs" % name)
        with open(p, "rb") as f:
            return f.read()

    return read


@pytest.fixture
def testdata():
    def read(name):
        return (TESTDATA / name).read_bytes()

    return read


@pytest.fixture(params=[False, True], ids=["fixed", "go_compat"])
def both_modes(request):
    """Run a test in kobopatcher's default mode and in go_compat mode."""
    with go_compat_mode(request.param):
        yield request.param


@pytest.fixture
def compat():
    with go_compat_mode(True):
        yield
