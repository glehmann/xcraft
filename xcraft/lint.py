"""rpmlint, with Fedora's rules, on the specs of our packages and on their binary packages, in the lint image.

The configuration of the project is in lint/, out of the package directories, which stay as imported: ``rpmlint.toml``
for all the packages, but ReleaseExtension, xcraft's, and ``<package>.rpmlintrc`` for the filters of one, after those of
its upstream rpmlintrc files, in its directory, if any. rpmlint runs once per package, for the filters to be its own
only, all in one container, natively: what it finds in the binary packages does not depend on their architecture. The
filters unused, like those of the binary packages when linting a spec, are not reported.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from xcraft import container, process
from xcraft.container import side_image
from xcraft.display import activity, console, messages, tagged
from xcraft.packages import is_debug
from xcraft.project import write_atomically
from xcraft.specs import main_spec

if TYPE_CHECKING:
    from rich.console import Console

    from xcraft.project import Project

LINT_DIR = "lint"
"""The directory of the configuration of rpmlint, in the project."""

_FINDING = re.compile(r"([^\s:]+)((?::\d+)?: ([EW]): .*)")
"""A line of what rpmlint found: an error or a warning, after the package, or the name of the spec and its line."""


def spec(project: Project, name: str) -> Path:
    """Return the spec of the package `name`."""
    return main_spec(project.package_dirs[name])


def binaries(rpms: Path) -> list[Path]:
    """Return the binary packages in the directory `rpms`, of a build, but the debug ones, long to lint for little."""
    return sorted(p for p in rpms.rglob("*.rpm") if not is_debug(p.name.rsplit("-", 2)[0]))


_CONFIG = r"""ReleaseExtension = '\+b\d.*$'
Checks = ["BuildDirectoryCheck"]
"""
"""The configuration of all the projects: the latest changelog entry compared with the version-release of the packages
without what %{autorev}, the release suffix of their builds, starts: the changelogs do not have it, nor the dist tag
after it; and xcraft's checks, in the lint image, see container.RPMLINT_CHECKS, added to rpmlint's.
"""

_FILTERS = 'addFilter(": E: unknown-key ")\n'
"""The filters of all the packages: the signatures of the builds, which xcraft checks, by keys rpmlint does not have."""


def config_dir(project: Project, directory: Path, *, linked: bool = False) -> None:
    """Write the configuration of all the packages into `directory`, xcraft's and the project's, in one directory, as
    rpmlint takes one -c: its files load in no given order, hence xcraft's ReleaseExtension only.

    The project's is `linked`, rather than copied, to follow its changes, where the project is at its path.
    """
    directory.mkdir(parents=True, exist_ok=True)
    write_atomically(directory / "xcraft.toml", _CONFIG)
    (directory / "rpmlint.toml").unlink(missing_ok=True)
    if (config := project.root / LINT_DIR / "rpmlint.toml").exists():
        if linked:
            (directory / "rpmlint.toml").symlink_to(config)
        else:
            write_atomically(directory / "rpmlint.toml", config.read_text())


def _rpmlintrc(project: Project, name: str) -> str:
    """Return the filters of the package `name`: xcraft's, those of its upstream rpmlintrc files, then the project's."""
    files = [*sorted(project.package_dirs[name].glob("*rpmlintrc")), project.root / LINT_DIR / f"{name}.rpmlintrc"]
    return _FILTERS + "".join(f.read_text() + "\n" for f in files if f.is_file())


_MARK = "@@xcraft-lint "
"""The line of the script before the output of rpmlint for a package, with its name."""


def _shown(path: Path) -> str:
    """Return how to show `path`, to open it from the terminal: relative to the current directory when in it."""
    return str(path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path)


def findings(
    project: Project, files: dict[str, list[Path]], *, show: str, tag: bool = True, width: int = 0
) -> dict[str, bool]:
    """Return whether rpmlint finds errors in the `files` of each package, its spec, or its binary packages, or fails
    to run, by package, showing `show` with the package linted while it runs, and what it finds as it comes, after the
    name of the package, padded to `width`, or to the longest name of `files`, unless not `tag`, like for the editors,
    colored like the build logs.
    """
    native = project.on_host()
    image = side_image(native, "lint")
    mounts = {d: f"/in/{i}" for i, d in enumerate(sorted({f.parent for paths in files.values() for f in paths}))}
    specs = {f.name: _shown(f) for paths in files.values() for f in paths if f.suffix == ".spec"}
    with project.tmp_dir("lint") as work, activity(show) as progress:
        config_dir(project, work / "config")
        script = []
        for i, (name, paths) in enumerate(files.items(), 1):
            (work / f"{name}.rpmlintrc").write_text(_rpmlintrc(project, name))
            cmd = ["rpmlint", "--ignore-unused-rpmlintrc", "-r", f"/work/{name}.rpmlintrc", "-c", "/work/config"]
            cmd += [f"{mounts[f.parent]}/{f.name}" for f in paths]
            script.append(f"echo {shlex.quote(f'{_MARK}{name} ({i}/{len(files)})')}")
            script.append(
                f"{shlex.join(cmd)} 2>&1 | tee /work/{name}.out; echo ${{PIPESTATUS[0]}} > /work/{name}.status"
            )

        width, shown = width or max(map(len, files), default=0), tagged("", 0, _out())

        def on_line(line: str) -> None:
            nonlocal shown
            if line.startswith(_MARK):
                progress(current := line.removeprefix(_MARK))
                # after the name of the package, like the warnings of the builds
                shown = tagged(current.rsplit(" (", 1)[0] if tag else "", width, _out())
            elif match := _FINDING.match(line.rstrip("\n")):
                shown(f"{specs.get(match[1], match[1])}{match[2]}")

        container.run(
            native,
            "lint",
            "\n".join(script),
            volumes=[f"{work}:/work", *(f"{d}:{m}:ro" for d, m in mounts.items())],
            on_line=on_line,
            image=image,
        )
        return {name: _failed(work, name) for name in files}


SPEC_DOC = Path("/usr/share/doc/rpm/spec.md")
"""rpm's manual of the specs, for rpmls's hovers: on the host, as the images are installed without documentation."""


def serve(project: Project) -> int:
    """Run rpmls, the language server of the specs, on stdin and stdout, in the lint image, linting them with rpmlint
    and the configuration of the project, for the diagnostics of xcraft lint --spec as the specs are typed, and return
    its exit status.

    The project is mounted read-only at its path, for the paths of the editor to be those of the container, where the
    spec typed runs without network, as rpm parses it, but lint/, where rpmls adds the filters of the findings
    suppressed, and the copies of the macro files of the image in .xcraft/lsp/macros/, for the editor to open their
    definitions. The changelog entries are those of git's user.
    """
    native = project.on_host()
    with contextlib.redirect_stdout(sys.stderr):  # the protocol's: the progress of building the image goes to stderr
        image = side_image(native, "lint")
    config_dir(project, (lsp := project.state / "lsp") / "config", linked=True)
    (macros := lsp / "macros").mkdir(parents=True, exist_ok=True)
    (lint := project.root / LINT_DIR).mkdir(exist_ok=True)
    args = ["--linter", "rpmlint", "--rpmlint-config", str(lsp / "config")]
    args += ["--rpmlintrc-dir", str(lint), "--macro-cache", str(macros)]
    args += ["--spec-doc", str(SPEC_DOC)] if SPEC_DOC.exists() else []
    volumes = [f"{project.root}:{project.root}:ro", f"{lint}:{lint}", f"{macros}:{macros}"]
    volumes += [f"{SPEC_DOC}:{SPEC_DOC}:ro"] if SPEC_DOC.exists() else []
    name, email = (
        process.run(["git", "config", k], check=False, capture=True).stdout.strip() for k in ("user.name", "user.email")
    )
    packager = f"%packager {name} <{email}>\n" if name and email else ""
    script = f"printf %s {shlex.quote(packager)} > ~/.rpmmacros && exec {shlex.join(['rpmls', *args])}"
    cmd = [
        "podman", "run", "--rm", "--interactive", "--platform", native.platform, "--userns=keep-id:uid=1000,gid=1000",
        "--security-opt", "label=disable", "--network", "none", *(a for v in volumes for a in ("--volume", v)),
        image, "bash", "-c", script,
    ]  # fmt: skip
    return process.run(cmd, check=False).returncode


def _out() -> Console:
    """Return the console of the findings, the results: the messages' when both are the terminal, for them to show
    above the progress, which other consoles would overwrite.
    """
    return messages if messages.is_interactive and console.is_terminal else console


def _failed(work: Path, name: str) -> bool:
    """Return whether rpmlint found errors for `name`, its output and status in `work`: 64 for errors, 66 for a badness
    over the threshold, others when it could not run, whose output is shown.
    """
    if (status := int((work / f"{name}.status").read_text())) not in (0, 64, 66):
        lines = (work / f"{name}.out").read_text().splitlines()[-20:]
        _out().print(Text("\n".join([f"rpmlint failed for {name}:", *lines]), style="red"))
    return status != 0
