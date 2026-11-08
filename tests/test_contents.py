from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, TOML, copy_project
from tests.test_build import runner, xcraft
from tests.test_buildid import commit_base
from xcraft.__main__ import app

if TYPE_CHECKING:
    from pathlib import Path

ISO = 'installer = ["bash"]\npackages = ["bc"]\n'


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_environment: Path) -> Path:
    root = tmp_path / "project"
    copy_project(root, build_environment)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


def _requires(root: Path, name: str, requires: str) -> None:
    spec = root / f"base/{name}/{name}.spec"
    spec.write_text(spec.read_text().replace("%description", f"Requires: {requires}\n%description", 1))


@pytest.mark.skipif(ARCH != "x86_64_v2", reason="the ISOs are for x86_64")
def test_contents(root: Path) -> None:
    (root / "xcraft.toml").write_text(f"{TOML}{ISO}")
    code, output = xcraft("contents", "--set", "base")
    assert code == 0, output
    base = output.splitlines()
    # ours, with their final versions, and what the hosts need
    assert {"trivial-1.0-1+b1.xcpng8.99.noarch", "with-deps-1.0-1+b1.xcpng8.99.noarch"} <= set(base)
    assert any(line.startswith(("bash-", "bc-")) for line in base)
    assert not any("-debuginfo-" in line for line in base)
    sources = xcraft("contents", "--set", "base/Source")[1].splitlines()
    assert {"trivial-1.0-1+b1.xcpng8.99.src", "with-deps-1.0-1+b1.xcpng8.99.src"} <= set(sources)
    # the repository of the ISO and install.img, in base
    iso, image = (set(xcraft("contents", "--set", s)[1].splitlines()) for s in ("iso", "install.img"))
    assert any(line.startswith("bc-") for line in iso)
    assert any(line.startswith("bash-") for line in image)
    assert iso | image <= set(base)
    # the build repository: what the builds need, besides base
    build = xcraft("contents", "--set", "build")[1].splitlines()
    assert any(line.startswith("rpm-build-") for line in build)
    assert not set(build) & set(base)
    # all of them, by source package, with how many each set has
    code, output = xcraft("contents")
    assert code == 0, output
    assert "trivial (source)" in output
    assert f"install.img: {len(image)} packages" in output
    # why a package is in each set
    code, output = xcraft("contents", "--why", "bash")
    assert code == 0, output
    assert "install.img: bash, in [iso] installer\n" in output
    code, output = xcraft("contents", "--why", "no-such-package")
    assert code == 2
    assert "no-such-package is in no set" in output
    assert xcraft("contents", "--set", "nothing")[0] == 2


def test_contents_diff(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commit_base(root)
    code, output = xcraft("contents", "--diff", "master")
    assert code == 0, output
    assert output == "no change of the packages\n"
    # with-deps requiring an upstream package, which the hosts get with it
    _requires(root, "with-deps", "bc")
    # as Markdown, alone on stdout, for a comment: the progress of the resolution of the change on stderr, like CI's
    monkeypatch.setenv("CI", "1")
    result = runner.invoke(app, ["contents", "--diff", "master", "--markdown"])
    assert result.exit_code == 0, result.output
    assert re.match(r"\*\*base: \d+ added, 1 updated\*\*\n", result.stdout)
    assert "- added `bc` " in result.stdout
    assert "resolving" in result.stderr
    code, output = xcraft("contents", "--diff", "master")
    assert code == 0, output
    lines = output.splitlines()
    # with what bc needs, on an empty system, which the builds had, from the build repository
    assert re.fullmatch(r"base: \d+ added, 1 updated", lines[0])
    assert re.fullmatch(r"build: \d+ removed", lines[lines.index("") + 1])
    assert "  ~ with-deps 1.0-1+b1.xcpng8.99 → 1.0-1+b2.xcpng8.99" in lines
    assert any(line.startswith("  + bc ") and line.endswith(", required by with-deps (bc)") for line in lines)
    # one revision only
    assert xcraft("contents", "--diff", "master", "--diff-stage", "main@incoming")[0] == 2
    code, output = xcraft("contents", "--diff-stage", "incoming")
    assert code == 2
    assert "<remote>@<stage>[/<snapshot>]" in output
