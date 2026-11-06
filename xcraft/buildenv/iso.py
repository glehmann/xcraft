"""Make the tree of the installation ISO from a snapshot of the distribution, with the hook of the project.

Run as root in the build environment, with the tools making the ISO installed: ``iso.py <config>``. The config is a
JSON object of ``snapshot``, the binaries of the snapshot for the architecture, ``templates``, the files of the ISO,
of install.img, and the hook, ``tree``, the directory to make, ``installer`` and ``packages`` (see
xcraft.project.Iso), ``netinstall``, ``signed``, ``keys``, the files of the keys: ours first, when signed, then
those of the repositories, and the ``name`` and ``version`` of the distribution. The tree gets the owner of the config,
even when it fails.

The hook, ``hook <stage> <dir> <hook.json>``, makes what is particular to the distribution: ``installimg``, on the root
of install.img, before its modules are indexed and it is archived, then ``tree``, on the tree of the ISO, before
efiboot.img is made of its EFI/BOOT/, with EFI/BOOT/efiboot.cfg for grub.cfg, if any. hook.json is the config, with
``rpms``, the files of the latest packages of the snapshot by name.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO

# out of the build environment, for the tests of the helpers, without dnf
# pyright: reportPossiblyUnboundVariable=false
with contextlib.suppress(ImportError):
    import dnf  # ty: ignore[unresolved-import]  # pyright: ignore[reportMissingImports]

SYSLINUX = Path("/usr/share/syslinux")
MEMTEST = Path("/usr/lib64/memtest86+")


def sh(
    *args: str | Path, cwd: Path | None = None, stdin: IO[bytes] | None = None, stdout: IO[bytes] | int | None = None
) -> None:
    """Run a command, failing when it fails."""
    subprocess.run(args, check=True, cwd=cwd, stdin=stdin, stdout=stdout)


def resolver(packages: list[str] | None = None) -> dnf.Base:
    """Return a dnf base of the snapshot alone, on an empty system, with `packages` to install resolved."""
    base = dnf.Base()
    base.conf.read()
    base.conf.installroot = tempfile.mkdtemp()
    base.conf.cachedir = tempfile.mkdtemp()  # see installable.py
    base.conf.install_weak_deps = False
    base.repos.add_new_repo("snapshot", base.conf, baseurl=[f"file://{snapshot}"])
    base.fill_sack(load_system_repo=False)
    for name in packages or []:
        base.install(name)
    if packages:
        base.resolve()
    return base


def extract(package: str, *patterns: str) -> Path:
    """Return a directory with the files of `package` matching `patterns`."""
    dest = Path(tempfile.mkdtemp())
    rpm2cpio = subprocess.Popen(["rpm2cpio", rpms[package]], stdout=subprocess.PIPE)
    sh("cpio", "-idm", "--quiet", *patterns, stdin=rpm2cpio.stdout, cwd=dest)
    if rpm2cpio.wait():
        sys.exit(f"cannot extract {rpms[package]}")
    return dest


def one(directory: Path, pattern: str) -> Path:
    """Return the only file of `directory` matching `pattern`."""
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        sys.exit(f"not one {pattern} in {directory}: {[f.name for f in found]}")
    return found[0]


def hook(stage: str, directory: Path) -> None:
    """Run the hook of the project at `stage`, on `directory`."""
    sh(templates / "hook", stage, directory, tree.parent / "hook.json")


def copy_templates(src: Path, dest: Path, *, symlinks: bool) -> None:
    """Copy the templates `src` into `dest` with the modes git keeps, which their key has: 0755 for the directories
    and the executable files, 0644 for the others, whatever the umask of the checkout.
    """
    shutil.copytree(src, dest, symlinks=symlinks, dirs_exist_ok=True)
    for path in src.rglob("*"):
        if not path.is_symlink():
            (dest / path.relative_to(src)).chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)


def prune_firmware(root: Path) -> None:
    """Remove the firmware files no driver names: the root filesystem would get too large to boot."""
    modules = [str(m) for m in (root / "usr/lib/modules").rglob("*.ko*") if m.is_file()]
    names = subprocess.run(
        ["xargs", "-0", "modinfo", "--field", "firmware"],
        input="\0".join(modules), capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    used = "\n".join(sorted({Path(name).name for name in names.split()}))

    def named(f: Path) -> bool:
        # like create-install-image: when its name is part of one of those named by the drivers
        return f.name.removesuffix(".xz").removesuffix(".zst") in used

    firmware = root / "usr/lib/firmware"
    targets = {f.resolve() for f in firmware.rglob("*") if f.is_symlink() and named(f)}
    for f in firmware.rglob("*"):
        if f.is_file() and not f.is_symlink() and not named(f) and f.resolve() not in targets:
            f.unlink()
    for f in firmware.rglob("*"):
        if f.is_symlink() and not f.exists():
            f.unlink()


def kernel(root: Path) -> Path:
    """Return the kernel of the installer's root filesystem `root`, from its package, as scriptlets may remove it, like
    host-installer's: in the directory of its modules, like AlmaLinux's, or else in /boot.
    """
    query = ["rpm", f"--root={root}", "-qa", "--qf", "[%{=NAME} %{FILENAMES}\n]"]
    files = [
        line.split(" ", 1)
        for line in subprocess.run(query, capture_output=True, text=True, check=True).stdout.splitlines()
    ]
    found = [f for f in files if re.fullmatch(r"(/usr)?/lib/modules/[^/]+/vmlinuz", f[1])] or [
        f for f in files if re.fullmatch(r"/boot/vmlinuz-[^/]*[0-9]", f[1])
    ]
    if len(found) != 1:
        sys.exit(f"not one kernel in install.img: {found}")
    name, path = found[0]
    return extract(name, f".{path}") / path.lstrip("/")


def install_img(dest: Path) -> None:
    """Make the installer's root filesystem, as a cpio archive compressed with zstd, and copy its kernel to
    boot/vmlinuz.
    """
    root, cache = Path(tempfile.mkdtemp()), tempfile.mkdtemp()  # a cache of its own, see installable.py
    sh(
        "dnf", "-q", "-y", f"--installroot={root}", "--disableplugin=*", "--disablerepo=*",
        f"--repofrompath=snapshot,{snapshot}", f"--releasever={dnf.rpm.detect_releasever('/')}",
        # the packages are checked when the snapshot is made
        "--setopt=snapshot.gpgcheck=0", "--setopt=install_weak_deps=False", "--setopt=tsflags=nodocs",
        f"--setopt=cachedir={cache}", f"--setopt=system_cachedir={cache}",
        "install", *config["installer"],
    )  # fmt: skip
    for path in ("var/lib/dnf", "var/cache/dnf"):
        shutil.rmtree(root / path, ignore_errors=True)
    for f in (root / "usr").rglob("*.py[co]"):
        f.unlink()
    prune_firmware(root)
    (root / "init").symlink_to("/sbin/init")
    copy_templates(templates / "installimg", root, symlinks=True)
    hook("installimg", root)
    for version in (root / "usr/lib/modules").iterdir():
        sh("depmod", "-a", "-b", root, version.name)
    shutil.copy2(kernel(root), tree / "boot/vmlinuz")
    with dest.open("wb") as out:
        sh("bash", "-o", "pipefail", "-c",
           "find . -print0 | LC_ALL=C sort -z | cpio -o -0 -H newc --quiet --reproducible | zstd -q -T0",
           cwd=root, stdout=out)  # fmt: skip
    shutil.rmtree(root)


def efiboot(efi: Path) -> None:
    """Make the FAT image the UEFI firmwares boot from, with grub, its modules, and efiboot.cfg or grub.cfg."""
    image = tree / "boot/efiboot.img"
    size = sum(f.stat().st_size + 4096 for f in efi.rglob("*") if f.is_file()) // 1024 + 1024
    sh("mkfs.fat", "-C", "-n", "EFIBOOT", image, str(size), stdout=subprocess.DEVNULL)
    sh("mmd", "-i", image, "::/EFI", "::/EFI/BOOT")
    sh("mcopy", "-s", "-i", image, efi / "BOOTX64.EFI", efi / "x86_64-efi", "::/EFI/BOOT/")
    cfg = efi / "efiboot.cfg"
    sh("mcopy", "-i", image, cfg if cfg.exists() else efi / "grub.cfg", "::/EFI/BOOT/grub.cfg")
    cfg.unlink(missing_ok=True)


def give_back() -> None:
    """Give what was made to the owner of the config, even on errors: the host could not remove what root owns."""
    owner = Path(sys.argv[1]).stat()
    for path in [tree.parent / "hook.json", tree, *tree.rglob("*")]:
        if path.is_symlink() or path.exists():
            os.lchown(path, owner.st_uid, owner.st_gid)


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    snapshot, templates, tree = Path(config["snapshot"]), Path(config["templates"]), Path(config["tree"])
    rpms = {p.name: str(snapshot / p.location) for p in resolver().sack.query().available().latest()}
    (tree.parent / "hook.json").write_text(json.dumps({**config, "rpms": rpms}))
    atexit.register(give_back)
    shutil.rmtree(tree, ignore_errors=True)
    copy_templates(templates / "tree", tree, symlinks=False)
    boot = tree / "boot"
    boot.mkdir(exist_ok=True)
    install_img(tree / "install.img")
    shutil.copy2(MEMTEST / "memtest86+x64.bin", boot / "memtest.bin")
    shutil.copy2(MEMTEST / "memtest86+x64.efi", boot)
    (boot / "isolinux").mkdir(exist_ok=True)
    for name in ("isolinux.bin", "ldlinux.c32", "libcom32.c32", "libutil.c32", "menu.c32"):
        shutil.copy2(SYSLINUX / name, boot / "isolinux")
    # grub's image for CDs, which reads its configuration, grub.cfg of the templates or the hook, and its modules in
    # /EFI/BOOT
    efi = tree / "EFI/BOOT"
    efi.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        one(extract("grub2-efi-x64-cdboot", "*/gcdx64.efi") / "boot/efi/EFI", "*/gcdx64.efi"), efi / "BOOTX64.EFI"
    )
    shutil.copytree(
        extract("grub2-efi-x64-modules", "./usr/lib/grub/*") / "usr/lib/grub/x86_64-efi", efi / "x86_64-efi"
    )
    if not config["netinstall"]:
        packages = tree / "Packages"
        packages.mkdir()
        for p in resolver(config["packages"]).transaction.install_set:
            shutil.copy2(snapshot / p.location, packages)
        # with gzip, which older installers read, rather than the zstd of newer createrepo_c
        sh("createrepo_c", "-q", "--compatibility", tree)
    hook("tree", tree)
    efiboot(efi)
