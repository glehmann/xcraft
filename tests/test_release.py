from __future__ import annotations

import re
from datetime import date

import pytest

from xcraft.commands.bump import same_version
from xcraft.release import add_changelog_entry, bump_release, reset_release, set_version
from xcraft.sources import archive_name


@pytest.mark.parametrize(
    ("release", "bumped"),
    [
        ("1%{?xsrel}.3%{?dist}", "1%{?xsrel}.4%{?dist}"),
        ("5%{?dist}", "6%{?dist}"),
        ("0.ydi.25%{?dist}", "0.ydi.26%{?dist}"),
        ("9", "10"),
    ],
)
def test_bump_release(release: str, bumped: str) -> None:
    spec = f"Name: foo\nRelease:  {release}\n%package devel\nRelease: 1\n"
    assert bump_release(spec) == f"Name: foo\nRelease:  {bumped}\n%package devel\nRelease: 1\n"


def test_bump_release_without_number() -> None:
    with pytest.raises(ValueError, match=re.escape("no number to bump in Release: %{_release}%{dist}")):
        bump_release("Release: %{_release}%{dist}\n")


def test_add_changelog_entry() -> None:
    spec = "Name: foo\n%changelog\n* Mon Jan 05 2026 Someone <s@example.com> - 1.0-1\n- Initial\n"
    entry = {"evr": "1.0-2", "author": "Me <me@example.com>", "message": "Fix it", "day": date(2026, 10, 19)}
    assert add_changelog_entry(spec, **entry) == (
        "Name: foo\n%changelog\n* Mon Oct 19 2026 Me <me@example.com> - 1.0-2\n- Fix it\n\n"
        "* Mon Jan 05 2026 Someone <s@example.com> - 1.0-1\n- Initial\n"
    )
    assert add_changelog_entry("Name: foo\n", **entry).endswith(
        "\n\n%changelog\n* Mon Oct 19 2026 Me <me@example.com> - 1.0-2\n- Fix it\n"
    )
    empty = add_changelog_entry(spec, evr="1.0-2", author="Me <me@example.com>", message="", day=date(2026, 10, 19))
    assert "- 1.0-2\n-\n\n* Mon Jan 05" in empty
    with pytest.raises(ValueError, match="autochangelog"):
        add_changelog_entry("%changelog\n%autochangelog\n", **entry)


@pytest.mark.parametrize(
    ("release", "reset"),
    [("1%{?xsrel}.3%{?dist}", "1%{?dist}"), ("1.0.ydi.2%{?dist}", "1%{?dist}"), ("5", "1"), ("%{rel}", "1%{rel}")],
)
def test_reset_release(release: str, reset: str) -> None:
    assert reset_release(f"Name: foo\nRelease: {release}\n") == f"Name: foo\nRelease: {reset}\n"


def test_set_version() -> None:
    assert set_version("Name: foo\nVersion:  1.0\n", "2.1") == "Name: foo\nVersion:  2.1\n"
    spec = "%global usver 6.12.0\nVersion: %{usver}\nSource0: linux-%{usver}.tar.xz\n"
    assert set_version(spec, "6.12.1") == "%global usver 6.12.1\nVersion: %{usver}\nSource0: linux-%{usver}.tar.xz\n"
    with pytest.raises(ValueError, match="made of macros"):
        set_version("Version: %{major}.%{minor}\n", "2.0")
    with pytest.raises(ValueError, match="not one definition"):
        set_version("Version: %{usver}\n", "2.0")


def test_archive_name() -> None:
    assert archive_name("https://ftp.gnu.org/gnu/grub/grub-2.12.tar.xz") == "grub-2.12.tar.xz"
    assert archive_name("https://github.com/x/y/archive/v1.0.tar.gz#/y-1.0.tar.gz") == "y-1.0.tar.gz"


def test_same_version() -> None:
    groups = [["kernel", "kernel-headers"]]
    assert same_version(groups, "kernel-headers") == ["kernel", "kernel-headers"]
    assert same_version(groups, "xen") == ["xen"]
