# XCP-ng build condition, we don't ship depmod config
%bcond_with depmod

# XCP-ng: make it easier o mix and match patches
%global _default_patch_fuzz 2

Summary: XenServer Installer
Name: host-installer
Version: 11.0.26
Release: 0.ydi.25%{?dist}
License: GPLv2
Group: Applications/System
Source0: host-installer-%{version}.tar.gz

# backport feature/host-netdev-order
Patch00: 0001-CP-54081-Remove-interface-rename-sideway-service.patch
Patch01: 0002-CP-53716-Drop-ordering-and-renaming-network-interfac.patch
Patch02: 0003-CP-53716-Drop-ordering-and-renaming-network-interfac.patch
Patch03: 0004-CP-53716-Minor-refine-for-comments.patch
Patch04: 0005-CP-53840-Do-not-write-mgmt-interface-info-in-xensour.patch
Patch05: 0006-CA-410647-Displaying-select-network-interface-should.patch
Patch06: 0007-CP-54444-Remove-interface-rename-logic-for-legacy-up.patch
Patch07: 0008-CP-54444-Use-MAC-addresses-to-find-out-the-mgmt-inte.patch
Patch08: 0009-CP-54444-In-upgrade-convert-interface-rename-data-fo.patch
Patch09: 0010-CP-307931-Remove-unused-net-admin-interface-key.patch

# pr#278
Patch20: 0001-Force-switch-tty-to-UTF-8-not-ISO-8859-1.patch

# support kernel-core package name
Patch30: 0001-Parametrize-kernel-package-name.patch
#Patch31: 0002-Set-kernel-package-name-for-alma-based-pacakging.patch

# help for debug
Patch40: 0001-Raise-rpm-verbosity-to-understand-scriptlet-errors.patch

# adjustment to Alma10
Patch50: 0001-Use-EFI-almalinux.patch
Patch51: 0002-Boot-on-Alma10.patch
Patch52: 0003-Set-GRUB-prefix-to-locate-its-modules-on-Alma10.patch
Patch53: 0001-Add-an-XCP-ng-9-specific-finalization-task.patch
Patch54: 0002-Second-dnf-install-run-for-crypto-policies.patch
Patch55: 0003-Make-sure-Alma-EPEL-repositories-are-disabled-by-def.patch

# Mandatory patches from XCP-ng 8.3
Patch1000: 0001-Use-xcp-ng-deps-instead-of-groups.xml.patch

# This is where we get 'multipath.conf' from
BuildRequires: device-mapper-multipath
#BuildRequires: python-mock
BuildRequires: python3-xcp-libs

Requires: device-mapper-multipath lvm2 iscsi-initiator-utils

# partitioning tools
Requires: gdisk kpartx e2fsprogs dosfstools

# CUI interface
Requires: python3-newt newt
Requires: ethtool sdparm pciutils eject net-tools xenserver-biosdevname
Requires: python3-xcp-libs python-simplejson

# archives used
Requires: bzip2 tar gzip rpm

# For killall
Requires: psmisc

# IPv6
Requires: ndisc6

Requires: pyOpenSSL

# LINSTOR (needs repoquery)
Requires: dnf

Requires: dmv-utils

Requires(post): initscripts

# Strictly no byte compiling python. Python in the install
# environment is different to the dom0 chroot
%global __os_install_post %(echo '%{__os_install_post}' | sed -e 's!/usr/lib[^[:space:]]*/brp-python-bytecompile[[:space:]].*$!!g')

%define installer_dir /opt/xensource/installer
%define feature_flag_dir /etc/xensource/features

# we cannot change the 'almalinux' branding without rebuilding at least grub and shim
%define efi_dir /EFI/almalinux

%define large_block_capable_sr_type largeblock

%description
XenServer Installer

%package startup
Summary: XenServer Installer
Group: Applications/System
Requires: host-installer
Requires: openssh-server
Requires(post): initscripts
%description startup
XenServer installer startup files

%package bootloader
Summary: XenServer Installer
Group: Applications/System
Requires: host-installer

%description bootloader
XenServer installer bootloader files

%global debug_package %{nil}
%prep
%autosetup -p1

%build

# %check
# test/test.sh

%install
rm -rf %{buildroot}

make install DESTDIR=%{buildroot} INSTALLER_DIR=%{installer_dir} SM_ROOTDIR= \
     XS_MPATH_CONF=/usr/share/doc/device-mapper-multipath/multipath.conf
rm %{buildroot}/etc/systemd/system/systemd-udevd.d/installer.conf

# not any more ?!
# # XCP-ng: our "Boot on Alma10" patch causes upstream grub-usb patch to be fuzzy, who cares
# rm %{buildroot}/EFI/almalinux/grub-usb.cfg.orig

mkdir -p %{buildroot}/%{feature_flag_dir}
# XCP-ng: no supplemental packs feature
# touch %{buildroot}/%{feature_flag_dir}/supplemental-packs
echo %{large_block_capable_sr_type} > %{buildroot}/%{feature_flag_dir}/large-block-capable-sr-type

%files
%defattr(775,root,root,-)

# Executables
%{installer_dir}/init
%{installer_dir}/report.py
/usr/bin/support.sh

%defattr(664,root,root,775)
# Installer gubbins
%{installer_dir}/answerfile.py
%{installer_dir}/backend.py
%{installer_dir}/constants.py
%{installer_dir}/disktools.py
%{installer_dir}/diskutil.py
%{installer_dir}/dmvutil.py
%{installer_dir}/driver.py
%{installer_dir}/functions
%{installer_dir}/generalui.py
%{installer_dir}/hardware.py
%{installer_dir}/init_constants.py
%{installer_dir}/install.py
%{installer_dir}/netinterface.py
%{installer_dir}/netutil.py
%{installer_dir}/product.py
%{installer_dir}/repository.py
%{installer_dir}/restore.py
%{installer_dir}/scripts.py
%{installer_dir}/snackutil.py
%{installer_dir}/uicontroller.py
%{installer_dir}/upgrade.py
%{installer_dir}/util.py
%{installer_dir}/xelogging.py
%{installer_dir}/fcoeutil.py

# Installer UI
%{installer_dir}/tui/__init__.py
%{installer_dir}/tui/init.py
%{installer_dir}/tui/installer/__init__.py
%{installer_dir}/tui/installer/screens.py
%{installer_dir}/tui/network.py
%{installer_dir}/tui/progress.py
%{installer_dir}/tui/repo.py
%{installer_dir}/tui/fcoe.py

# Data
%{installer_dir}/common_criteria_firewall_rules
%{installer_dir}/keymaps
%{installer_dir}/timezones
%config /etc/multipath.conf.disabled
/etc/dracut.conf.d/installer.conf

# Feature Flags
%{feature_flag_dir}

%license LICENSE

%files startup
%defattr(775,root,root,-)

%{installer_dir}/preinit
%attr(755,root,root) %{installer_dir}/S05ramdisk

%defattr(664,root,root,775)
/etc/modprobe.d/*
/etc/modules-load.d/iscsi.conf

%doc

%files bootloader
%defattr(444,root,root,-)
%{efi_dir}/grub.cfg
%{efi_dir}/grub-usb.cfg
#/boot/isolinux/isolinux.cfg

%post
# these are started by the installer
/usr/bin/systemctl disable multipathd
/usr/bin/systemctl mask iscsi-shutdown.service
/usr/bin/systemctl mask iscsiuio.service
/usr/bin/systemctl mask iscsid.service
/usr/bin/systemctl mask iscsid.socket
/usr/bin/systemctl mask iscsiuio.socket
/usr/bin/systemctl mask iscsi.service

# avoid to lock disks
/usr/bin/systemctl disable lvm2-lvmetad
/usr/bin/systemctl disable lvm2-monitor

%triggerin -- kernel, kernel-alt
rm -rf /boot/*
rm -rf /usr/lib/modules/*/kernel/{drivers/infiniband,fs/{autofs4,btrfs,cramfs,exofs,gfs2,jfs,nfsd,ntfs,ocfs2,reiserfs,ufs,xfs}}

%triggerin -- xen-hypervisor
rm -rf /boot/*

# XCP-ng: disable this optimization, not aware that linux-firmware can
# install files in unreachable directories but still get used through
# symlinks
#%triggerin -- linux-firmware
#basename -a $(find /lib/modules -type f -name '*.ko' | xargs modinfo --field firmware) | sort -u >/tmp/firmware-used.$$
#for f in $(find /lib/firmware/ -type f); do
#   b=$(basename $f)
#   # keep files referenced by modinfo
#   grep -q $b /tmp/firmware-used.$$ && continue
#   # keep files referenced by modules
#   grep -qr $b /lib/modules && continue
#   rm -f $f
#done
#rm -f /tmp/firmware-used.$$

%changelog
* Tue Jul 15 2025 Yann Dirson <yann.dirson@vates.tech> - 11.0.26-0.ydi.25
- Update to v11.0.26
  - HACK restore patch fuzz tolerance to make it easier to use patch from
    different branches
  - Upstream stopped messing with depmod, follow suit (still have to remove
    systemd-udevd.d/installer.conf)
  - Upstream master does not have tests
  - Upstream dropped booting using BIOS with isolinux
  - Now ships interface-rename-sideway service
  - v11.0.12 dropped early-blacklist
  - Pick limited number of 8.3 patches for now:
    - xcp-ng-deps not packagegroups (possibly to reconsider)
  - Drop unused dependencies on python-six
- Build for Alma 10:
  - Updated deps to python3-xcp-libs
  - Explicitly disable debug_package
  - Uses dnf not yum
  - Add patch for proper console unicode display (pr#278)
  - (Do not) Add patch for kernel-core support
  - Add patch to use EFI/almalinux
  - Add patch for GRUB to be able to load its payloads
  - Add patch for GRUB to be able to load its modules
  - Add (temporary) patch to apply final workarounds to installed system
  - Add (temporary) patch to install crypto-policies after xcp-ng-deps (since its first
    attempt silently fails)
  - Disable removal of firmware files
- Imported patches from feature/host-netdev-order
  - drops interface-rename-sideway
- Add patch for debugging rpm scriptlet errors
- TEMP HACK remove dependency on device-mapper-multipath, which needs work
- TEMP HACK depend on lvm2 not xenserver-lvm2, which needs work
- TEMP HACK point to some multipath.conf so "make install" passes
- Make sure Alma/EPEL repositories on host are disabled by default
- Requires: dmv-utils

* Wed Jun 04 2025 Yann Dirson <yann.dirson@vates.tech> - 10.10.29.xcpng.3-1
- Update to v10.10.29.xcpng.3 release:
  - Don't ask repoquery to check signed repos, it cannot do that

* Wed Jun 04 2025 Yann Dirson <yann.dirson@vates.tech> - 10.10.29.xcpng.2-2
- Add missing Requires: yum-utils, necessary to handle LINSTOR upgrades

* Tue Jun 03 2025 Yann Dirson <yann.dirson@vates.tech> - 10.10.29.xcpng.2-1
- Update to v10.10.29.xcpng.2 release:
  - Be aware of LINSTOR being installed and make sure the proper version
    is available in the upgrade
  - Fix upgrade of hosts using static IPv6 for management interface

* Thu May 15 2025 Yann Dirson <yann.dirson@vates.tech> - 10.10.29.xcpng.1-1
- Update to v10.10.29.xcpng.1 release
- Reintroduced differently the "Stop messing with depmod" changes from 10.10.5.xcpng.1-1,
  now that a Makefile is used
- Sync with xenserver 10.10.29-1:
  * Tue Apr 29 2025 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.28-1
  - CA-409996: Fix manual upgrade

  * Mon Apr 07 2025 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 10.10.28-1
  - XSI-1863: Use a disk size of zero if a disk size cannot be determined
  - doc/answerfile: reduce admin-interface confusion
  - Avoid attempting to re-read inventory file from unmounted partition
  - __mkinitrd: pass -f to dracut
  - doc/answerfile: clarify and fix description of --network_device
  - Makefile: give more flexibility to select base multipath.conf
  - CP-47621: Enable ability to set hostname from DHCP

  * Wed Mar 12 2025 Deli Zhang <deli.zhang@cloud.com> - 10.10.27-1
  - CA-400058: Revert "Fix preserve-first-partition installation"
  - CA-407867: Add missed "attr" parameter to restore_file()

  * Wed Mar 05 2025 Deli Zhang <deli.zhang@cloud.com> - 10.10.26-1
  - CP-50298: Support restoring dir to specified mode and user/group attributes
  - CA-400058: Fix preserve-first-partition installation

  * Fri Dec 06 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.25-1
  - CA-403412: Do not backup GPT layout on temporary directory

  * Thu Nov 28 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.24-1
  - CP-51858: Support upgrades for SDX 8900 platform
  - CP-51857: Support clean install for SDX 8900 platform

  * Mon Jul 22 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.10.23-1
  - CA-395582: Fix installation with multipath enabled

  * Tue Jul 02 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.22-1
  - Use Makefile instead of duplicating code

  * Thu Jun 27 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.21-1
  - CA-392317: Make sure kernel is up to date using Gdisk
  - Handle corrupted GPT data if disk is using MBR

  * Mon Jun 03 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.20-1
  - CA-393429: Fix upgrade to XS8 from downgraded SDX MBR installation

* Thu Sep 26 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.19.xcpng.5-2
- Add missing dependency on pyOpenSSL

* Wed Sep 25 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.19.xcpng.5-1
- Update to v10.10.19.xcpng.5 release, bringing:
  - Forbid upgrading with a key XAPI will reject

* Thu Aug 08 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.19.xcpng.4-1
- Update to v10.10.19.xcpng.4 release, bringing:
  - Preserve stunnel certs
  - Mark 'Dual Stack' as experimental
  - Boot mode checks
  - Misc debugging improvements

* Wed Jul 10 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.19.xcpng.3-1
- Update to v10.10.19.xcpng.3 release, bringing:
  - fix usage of driver disks such as intel-igb
  - fix abusive logging at "critical" level

* Fri Jul 05 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.19.xcpng.2-1
- Update to v10.10.19.xcpng.2 release, bringing:
  - Fix installation of kernel-alt package when in kernel-alt mode

* Wed Jul 03 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.19.xcpng.1-1
- Update to v10.10.19.xcpng.1 release, bringing:
  - #151: Fix UEFI Restore
  * Thu May 23 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.19-1
  - CA-392758: Remove Firmware Boot Selected flag check
  - CP-49195: Allows to preserve first partition from MBR layout
  - CP-49641: Ignore errors mounting/unmounting explicit mount points

  * Thu May 16 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.18-1
  - CA-392935: Release device if partition does not need to be resized
  - CA-392916: Create directory for mount= option

  * Thu May 02 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 10.10.17-1
  - Better support for SDX upgrade
  - CA-391659: Set correct partition type restoring XS 7.1
  - CP-47625: Replace mkinitrd with dracut command
  - CA-391027: Mount devices specified by mount= only when needed

  * Wed Feb 28 2024 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 10.10.16-1
  - CA-389160: Filter secrets when logging results/answers during failures

* Wed Jun 12 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 10.10.6.xcpng.2-2
- Remove 0001-Prevent-upgrading-from-platform-3.4.0.patch
  - Makes upgrading from 8.3 or XenServer 8 possible again

* Fri Apr 26 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.16.xcpng.2-1
- Update to v10.10.16.xcpng.2 release, bringing:
  - Make the unsupported / for-troubleshooting status of kernel-alt more obvious

* Mon Apr 22 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.16.xcpng.1-1
- Loosely rebase packaging on XS's 10.10.16-1, bringing:
  - declaring support for 4KN-capable SR
  - run tests on package build
- Update to v10.10.16.xcpng.1 release, bringing:
  - deprecate legacy BIOS boot
  - support for upgrading from old MBR to new GPT layout
  - allow 4KN-capable SR type to be used

* Wed Feb 07 2024 Yann Dirson <yann.dirson@vates.tech> - 10.10.11.xcpng.3-3
- Prevent upgrades from XS 8 and XCP-ng 8.3 (intended for 8.3beta2 only)

* Mon Jan 22 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 10.10.11.xcpng.3-2
- Loosely rebase on XS's 10.10.11-1 SRPM: only apply relevant packaging changes.
- Sources unchanged: we keep our tarball.
- Upstream packaging change included from XenServer's SRPM:
  - Handling of feature flags

* Fri Dec 22 2023 Yann Dirson <yann.dirson@vates.fr> - 10.10.11.xcpng.3-1
- Update to v10.10.11.xcpng.3, bringing:
  - Fix regression preserving multipath configuration on upgrade

* Fri Dec 15 2023 Yann Dirson <yann.dirson@vates.fr> - 10.10.11.xcpng.2-1
- Update to v10.10.11.xcpng.2, bringing:
  - Displays Supplemental Packs dialog when Driver Disks are used
  - XSI-1498: Log any exceptions parsing existing bootloader config
  - CA-385350 Fix handling of NTP configuration over upgrade

* Mon Oct 16 2023 Yann Dirson <yann.dirson@vates.fr> - 10.10.9.xcpng.1-1
- Update to v10.10.9.xcpng.1

* Wed Jun 14 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 10.10.5.xcpng.1-2
- Loosely rebase on XS's 10.10.4-1 SRPM: only apply relevant packaging changes.
- Sources unchanged: we keep our tarball.
- Upstream packaging change included from XenServer's SRPM:
  - CP-40676: Clarify licensing

* Wed Jun 14 2023 Benjamin Reis <benjamin.reis@vates.fr> - 10.10.5.xcpng.1-1
- Update to v10.10.5.xcpng.1
- Drop our patches as they're in the tarball from our own repo now
- IPv6 support in TUI
- Local SR: make EXT the default and first option instead of LVM
- RAID: only propose creation with at least 2 disks and no RAID
- RAID support: zap partition tables of disks before building a RAID
- Show primary disk selection even if there is only one disk
- Remove supplemental pack dialog at the end of installation
- Don't install a depmod config identical to CentOS7, and stop causing
  udev service start to run "depmod -a"
- apply module-removing trigger to kernel-alt too

* Wed Nov 23 2022 Yann Dirson <yann.dirson@vates.fr> - 10.10.0-1.1
- Include a diff from v10.10.0 to v10.10.3-21-g3d5df76

* Mon Jun 06 2022 Mark Syms <mark.syms@citrix.com> - 10.10.0-1
- CP-39332: Check gpg signing on repo

* Wed Mar 09 2022 Mark Syms <mark.syms@citrix.com> - 10.9.2-1
- CP-39330: remove obsolete GPG key injection, now in release

* Wed Mar 02 2022 Mark Syms <mark.syms@citrix.com> - 10.9.1-1
- CA-364633: ensure the dir for the management file exists

* Wed Jan 05 2022 Alex Brett <alex.brett@citrix.com> - 10.9.0-1
- CP-38454: Support use of stacked repositories during installation
- CA-359850: Fix screen skipping on Set Time Manually screen

* Mon Nov 29 2021 Deli Zhang <deli.zhang@citrix.com> - 10.8.1-1
- CP-37849: Support .treeinfo new format

* Tue Aug 10 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.8.0-1
- CP-37784: Add common-criteria-prep menu entry to GRUB config
- CP-35398: Upgrade from PBIS to winbind

* Thu Jul 08 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.12-1
- CA-349118: Create modprobe files during upgrade

* Fri Jun 04 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.11-1
- CA-355255: Don't fail to install over another OS

* Tue May 04 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.10-1
- CA-353423: Upgrades should fail sooner when DOS util partition is detected

* Thu Apr 15 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.9-1
- Remove support for legacy partitions
- CP-35049: Add support for optional build numbers

* Tue Oct 06 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.8-1
- CA-343729: Log exceptions from iSCSI setup

* Mon Sep 21 2020 Ben Anson <ben.anson@citrix.com> - 10.7.7-1
- Revert "CP-34815: Force a valid hostname at all times"

* Wed Sep 16 2020 Ben Anson <ben.anson@citrix.com> - 10.7.6-1
- CP-34815: Force a valid hostname at all times
- CP-34873: remove references to genptoken services

* Fri May 29 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.5-1
- CA-337001: Prompt the user to re-init the disk if needed

* Thu Mar 26 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.4-1
- CA-336696: Handle errors when copying ownership
- CA-336693: Pass the correct parameters to setEfiBootEntry()
- CA-336693: Mount the ESP before restoring files
- CA-336693: Fix UEFI restore when there is no storage partition

* Wed Feb 05 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.3-1
- CP-31090: Add network-init as a must-run service

* Thu Jan 23 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.2-1
- CA-332901: Fix sshd service enable with cc-preparations
- CP-31092: Use different pool token generation in CC mode

* Thu Dec 12 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.1-1
- CP-32298: Don't expect firstboot state to exist

* Fri Dec 06 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.7.0-1
- Unbreak "safe" boot option on x2APIC machines
- CP-31092: Move CC preparations from firstboot script to the installer
- Remove outdated logrotate code
- CP-32298: Reimplement 95-legacy-logrotate in the installer

* Thu Nov 14 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.10-1
- CA-327217: Log efibootmgr failures when updating

* Thu Oct 31 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.9-1
- CP-32365: Remove bugtool.py
- CP-32365: Avoid logging ftp username/password in report.py
- Restore username/passwords in URL dialog
- Quote credentials correctly when saving reports
- CA-329747: Fix url-encoding issues with username/passwords
- CA-329747: Remove splitNetloc
- CA-329192: Don't reveal if username doesn't have a password
- CA-329837: Don't log the pool token

* Thu Oct 24 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.8-1
- CA-329192: Don't leak HTTP/FTP credentials

* Thu Aug 22 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.7-1
- CP-30221 / CA-325143: Switch to use chronyd for NTP
- CP-25099: Remove timeutil
- CP-25099: Set time before installation is started
- CA-293794: Prefer NTP servers from DHCP
- CP-30221: Use chrony.conf during upgrades

* Mon Aug 12 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.6-1
- CA-310853: Add check to TUI call to prevent erroneous backward jumps
- CA-322208: Ignore errors raised by deactivateAll()
- CP-32019: Rework FCoE setup code to match post-installation

* Thu Jul 18 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.5-1
- CA-322144: Dump iBFT state for debugging
- CA-323227: Fix interactive installation with multipath
- CA-322479: Prevent iSCSI session(s) getting logged out during shutdown
- CA-317858: Re-enable job control for bash-shell
- Remove xencons kernel parameter
- CA-323599: Use full "com" parameter after installation

* Mon Jun 10 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.4-1
- Improve "Installing ..." text
- CA-319463: Don't restore certain files after upgrade

* Tue May 28 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.3-1
- CA-317754: Drop unneeded legacy disk/by-id symlink handling
- CA-317755: Fix installation when iBFT has no gateway
- CA-260176: Use new DNS format for management interface config
- CA-317642: Move repo length check earlier
- Code style improvements and modernization

* Wed May 08 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.2-1
- CA-296436: Disable mcelog on unsupported processors

* Fri Apr 12 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.1-1
- CA-311401: Handle the upgrade of disks from aacraid to smartpqi

* Wed Mar 27 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.6.0-1
- CP-30566: Add support for VirtIO Blk devices
- CA-312390: Fix agetty parametrs, treat all ttys equally, fixing serial handling.
- CA-313248 Match old branding EFI boot entries

* Thu Mar 07 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.5.3-1
- CA-311773: Keep order of updates specified in the answerfile
- CA-311543: Add serial support to ISOLINUX

* Wed Feb 27 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.5.2-1
- CA-311306: Bump crash kernel memory to 256M

* Fri Feb 22 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.5.1-2
- CA-301159: Drop dependency on xenserver-systemd-networkd

* Thu Feb 14 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.5.1-1
- CA-310736: iscsi: Don't use gateway if target is on the same subnet

* Wed Feb 06 2019 jenniferhe <jennifer.herbert@citrix.com> - 10.5.0-1
- CA-306058: Don't try to restart iscsid
- CA-306058: Fix setting up routing for iSCSI NICs
- CP-30205: Remove legacy toolstack db references
- CP-29627: Increase dom0 memory for the installer
- CP-30557: Update installer to use new default_memory_for_version() function.
- CA-309041: Apply updated dom0 memory size during upgrade

* Tue Jan 15 2019 rossla <ross.lagerwall@citrix.com> - 10.4.1-1
- CP-30205: Remove legacy 6.x upgrade/restore code
- CP-30232: Remove dead code in product._readSettings()

* Thu Dec 06 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.4.0-1
- CA-299191: write Dom0 uuid into /etc/sysconfig/xencommons
- CA-304547: Rewrite iSCSI code
- CA-304341: Prevent upgrades from versions of XS prior to 7
- CA-304341: Prevent restores to versions of XS prior to 7
- CP-30152: Use up to 16 vCPUs for installation
- CP-30232: Add an option to use the legacy partition layout

* Fri Nov 23 2018 Simon Rowe <simon.rowe@citrix.com> - 10.3.3-1
- CA-296524: retain any domain part of the hostname
- CA-294300: Removed XenCenter.iso entry from fstab.

* Fri Oct 12 2018 Simon Rowe <simon.rowe@citrix.com> - 10.3.2-1
- CP-29241: hide CC UEFI menu entry for master
- CP-29685: Switch network stack to Linux Bridge when in CC mode

* Fri Sep 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.3.1-1
- CP-29085: transform to cc-preparations
- CP-29091: parse service tag in answer file
- CA-298702: COMPANY_PRODUCT_BRAND missing in /etc/xensource-inventory

* Thu Aug 30 2018 Simon Rowe <simon.rowe@citrix.com> - 10.3.0-1
- CA-293743: wipe log system before mkfs
- CA-290899: use file system tree rooted directory for rpm query
- CA-295381: all packages should be upgraded

* Fri Jul 27 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.2.9-1
- CA-294085: log management config alternatives

* Wed Jul 18 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.8-1
- CP-28832: Host installer should tolerate indexes on PCI bus locations

* Tue Jul 17 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.7-1
- CP-28858: add extension to systemd-udevd.service

* Mon Jun 25 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.6-1
- CA-290859 - Use `1-$MAX` syntax for dom0's vcpus
- CA-291084: fix make-ramdisk option for update packaging format
- CA-291084: restore S06mount
- CA-291084: prevent multiple runs of subordinate scripts
- Fixup whitespace

* Wed May 30 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.5-1
- CA-289215: drop new format build from visual_version

* Tue May 15 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.4-1
- CA-289278: Preserve xapi-clusterd db config after reboot

* Mon Apr 23 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.3-1
- CA-288312: prefer hostname from /etc/hostname
- CA-288312: do not write hostname to /etc/sysconfig/network

* Mon Apr 16 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.2-1
- CA-283258: Ensure swap partition gets mounted during boot

* Thu Feb 01 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.1-1
- CA-280682: Capture stderr rather than leaking to the console

* Wed Jan 10 2018 Simon Rowe <simon.rowe@citrix.com> - 10.2.0-1
- CA-259657: determine timezone from /etc/localtime
- CA-259657: extract keyboard map from /etc/vconsole.conf in preference
- CA-259657: do not create obsolete files
- CA-259657: hoist writei18n() to initial seq definition
- CA-259657: always write /etc/hostname

* Tue Nov 14 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.6-1
- CA-273150: Add missing import

* Mon Oct 16 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.5-1
- CA-267428: Remove leftover logging
- CA-267428: Use splitlines() rather than split()

* Tue Oct 03 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.4-3
- CA-267961: delete unwanted firmware

* Mon Oct 02 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.4-2
- CA-267877: inhibit creation of the initrd and cleanup /boot

* Wed Sep 20 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.4-1
- CA-265171: Install bootloader to partition if needed
- CA-263790: Don't remove the wrong volume group during upgrade
- CA-266760: Allow upgrading packages from a driver disk
- CA-266760: Fix progress calculation when upgrading packages

* Tue Sep 05 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.3-1
- CA-261633: Add a helper function for multipath root
- CA-261633: Fix restoring when using a multipath root
- CA-263669: Don't hardcode the product name for upgrades
- CA-263669: Fix upgrade with incorrect product names

* Tue Aug 15 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.2-1
- CP-22698: drop package properties from inventory
- Complete removal of BUILD_NUMBER
- CA-260649: Use the default LVM directory
- Change crash kernel to 192M somewhere below 4G

* Wed Jun 28 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.1-1
- CA-257937: Ensure enough free space is available on the log partition
- Remove some obsolete files

* Wed Jun 14 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.0-2
- CP-22732: remove unwanted kernel files

* Mon Jun 05 2017 Simon Rowe <simon.rowe@citrix.com> - 10.1.0-1
- CP-22508: extend branding with build number
- Replace tabs with spaces.  Python, being whitespace delimited, is sensitive to this being correct.
- CA-255453: Add /var/lib/xcp/blobs to the restore list

- CA-255453: Speed up restoring files
- CP-17277, CP-17273: Enable tagged vlan support for host installer network.
- CP-17274, CP-17275: Adding VLAN support for XenServer management interface
- CP-17508: setting MANAGEMENT_INTERFACE in xensource_inventory is not needed during fresh install.
- CP-17702: Parsing of existing management interface when VLAN is present.
- CP-17703: 'more info' on 'use existing network' shows ip on vlan networks also.

* Fri Apr 28 2017 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.0.7-1
- CA-250911: write BRAND_CONSOLE_URL to /etc/xensource-inventory

* Thu Apr 20 2017 Simon Rowe <simon.rowe@citrix.com> - 10.0.6-1
- remove build number from welcome message
- CA-250335: Update results dict in place
- CA-250869: Check fcoeadm contains VLAN id before parsing it

* Tue Apr 11 2017 Simon Rowe <simon.rowe@citrix.com> - 10.0.5-1
- Remove unused module loading functions from hardware.py
- CA-247345: Preserve log partition during upgrade (take 2)
- CA-249629: Fix partitionsOnDisk() to handle multipath devices
- CA-249629: Match sfdisk output lines containing a colon correctly

* Mon Apr 03 2017 Simon Rowe <simon.rowe@citrix.com> - 10.0.4-1
- Revert "CA-247345: Preserve log partition during upgrade"

* Mon Mar 27 2017 Simon Rowe <simon.rowe@citrix.com> - 10.0.3-1
- CA-247345: Preserve log partition during upgrade

* Wed Mar 15 2017 Simon Rowe <simon.rowe@citrix.com> - 10.0.2-1
- tui: Fix appearance of build identifier

* Thu Mar 09 2017 Simon Rowe <simon.rowe@citrix.com> - 10.0.1-1
- CA-246294: Don't hard-code the list of devices for local media

