"""Put the pinned upstream binary packages of an architecture in a directory, from the store of the pinned RPMs.

Run natively, as root, in the base image, with the repositories of the architecture, by xcraft.pinned, with the
input file, the store, mounted on /store, and the directory, in it: each RPM missing in /store/rpms/, by its SHA-256
and its file name, of its name, version and architecture, is downloaded, and kept there only when it has the SHA-256
of its pin; the directory has them all, hard-linked, for xcraft to make a repository of. Nothing is kept of a package
of other contents, or missing: the repositories do not have the pinned ones anymore.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]
import dnf.rpm  # ty: ignore[unresolved-import]

STORE = Path("/store")


def stored(name: str, pin: dict) -> Path:
    """Return the path of the RPM of the pinned package `name` in the store."""
    return STORE / "rpms" / pin["sha256"] / f"{name}-{pin['evr'].split(':')[-1]}.{pin['arch']}.rpm"


def sha256(path: Path) -> str:
    """Return the SHA-256 of the file `path`."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(arch: str, pins: dict[str, dict]) -> None:
    """Download the pinned RPMs `pins` of `arch` into the store, checked against their SHA-256."""
    base = dnf.Base()
    base.conf.read()
    base.conf.substitutions["arch"] = arch
    base.conf.substitutions["basearch"] = dnf.rpm.basearch(arch)
    base.read_all_repos()
    base.fill_sack(load_system_repo=False)
    wanted, problems = [], []
    for name, pin in sorted(pins.items()):
        nevra = f"{name}-{pin['evr']}.{pin['arch']}"
        found = base.sack.query().available().filter(name=name, evr=pin["evr"], arch=pin["arch"]).run()
        same = [p for p in found if p.chksum and p.chksum[1].hex() == pin["sha256"]]
        if same:
            wanted.append(same[0])
        elif found:
            problems.append(
                f"{nevra}: the repositories have other contents, {found[0].chksum[1].hex()}, than pinned, "
                f"{pin['sha256']}: xcraft lock --update {pin['source']} pins them"
            )
        else:
            problems.append(f"{nevra}: pinned, but in no repository anymore")
    if problems:
        sys.exit("the pinned packages cannot be downloaded:\n" + "\n".join(problems))
    base.download_packages(wanted)
    for package in wanted:
        path, pin = Path(package.localPkg()), pins[package.name]
        if sha256(path) != pin["sha256"]:
            sys.exit(f"{package}: downloaded with other contents than pinned, {pin['sha256']}")
        rpm = stored(package.name, pin)
        rpm.parent.mkdir(parents=True, exist_ok=True)
        part = rpm.with_name(f".{rpm.name}.{os.getpid()}")
        part.write_bytes(path.read_bytes())
        part.chmod(0o444)
        part.rename(rpm)


def main() -> None:
    """Put the pinned packages of the input file in the directory, downloaded when missing in the store."""
    data, directory = json.loads(Path(sys.argv[1]).read_text()), STORE / sys.argv[2]
    (STORE / "rpms").mkdir(parents=True, exist_ok=True)
    pins = data["pins"]
    if missing := {n: p for n, p in pins.items() if not stored(n, p).exists()}:
        download(data["arch"], missing)
    directory.mkdir(parents=True)
    for name, pin in pins.items():
        os.link(stored(name, pin), directory / stored(name, pin).name)


if __name__ == "__main__":
    main()
