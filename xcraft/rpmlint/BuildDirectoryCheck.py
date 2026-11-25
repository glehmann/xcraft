"""An rpmlint check of xcraft's, installed in the lint image: the build directory of a spec without its version.

The compiler cache hashes the working directory of the compilers, with -g, and the paths of their command lines: the
sources of a new version, unpacked in a directory named after it, are all compiled again. %setup -n %{name} -C, or
%autosetup's, unpacks them in a directory without their version, whatever the top directory of their archive.
"""

import re
from pathlib import Path

import rpm  # ty: ignore[unresolved-import]  # pyright: ignore[reportMissingImports]
from rpmlint.checks.AbstractCheck import AbstractCheck  # ty: ignore[unresolved-import]  # pyright: ignore[reportMissingImports]
from rpmlint.pkg import Pkg  # ty: ignore[unresolved-import]  # pyright: ignore[reportMissingImports]

SETUP = re.compile(r"^\s*%(auto)?setup\b")


class BuildDirectoryCheck(AbstractCheck):
    """Report the build directory of a spec, %{buildsubdir} once rpm parsed it, with the version of the package."""

    def check_spec(self, pkg: Pkg) -> None:  # noqa: D102
        try:
            parsed = rpm.spec(str(pkg.name))
        except (ValueError, rpm.error):
            return  # reported by SpecCheck
        directory, version = rpm.expandMacro("%{?buildsubdir}"), parsed.sourceHeader["version"]
        if not re.search(rf"(?<![0-9.]){re.escape(version)}(?![0-9])", directory):
            return
        lines = Path(pkg.name).read_text(encoding="utf-8", errors="replace").splitlines()
        # at the last %setup, which sets the build directory, for the editors
        pkg.current_linenum = max((n for n, line in enumerate(lines, 1) if SETUP.match(line)), default=None)
        self.output.add_info("E", pkg, "versioned-build-directory", directory)
        pkg.current_linenum = None
