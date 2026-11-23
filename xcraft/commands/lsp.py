"""xcraft lsp."""

from __future__ import annotations

import typer

from xcraft import lint as linter
from xcraft.project import Project


def lsp() -> None:
    """Run the language server of the specs on stdin and stdout, for the editors: rpmls, with the diagnostics of xcraft
    lint --spec as they are typed, the definitions of the macros, their documentation, completion, and the fixes of the
    findings, filtered in lint/<package>.rpmlintrc, see docs/builds.md.
    """
    # not using the project while the editor runs, which would hold xcraft clean off: rpmls copies the macro files
    # again, when removed
    raise typer.Exit(linter.serve(Project.find(use=False)))
