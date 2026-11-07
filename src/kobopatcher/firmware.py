"""Reading the firmware and writing KoboRoot.tgz."""

import gzip
import io
import lzma
import tarfile
import time
import zipfile

from .errors import KobopatcherError


class FirmwareError(KobopatcherError):
    pass


class Entry:
    """A tar entry of the firmware's KoboRoot.tgz."""

    __slots__ = ("info", "_tf")

    def __init__(self, info, tf):
        self.info, self._tf = info, tf

    @property
    def name(self) -> str:
        return self.info.name

    def is_regular(self) -> bool:
        return self.info.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE)

    def read(self) -> bytes:
        f = self._tf.extractfile(self.info)
        return f.read() if f is not None else b""


def open_input(path: str, say):
    """Iterate over the entries of the firmware.

    path is either a firmware update zip (containing KoboRoot.tgz) or a
    kobopatch-testdata .tar.xz (a flat tar of the binaries).
    """
    if path.endswith(".tar.xz"):
        say("Reading input firmware testdata tarball")
        try:
            f = open(path, "rb")
        except OSError as e:
            raise FirmwareError("could not open firmware tarball: %s" % _oserr("open", path, e)) from None
        stream = lzma.LZMAFile(f)
        closers = [stream, f]
    else:
        say("Reading input firmware zip")
        try:
            zf = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as e:
            raise FirmwareError("could not open firmware zip: %s" % (_oserr("open", path, e) if isinstance(e, OSError) else "zip: not a valid zip file")) from None
        kr = None
        for zi in zf.infolist():
            if zi.filename == "KoboRoot.tgz":
                try:
                    kr = zf.open(zi)
                except Exception as e:  # noqa: BLE001
                    raise FirmwareError("could not open KoboRoot.tgz in firmware zip: %s" % e) from None
                break
        if kr is None:
            zf.close()
            raise FirmwareError("could not find KoboRoot.tgz")
        stream = gzip.GzipFile(fileobj=kr)
        closers = [stream, kr, zf]
    try:
        tf = tarfile.open(fileobj=stream, mode="r|")
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError) as e:
        for c in closers:
            c.close()
        raise FirmwareError("could not decompress KoboRoot.tgz: %s" % e) from None

    def gen():
        try:
            while True:
                try:
                    info = tf.next()
                except (tarfile.TarError, OSError, EOFError, lzma.LZMAError) as e:
                    raise FirmwareError("could not read input firmware: %s" % e) from None
                if info is None:
                    return
                yield Entry(info, tf)
        finally:
            tf.close()
            for c in closers:
                c.close()

    return gen()


def _oserr(op, name, e):
    msg = e.strerror.lower() if getattr(e, "strerror", None) else str(e)
    return "%s %s: %s" % (op, name, msg)


class OutputTgz:
    """Builds KoboRoot.tgz in memory, like kobopatch.

    Patched files keep the original header (mode, owner, names, format) with a
    new size and the current time; added files are mode 0777, owned by root.
    """

    def __init__(self):
        self._raw = io.BytesIO()
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb", compresslevel=6, mtime=0)
        self.expected_size = 0
        self.members = []

    def _write(self, ti: tarfile.TarInfo, data: bytes, fmt):
        self._gz.write(ti.tobuf(fmt, "utf-8", "surrogateescape"))
        if data:
            self._gz.write(data)
            pad = (-len(data)) % tarfile.BLOCKSIZE
            if pad:
                self._gz.write(b"\x00" * pad)
        self.members.append(ti.name)

    def add_patched(self, orig: tarfile.TarInfo, data: bytes):
        ti = tarfile.TarInfo(orig.name)
        ti.type = tarfile.REGTYPE if orig.type in (tarfile.AREGTYPE, tarfile.CONTTYPE) else orig.type
        ti.mode, ti.uid, ti.gid = orig.mode, orig.uid, orig.gid
        ti.uname, ti.gname = orig.uname, orig.gname
        ti.pax_headers = dict(orig.pax_headers)
        ti.mtime = int(time.time())
        ti.size = len(data)
        fmt = tarfile.PAX_FORMAT if orig.pax_headers else tarfile.GNU_FORMAT
        self._write(ti, data, fmt)

    def add_file(self, name: str, data: bytes, mode: int = 0o777):
        ti = tarfile.TarInfo(name)
        ti.type = tarfile.REGTYPE
        ti.mode, ti.uid, ti.gid = mode, 0, 0
        ti.uname = ti.gname = ""
        ti.mtime = int(time.time())
        ti.size = len(data)
        self._write(ti, data, _fmt_for(name))

    def add_symlink(self, name: str, target: str):
        ti = tarfile.TarInfo(name)
        ti.type = tarfile.SYMTYPE
        ti.linkname = target
        ti.mode, ti.uid, ti.gid = 0o777, 0, 0
        ti.uname = ti.gname = ""
        ti.mtime = int(time.time())
        self._write(ti, b"", _fmt_for(name, target))

    def finish(self) -> bytes:
        # end of archive: two zero blocks, no padding to a full record (like Go)
        self._gz.write(b"\x00" * (2 * tarfile.BLOCKSIZE))
        self._gz.close()
        return self._raw.getvalue()


def _fmt_for(name: str, link: str = "") -> int:
    # Go picks USTAR when the names fit, else PAX
    if len(name.encode()) <= 100 and len(link.encode()) <= 100 and name.isascii() and link.isascii():
        return tarfile.USTAR_FORMAT
    return tarfile.PAX_FORMAT


def check_output(path: str, expected: int) -> None:
    """Re-read the written KoboRoot.tgz and compare the total size of its files."""
    total = 0
    with tarfile.open(path, "r:gz") as tf:
        for ti in tf:
            total += ti.size
    if total != expected:
        raise FirmwareError("size mismatch: expected %d, got %d (please report this)" % (expected, total))
