from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests import TOML
from xcraft.__main__ import app
from xcraft.display import diagnostics, read_tail

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

LOG = """\
Installing build dependencies
Executing(%prep): /bin/sh -e /var/tmp/rpm-tmp.1
+ tar xf foo.tar.gz
Executing(%build): /bin/sh -e /var/tmp/rpm-tmp.2
+ make
gcc -c foo.c
foo.c:3: error: oops
make: *** [all] Error 1
"""


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "xcraft.toml").write_text(TOML)
    (tmp_path / ".xcraft/logs").mkdir(parents=True)
    (tmp_path / ".xcraft/build").mkdir()
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    path = tmp_path / ".xcraft/logs/foo.log"
    path.write_text(LOG)
    return path


def xcraft(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    error = result.exception if result.exception and not isinstance(result.exception, SystemExit) else ""
    return result.exit_code, f"{result.output}{error}"


@pytest.mark.usefixtures("log")
def test_log_filters() -> None:
    assert xcraft("log", "foo") == (0, LOG)
    assert xcraft("log", "foo", "--step", "prep") == (
        0,
        "Executing(%prep): /bin/sh -e /var/tmp/rpm-tmp.1\n+ tar xf foo.tar.gz\n",
    )
    assert xcraft("log", "foo", "--step", "build", "--grep", r"error|Error", "--last", "1") == (
        0,
        "make: *** [all] Error 1\n",
    )
    assert xcraft("log", "foo", "-n", "2") == xcraft("log", "foo", "--last", "2")
    assert xcraft("log", "-h") == xcraft("log", "--help")
    code, output = xcraft("log", "foo", "--step", "install")
    assert code == 1
    assert output == "no install step in the log of foo, it has: prep, build"
    assert xcraft("log", "bar") == (1, "bar has no build log")


def test_log_follow(log: Path) -> None:
    pid_file = log.parent.parent / "build/foo.pid"
    pid_file.write_text(str(os.getpid()))

    def build() -> None:
        time.sleep(0.5)
        with log.open("a") as f:
            f.write("Wrote: foo-1.0-1.x86_64.rpm\nnot a match\n")
        time.sleep(0.5)
        pid_file.unlink()

    thread = threading.Thread(target=build)
    thread.start()
    # returns once the build is over, with the lines it added
    assert xcraft("log", "foo", "--follow", "--grep", "Wrote|Error") == (
        0,
        "make: *** [all] Error 1\nWrote: foo-1.0-1.x86_64.rpm\n",
    )
    thread.join()


def test_read_tail(tmp_path: Path) -> None:
    path = tmp_path / "log"
    path.write_text("first line\nsecond\nthird\n")
    assert read_tail(path) == ["first line", "second", "third"]
    assert read_tail(path, size=10) == ["third"]  # without the partial line


def test_diagnostics() -> None:
    shown: list[str] = []
    on_line = diagnostics(shown.append)
    for line in [
        "Executing(%build): /bin/sh -e /var/tmp/rpm-tmp.WXoNig",
        "error: bad date in %changelog: Fri 02 Oct 2026 Yann Dirson <yann.dirson@vates.tech> - 26.4.0-1.3",
        "gcc -O2 -c foo.c",
        "foo.c:3:5: \x1b[01;35mwarning: \x1b[munused variable",
        "foo.c:4:1: error: expected ';'",
        "warning: Macro expanded in comment on line 40: %{_sbindir}/sendmail",
        "Failed to preset unit: Unit mpcount.service does not exist",
        "FAILED: test_foo",
        "make: *** [Makefile:60: test] Error 1",
        "RPM build warnings:",
        "",
        "RPM build errors:",
        "    source_date_epoch_from_changelog set but %changelog is missing",
        "    Bad exit status from /var/tmp/rpm-tmp.WXoNig (%check)",
        "",
        "real\t1m51.217s",
    ]:
        on_line(line)
    assert shown == [
        "error: bad date in %changelog: Fri 02 Oct 2026 Yann Dirson <yann.dirson@vates.tech> - 26.4.0-1.3",
        "foo.c:4:1: error: expected ';'",
        "warning: Macro expanded in comment on line 40: %{_sbindir}/sendmail",
        "FAILED: test_foo",
        "RPM build warnings:",
        "RPM build errors:",
        "    source_date_epoch_from_changelog set but %changelog is missing",
        "    Bad exit status from /var/tmp/rpm-tmp.WXoNig (%check)",
    ]


def test_diagnostics_header() -> None:
    shown: list[str] = []
    on_line = diagnostics(shown.append, header="[1/2] building foo")
    on_line("gcc -O2 -c foo.c")
    assert shown == []
    for line in ["error: one", "error: two"]:
        on_line(line)
    assert shown == ["[1/2] building foo", "error: one", "error: two"]


def test_progress_in_ci() -> None:
    # coloured, but without the frames of the progress: the logs of CI are not terminals
    code = (
        "from xcraft.display import activity, console\n"
        "with activity('building') as on_line:\n"
        "    on_line('a line')\n"
        "console.print('[green]built[/]')\n"
    )
    env = {**os.environ, "FORCE_COLOR": "1"}
    output = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
    assert output == "\x1b[32mbuilt\x1b[0m\n"
