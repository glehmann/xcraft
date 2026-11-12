"""Repository metadata made of the entries of other repositories' metadata, without the packages they list.

createrepo_c writes an entry for each package in each of primary, filelists and other, all keyed by the SHA-256 of
the package, its pkgid: the entries of a package are the same in any repository, but for its location.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import importlib
import lzma
import re
import shutil
import time
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr, unescape

if TYPE_CHECKING:
    from pathlib import Path

KINDS = {
    "primary": 'metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm"',
    "filelists": 'filelists xmlns="http://linux.duke.edu/metadata/filelists"',
    "other": 'otherdata xmlns="http://linux.duke.edu/metadata/other"',
}
"""The metadata of the packages, with their root elements."""
_REPO = "{http://linux.duke.edu/metadata/repo}"
_PACKAGE = re.compile(r"<package\b.*?</package>", re.DOTALL)  # their text is escaped: no </package> in it
_PKGID = re.compile(r'<checksum type="sha256" pkgid="YES">([0-9a-f]{64})</checksum>|\bpkgid="([0-9a-f]{64})"')
_LOCATION = re.compile(r"<location\b[^>]*/>")
_HREF = re.compile(r'<location\b[^>]*\bhref="([^"]*)"')
_SOURCE = re.compile(r"<rpm:sourcerpm>([^<]*)</rpm:sourcerpm>")
_NAME = re.compile(r"<name>([^<]*)</name>")
_ARCH = re.compile(r"<arch>([^<]*)</arch>")
_VERSION = re.compile(r'<version epoch="([^"]*)" ver="([^"]*)" rel="([^"]*)"/>')
_METADATA = re.compile(r"repodata/(?!\.)[\w.+-]+")

type Entry = tuple[str, str, str]
"""The primary, filelists and other entries of a package."""


def _decompress(path: Path) -> bytes | None:
    """Return the content of `path`, or None when Python cannot decompress it: zstd before Python 3.14."""
    data = path.read_bytes()
    match path.suffix:
        case ".gz":
            return gzip.decompress(data)
        case ".xz":
            return lzma.decompress(data)
        case ".bz2":
            return bz2.decompress(data)
        case ".zst":
            try:
                return importlib.import_module("compression.zstd").decompress(data)
            except ImportError:
                return None
    return data


def location(entry: Entry) -> str:
    """Return the location of a package in its repository."""
    return unescape(found[1], {"&quot;": '"'}) if (found := _HREF.search(entry[0])) else ""


def nevra(entry: Entry) -> tuple[str, str, str]:
    """Return the name, the epoch-version-release, without a zero epoch, like the lock, and the architecture of a
    package, from its primary entry: the first of each, its own, before those of its dependencies.
    """
    name, arch, version = _NAME.search(entry[0]), _ARCH.search(entry[0]), _VERSION.search(entry[0])
    if not (name and arch and version):
        return "", "", ""
    epoch, ver, rel = version.groups()
    return unescape(name[1]), f"{epoch}:{ver}-{rel}" if epoch not in ("", "0") else f"{ver}-{rel}", arch[1]


def checksums(repomd: bytes) -> dict[str, str]:
    """Return the SHA-256 of the metadata files of a repomd.xml, by location: none for the other checksums."""
    return {
        location.get("href", ""): checksum.text or ""
        for d in ET.fromstring(repomd).iter(f"{_REPO}data")  # noqa: S314
        if (location := d.find(f"{_REPO}location")) is not None
        and (checksum := d.find(f"{_REPO}checksum")) is not None
        and checksum.get("type") == "sha256"
    }


def files(repomd: bytes) -> dict[str, str]:
    """Return the locations of the metadata of the packages in a repomd.xml, by kind, and raise ValueError for those
    out of its repodata/ directory: it may come unsigned, from the upstream copy.
    """
    found = {
        d.get("type", ""): location.get("href", "")
        for d in ET.fromstring(repomd).iter(f"{_REPO}data")  # noqa: S314
        if d.get("type") in KINDS and (location := d.find(f"{_REPO}location")) is not None
    }
    if bad := [href for href in found.values() if not _METADATA.fullmatch(href)]:
        msg = f"invalid metadata location in repomd.xml: {bad[0]!r}"
        raise ValueError(msg)
    return found


def read(repo: Path) -> dict[str, Entry] | None:
    """Return the entries of the packages of the metadata of `repo`, by pkgid, or None when it cannot be read."""
    found: dict[str, list[str]] = {}
    for i, href in enumerate(files((repo / "repodata/repomd.xml").read_bytes())[kind] for kind in KINDS):
        if (data := _decompress(repo / href)) is None:
            return None
        for package in _PACKAGE.findall(data.decode()):
            if pkgid := _PKGID.search(package):
                found.setdefault(pkgid[1] or pkgid[2], ["", "", ""])[i] = package
    return {pkgid: (p, f, o) for pkgid, (p, f, o) in found.items() if p and f and o}


def sourcerpm(entry: Entry) -> str:
    """Return the source package of a package."""
    return found[1] if (found := _SOURCE.search(entry[0])) else ""


def write(repo: Path, entries: dict[str, Entry]) -> None:
    """Write the metadata of `repo` listing the packages of `entries`, by location in it."""
    repodata = repo / "repodata"
    shutil.rmtree(repodata, ignore_errors=True)
    repodata.mkdir(parents=True)
    now = int(time.time())
    records = []
    for i, (kind, root) in enumerate(KINDS.items()):
        packages = (
            _LOCATION.sub(f"<location href={quoteattr(href)}/>", entry[i]) if i == 0 else entry[i]
            for href, entry in sorted(entries.items())
        )
        xml = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} packages="{len(entries)}">\n'
            + "".join(f"{p}\n" for p in packages)
            + f"</{root.split()[0]}>\n"
        ).encode()
        data = gzip.compress(xml, mtime=0)
        digest = hashlib.sha256(data).hexdigest()
        (repodata / f"{digest}-{kind}.xml.gz").write_bytes(data)
        records.append(
            f'  <data type="{kind}">\n'
            f'    <checksum type="sha256">{digest}</checksum>\n'
            f'    <open-checksum type="sha256">{hashlib.sha256(xml).hexdigest()}</open-checksum>\n'
            f'    <location href="repodata/{digest}-{kind}.xml.gz"/>\n'
            f"    <timestamp>{now}</timestamp>\n"
            f"    <size>{len(data)}</size>\n"
            f"    <open-size>{len(xml)}</open-size>\n"
            "  </data>\n"
        )
    (repodata / "repomd.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<repomd xmlns="http://linux.duke.edu/metadata/repo" xmlns:rpm="http://linux.duke.edu/metadata/rpm">\n'
        f"  <revision>{now}</revision>\n" + "".join(records) + "</repomd>\n"
    )
