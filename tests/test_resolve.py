"""The helpers of the resolver, which run in the build environment otherwise."""

from __future__ import annotations

import gzip
import hashlib
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import pytest

from xcraft.buildenv.resolve import canonical, entry, evrs_of, follows, primary, versioned_names, write_repo

if TYPE_CHECKING:
    from pathlib import Path

RPM, COMMON = "{http://linux.duke.edu/metadata/rpm}", "{http://linux.duke.edu/metadata/common}"


def test_canonical() -> None:
    assert canonical("glibc-2.39-145.el10_2.src.rpm", []) == ("glibc", "2.39-145.el10_2")
    # the rebuilds of an architecture have the versions of the others
    assert canonical("lcov-2.0-5.el10_1.alma_altarch.src.rpm", [".alma_altarch"]) == ("lcov", "2.0-5.el10_1")
    assert canonical("python3.12-3.12.9-1.el10.src.rpm", []) == ("python3.12", "3.12.9-1.el10")


@pytest.mark.parametrize(
    ("dep", "attributes"),
    [
        ("foo", {"name": "foo"}),
        ("foo >= 1:2.0-3", {"name": "foo", "flags": "GE", "epoch": "1", "ver": "2.0", "rel": "3"}),
        ("libfoo.so.1()(64bit)", {"name": "libfoo.so.1()(64bit)"}),
        ("bar = 4.17", {"name": "bar", "flags": "EQ", "epoch": "0", "ver": "4.17"}),
        ("(a if b)", {"name": "(a if b)"}),
        ("q&a < 1", {"name": "q&a", "flags": "LT", "epoch": "0", "ver": "1"}),
    ],
)
def test_entry(dep: str, attributes: dict[str, str]) -> None:
    element = ET.fromstring(entry(dep).replace("rpm:", ""))  # noqa: S314, ours
    assert element.attrib == attributes


def test_repository_of_ours(tmp_path: Path) -> None:
    binary = {
        "name": "xen-libs", "evr": "4.17.6-1.xcpng8.99", "arch": "x86_64", "package": "xen",
        "provides": ["xen-libs = 4.17.6-1.xcpng8.99"], "requires": ["glibc", "lzo"], "recommends": [],
    }  # fmt: skip
    metadata = ET.fromstring(primary([binary]))  # noqa: S314, ours
    package = metadata[0]
    assert (metadata.get("packages"), package.findtext(f"{COMMON}name")) == ("1", "xen-libs")
    version = package.find(f"{COMMON}version")
    assert version is not None
    assert version.attrib == {"epoch": "0", "ver": "4.17.6", "rel": "1.xcpng8.99"}
    requires = package.findall(f"{COMMON}format/{RPM}requires/{RPM}entry")
    assert [e.get("name") for e in requires] == ["glibc", "lzo"]
    assert not package.findall(f"{COMMON}format/{RPM}recommends")
    # the checksums of the repository metadata
    write_repo(tmp_path, [binary])
    compressed = (tmp_path / "repodata/primary.xml.gz").read_bytes()
    assert gzip.decompress(compressed) == primary([binary])
    assert hashlib.sha256(compressed).hexdigest() in (tmp_path / "repodata/repomd.xml").read_text()


@pytest.mark.parametrize(
    ("dep", "names"),
    [
        ("trivial >= 1.0-2", ["trivial"]),
        ("trivial", []),  # without a version
        ("/usr/bin/toyrun", []),
        ("pkgconfig(foo) = 1.0", ["pkgconfig(foo)"]),
        # rich: all the words, the capabilities with their own parentheses
        ("(pkgconfig(example) >= 1.0-2 or nonexistent)", ["pkgconfig(example)", ">=", "1.0-2", "or", "nonexistent"]),
        ("((a or b) with c(x))", ["a", "or", "b", "with", "c(x)"]),
    ],
)
def test_versioned_names(dep: str, names: list[str]) -> None:
    assert versioned_names(dep) == names


@pytest.mark.parametrize(
    ("dep", "followed"),
    [
        ("pair-libs = 1.0-2.xcpng8.99", True),  # like Requires: pair-libs = %{version}-%{release}
        ("pkgconfig(pair) >= 1.0-2.xcpng8.99", True),  # by a capability, with the version of the package having it
        ("pair-libs >= 1.0-1.xcpng8.99", False),  # another version: its key changes with another release
        ("pair-libs", False),
        ("upstream = 1.0-2.xcpng8.99", False),
        ("(pair-libs = 1.0-2.xcpng8.99 or upstream)", False),  # rich
    ],
)
def test_follows(dep: str, followed: bool) -> None:  # noqa: FBT001
    ours = [{"name": "pair-libs", "evr": "1.0-2.xcpng8.99", "provides": ["pkgconfig(pair) = 1.0"]}]
    assert follows(dep, evrs_of(ours)) == followed
