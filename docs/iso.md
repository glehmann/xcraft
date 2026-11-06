# Installation ISO

`xcraft iso` makes the installation ISO of the distribution, from a snapshot like `xcraft publish --local` makes, of
its architecture only, with nothing else to install, like
[create-install-image](https://github.com/xcp-ng/create-install-image) does for XCP-ng:

- `install.img`, the installer's root filesystem, is installed from the snapshot: its packages, `[iso] installer` in
  `xcraft.toml`, are published with the others. The firmware files no driver names are removed, and the files of
  `iso/installimg/` of the project are added, like its systemd units;
- the boot files are its kernel, grub's image for CDs and its modules, from `grub2-efi-x64-cdboot` and
  `grub2-efi-x64-modules` of the snapshot, for UEFI, and isolinux, for BIOS, configured by the files of `iso/tree/`;
- its repository has `[iso] packages`, like `xcp-ng-deps`, and what they need, signed like the snapshot, unless
  `--netinstall`, where the installer gets the packages from the network;
- it is hybrid: it boots with BIOS and UEFI, from a CD or a USB stick.

What is particular to the distribution, like the installer, Xen or the branding of XCP-ng, is made by the hook of the
project, the executable `iso/hook`, which xcraft runs as root in the build environment, on `install.img` then on the
tree of the ISO: see `xcraft/buildenv/iso.py` for its arguments. `[iso]` names the ISOs:

```toml
[iso]
name = "xcp-ng"
version = "8.99.0"
volid = "XCP-NG_899"  # the name and version, in capitals, with underscores, by default
```

The tools making it, like xorriso and syslinux, are installed in the containers making it, from the packages pinned
for them in the lock, in `iso`, resolved with the build roots. The ISO is `.xcraft/iso/<name>-<version>-<id>-<arch>[-netinstall].iso` by default:
`<arch>` is the RPM architecture, like `x86_64_v2`, and `<id>`, like `4772c1`, identifies what the ISOs are made of,
the same for the full and netinstall ones of all the architectures: the beginning of a hash of their keys, which
cover the packages, the lock and the ISO's configuration.
Images are cached in `.xcraft/cache/isos/<arch>/<key>/`, which those of `.xcraft/iso/` are hard links to, rather than
copies; `xcraft clean` removes them but those of the checkout: full and netinstall, signed with the default key, with
those of the builds of the storage, or unsigned, of final builds or of the development ones. Both `xcraft iso` and `xcraft boot` reuse the matching local
image, then try the trusted remote storage, and only assemble a snapshot and build an ISO on a cache miss. A cache
hit needs no build containers or signing secrets. The key covers package inputs and the whole of both locks, on all
the architectures, as the build keys do, read before the build IDs are computed again, ISO templates and
configuration, the build recipe and its format, architecture, netinstall and volume-id options, and signing identity.
Local edits invalidate it; the publish stage, remote credentials and output path do not affect it.

Like `xcraft build`, `xcraft iso` and `xcraft boot` take the development builds of the packages whose build ID is not
the main branch's, see [Development builds](builds.md#development-builds), with `--dev ID`, or final builds with `--final`: the
ISO is the one of what `xcraft build` just made, without building them again as final ones. Its key covers the
suffixes of the development builds, and its name has their identifier, like `<name>-<version>-<id>.dev-<arch>.iso`;
it is never taken from the remote storage, nor pushed to it, like them. Of final builds, without `--key` nor
`--unsigned`, when the ISO signed with the default key, like the developer's, is neither cached nor in the remote
storage, the one CI published, signed with a key of `build_keys` of the storage, is taken rather than made.

`xcraft push isos` uploads cached images matching the checkout, with signed manifests like RPM builds. Use
`xcraft push isos --unsigned` for images built with `--unsigned`; the upload manifest still needs a signing key.
Publishing ISOs also populates this keyed remote cache, independently of the dated snapshots.

Repository assembly caches successful RPM signature and installability checks locally. Signature checks are keyed
by the RPM's bytes, trusted keys and verification environment. Installability checks are keyed by the ordered
repositories' RPM contents and checker environment. Changed packages, keys or verification recipes are checked again.

`xcraft publish` publishes both ISOs, with the packages and netinstall, with the snapshot: see
[Publishing](publishing.md).

## Booting the installer locally

`xcraft boot` reuses or downloads the ISO matching the checkout, building it on a cache miss, and
boots it in QEMU/KVM. It currently supports x86_64 and x86_64_v2, with UEFI boot, 8 GiB of RAM, four virtual CPUs,
and an 80 GiB sparse qcow2 disk in `.xcraft/vm/<arch>/disk.qcow2`.

```bash
xcraft patchset /path/to/host-installer --package host-installer --base <ref>
xcraft boot --unsigned                 # reuse, fetch or build the matching ISO, then run the installer
xcraft vm start                        # boot the resulting installation, without the ISO
xcraft boot --iso /path/to/image.iso   # boot an existing ISO
```

The disk persists between runs and is never resized or recreated automatically. `xcraft boot` boots the ISO even when
the disk has an installed system, whose boot entry the UEFI firmware variables would put first: the ISO is first in
the boot order QEMU gives the firmware, with BIOS and UEFI alike. Use `--disk /path/to/new.qcow2`
for a fresh installation, with `--disk-size 100G` to change the size of a new disk. The installer VM exits when it
requests a reboot, which tells the installation finished: `xcraft boot` ends then with a ✓, and fails when the VM
stops otherwise, from the reason QEMU gives, like a power-off, or a kernel panic with the pvpanic driver of the
guest. `xcraft vm start` then boots its disk, with the options of `xcraft boot`, its firmware variables
and its SSH port, if still free; `--memory`, `--cpus` and `--ssh-port` change them. The project lock covers startup, then is released so packages
can be edited and rebuilt while the VM runs.

`--kernel-args` adds arguments to the kernel command line of the default entry of the ISO's GRUB menu, in GRUB's
editor, then waits for the kernel booted with them. `--unattended` adds those of `unattended` in the `[iso]` section of
`xcraft.toml`, like an answer file of the installer, which do not change the ISO:

```toml
[iso]
unattended = "rt_answerfile=file:///opt/xensource/installer/answerfile-vm.xml"
```

```bash
xcraft boot --unattended   # then xcraft vm start
```

The firmware boots the entries of its variables first, like the one of a previous installation: use a new session,
with `--vm-dir`, to install again.

OVMF firmware is detected in common distribution locations. Use `--firmware-code` and `--firmware-vars` together
to supply another CODE image and VARS template. The template is copied only once to the session's `OVMF_VARS.fd`;
that writable copy persists across installer and installed-disk boots. `--bios` selects legacy BIOS explicitly.

`--memory` sets RAM in MiB, and `--cpus` sets the CPU count. `--headless` disables the display window; use `xcraft vm`
for console access. `--background` also disables the window and returns once the control monitor is ready. A foreground
VM stops on Ctrl-C. The default accelerator is KVM, which needs access to `/dev/kvm`; `--accel tcg` uses slower
software emulation. With KVM, QEMU exposes the host CPU, including nested virtualization when enabled on the host.

Networking uses NAT with an emulated e1000 NIC. Once SSH is running in the installed host, connect with
`ssh -p 2222 root@127.0.0.1`, or the port `xcraft boot` shows: the first free one from 2222, as another VM may use it.
`--ssh-port` gives the forwarded localhost port, or disables forwarding with `0`.
Extra QEMU arguments can be supplied individually, like `--qemu-arg=-display --qemu-arg=gtk`.

### Controlling a session

Each session retains `session.json`, `qmp.sock`, the `serial.in` input FIFO, `serial.log`, and `qemu.log` alongside
its disk and firmware variables. Serial capture starts with QEMU and continues while no agent is connected. A new
launch archives the previous transcripts. A second launch in a running session is rejected.

```sh
xcraft boot --background --iso /path/to/image.iso
xcraft vm status                       # JSON launch details and live QEMU status
xcraft vm console --wait 'Select Keymap' --timeout 90
xcraft vm text --escape '\t\r'         # serial input, interpreting backslash escapes
xcraft vm text --enter 'some text'     # literal UTF-8 followed by Enter
xcraft vm key ctrl alt f2              # physical QEMU key-code chord
xcraft vm text --physical --enter 'tail -60 /tmp/install-log'
xcraft vm screenshot /tmp/installer.png
xcraft vm logs /tmp/installer-diagnostics
xcraft vm stop
xcraft vm start --background           # the installed disk, without the ISO
xcraft vm ssh-copy-id                  # your SSH key for root, asking its password once
xcraft vm ssh                          # a login shell in the VM, or: xcraft vm ssh rpm -qa --last
xcraft vm destroy                      # stopped, and its session deleted, with its disk: asks first, but --yes
```

`ssh` and `ssh-copy-id` reach root on the SSH port of the VM, on localhost, without checking its host key, which each
installation changes; like ssh, `ssh` gives the remote shell its command as one line, and exits with its code.

`console` renders the current 80-by-24 serial screen and waits for output stability. A wait timeout prints the screen,
saves a diagnostics bundle in the session directory, and fails. Physical typing uses the US keyboard layout, which
is useful for diagnostic shells that do not read serial input.

Use `boot --vm-dir /tmp/my-vm` for a separate session, then `xcraft vm --vm-dir /tmp/my-vm <command>` to attach,
even outside the project. Otherwise both commands select `.xcraft/vm/<arch>`; use `--arch` before the VM subcommand
to select another architecture. A custom `--disk` changes the disk path, not the session directory.

`vm logs` creates a new directory with the launch details, raw logs, rendered console, and a screenshot/status when
the VM is reachable. It also works after QEMU exits. Guest files such as `/tmp/install-log` require access inside the
guest; the `installer-vm` skill in `xcp-ng-dist` includes a retrieval helper. SSH forwarding does not enable guest SSH.
