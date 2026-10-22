# We only build the ROMs if on an x86 build host. The resulting
# binary RPM will be noarch, so other archs will still be able
# to use the binary ROMs.
#
# We do cross-compilation for 32->64-bit, but not for other arches
# because EDK II does not support big-endian hosts.
%global buildarches x86_64

# debugging firmwares does not goes the same way as a normal program.
# moreover, all architectures providing debuginfo for a single noarch
# package is currently clashing in koji, so don't bother.
%global debug_package %{nil}

Source0: ipxe-v2.0.0.tar.gz

# Export using: --no-stat --no-numbered --no-signature --zero-commit --abbrev=12

# Ported from XenServer ipxe
Patch0: 0001-Check-Vendor-Class-ID-from-PROXYDHCP_SETTINGS_NAME.patch
Patch1: 0002-ipxe-no-post-prompt.patch
Patch2: 0003-CA-247413-Make-pxebs-accept-broadcast-DHCP-packets.patch
Patch3: 0004-CP-46112-Not-build-unused-NICs.patch
# Rewritten from the XenServer ipxe equivalent
Patch4: 0005-Enable-serial-console.patch

# Ported from XenServer ipxe-efi
Patch5: 0006-efi-snp-Limit-rx-queue-to-64-packets.patch

BuildArch: noarch

BuildRequires: gcc
BuildRequires: perl-interpreter, perl-libs, perl(lib), perl(FindBin)
# BuildRequires: syslinux
# BuildRequires: mtools
# BuildRequires: mkisofs
BuildRequires: binutils-devel
BuildRequires: xz-devel
%{?_cov_buildrequires}

Summary: A network boot loader
Name: ipxe
Version: 2.0.0
Release: 1%{?dist}
License: GPLv2

%description
iPXE is an open source network bootloader. It provides a direct
replacement for proprietary PXE ROMs, with many extra features such as
DNS, HTTP, iSCSI, etc.

%package efi
Summary: iPXE EFI drivers

%description efi
A build of iPXE in the form of EFI NIC drivers that can be used in an
UEFI environment or embedded into OVMF.

%prep
%autosetup -p1
%{?_cov_prepare}

%build
%{?_cov_wrap} make %{?_smp_mflags} -C src bin/rtl8139.rom
%{?_cov_wrap} make %{?_smp_mflags} -C src bin/8086100e.rom
%{?_cov_wrap} make %{?_smp_mflags} -C src bin-x86_64-efi/10ec8139.drv.efi CONFIG=qemu
%{?_cov_wrap} make %{?_smp_mflags} -C src bin-x86_64-efi/8086100e.drv.efi CONFIG=qemu

%install
cat src/bin/rtl8139.rom src/bin/8086100e.rom > src/bin/ipxe.bin
install -D -m 0644 src/bin/ipxe.bin %{buildroot}/%{_datadir}/ipxe/ipxe.bin
install -m 644 src/bin-x86_64-efi/10ec8139.drv.efi %{buildroot}/%{_datadir}/ipxe/10ec8139.efi
install -m 644 src/bin-x86_64-efi/8086100e.drv.efi %{buildroot}/%{_datadir}/ipxe/8086100e.efi
%{?_cov_install}

%files
%license COPYING
%license COPYING.GPLv2
%{_datadir}/ipxe/ipxe.bin

%files efi
%license COPYING
%license COPYING.GPLv2
%{_datadir}/ipxe/10ec8139.efi
%{_datadir}/ipxe/8086100e.efi

%{?_cov_results_package}

%changelog
* Wed Jul 15 2026 Tu Dinh <ngoc-tu.dinh@vates.tech> - 2.0.0-1
- Build ipxe and ipxe-efi v2.0.0 for XCP-ng 9

* Mon Jul 29 2024 Stephen Cheng <stephen.cheng@cloud.com> - 20121005-1.0.7
- CP-46112: Build compatible with XS9

* Fri Feb 11 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 20121005-1.0.6
- CP-38416: Enable static analysis

* Tue Nov 30 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 20121005-1.0.5
- CA-359977: Output to the serial console by default

* Fri Dec 04 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 20121005-1.0.4
- CP-35517: Bump release to rebuild

* Fri Apr 12 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 20121005-1.0.3
- CA-294898: Backport patch for gcc bug

* Mon Sep 24 2018 Sergey Dyasli <sergey.dyasli@citrix.com> - 20121005-1.0.2
- Initial packaging
