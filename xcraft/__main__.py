"""xcraft command line."""

from __future__ import annotations

import inspect
import logging
import os
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, Annotated, ClassVar

import typer
from rich.text import Text
from typer import _completion_shared

from xcraft import process
from xcraft.commands import (
    boot,
    build,
    bump,
    clean,
    contents,
    doctor,
    fetch,
    graph,
    history,
    import_,
    iso,
    key,
    lint,
    lock,
    log,
    login,
    maint,
    patchset,
    publish,
    push,
    repo,
    reuse,
    shell,
    status,
    unpublish,
    vm,
)
from xcraft.display import console, known_commands, messages

if TYPE_CHECKING:
    from collections.abc import Iterator

# Typer's Rich help does not use Click's max_content_width: its width, from $TERMINAL_WIDTH, which typer.rich_utils
# reads once imported, for the help and the errors only, rather than at each start, like for each completion.
os.environ["TERMINAL_WIDTH"] = str(min(120, console.width, int(os.environ.get("TERMINAL_WIDTH") or 120)))

_FISH_COMPLETION = """
function __%(complete_func)s
    set -g __%(complete_func)s_items (env %(autocomplete_var)s=complete_fish _TYPER_COMPLETE_FISH_ACTION=get-args \\
        _TYPER_COMPLETE_ARGS=(commandline -cp) %(prog_name)s)
    test -n "$__%(complete_func)s_items"
end
complete --command %(prog_name)s --no-files --condition __%(complete_func)s \\
    --arguments '(printf "%%s\\n" $__%(complete_func)s_items)'
"""
"""The completion of fish: typer's runs xcraft once to know whether there are completions, then again to get them;
this one once only, its condition getting them, which its arguments give, for each completion."""
_completion_shared._completion_scripts["fish"] = _FISH_COMPLETION  # noqa: SLF001, which typer has no API to change

app = typer.Typer(
    help="Build the XCP-ng distribution.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
for panel, commands in {
    "Building": [
        build.build,
        repo.repo,
        shell.shell,
        log.log,
        lint.lint,
        graph.graph,
        contents.contents,
        status.status,
        clean.clean,
    ],
    "Packages": [bump.bump, patchset.patchset, history.history],
    "Upstream packages": [lock.lock, reuse.reuse],
    "Publishing": [iso.iso, boot.boot, unpublish.unpublish],
    "Host": [doctor.doctor],
}.items():
    for command in commands:
        app.command(rich_help_panel=panel)(command)
app.command("st", hidden=True)(status.status)
app.command("import", rich_help_panel="Packages")(import_.import_)
app.add_typer(push.app, name="push", rich_help_panel="Sharing")
app.command(rich_help_panel="Sharing")(login.login)
app.command(rich_help_panel="Sharing")(login.logout)
app.add_typer(fetch.app, name="fetch", rich_help_panel="Sharing")
app.add_typer(publish.app, name="publish", rich_help_panel="Publishing")
app.add_typer(key.app, name="key", rich_help_panel="Publishing")
app.add_typer(maint.app, name="maint", rich_help_panel="Publishing")
app.add_typer(vm.app, name="vm", rich_help_panel="Host")


@app.callback()
def options(
    trace: Annotated[bool, typer.Option("--trace", help="Show stack traces on errors.")] = False,  # noqa: ARG001
    debug: Annotated[bool, typer.Option("--debug", help="Show the commands xcraft runs.")] = False,  # noqa: ARG001
    maintenance: Annotated[
        str | None,
        typer.Option(
            "--maintenance",
            metavar="BRANCH",
            help="Number the builds against this branch, the main one or a maintenance one, like the target of a pull "
            "request in CI, rather than by where the current one forks. $XCRAFT_MAINTENANCE, by default.",
        ),
    ] = None,
) -> None:
    """Build the XCP-ng distribution."""
    # --trace and --debug are handled in main(), they are declared here for --help
    from xcraft import buildid, remote  # noqa: PLC0415, with urllib3: not for the completions

    remote.forget()  # what another command, in the same process like the tests, read
    buildid.target = maintenance


def _reflowed(doc: str | None) -> str | None:
    """Return the docstring `doc` with each paragraph on one line, for typer, which wraps them to the terminal, but
    keeps their line breaks otherwise.
    """
    return doc and "\n\n".join(" ".join(p.split("\n")) for p in inspect.cleandoc(doc).split("\n\n"))


def _reflow(typer_app: typer.Typer) -> None:
    """Reflow the help of the commands of `typer_app`, at all depths, see _reflowed()."""
    infos = [*typer_app.registered_commands, *filter(None, [typer_app.registered_callback])]
    for info in infos:
        info.help = info.help or _reflowed(getattr(info.callback, "__doc__", None))
    for group in typer_app.registered_groups:
        if group.typer_instance:
            _reflow(group.typer_instance)


_reflow(app)


class _Handler(logging.Handler):
    """Show the log messages on whole lines, above the progress of the activities, like the other messages, after
    their level, in its color, like `debug:` in blue.
    """

    _COLORS: ClassVar = {logging.DEBUG: "blue", logging.WARNING: "yellow", logging.ERROR: "red"}

    def emit(self, record: logging.LogRecord) -> None:
        level = Text(f"{record.levelname.lower()}:", self._COLORS.get(record.levelno, ""))
        messages.print(Text.assemble(level, " ", self.format(record)))


def _names(typer_app: typer.Typer) -> Iterator[str]:
    """Return the names of the subcommands of `typer_app`, at all depths, as typer names them: from what it registered,
    rather than from its commands, which it makes at each start, from their signatures.
    """
    for info in typer_app.registered_commands:
        yield info.name or typer.main.get_command_name(getattr(info.callback, "__name__", ""))
    for group in typer_app.registered_groups:
        yield from filter(None, [group.name])
        if group.typer_instance:
            yield from _names(group.typer_instance)


known_commands(_names(app))


def main() -> None:
    """Run the command line, showing errors without a stack trace unless --trace is given."""
    socket.setdefaulttimeout(60)  # the downloads, from servers not answering anymore: boto3 has its own
    args = sys.argv[1:]
    end = args.index("--") if "--" in args else len(args)
    if "--debug" in args[:end]:
        process.log.addHandler(_Handler())
        process.log.setLevel(logging.DEBUG)
    try:
        app(args=[arg for arg in args[:end] if arg != "--debug"] + args[end:])
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        if "--trace" in sys.argv:
            raise
        messages.print(f"error: {exc}", style="red", markup=False)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            messages.print(exc.stderr.strip(), markup=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
