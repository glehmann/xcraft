from __future__ import annotations

import re
from datetime import date

import pytest

from xcraft.release import add_changelog_entry, bump_release


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
    with pytest.raises(ValueError, match="autochangelog"):
        add_changelog_entry("%changelog\n%autochangelog\n", **entry)
