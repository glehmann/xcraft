"""rpmlint on the specs and the binary packages of the fixture packages, in the lint image."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests import copy_project
from tests.test_buildid import commit_base, git
from xcraft.__main__ import app

runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_environment: Path) -> Path:
    """Return the project, on a branch of its main one, for its builds to be development builds."""
    root = tmp_path / "project"
    copy_project(root, build_environment)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    return root


def xcraft(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    error = result.exception if result.exception and not isinstance(result.exception, SystemExit) else ""
    return result.exit_code, f"{result.output}{error}"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_spec_errors(root: Path) -> None:
    spec = root / "base/trivial/trivial.spec"
    edit(spec, "%build\ncd %{name}-%{version}", "%build\ncd %{name}-%{version} && ls $RPM_SOURCE_DIR")
    code, output = xcraft("lint", "trivial")
    assert code == 1, output
    assert "trivial.spec:16: E: use-of-RPM_SOURCE_DIR" in output
    assert "not built, only their specs linted: trivial" in output
    # shown before the development builds, which go on
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert output.index("E: use-of-RPM_SOURCE_DIR") < output.index("✓ trivial built")
    # but fail the final builds
    code, output = xcraft("build", "--final", "trivial")
    assert code == 1, output
    assert "✗ trivial: rpmlint found errors in its spec" in output
    # filtered by the project, out of the package directory
    (root / "lint/trivial.rpmlintrc").write_text('addFilter("use-of-RPM_SOURCE_DIR")\n')
    code, output = xcraft("lint", "trivial")
    assert code == 0, output
    assert "use-of-RPM_SOURCE_DIR" not in output
    # with the filters of its upstream rpmlintrc, and those of the project's configuration
    (root / "lint/trivial.rpmlintrc").unlink()
    (root / "base/trivial/trivial.rpmlintrc").write_text('addFilter("use-of-RPM_SOURCE_DIR")\n')
    config = root / "lint/rpmlint.toml"
    config.write_text(config.read_text().replace("Filters = [", 'Filters = [\n    "no-%check-section",'))
    code, output = xcraft("lint", "trivial")
    assert code == 0, output
    assert "use-of-RPM_SOURCE_DIR" not in output
    assert "no-%check-section" not in output


def test_binary_errors(root: Path) -> None:
    spec = root / "base/trivial/trivial.spec"
    local = "mkdir -p %{buildroot}/usr/local/share\necho x > %{buildroot}/usr/local/share/x\n"
    edit(spec, "%install\n", f"%install\n{local}")
    edit(spec, "%files\n", "%files\n/usr/local/share/x\n")
    # not in the spec: the development builds, without --lint, never see it
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert "dir-or-file-in-usr-local" not in output
    code, output = xcraft("lint", "trivial")
    assert code == 1, output
    assert "trivial.noarch: E: dir-or-file-in-usr-local /usr/local/share/x" in output
    # with --lint, shown when built, without failing them
    edit(spec, "%files\n", "# built again\n%files\n")
    code, output = xcraft("build", "--lint", "trivial")
    assert code == 0, output
    assert "E: dir-or-file-in-usr-local" in output
    # the final builds fail, not cached
    code, output = xcraft("build", "--final", "trivial")
    assert code == 1, output
    assert "E: dir-or-file-in-usr-local" in output
    assert "✗ trivial: rpmlint found errors in its packages" in output
    code, output = xcraft("lint", "--final", "trivial")
    assert "not built, only their specs linted: trivial" in output
