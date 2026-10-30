"""Signing our packages, with the host's gpg, and rpmsign in a container.

The key never gets near the builds, which run the specs' code: only the host's
gpg uses it, so it can be in an HSM, through gpg. Development builds are signed with the developer's
own key, which xcraft makes in its own keyring, without passphrase: it is only
for test hosts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft import process
from xcraft.container import ContainerError, image_tag, run, signing_image
from xcraft.display import console
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from xcraft.project import Project


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
        """Sign the RPMs under `root`, in place, with rpmsign in a container, and the key through the host's gpg.

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
            f" && xargs -a /work/rpms rpmsign --addsign --define '_gpg_name {self.key}'"
        )
        with project.work("sign") as work:
            (work / "key.asc").write_text(self.public_key())
            (work / "rpms").write_text("".join(f"/signed/{rpm}\n" for rpm in rpms))
            try:
                run(
                    project,
                    "sign",
                    script,
                    volumes=[f"{root}:/signed", f"{work}:/work:ro", f"{socket}:/gpg-agent"],
                    show=f"signing {len(rpms)} packages",
                    network=False,
                    image=signing_image(project),
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


def release_signer(project: Project) -> Signer | None:
    """Return the signer of the published packages, from the configuration, if any."""
    if not (sign := project.config.sign):
        return None
    return Signer(key=sign.key, gnupghome=Path(sign.gnupghome).expanduser() if sign.gnupghome else None)


def fingerprint(key: str) -> str:
    """Return the fingerprint `key`, of 40 hexadecimal digits, in uppercase, like the storage has the public keys."""
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", key):
        msg = f"not the fingerprint of a key, of 40 hexadecimal digits: {key}"
        raise SigningError(msg)
    return key.upper()


def signing_key(project: Project, key: str | None = None) -> Signer | None:
    """Select an explicit key, the configured release key, or the checkout's developer key."""
    signer = release_signer(project)
    if key:
        return Signer(fingerprint(key), signer.gnupghome if signer else None)
    return signer or dev_signer(project)


def dev_keyring(project: Project) -> Path:
    """Return the keyring of the developer's key for development builds."""
    return project.state / "key"


def dev_signer(project: Project) -> Signer | None:
    """Return the signer of development builds, with the developer's key, when there is one."""
    keyring = dev_keyring(project)
    if not (keyring / "fingerprint").exists():
        return None
    return Signer(key=(keyring / "fingerprint").read_text().strip(), gnupghome=keyring)


def create_dev_key(project: Project, name: str) -> Signer:
    """Make the developer's key for development builds, `name` being its user id, and return its signer."""
    keyring = dev_keyring(project)
    if (keyring / "fingerprint").exists():
        msg = f"there is already a key in {keyring}"
        raise SigningError(msg)
    keyring.mkdir(parents=True, exist_ok=True, mode=0o700)
    signer = Signer(key="", gnupghome=keyring)
    # without passphrase: only for test hosts
    signer.gpg("--passphrase", "", "--quick-generate-key", name, "rsa4096", "sign", "never")
    fingerprints = [
        line.split(":")[9] for line in signer.gpg("--with-colons", "--list-secret-keys").splitlines()
        if line.startswith("fpr:")
    ]  # fmt: skip
    signer.key = fingerprints[0]
    (keyring / "fingerprint").write_text(f"{signer.key}\n")
    return signer


def check_signatures(
    project: Project, root: Path, files: list[str], *, what: str, public_key: str | None = None
) -> None:
    """Check the packages `files`, relative to `root`, are signed with `public_key`, or the keys of the build image.

    The build image has those of the upstream repositories, which dnf checks when installing, not when downloading.
    """
    if not files:
        return
    # all: a package without signature fails too
    check = "xargs -a /work/packages rpmkeys --checksig --define '_pkgverify_level all'"
    context = hashlib.sha256(json.dumps([image_tag(project), check, public_key]).encode()).hexdigest()
    cache = project.state / "cache/checks/signatures" / context
    digests = {f: sha256_of(root / f) for f in files}
    files = [f for f in files if not (cache / digests[f]).exists()]
    if not files:
        return
    with project.work("signatures") as work:
        (work / "packages").write_text("".join(f"/checked/{f}\n" for f in files))
        if public_key:
            (work / "key.asc").write_text(public_key)
            db = "--define '_dbpath /tmp/keys'"
            check = f"mkdir /tmp/keys && rpmkeys {db} --import /work/key.asc && {check} {db}"
        try:
            run(
                project,
                "signatures",
                f"{check} > /work/checks 2>&1",
                volumes=[f"{root}:/checked:ro", f"{work}:/work"],
                show=f"checking the signatures of the {what}",
                network=False,
            )
        except ContainerError:
            for line in (work / "checks").read_text().splitlines():
                if not line.endswith(": digests signatures OK"):
                    console.print(f"[red]✗[/] {escape(line.removeprefix('/checked/'))}")
            msg = f"some {what} are not signed with {'their key' if public_key else 'the keys of the repositories'}"
            raise SigningError(msg) from None
    cache.mkdir(parents=True, exist_ok=True)
    for file in files:
        if sha256_of(root / file) == digests[file]:
            (cache / digests[file]).touch()
