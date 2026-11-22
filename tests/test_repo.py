"""xcraft repo: the repository of the builds, and the test hosts it is copied to."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import typer

from tests import xcraft
from xcraft.commands.repo import _kept
from xcraft.hosts import Host, connected, ssh_host


def should_report_newer_packages_kept_by_test_hosts() -> None:
    rpms = ["xen-4.17-1+b3~2.dev.x86_64.rpm", "xen-libs-4.17-1+b3~2.dev.x86_64.rpm", "qemu-9-1+b5~1.dev.x86_64.rpm"]
    installed = [
        "xen-4.17-1+b3~2.dev.x86_64",  # upgraded
        "xen-libs-4.17-1+b3~4.qcow2.x86_64",  # newer, from another identifier
        "package qemu is not installed",
    ]
    assert _kept(installed, rpms) == [("xen-libs-4.17-1+b3~4.qcow2.x86_64", "xen-libs-4.17-1+b3~2.dev.x86_64")]


def should_accept_test_hosts_with_their_ports() -> None:
    assert ssh_host("root@h") == Host("root@h", "root@h")
    assert ssh_host("ssh://root@127.0.0.1:2222") == Host("ssh://root@127.0.0.1:2222", "root@127.0.0.1", ["-p", "2222"])
    assert ssh_host("ssh://h") == Host("ssh://h", "h")
    for bad in ("-oProxyCommand=x", "ssh://-oProxyCommand=x", "ssh://-u@h", "ssh://h:port", "ssh://h/path", "ssh://"):
        with pytest.raises(typer.BadParameter, match="not a host"):
            ssh_host(bad)


@pytest.mark.skipif(not shutil.which("sshd", path="/usr/sbin:/usr/bin"), reason="needs sshd")
def should_connect_once_to_a_test_host(tmp_path: Path) -> None:
    # a real sshd on localhost, as this user, which the commands of a copy reach through one connection
    for key in ("host", "user"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", tmp_path / key], check=True)
    shutil.copy(tmp_path / "user.pub", tmp_path / "authorized_keys")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {tmp_path / 'host'}\nAuthorizedKeysFile "
        f"{tmp_path / 'authorized_keys'}\nPasswordAuthentication no\nUsePAM no\nStrictModes no\n"
    )
    sshd = shutil.which("sshd", path="/usr/sbin:/usr/bin")
    assert sshd
    log = tmp_path / "sshd.log"
    with subprocess.Popen([sshd, "-D", "-f", tmp_path / "sshd_config", "-E", log]) as server:
        try:
            options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
            options += ["-i", str(tmp_path / "user"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
            host = Host("test", "127.0.0.1", ["-p", str(port), *options])
            for _ in range(100):  # until it listens
                if subprocess.run([*host.ssh("true")], capture_output=True, check=False).returncode == 0:
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"sshd did not accept connections in 10s: {log.read_text()}")
            before = log.read_text().count("Accepted publickey")
            with connected(host) as shared:
                for _ in range(3):
                    subprocess.run(shared.ssh("true"), check=True)
                control = Path(next(o for o in shared.options if o.startswith("ControlPath=")).split("=", 1)[1])
            assert log.read_text().count("Accepted publickey") == before + 1
            assert not control.parent.exists()
        finally:
            server.terminate()


@pytest.mark.usefixtures("unprepared_root")
def should_reject_inconsistent_host_options() -> None:
    # before preparing the project
    for args, error in [
        (["--host", "h", "--final"], "--host, --vm and --upgrade are for the development builds of the given packages"),
        (["--upgrade"], "--upgrade needs --host or --vm"),
        (["--host", "h", "--baseurl", "http://x/"], "with --host or --vm, the hosts find the repository in"),
        (["--host", "ssh://root@h:port"], "not a host: ssh://root@h:port, as ssh://[user@]host[:port]"),
    ]:
        code, output = xcraft("repo", "trivial", *args)
        assert code == 2, (args, output)
        assert error in " ".join(output.replace("│", "").split()), (args, output)


@pytest.mark.usefixtures("root")
def should_refuse_to_copy_to_a_vm_not_running() -> None:
    code, output = xcraft("repo", "trivial", "--vm")
    assert code == 2, output
    assert "no VM running in" in " ".join(output.replace("│", "").split())


def should_make_a_repository_with_its_dependencies_satisfied(root: Path) -> None:
    code, output = xcraft("repo", "with-deps")
    assert code == 0, output
    assert f"repository in {root / 'repo'}, with all dependencies satisfied" in output
    assert sorted(p.name.split("-1.0")[0] for p in (root / "repo").glob("*.rpm")) == ["trivial", "with-deps"]
    assert (root / "repo/repodata/repomd.xml").exists()
    # with the cached metadata and builds, after clean removed .xcraft/tmp, what a dry run shows only
    code, output = xcraft("clean", "-n")
    assert code == 0, output
    assert re.search(r"^local repositories +1 to remove$", output, re.MULTILINE), output
    assert (root / ".xcraft/repos/with-deps").exists()  # those of the builds
    code, output = xcraft("clean")
    assert code == 0, output
    assert re.search(r"^local repositories +1 removed$", output, re.MULTILINE), output
    assert not (root / ".xcraft/repos").exists()
    code, output = xcraft("repo", "with-deps")
    assert code == 0, output

    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nRequires: no-such-package"))
    code, output = xcraft("repo", "trivial", "--output", str(root / "out"))
    assert code == 1
    assert "no-such-package" in output
    assert "some dependencies cannot be satisfied" in output
