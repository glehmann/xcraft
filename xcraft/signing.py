"""Signing our packages, with the host's gpg, and rpmsign in a container.

The key never gets near the builds, which run the specs' code: only the host's
gpg uses it, so it can be in an HSM, through gpg. Each build is signed once made,
by the key of the command, see build_signer(): in dev mode, the developer's own
key, which xcraft makes in its own keyring, without passphrase, when there is
none: it is only for test hosts, and trusted by nobody.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft import container, process
from xcraft.container import ContainerError, side_image
from xcraft.display import console
from xcraft.project import fingerprint, write_atomically
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from xcraft.project import Project


KEY_FILE = "RPM-GPG-KEY"
"""The public key of the signer of a build, in it, and of a snapshot, in it."""


def signer_key_file(fingerprint: str) -> str:
    """Return the name of the file of the public key `fingerprint` of a signer of the packages of a snapshot, next to
    its key, KEY_FILE: a file by key, as older versions of rpm import only the first key of a file.
    """
    return f"{KEY_FILE}-{fingerprint}"


class SigningError(RuntimeError):
    """Raised when packages cannot be signed."""


@dataclass
class Signer:
    """A gpg key to sign packages and repository metadata with."""

    key: str
    """Fingerprint of the key."""
    gnupghome: Path | None = None
    """The keyring, gpg's default one when not given."""

    def gpg(self, *args: str) -> str:
        """Run gpg in the keyring, and return its output."""
        try:
            return process.run(["gpg", "--batch", *args], capture=True, env=self._env()).stdout
        except subprocess.CalledProcessError as e:
            msg = f"gpg {' '.join(args)}: {e.stderr.strip()}"
            raise SigningError(msg) from e

    def public_key(self) -> str:
        """Return the public key, armored."""
        if not (key := self.gpg("--armor", "--export", self.key)):
            msg = f"no key {self.key} in the keyring"
            raise SigningError(msg)
        return key

    def _env(self) -> dict[str, str]:
        return {**os.environ, **({"GNUPGHOME": str(self.gnupghome)} if self.gnupghome else {})}

    def sign_rpms(self, project: Project, root: Path) -> None:
        """Sign the RPMs under `root`, in place, with rpmsign in a container, and the key through the host's gpg,
        replacing their signatures, if any.

        The container uses the restricted socket of the host's gpg agent, made for that: the secret key, in a
        keyring or an HSM, stays out of it.
        """
        if not (rpms := sorted(f.relative_to(root).as_posix() for f in root.rglob("*.rpm"))):
            return
        env = self._env()
        try:
            process.run(["gpg-connect-agent", "/bye"], capture=True, env=env)  # starts it
            socket = process.run(["gpgconf", "--list-dirs", "agent-extra-socket"], capture=True, env=env).stdout.strip()
        except subprocess.CalledProcessError as e:
            msg = f"no gpg agent: {e.stderr.strip()}"
            raise SigningError(msg) from e
        script = (
            "export GNUPGHOME=/tmp/gnupg && mkdir -m 700 $GNUPGHOME && echo no-autostart > $GNUPGHOME/gpg.conf"
            " && ln -s /gpg-agent $GNUPGHOME/S.gpg-agent && gpg --batch -q --import /work/key.asc"
            f" && xargs -a /work/rpms rpmsign --resign --define '_gpg_name {self.key}'"
        )
        with project.tmp_dir("sign") as work:
            (work / "key.asc").write_text(self.public_key())
            (work / "rpms").write_text("".join(f"/signed/{rpm}\n" for rpm in rpms))
            try:
                container.run(
                    project.on_host(),  # natively: the signatures do not depend on the architecture
                    "sign",
                    script,
                    volumes=[f"{root}:/signed", f"{work}:/work:ro", f"{socket}:/gpg-agent"],
                    show=f"signing {len(rpms)} packages",
                    image=side_image(project.on_host(), "sign"),
                )
            except ContainerError as e:
                msg = f"cannot sign the packages, see {project.state / 'logs/_sign.log'}"
                raise SigningError(msg) from e

    def clearsign(self, file: Path, signed: Path) -> None:
        """Write `file` signed in clear, as `signed`: its text, then its armored signature."""
        signed.unlink(missing_ok=True)
        self.gpg("--local-user", self.key, "--clearsign", "--output", str(signed), str(file))

    def sign_file(self, file: Path) -> Path:
        """Write the armored detached signature of `file`, as ``<file>.asc``, and return it."""
        signature = file.with_name(f"{file.name}.asc")
        signature.unlink(missing_ok=True)
        self.gpg("--local-user", self.key, "--armor", "--detach-sign", "--output", str(signature), str(file))
        return signature


def signed_by(public_key: str, fingerprint: str, file: Path, signature: Path) -> bool:
    """Return whether `signature` is a valid detached signature of `file` by the key `fingerprint`, of `public_key`,
    checked in a keyring of its own, with that key only.
    """
    with tempfile.TemporaryDirectory(prefix="xcraft-verify-") as tmp:
        keyring = Path(tmp) / "keys"
        keyring.mkdir(mode=0o700)
        (Path(tmp) / "key.asc").write_text(public_key)
        verifier = Signer(key="", gnupghome=keyring)
        try:
            verifier.gpg("--import", str(Path(tmp) / "key.asc"))
            status = verifier.gpg("--status-fd", "1", "--verify", str(signature), str(file))
        except SigningError:
            return False
    return any(
        fingerprint.upper() in (line.split()[2], line.split()[-1])  # the signing key, then the primary one
        for line in status.splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
    )


def release_signer(project: Project) -> Signer | None:
    """Return the signer of the published packages, from the configuration, if any."""
    if not (sign := project.config.sign):
        return None
    return Signer(key=sign.key, gnupghome=Path(sign.gnupghome).expanduser() if sign.gnupghome else None)


def dev_keyring(project: Project) -> Path:
    """Return the keyring of the developer's key for development builds."""
    return project.state / "key"


def dev_signer(project: Project) -> Signer | None:
    """Return the signer of development builds, with the developer's key, when there is one."""
    keyring = dev_keyring(project)
    if not (keyring / "fingerprint").exists():
        return None
    return Signer(key=(keyring / "fingerprint").read_text().strip(), gnupghome=keyring)


def key_signer(project: Project, key: str | None = None) -> Signer | None:
    """Return the signer of `key`, in the keyring of [sign], or else the one of [sign], or else the developer's."""
    signer = release_signer(project)
    if key:
        return Signer(fingerprint(key), signer.gnupghome if signer else None)
    return signer or dev_signer(project)


def build_signer(project: Project, key: str | None, *, final: bool) -> Signer:
    """Return the key signing the builds: `key`, or the default one, see key_signer(), or else, unless `final`, the
    developer's key, made now, as the development builds are never published by others.
    """
    if signer := key_signer(project, key):
        return signer
    if final:
        msg = "no key to sign the final builds: run xcraft key create, configure [sign], or use --key"
        raise SigningError(msg)
    return new_dev_key(project)


def new_dev_key(project: Project) -> Signer:
    """Make the developer's key, for the builds of dev mode and their publications, when there is none, and return
    it, telling it.
    """
    signer = create_dev_key(project, f"xcraft developer of {getpass.getuser()}@{socket.gethostname()}")
    console.print(
        f"[yellow]made the developer's key {signer.key}[/], in {dev_keyring(project)}, signing the builds: trusted by"
        " nobody, it only tells they were made on this machine; use --key or \\[sign] for another key"
    )
    return signer


def run_signer(project: Project) -> Signer:
    """Return the key signing the builds of the command: its own, see build_signer(), or else the one of dev mode."""
    if not project.signer:
        project.signer = build_signer(project, None, final=False)
    return project.signer


def create_dev_key(project: Project, name: str) -> Signer:
    """Make the developer's key for development builds, `name` being its user id, and return its signer."""
    keyring = dev_keyring(project)
    if (keyring / "fingerprint").exists():
        msg = f"there is already a key in {keyring}"
        raise SigningError(msg)
    keyring.mkdir(parents=True, exist_ok=True, mode=0o700)
    signer = Signer(key="", gnupghome=keyring)
    # without passphrase: only for test hosts
    signer.gpg("--passphrase", "", "--quick-generate-key", name, "ed25519", "sign", "never")
    fingerprints = [
        line.split(":")[9] for line in signer.gpg("--with-colons", "--list-secret-keys").splitlines()
        if line.startswith("fpr:")
    ]  # fmt: skip
    signer.key = fingerprints[0]
    (keyring / "fingerprint").write_text(f"{signer.key}\n")
    return signer


def repo_keys(project: Project) -> list[str]:
    """Return the URLs of the keys of the repositories of xcraft.toml."""
    return sorted({key for repo in project.config.repos.values() for key in (repo.gpgkey or "").split()})


def _fingerprints(key: Path, work: Path) -> set[str]:
    """Return the fingerprints of the primary keys of the file `key`, read by gpg in a keyring of `work`."""
    (home := work / "gnupg").mkdir(mode=0o700, exist_ok=True)
    try:
        listing = process.run(
            ["gpg", "--batch", "--homedir", str(home), "--show-keys", "--with-colons", str(key)], capture=True
        ).stdout
    except subprocess.CalledProcessError as e:
        msg = f"cannot read the key {key.name}: {e.stderr.strip()}"
        raise SigningError(msg) from e
    fingerprints, record = set(), ""
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] == "fpr" and record == "pub":
            fingerprints.add(fields[9])
        if fields[0] in ("pub", "sub"):
            record = fields[0]
    return fingerprints


def key_paths(project: Project, work: Path) -> list[str]:
    """Return the paths of the keys of the repositories of xcraft.toml in the containers: those of files of the image
    as they are, the others in `work`, mounted on /work, as the checks have no network: from the cache, or else
    downloaded, then cached, once checked against the fingerprints of their repositories, as they are then.
    """
    pinned: dict[str, set[str]] = {}
    for repo in project.config.repos.values():
        for url in (repo.gpgkey or "").split():
            pinned.setdefault(url, set()).update(repo.fingerprints)
    paths = []
    for i, url in enumerate(repo_keys(project)):
        if url.startswith("file://"):
            paths.append(url.removeprefix("file://"))
            continue
        key = work / f"key-{i}.asc"
        cached = project.state / "cache/keys" / hashlib.sha256(url.encode()).hexdigest()
        if cached.exists() and (found := _fingerprints(cached, work)) and not found - pinned[url]:
            shutil.copyfile(cached, key)
        else:
            process.download(url, key)
            if not (found := _fingerprints(key, work)) or found - pinned[url]:
                keys = ", ".join(sorted(found)) or "none"
                msg = f"{url} gives the keys {keys}, not those of fingerprints in xcraft.toml"
                raise SigningError(msg)
            cached.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(cached, key.read_text())
        paths.append(f"/work/{key.name}")
    return paths


def check_signatures(
    project: Project,
    root: Path,
    files: list[str],
    *,
    what: str,
    public_key: str | None = None,
    digests: dict[str, str] | None = None,
) -> None:
    """Check the packages `files`, relative to `root`, are signed with `public_key`, or the keys of the repositories
    of xcraft.toml, in the base image, rather than the build image, which installs the pinned packages without them,
    and is made of them. The SHA-256 of the files known already, `digests`, are not computed again.
    """
    if not files:
        return
    # all: a package without signature fails too
    check = "xargs -a /work/packages rpmkeys --checksig --define '_pkgverify_level all'"
    native = project.on_host()  # the checks do not depend on the architecture
    with project.tmp_dir("signatures") as work:
        if public_key:
            (work / "key.asc").write_text(public_key)
            imported = ["/work/key.asc"]
        else:
            imported = key_paths(project, work)
        # the keys themselves, which their URLs may give other ones of: those of the image are of its tag
        keys = [sha256_of(work / k.removeprefix("/work/")) if k.startswith("/work/") else k for k in imported]
        image = container.base_image(native)
        context = hashlib.sha256(json.dumps([image, check, keys]).encode()).hexdigest()
        cache = project.state / "cache/checks/signatures" / context
        known, digests = bool(digests), digests or {f: sha256_of(root / f) for f in files}
        if not (files := [f for f in files if not (cache / digests[f]).exists()]):
            return
        (work / "packages").write_text("".join(f"/checked/{f}\n" for f in files))
        db = "--define '_dbpath /tmp/keys'"
        importing = f"rpmkeys {db} --import {' '.join(imported)} && " if imported else ""
        check = f"mkdir /tmp/keys && {importing}{check} {db}"
        try:
            container.run(
                native,
                "signatures",
                f"{check} > /work/checks 2>&1",
                volumes=[f"{root}:/checked:ro", f"{work}:/work"],
                show=f"checking the signatures of the {what}",
                image=image,
            )
        except ContainerError:
            for line in (work / "checks").read_text().splitlines():
                if not line.endswith(": digests signatures OK"):
                    console.print(f"[red]✗[/] {escape(line.removeprefix('/checked/'))}")
            msg = f"some {what} are not signed with {'their key' if public_key else 'the keys of the repositories'}"
            raise SigningError(msg) from None
    cache.mkdir(parents=True, exist_ok=True)
    for file in files:
        if known or sha256_of(root / file) == digests[file]:
            (cache / digests[file]).touch()
