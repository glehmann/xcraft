# Design: Secure Boot signing

Status: proposed. The repacking was tried by hand on XCP-ng's grub, shim and kernel; nothing is implemented in
xcraft.

## Context and goals

xcraft will sign the binaries that UEFI Secure Boot verifies when it publishes a snapshot, by unpacking the selected
RPMs, signing their files and rebuilding them with their original metadata. This lets XCP-ng hosts boot with Secure
Boot enabled without changing the specs or giving the builds access to the keys.

Goals:

- Sign the EFI binaries (shim's MokManager and fallback, grub, Xen), the dom0 kernel image and the kernel modules of
  the packages listed in `xcraft.toml`.
- Keep the Secure Boot keys out of the builds, like the GPG key today: only the publishing step uses them, through an
  HSM or a host-side agent.
- Keep the signed packages identical to the built ones but for the signed files: same name, version, source RPM,
  build date and host, scriptlets and dependencies.
- Make the signed packages fetchable and cached like the GPG-signed builds of today.

Non-goals:

- Secure Boot of guest VMs (edk2, varstored, the dbx data of `base/varstored`): a separate topic.
- Signing inside the specs at build time, as Fedora does with `%pesign`.
- Measured boot and TPM attestation.

## Boot chain

Only shim needs Microsoft's signature; xcraft signs every later stage with the XCP-ng key, whose CA certificate shim
embeds.

```
UEFI firmware   db: Microsoft UEFI CA 2023, provided by the hardware vendor
  │             verifies shim with Microsoft's CA
shimx64.efi     signed by Microsoft after shim-review, embeds the XCP-ng CA
  │             verifies the next stage with the XCP-ng CA, db or the MOK list
grub            grubx64.efi, gcdx64.efi, signed by xcraft with the XCP-ng key
  │             verifies Xen through shim (boot path open)
Xen             xen-4.21.2-3.efi, signed by xcraft with the XCP-ng key
  │             verifies the dom0 kernel through shim, locks down
dom0 kernel     vmlinuz-6.12.0 of kernel-xcpng, signed by xcraft with the XCP-ng key
  │             trusts the XCP-ng certificate, locks down
modules         1869 .ko files, and out-of-tree ones, signed by xcraft with sign-file
```

The firmware trusts only Microsoft's CA, so shim is the bridge to XCP-ng's own CA. Whether grub or shim loads Xen is
still open.

## Experiment

`rpmrebuild` can sign and repack XCP-ng's packages while keeping the metadata that matters: all four tested packages
built, passed `rpm -K` and `rpm -V`, and their signed binaries passed `sbverify`. It was tested on 2026-11-25 with
`rpmrebuild` 2.20 from EPEL 10, in an AlmaLinux 10 container, with a throwaway self-signed certificate.

| Package | Signed with | Files that differ |
| --- | --- | --- |
| `grub2-efi-x64` 2.12-21+b2 | `sbsign`: `grubx64.efi` | only that file |
| `grub2-efi-x64-cdboot` 2.12-21+b2 | `sbsign`: `gcdx64.efi` | only that file |
| `shim-x64` 16.1-4.el10.alma.1 (`x86_64_v2`) | `sbsign`: 5 EFI binaries | only those 5 |
| `kernel-xcpng` 6.12.0-9+b1 | `sbsign`: `vmlinuz`; `sign-file`: 1869 modules | only those 1870 |

What is kept, and how:

- **Source RPM name:** the spec `rpmrebuild` generates is filtered to make the package a subpackage (`%package -n`)
  of a main package named after its source, which is never built. Packages already named like their source, like
  `kernel-xcpng`, are left as they are.
- **Build date and host:** `SOURCE_DATE_EPOCH` set to the original build time, with
  `use_source_date_epoch_as_buildtime`, and `_buildhost`.
- **Payload and flags:** `_binary_payload` and `optflags` read from the original header; `_build_id_links none` so
  that no build-id links are added.
- **Dependencies:** `AutoReqProv: no` in the subpackage, otherwise `kmod()` provides are generated twice.
- **mtimes and `%ghost` sizes:** restored from the original header after signing.
- **`x86_64_v2` packages:** `rpmrebuild` wraps `rpmbuild` in `setarch x86_64_v2`, which fails; a `setarch` that only
  runs its command fixes it, as `rpmbuild` builds that arch natively.

What still differs, none of it affecting installation:

- Dependency qualifiers: `Requires(posttrans)` becomes `Requires`, which is stricter; `config()` deps lose their
  `config` type; interpreter requires on `/bin/sh` appear twice.
- File flags: `%artifact` on the build-id links and `%license` are lost; both can be restored by the spec filter.
- Regenerated tags: the per-file dependency maps, the file classes and `SOURCEPKGID`; `PLATFORM` becomes
  `x86_64_v2-redhat-linux-gnu` for shim.

Rewriting `rpmrebuild` would not remove these differences: they come from `rpmbuild`. Only rewriting the RPM in place
would, which was ruled out because of the header's immutable region.

## Design in xcraft

The Secure Boot signing is one more step of `cache.signed_build()`, before the GPG signing it already does: the
packages listed in a new `[secureboot]` section are repacked with their files signed, then all packages are
GPG-signed as today.

### Configuration

```toml
[secureboot]
certificate = "keys/xcp-ng-secureboot.crt"   # public, in the repository
key = "pkcs11:token=xcp-ng;object=secureboot"  # an HSM, or a PEM file outside the repository

[secureboot.packages]
grub2-efi-x64 = ["/boot/efi/EFI/*/grubx64.efi"]
grub2-efi-x64-cdboot = ["/boot/efi/EFI/*/gcdx64.efi"]
xen-hypervisor = ["/boot/xen-*.efi"]
kernel-xcpng = { pe = ["/boot/vmlinuz-*"], modules = ["/lib/modules/*/**/*.ko"] }
```

- A `pydantic` model next to `Sign` in `project.py`, with `extra="forbid"`.
- Each glob must match at least one regular file of its package, or the publication fails: a renamed file must not
  ship unsigned silently.
- `pe` files are signed with `sbsign`, `modules` with the kernel's `sign-file`. A plain list means `pe`.

### Where it runs

1. `cache.signed_build()` copies the build, as today.
2. For each configured package of the build, xcraft repacks it in a container of the host image (`project.on_host()`,
   a side image with EPEL's `rpmrebuild`, `sbsigntools` and `rpm-build`):
    1. `rpmrebuild -p -s` generates the spec only; xcraft post-processes it in Python (subpackage of the source name,
       `AutoReqProv: no`, `%license`, `%artifact` and the `posttrans` qualifiers restored from the original header).
    2. `rpm2cpio` unpacks the payload; the configured files are signed; the mtimes and `%ghost` sizes of the original
       header are restored.
    3. `rpmbuild -bb` rebuilds it with the defines of the experiment.
    4. `sbverify` and `modinfo -F signer` check every signed file.
3. `sign_rpms()` GPG-signs all the packages of the copy, as today.

Using `rpmrebuild` only for the spec, and running the rest from xcraft, avoids its failing final lookup with two
packages in a spec, its `setarch` wrapping, and its silent success when the change script signs nothing.

### Keys

- The key never enters a build container, only the signing container, like the GPG key through the gpg agent's
  restricted socket.
- With an HSM, the container reaches it through a PKCS#11 URI: `sbsign --engine pkcs11` and `sign-file` both take
  one. A forwarded `p11-kit` socket keeps the HSM's module on the host.
- `sign-file` is a binary of the kernel's architecture: for aarch64 kernels on an x86 host, xcraft uses the
  `sign-file` of the host image's `kernel-devel`, or builds it from `scripts/sign-file.c`.

### Cache and storage

- `signed_build_dir()` and `build_path()` are keyed by the GPG fingerprint only. They gain the SHA-256 of the Secure
  Boot certificate and of the package's `[secureboot]` entry: a new key or a new glob must not reuse an old copy.
- `snapshot_key()` includes them too, so that a changed configuration makes a new snapshot.
- The repacked packages keep their names and versions: only the signed copy is ever published, as with the
  GPG-signed copies of today.

### Dev mode

- Development builds are not re-signed today, so they would ship unsigned EFI binaries. Test hosts boot them with
  Secure Boot disabled.
- To test Secure Boot itself, `xcraft key create` also makes a self-signed development certificate, which test VMs
  enroll in their UEFI `db` or as a MOK, like the developer's GPG key.

### Tests

An executable specification on a toy package of `tests/upstream/`, with scriptlets, a `%ghost`, a `%config`, a
`%license` and a small PE binary: after publishing, `rpm -qp --dump`, `--scripts`, `--triggers`, `--requires`,
`--provides` and `-qi` match the original but for the signed file, and `sbverify` accepts it.

## Keys by stage

Each stage signs with its own Secure Boot key, like the GPG keys of the [publishing
layout](publishing-layout-design.md), and only releases get the production key. A binary signed by the production key
boots on every machine trusting Microsoft's CA, whatever repository it came from, until SBAT or dbx revokes it:
signing with it is releasing.

| Stage | Bucket | Secure Boot key | Trusted by | Tested on |
| --- | --- | --- | --- | --- |
| Pull requests and developers' stages (`publish --dev`) | dev | None by default; optionally the PR or developer's test key | Machines that enrolled that test certificate | Hosts with Secure Boot off; boot-chain PRs on test VMs |
| incoming (CI on master) | main | A CI test key, in the CI secrets like the CI GPG key | Machines that enrolled the CI certificate as a MOK, or test VMs with it in `db` | CI's test hosts and VMs |
| testing and stable (releases) | release | The production key, in the HSM, next to the release GPG key | Every machine with Microsoft's CA, through our shim | Users |

- **Releases:** a release already signs incoming's builds again with the release GPG key; the Secure Boot signing
  happens at that same step, with the HSM key. testing gets it too, as its users boot on stock firmware.
- **incoming:** published like the releases, to exercise what they use, but with a CI key whose certificate does not
  chain to the production CA embedded in shim. Otherwise a leaked CI key would boot anything on every XCP-ng host.
- **Pull requests and developers:** `publish --dev` signs nothing today, so these stages ship unsigned EFI binaries
  and modules, tested with Secure Boot off. A pull request touching grub, Xen, the kernel or `[secureboot]` can opt in
  to signing with the PR or developer's key, at the cost of a repack per publication.

How test machines trust a test certificate:

- **MOK:** `mokutil --import` on a running host, confirmed in MokManager at the next boot. The Microsoft-signed shim
  accepts it, so the chain tested is the production one but for its last certificate.
- **Test VMs:** the certificate in their UEFI `db`, through XCP-ng's custom Secure Boot certificates for guests. This
  works before we have a Microsoft-signed shim, with our own shim signed by the test key.
- **ISOs:** an incoming ISO boots with Secure Boot only where the CI certificate is trusted; on hardware, the ISO can
  carry the certificate for MokManager to enroll from the disc.

Rules for every stage:

- **Sign from the unsigned build, never from another stage's signed copy:** `sbsign` adds a signature next to the
  existing ones and `sign-file` appends another, so a release made from incoming's copy would carry the CI signature
  too. `signed_build()` copies from the build itself; the Secure Boot copies are never its input.
- **`shimx64.efi` is never signed by xcraft in a release:** the binary Microsoft signed ships unchanged in every
  stage. xcraft signs only `mmx64.efi` and `fbx64.efi` of the shim package, with the stage's key, and `shimx64.efi`
  itself only in the test stages before the Microsoft signature exists, never with the production key. See [shim
  packaging](#shim-packaging).
- **Modules follow the same keys:** the kernel trusts what the host trusts, `db` and MOK through the machine keyring,
  so a module signed by the CI key loads only on CI's test hosts.

## shim packaging

shim is packaged in two source packages, as in Fedora, RHEL and AlmaLinux: one builds it from source, the other ships
the binary Microsoft signed. Only `shimx64.efi` is signed by Microsoft; MokManager and the fallback loader are signed
by us, like grub.

| Binary | Role | Signed by | Verified by |
| --- | --- | --- | --- |
| `shimx64.efi` | First stage, with the XCP-ng CA certificate built in | Microsoft, UEFI CA 2023 | The firmware's `db` |
| `mmx64.efi` (MokManager) | Enrolls MOKs at boot | xcraft, with the stage's key | shim, with its embedded CA, `db` and the MOK list |
| `fbx64.efi` (fallback) | Recreates the boot entries | xcraft, with the stage's key | shim, the same way |

### From source to the Microsoft signature

1. **`shim-unsigned-x64`**, built by xcraft like any package: the shim 16.1 release tarball, the XCP-ng CA
   certificate as `VENDOR_CERT_FILE`, an `xcpng` entry appended to shim's SBAT. It produces the three binaries,
   unsigned. The build must be reproducible: the review gets a Dockerfile rebuilding the same `shimx64.efi` byte for
   byte, from the toolchain versions xcraft locks.
2. **shim-review**, outside xcraft: the unsigned `shimx64.efi`, its SHA-256, the build logs and the answers, in a tag
   of a shim-review fork, and an issue at rhboot/shim-review. 2 to 3 months.
3. **Microsoft submission**, once the issue is labelled accepted: the binary in a CAB file signed by the EV
   certificate, through Partner Center. Microsoft returns the same binary with its signature added.
4. **`shim-x64`**, which compiles nothing:
    - its sources are the Microsoft-signed `shimx64.efi`, stored with the spec and pinned by its SHA-256, and a
      `BuildRequires` on `shim-unsigned-x64` for `mmx64.efi` and `fbx64.efi`;
    - its `%build` checks the signature against Microsoft's UEFI CA 2023 certificate with `sbverify`, and that the
      binary without it (`sbattach --remove`) is identical to the reviewed `shimx64.efi` of `shim-unsigned-x64`: what
      ships is tied to what was reviewed;
    - it installs `shimx64.efi` unchanged, and as `BOOTX64.EFI`, next to `mmx64.efi` and `fbx64.efi`.

AlmaLinux's `shim-16.1-4.el10.alma.1.src.rpm`, in our lock today, is such a package: it embeds AlmaLinux's
certificate, hence our own.

### What xcraft signs

```toml
[secureboot.packages]
shim-x64 = ["/boot/efi/EFI/*/mmx64.efi", "/boot/efi/EFI/BOOT/fbx64.efi"]
```

The globs must not match `shimx64.efi`: a signature added next to Microsoft's would not break it, but it must ship as
Microsoft signed it.

### Through the stages

- **With a Microsoft-signed shim:** every stage ships the same `shim-x64`. Only the key signing grub, Xen, the kernel,
  the modules, `mmx64.efi` and `fbx64.efi` differs by stage, and test certificates are trusted through a MOK or a test
  VM's `db`.
- **Before it, during the review:** the test stages use the unsigned `shimx64.efi` of `shim-unsigned-x64`, signed by
  xcraft with the CI or developer's key, on test VMs whose `db` holds that certificate. The only case where xcraft
  signs `shimx64.efi`, never with the production key.
- **When shim changes:** a shim release, a shim CVE, an SBAT bump of shim itself or a new CA means a new
  `shim-unsigned-x64`, a new review and a new Microsoft signature, months ahead. grub, Xen and kernel updates need
  none of it: their SBAT bumps only change grub's entries.
- **aarch64:** `shimaa64.efi` follows the same path, with its own review tag and Microsoft signature.

## External requirements

The xcraft work is the small part: booting XCP-ng on stock hardware needs an XCP-ng shim signed by Microsoft, which
takes a public review of 2 to 3 months, an HSM, an EV certificate and kernel and grub changes.

| What | Who provides it | Notes |
| --- | --- | --- |
| XCP-ng Secure Boot CA and signing key | Vates | RSA 4096 or more for a new certificate embedded in shim. The CA is embedded in shim as its vendor certificate; the key signing grub, Xen, the kernel and the modules chains to it. |
| HSM | Vates | Microsoft requires the private key in a hardware module of at least FIPS 140-2 Level 2, with backups and recovery under dual-factor authorization in a physically secured place. xcraft reaches it through PKCS#11. |
| EV code signing certificate | A CA, for Vates | Signs the submission in the Microsoft Partner Center (Hardware Dev Center) account; its company name must match the account's verified name. Recommended for shim, required to submit. |
| Partner Center account | Microsoft, for Vates | Where the shim is submitted for signing once the review accepts it. |
| shim-review acceptance | rhboot/shim-review reviewers | A public issue with the reproducible build (a Dockerfile), the build logs, the SBAT entries, the embedded certificates, the key protection and two security contacts verified by PGP. 2 to 3 months. |
| shim signed by Microsoft UEFI CA 2023 | Microsoft | Since 2026-06-27, new shims are signed only by the 2023 CA. Hosts whose firmware `db` lacks it must get it from their vendor's firmware or a `db` update. |
| SBAT entries | XCP-ng packaging | An `xcpng` entry appended to the SBAT of shim and grub, with grub at SBAT generation 5; bumped to revoke a vulnerable release. |

What the review asks of the boot chain, which XCP-ng's packages must meet before submitting:

- **shim:** built from the 16.1 release tarball (15.8 at least for Microsoft), reproducibly, with the vendor
  certificate; NX-compatible (`NX_COMPAT`, no section both writable and executable, 4 KB alignment) if the NX bit is
  set; a signed SPDX SBOM in a `.sbom` section.
- **grub:** the fixes of all the GRUB CVE series up to February 2025, upstream `shim_lock` verification, only built-in
  modules in the signed image. Microsoft does not sign grub itself, as it is GPLv3: shim verifies it with the vendor
  certificate.
- **kernel:** the upstream lockdown commits, lockdown enforced when Secure Boot is on, and module signatures enforced.
  Today's `kernel-xcpng` has `CONFIG_LOCK_DOWN_KERNEL_FORCE_NONE` and an empty `CONFIG_SYSTEM_TRUSTED_KEYS`: it must
  trust the XCP-ng certificate, embedded or through the machine keyring, and lock down under Secure Boot.
- **modules:** the review asks whether module keys are ephemeral, or how a module of one kernel is kept from loading
  into another. Publish-time signing uses a persistent key, so the answer must be a per-release key, or the kernel
  version in the module's signature checked.
- **Xen:** anything shim launches must refuse unsigned code. Xen must verify the dom0 kernel through shim's protocol
  and lock down. The review mostly knows grub and systemd-boot, so a Xen chain will need explaining.
- **Revocation:** a `vendor_dbx` strategy and the hashes of older shims if a certificate is reused.

The review also exempts the shim from Microsoft's yearly OCP SAFE audit, as long as it only loads open-source boot
loaders.

Sources (opened 2026-11-25): [rhboot/shim-review](https://github.com/rhboot/shim-review) and its [review
template](https://github.com/rhboot/shim-review/blob/main/README.md); [Microsoft UEFI Signing
Requirements](https://techcommunity.microsoft.com/blog/hardware-dev-center/updated-uefi-signing-requirements/1062916),
version 9.0, effective 2025-10-20; [Act now: Secure Boot certificates expire in June
2026](https://techcommunity.microsoft.com/blog/windows-itpro-blog/act-now-secure-boot-certificates-expire-in-june-2026/4426856).

## Open questions

- Boot path under Secure Boot: shim loading `xen.efi` directly, or grub loading Xen with multiboot2? grub's
  `shim_lock` verifies Linux kernels, and its lockdown may refuse multiboot2.
- Does Xen 4.21 verify the dom0 kernel through shim and lock down when Secure Boot is on, or do we need the patches
  XenServer carries?
- One key for the EFI binaries and the modules, or a separate module key per kernel release, to answer the review's
  cross-kernel question?
- Which HSM, and where does the publishing run: CI, or a dedicated signing host?
- A Secure Boot key by stage in `xcraft.toml`, or by run? The GPG key is chosen by run, `--key`, `$XCRAFT_KEY`,
  `[sign]` or the developer's key, as CI and the release manager reach theirs differently, from its secrets or a
  hardware token. The Secure Boot key could be too: `--secureboot-key`, a PKCS#11 URI or a PEM file, which also gives
  the certificate, or `$XCRAFT_SECUREBOOT_KEY`, or a default `[secureboot] key`, and no Secure Boot signing without
  any; `[secureboot.packages]` stays the same for all stages. A setting by stage would then be a guard only: the
  SHA-256 of the certificate expected, `[publish.stages.<stage>] secureboot_certificate`, refusing a production key on
  incoming or a test key on stable. The GPG key could get the same guard.
- aarch64: same chain with `shimaa64.efi` and `grubaa64.efi`, and the same review?

## Plan

1. **xcraft repacking**, with a self-signed certificate: the `[secureboot]` section, the repack step in
   `signed_build()`, the cache keys, the toy-package specification. Needs no external input.
2. **Kernel and Xen changes:** trust the XCP-ng certificate, enforce lockdown and module signatures under Secure Boot;
   settle the Xen boot path.
3. **Test hosts and VMs** booting the self-signed chain, with the certificate enrolled in their `db` or as a MOK.
4. **Keys:** the CA and signing key generated in the HSM, the EV certificate, the Partner Center account. Can start
   now: it takes the longest to procure.
5. **shim:** a reproducible XCP-ng shim build with the vendor certificate and SBAT, then the shim-review submission
   (2 to 3 months), then the Microsoft submission.
6. **Production:** `[secureboot]` pointed at the HSM key, and the Microsoft-signed shim shipped as is.
