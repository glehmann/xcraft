"""Run a command in the root of the build environment, /buildroot, where the builds run: ``chroot.py <command>...``.

Run in the build environment, as the user of the command, in a directory the root has at the same path, like /build. It
runs again as root, with sudo, for a user namespace mapping the same ids as the container, all of them: with only the
user's, changing the owner of a file to root would fail with EINVAL, which fakeroot, unlike EPERM, does not ignore. Root
maps them from outside the new user namespace, which takes CAP_SETUID there, and in it, with a mount namespace of its
own, binds in the root /proc, /sys, /dev and the volumes of the builds, and the files naming the host, then makes it the
root of the mount namespace, with pivot_root rather than chroot, from which the kernel refuses to create user
namespaces, like those of the sandboxes of bwrap, and runs the command as the user, with its environment, which sudo
would have trimmed, without gaining privileges, so that sudo cannot change the build root once recorded: the root has
neither sudo nor dnf, which install its packages from outside, see build.sh.
"""

import ctypes
import json
import os
import signal
import sys
import warnings  # noqa: F401, which os.execvpe imports, not to be found in the root
from pathlib import Path

ROOT = Path("/buildroot")
BINDS = ["/proc", "/sys", "/dev", "/build", "/ccache", "/dune-cache", "/etc/hosts", "/etc/resolv.conf"]
"""What the root gets of the container, when the container has it."""
CLONE_NEWNS, CLONE_NEWUSER = 0x20000, 0x10000000
MS_BIND, MS_REC, MS_PRIVATE = 0x1000, 0x4000, 0x40000
MNT_DETACH = 2
SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41}
"""The number of the system call pivot_root, which glibc has no function for, by architecture of the tools."""
PR_SET_NO_NEW_PRIVS = 38

libc = ctypes.CDLL(None, use_errno=True)


def check(result: int, what: str) -> None:
    """Raise OSError when the `result` of the libc function doing `what` is an error."""
    if result:
        errno = ctypes.get_errno()
        raise OSError(errno, f"{what}: {os.strerror(errno)}")


if os.getuid():
    command = [str(os.getuid()), str(os.getgid()), str(Path.cwd()), json.dumps(dict(os.environ)), *sys.argv[1:]]
    os.execvp("sudo", ["sudo", sys.executable, "-I", __file__, *command])  # noqa: S606
uid, gid, cwd, environment, *command = sys.argv[1:]
unshared, mapped = os.pipe(), os.pipe()
# each process closes the write end of the pipe it reads: an EOF when the other one dies first
if pid := os.fork():
    os.close(unshared[1])
    # the ranges of ids of the container, mapped to themselves, which only a process outside the new user namespace
    # can do, with CAP_SETUID there
    if os.read(unshared[0], 1) == b"x":
        for name in ("uid_map", "gid_map"):
            ranges = [line.split() for line in Path(f"/proc/self/{name}").read_text().splitlines()]
            Path(f"/proc/{pid}/{name}").write_text("".join(f"{r[0]} {r[0]} {r[2]}\n" for r in ranges))
        os.write(mapped[1], b"x")
    sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
os.close(mapped[1])
try:
    check(libc.unshare(CLONE_NEWUSER | CLONE_NEWNS), "unshare")
except OSError:
    os.write(unshared[1], b"-")
    sys.exit("the build root needs a user namespace, which qemu-user cannot make, multithreaded: see xcraft doctor")
os.write(unshared[1], b"x")
if os.read(mapped[0], 1) != b"x":
    sys.exit("the ids of the container could not be mapped")
os.setgid(0)
os.setuid(0)
check(libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None), "mount --make-rprivate /")
for path in map(Path, BINDS):
    if not path.exists():
        continue
    target = ROOT / path.relative_to("/")
    if path.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    elif not target.exists():
        target.touch()
    check(libc.mount(str(path).encode(), str(target).encode(), None, MS_BIND | MS_REC, None), f"mount --rbind {path}")
# a mount point, which pivot_root needs, with the binds; the old root stacked under the new one, then detached
check(libc.mount(str(ROOT).encode(), str(ROOT).encode(), None, MS_BIND | MS_REC, None), f"mount --rbind {ROOT}")
os.chdir(ROOT)
check(libc.syscall(SYS_PIVOT_ROOT[os.uname().machine], b".", b"."), "pivot_root")
check(libc.umount2(b".", MNT_DETACH), "umount the old root")
os.chdir(cwd)
os.setgroups([])
os.setgid(int(gid))
os.setuid(int(uid))
check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(PR_SET_NO_NEW_PRIVS)")
# Python ignores them, and the command would inherit it, like rpmbuild does: writing to a closed pipe, like sed in a
# pipeline whose reader exits first, would fail with EPIPE, and an error, rather than being killed quietly
for sig in (signal.SIGPIPE, signal.SIGXFSZ):
    signal.signal(sig, signal.SIG_DFL)
os.execvpe(command[0], command, json.loads(environment))  # noqa: S606, the command of the caller
