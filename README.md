# kobopatcher

A command-line patcher for Kobo eReader firmware, written in Python. It applies
patches to the binaries inside a Kobo firmware update and builds a
`KoboRoot.tgz` that the device installs.

kobopatcher is an independent reimplementation of
[kobopatch](https://github.com/pgaskin/kobopatch) by Patrick Gaskin and reads
the same configuration and patch formats.

The Python port is in progress on the `python-port` branch.
