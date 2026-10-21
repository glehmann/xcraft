"""Make the tree of the installation ISO from a snapshot of the distribution, like XCP-ng's create-install-image.

Run as root in the build environment, with the tools making the ISO installed: ``iso.py <config>``. The config is
a JSON object of ``snapshot``, the directory of the snapshot, ``templates``, the files of the ISO and of install.img,
``tree``, the directory to make, ``installer`` and ``packages`` (see xcraft.project.Iso), ``netinstall`` and
``signed``. The tree, with ``info.json`` next to it, the version and volume id of the ISO, gets the owner of the
config, even when it fails.
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

import dnf  # ty: ignore[unresolved-import]

config = json.loads(Path(sys.argv[1]).read_text())
snapshot, templates, tree = Path(config["snapshot"]), Path(config["templates"]), Path(config["tree"])
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
    base.conf.install_weak_deps = False
    base.repos.add_new_repo("snapshot", base.conf, baseurl=[f"file://{snapshot}/os"])
    base.fill_sack(load_system_repo=False)
    for name in packages or []:
        base.install(name)
    if packages:
        base.resolve()
    return base


rpms = {p.name: snapshot / "os" / p.location for p in resolver().sack.query().available().latest()}


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


def prune_firmware(root: Path) -> None:
    """Remove the firmware files no driver names: the root filesystem would get too large to boot."""
    modules = [str(m) for m in (root / "usr/lib/modules").rglob("*.ko*") if m.is_file()]
    names = subprocess.run(
        ["xargs", "-0", "modinfo", "--field", "firmware"],
        input="\0".join(modules), capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    used = "\n".join(sorted({Path(name).name for name in names.split()}))
    firmware = root / "usr/lib/firmware"
    for f in firmware.rglob("*"):
        # like create-install-image: kept when its name is part of one of those named by the drivers
        if f.is_file() and not f.is_symlink() and f.name.removesuffix(".xz").removesuffix(".zst") not in used:
            f.unlink()
    for f in firmware.rglob("*"):
        if f.is_symlink() and not f.exists():
            f.unlink()


def install_img(dest: Path) -> None:
    """Make the installer's root filesystem, as a cpio archive compressed with zstd."""
    root = Path(tempfile.mkdtemp())
    sh(
        "dnf", "-q", "-y", f"--installroot={root}", "--disableplugin=*", "--disablerepo=*",
        f"--repofrompath=snapshot,{snapshot}/os", f"--releasever={dnf.rpm.detect_releasever('/')}",
        # the packages are checked when the snapshot is made
        "--setopt=snapshot.gpgcheck=0", "--setopt=install_weak_deps=False", "--setopt=tsflags=nodocs",
        "install", *config["installer"],
    )  # fmt: skip
    shadow = root / "etc/shadow"
    shadow.write_text("".join("root::::::::\n" if line.startswith("root:") else line
                              for line in shadow.read_text().splitlines(keepends=True)))  # fmt: skip
    for path in ("usr/share/cracklib", "usr/lib/udev/hwdb.d", "var/lib/dnf", "var/cache/dnf"):
        shutil.rmtree(root / path, ignore_errors=True)
    for f in (root / "usr").rglob("*.py[co]"):
        f.unlink()
    prune_firmware(root)
    (root / "init").symlink_to("/sbin/init")
    shutil.copytree(templates / "installimg", root, symlinks=True, dirs_exist_ok=True)
    for version in (root / "usr/lib/modules").iterdir():
        sh("depmod", "-a", "-b", root, version.name)
    installer = root / "opt/xensource/installer"
    (installer / "EULA").symlink_to("../../../EULA")
    branding = one(root / "usr/lib", "python3*/site-packages/xcp/branding.py")
    (installer / "version.py").symlink_to(f"../../../{branding.relative_to(root)}")
    sh("systemctl", f"--root={root}", "enable", "installer", "installer-getty@tty2")
    for unit in ("getty@tty1", "fcoe", "lldpad", "xen-init-dom0", "xenconsoled", "xenstored", "chronyd", "chrony-wait"):
        subprocess.run(["systemctl", f"--root={root}", "--quiet", "disable", unit], check=False)  # when installed
    with dest.open("wb") as out:
        sh("sh", "-c", "find . -print0 | LC_ALL=C sort -z | cpio -o -0 -H newc --quiet --reproducible | zstd -q -T0",
           cwd=root, stdout=out)  # fmt: skip
    shutil.rmtree(root)


def boot_params(param: str) -> None:
    """Add `param` to the kernel command line of all the entries of the boot loaders."""
    for cfg in [tree / "boot/isolinux/isolinux.cfg", *(tree / "EFI/BOOT").glob("grub*.cfg")]:
        cfg.write_text(cfg.read_text().replace("/vmlinuz", f"/vmlinuz {param}"))


def efiboot(efi: Path, grub_cfg: Path) -> None:
    """Make the FAT image the UEFI firmwares boot from, with grub, its modules, and `grub_cfg`."""
    image = tree / "boot/efiboot.img"
    size = sum(f.stat().st_size + 4096 for f in efi.rglob("*") if f.is_file()) // 1024 + 1024
    sh("mkfs.fat", "-C", "-n", "EFIBOOT", image, str(size), stdout=subprocess.DEVNULL)
    sh("mmd", "-i", image, "::/EFI", "::/EFI/BOOT")
    sh("mcopy", "-s", "-i", image, efi / "BOOTX64.EFI", efi / "x86_64-efi", "::/EFI/BOOT/")
    sh("mcopy", "-i", image, grub_cfg, "::/EFI/BOOT/grub.cfg")


def repository(branding: dict[str, str]) -> None:
    """Add the repository of the packages to install, and the description of the ISO's tree."""
    treeinfo = tree / ".treeinfo"
    text = treeinfo.read_text().replace("@@TIMESTAMP@@", f"{time.time():.2f}")
    for name in ("PLATFORM_NAME", "PLATFORM_VERSION", "PRODUCT_BRAND", "PRODUCT_VERSION"):
        text = text.replace(f"@@{name}@@", branding[name])
    if not config["signed"]:
        # the installer looks for the keys even without checking the signatures
        text = text.replace("\nkey", "\n#key")
    treeinfo.write_text(text)
    packages = tree / "Packages"
    packages.mkdir()
    for p in resolver(config["packages"]).transaction.install_set:
        shutil.copy2(snapshot / "os" / p.location, packages)
    # with gzip: the installer does not read the zstd of newer createrepo_c
    sh("createrepo_c", "-q", "--compatibility", tree)


def give_back() -> None:
    """Give what was made to the owner of the config, even on errors: the host could not remove what root owns."""
    owner = Path(sys.argv[1]).stat()
    for path in [tree.parent / "info.json", tree, *tree.rglob("*")]:
        if path.is_symlink() or path.exists():
            os.lchown(path, owner.st_uid, owner.st_gid)


atexit.register(give_back)
shutil.rmtree(tree, ignore_errors=True)
shutil.copytree(templates / "tree", tree)
install_img(tree / "install.img")
boot = tree / "boot"
shutil.copy2(one(extract("kernel", "./boot/vmlinuz-*") / "boot", "vmlinuz-*[0-9]"), boot / "vmlinuz")
# the debug builds of the hypervisor, like XenServer, for a more useful xl dmesg when something goes wrong
shutil.copy2(one(extract("xen-hypervisor-elf", "./boot/*") / "boot", "xen-*-d.gz"), boot / "xen.gz")
shutil.copy2(MEMTEST / "memtest86+x64.bin", boot / "memtest.bin")
shutil.copy2(MEMTEST / "memtest86+x64.efi", boot)
branding_dir = extract("branding-xcp-ng", "./usr/src/branding/*") / "usr/src/branding"
for name in ("EULA", "LICENSES"):
    shutil.copy2(branding_dir / name, tree)
branding = dict(
    line.split("=", 1) for line in (branding_dir / "branding").read_text().splitlines() if line[:1].isupper()
)
for directory, files in [
    ("isolinux", ["isolinux.bin", "ldlinux.c32", "libcom32.c32", "libutil.c32", "mboot.c32", "menu.c32"]),
    # for PXE boot, where XCP-ng 8 had them
    ("pxelinux", ["pxelinux.0", "ldlinux.c32", "libcom32.c32", "libutil.c32", "mboot.c32", "menu.c32"]),
]:
    (boot / directory).mkdir(exist_ok=True)
    for name in files:
        shutil.copy2(SYSLINUX / name, boot / directory)
# grub's image for CDs, which reads its configuration and its modules in /EFI/BOOT, and host-installer's
# configurations
efi = tree / "EFI/BOOT"
efi.mkdir(parents=True)
shutil.copy2(extract("grub2-efi-x64-cdboot", "*/gcdx64.efi") / "boot/efi/EFI/almalinux/gcdx64.efi", efi / "BOOTX64.EFI")
shutil.copytree(extract("grub2-efi-x64-modules", "./usr/lib/grub/*") / "usr/lib/grub/x86_64-efi", efi / "x86_64-efi")
grub = extract("host-installer-bootloader", "./EFI/almalinux/*") / "EFI/almalinux"
# with grub-usb.cfg for efiboot.img, which some firmwares take for the root, finding the ISO's files by install.img
for name in ("grub.cfg", "grub-usb.cfg"):
    # our xen.efi has no multiboot2 header, unlike XenServer's: grub boots the ELF one
    (efi / name).write_text((grub / name).read_text().replace("/boot/xen.efi", "/boot/xen.gz"))
if config["netinstall"]:
    (tree / ".treeinfo").unlink()
    boot_params("netinstall")
else:
    repository(branding)
    if not config["signed"]:
        boot_params("no-repo-gpgcheck")
efiboot(efi, efi / "grub-usb.cfg")
(efi / "grub-usb.cfg").unlink()
(tree.parent / "info.json").write_text(
    json.dumps(
        {"version": branding["PRODUCT_VERSION"], "volid": f"XCP-NG_{branding['PRODUCT_VERSION_TEXT'].replace('.', '')}"}
    )
)
