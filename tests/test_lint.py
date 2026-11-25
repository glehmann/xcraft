"""rpmlint on the specs and the binary packages of the fixture packages, in the lint image."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import IO

import pytest

from tests import commit_base, git, xcraft


@pytest.fixture
def root(root: Path) -> Path:
    """Return the project, on a branch of its main one, for its builds to be development builds."""
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    return root


def replace(path: Path, old: str, new: str) -> None:
    """Replace the first `old` of the file `path`, which has it, by `new`."""
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def _spec_error(root: Path) -> Path:
    """Return the spec of trivial, given an error of rpmlint, use-of-RPM_SOURCE_DIR."""
    spec = root / "base/trivial/trivial.spec"
    replace(spec, "%build\ncd %{name}-%{version}", "%build\ncd %{name}-%{version} && ls $RPM_SOURCE_DIR")
    return spec


def should_report_the_errors_of_the_specs(root: Path) -> None:
    spec = _spec_error(root)
    code, output = xcraft("lint", "trivial")
    assert code == 1, output
    assert f"{spec}:16: E: use-of-RPM_SOURCE_DIR" in output
    assert "not built, only their specs linted: trivial" in output
    # alone, for the editors: its findings only
    code, output = xcraft("lint", "--spec", str(spec))
    assert code == 1, output
    assert f"{spec}:16: E: use-of-RPM_SOURCE_DIR" in output
    assert all(line.startswith(str(spec)) for line in output.splitlines()), output
    code, output = xcraft("lint", "--spec", str(root / "xcraft.toml"))
    assert code == 2, output
    assert "not in the directory of a package" in output


def should_report_the_build_directories_named_after_the_version(root: Path) -> None:
    # which the compiler cache does not find again with the next version, at the %setup setting it
    spec = root / "base/trivial/trivial.spec"
    replace(spec, "rm -rf %{name}-%{version}\nmkdir %{name}-%{version}", "%setup -q -c -T")
    code, output = xcraft("lint", "--spec", str(spec))
    assert code == 1, output
    assert f"{spec}:12: E: versioned-build-directory trivial-1.0" in output
    replace(spec, "%setup -q -c -T", "%setup -q -c -T -n %{name}")
    code, output = xcraft("lint", "--spec", str(spec))
    assert code == 0, output
    assert "versioned-build-directory" not in output


def should_fail_final_builds_when_the_spec_has_errors(root: Path) -> None:
    _spec_error(root)
    # shown before the development builds, which go on
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert output.index("E: use-of-RPM_SOURCE_DIR") < output.index("✓ trivial built")
    # but fail the final builds
    code, output = xcraft("build", "--final", "trivial")
    assert code == 1, output
    assert "✗ trivial: rpmlint found errors in its spec" in output


def should_filter_the_errors_of_the_specs_as_configured(root: Path) -> None:
    _spec_error(root)
    # by the project, out of the package directory
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


def should_fail_final_builds_when_the_packages_have_errors(root: Path) -> None:
    spec = root / "base/trivial/trivial.spec"
    local = "mkdir -p %{buildroot}/usr/local/share\necho x > %{buildroot}/usr/local/share/x\n"
    replace(spec, "%install\n", f"%install\n{local}")
    replace(spec, "%files\n", "%files\n/usr/local/share/x\n")
    # not in the spec: the development builds, without --lint, never see it
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert "dir-or-file-in-usr-local" not in output
    code, output = xcraft("lint", "trivial")
    assert code == 1, output
    assert "trivial.noarch: E: dir-or-file-in-usr-local /usr/local/share/x" in output
    # the changelog, 1.0-1, compared without the release suffix, +b1.xcpng8.99
    assert "incoherent-version-in-changelog" not in output
    # with --lint, shown when built, without failing them, a release without its changelog entry too
    replace(spec, "Release: 1%", "Release: 2%")
    code, output = xcraft("build", "--lint", "trivial")
    assert code == 0, output
    assert "E: dir-or-file-in-usr-local" in output
    assert "W: incoherent-version-in-changelog 1.0-1" in output
    # the final builds fail, not cached
    code, output = xcraft("build", "--final", "trivial")
    assert code == 1, output
    assert "E: dir-or-file-in-usr-local" in output
    assert "✗ trivial: rpmlint found errors in its packages" in output
    code, output = xcraft("lint", "--final", "trivial")
    assert code == 0, output
    assert "not built, only their specs linted: trivial" in output


def _message(out: IO[bytes]) -> dict:
    """Return the next message of a language server on `out`."""
    headers = dict(line.decode().strip().split(": ", 1) for line in iter(out.readline, b"\r\n"))
    return json.loads(out.read(int(headers["Content-Length"])))


def should_serve_the_findings_of_the_specs_to_the_editors(root: Path) -> None:
    spec = _spec_error(root)
    cmd = [sys.executable, "-m", "xcraft", "lsp"]
    server = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdin, stdout = server.stdin, server.stdout
    assert stdin
    assert stdout

    def send(method: str, params: dict | None, id_: int = 0) -> dict:
        """Send a notification, or a request of `id_`, and return its response."""
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params} | ({"id": id_} if id_ else {}))
        stdin.write(f"Content-Length: {len(body)}\r\n\r\n{body}".encode())
        stdin.flush()
        while id_ and (message := _message(stdout)).get("id") != id_:
            pass
        return message if id_ else {}

    send("initialize", {"capabilities": {}}, 1)
    send("initialized", {})
    document = {"uri": spec.as_uri(), "languageId": "spec", "version": 1, "text": spec.read_text()}
    send("textDocument/didOpen", {"textDocument": document})
    # the findings of xcraft lint --spec, as the spec is typed
    while (message := _message(stdout)).get("method") != "textDocument/publishDiagnostics":
        pass
    [diagnostic] = [d for d in message["params"]["diagnostics"] if d["severity"] == 1]  # the errors
    assert (diagnostic["code"], diagnostic["range"]["start"]["line"]) == ("use-of-RPM_SOURCE_DIR", 15), diagnostic
    # filtered in lint/<package>.rpmlintrc
    params = {"textDocument": {"uri": spec.as_uri()}, "range": diagnostic["range"]}
    actions = send("textDocument/codeAction", params | {"context": {"diagnostics": [diagnostic]}}, 2)["result"]
    fix = next(a for a in actions if a["kind"] == "quickfix")
    assert fix["title"] == "Suppress use-of-RPM_SOURCE_DIR in trivial.rpmlintrc", fix
    send("workspace/executeCommand", fix["command"], 3)
    assert 'addFilter("use-of-RPM_SOURCE_DIR")' in (root / "lint/trivial.rpmlintrc").read_text()
    # linted again, without it
    while (message := _message(stdout)).get("method") != "textDocument/publishDiagnostics":
        pass
    assert all(d["severity"] != 1 for d in message["params"]["diagnostics"]), message
    send("shutdown", None, 4)
    send("exit", None)
    assert server.wait(timeout=30) == 0
