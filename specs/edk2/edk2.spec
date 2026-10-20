%global debug_package %{nil}

Name: edk2
Summary: EFI Development Kit II
Version: 202602
Release: 1%{?dist}

License: BSD and MIT
URL: https://github.com/tianocore/edk2
Source0: edk2-stable%{version}.tar.gz
Source1: calc-pcrs.py

# https://github.com/google/brotli/archive/e230f474b87134e8c6c85b630084c612057f253e.tar.gz
Source2: brotli-e230f474b87134e8c6c85b630084c612057f253e.tar.gz
# https://github.com/ARMmbed/mbedtls/archive/e185d7fd85499c8ce5ca2a54f5cf8fe7dbe3f8df.tar.gz
Source3: mbedtls-e185d7fd85499c8ce5ca2a54f5cf8fe7dbe3f8df.tar.gz
# https://github.com/openssl/openssl/archive/aea7aaf2abb04789f5868cbabec406ea43aa84bf.tar.gz
Source4: openssl-aea7aaf2abb04789f5868cbabec406ea43aa84bf.tar.gz
# https://github.com/kkos/oniguruma/archive/4ef89209a239c1aea328cf13c05a2807e5c146d1.tar.gz
Source5: oniguruma-4ef89209a239c1aea328cf13c05a2807e5c146d1.tar.gz
# https://github.com/devicetree-org/pylibfdt/archive/cfff805481bdea27f900c32698171286542b8d3c.tar.gz
Source6: pylibfdt-cfff805481bdea27f900c32698171286542b8d3c.tar.gz
# https://github.com/MIPI-Alliance/public-mipi-sys-t/archive/370b5944c046bab043dd8b133727b2135af7747a.tar.gz
Source7: public-mipi-sys-t-370b5944c046bab043dd8b133727b2135af7747a.tar.gz
# https://github.com/akheron/jansson/archive/e9ebfa7e77a6bee77df44e096b100e7131044059.tar.gz
Source8: jansson-e9ebfa7e77a6bee77df44e096b100e7131044059.tar.gz
# https://github.com/DMTF/libspdm/archive/1be116c7b7713fa9003e1bd53b53a34758549eb9.tar.gz
Source9: libspdm-1be116c7b7713fa9003e1bd53b53a34758549eb9.tar.gz

# XCP-ng branding
Source1001: Logo.bmp

# Backports
Patch1: 0001-OvmfPkg-XenPvBlkDxe-Advertise-the-correct-IO-alignme.patch
Patch2: 0002-OvmfPkg-OvmfXen-Page-align-sections-of-DXE-and-UEFI-.patch

# Ported from XenServer 9
Patch3: 0003-OvmfPkg-XenPvBlkDxe-Fix-memory-barrier-macro.patch
Patch4: 0004-OvmfXen-Add-TPM2-support.patch
Patch5: 0005-MdePkg-SecPeiDxeTimerLibCpu-Support-for-dynamic-PcdF.patch
Patch6: 0006-OvmfPkg-OvmfXen-Use-RuntimeTimerLibCpu-for-DXE_DRIVE.patch
Patch7: 0007-Add-an-option-to-disable-BGRT.patch
Patch8: 0008-use-rtc.patch
Patch9: 0009-embed-nic-drivers.patch
Patch10: 0010-add-xen-variable.patch
Patch11: 0011-xen-platform-Add-device_id-for-Windows-VMs.patch
Patch12: 0012-Disable-unwanted-modules.patch
Patch13: 0013-xenorder.patch
Patch14: 0014-CA-297688-CA-293634-Keep-caching-enabled-during-SEC-.patch
Patch15: 0015-CP-30787-Crypto-code-build-linking-changes.patch
Patch16: 0016-add-Tcg2PhysicalPresenceLibXen.patch
Patch17: 0017-Tcg2Config-Fix-prompt-for-PPI-operation-parameter.patch
Patch18: 0018-Ovmf-Set-TPM2-ACPI-table-revision-for-Xen.patch
Patch19: 0019-SecurityPkg-Tcg2Config-Hide-unsupported-configuratio.patch
Patch20: 0020-OVMF-Shadow-PEI-for-consistent-measurements.patch
Patch21: 0021-set-default-resolution-1024-768.patch
Patch22: 0022-Add-extra-debug-statements.patch
Patch23: 0023-remove-vlan-tag-from-a-packet.patch

# XCP-ng patches
Patch24: 0024-Suppress-HashAlg-unsupported-by-PCR-bank-error.patch

# Boot logo placement: https://github.com/tianocore/edk2/pull/12813
Patch25: 0025-MdeModulePkg-Add-EdkiiPlatformLogoDisplayAttributeMi.patch
# Patch to use the above
Patch26: 0026-Place-the-boot-logo-according-to-Microsoft-s-guideli.patch

BuildRequires: gcc
BuildRequires: g++
BuildRequires: python3
BuildRequires: libuuid-devel
BuildRequires: nasm >= 2.15
BuildRequires: iasl
BuildRequires: ipxe-efi
BuildRequires: perl(IPC::Cmd), perl(JSON), perl(FindBin), perl(lib), perl(ExtUtils::MakeMaker)
%{?_cov_buildrequires}


%description
Provides the Open Virtual Machine Firmware (OVMF) built for x64.
This is a build of the EFI Development Kit II suitable for using
as firmware in a virtual machine.


%prep
%setup -q -n edk2-edk2-stable%{version}
%{?_cov_prepare}

mkdir -p BaseTools/Source/C/BrotliCompress/brotli
tar -C BaseTools/Source/C/BrotliCompress/brotli --strip-components=1 -xf %{SOURCE2}
mkdir -p CryptoPkg/Library/MbedTlsLib/mbedtls
tar -C CryptoPkg/Library/MbedTlsLib/mbedtls --strip-components=1 -xf %{SOURCE3}
mkdir -p CryptoPkg/Library/OpensslLib/openssl
tar -C CryptoPkg/Library/OpensslLib/openssl --strip-components=1 -xf %{SOURCE4}
mkdir -p MdeModulePkg/Library/BrotliCustomDecompressLib/brotli
tar -C MdeModulePkg/Library/BrotliCustomDecompressLib/brotli --strip-components=1 -xf %{SOURCE2}
mkdir -p MdeModulePkg/Universal/RegularExpressionDxe/oniguruma
tar -C MdeModulePkg/Universal/RegularExpressionDxe/oniguruma --strip-components=1 -xf %{SOURCE5}
mkdir -p MdePkg/Library/BaseFdtLib/libfdt
tar -C MdePkg/Library/BaseFdtLib/libfdt --strip-components=1 -xf %{SOURCE6}
mkdir -p MdePkg/Library/MipiSysTLib/mipisyst
tar -C MdePkg/Library/MipiSysTLib/mipisyst --strip-components=1 -xf %{SOURCE7}
mkdir -p RedfishPkg/Library/JsonLib/jansson
tar -C RedfishPkg/Library/JsonLib/jansson --strip-components=1 -xf %{SOURCE8}
mkdir -p SecurityPkg/DeviceSecurity/SpdmLib/libspdm
tar -C SecurityPkg/DeviceSecurity/SpdmLib/libspdm --strip-components=1 -xf %{SOURCE9}

%autopatch -p1

# XCP-ng: setup branding logo
cp %{SOURCE1001} MdeModulePkg/Logo/Logo.bmp

%build

python3 CryptoPkg/Library/OpensslLib/configure.py

cp %{_datadir}/ipxe/10ec8139.efi .
cp %{_datadir}/ipxe/8086100e.efi .

# Add "-D DEBUG_ON_HYPERVISOR_CONSOLE" to print logs to Xen's console and avoid
# the need to change qemu-wrapper.

%{?_cov_wrap} OvmfPkg/build.sh \
    -D SECURE_BOOT_ENABLE=TRUE \
    -D BGRT_ENABLE=FALSE \
    -D NETWORK_IP6_ENABLE=TRUE \
    -D IPXE_ENABLE=TRUE \
    -D NETWORK_HTTP_BOOT_ENABLE=FALSE \
    -D NETWORK_TLS_ENABLE=FALSE \
    -D NETWORK_ISCSI_ENABLE=FALSE \
    -D XEN_VARIABLE_ENABLE=TRUE \
    -D EXTRA_MODULES_ENABLE=FALSE \
    -D FD_SIZE_2MB \
    -D TPM1_ENABLE=FALSE \
    -D TPM2_ENABLE \
    -b DEBUG \
    --pcd gEfiMdePkgTokenSpaceGuid.PcdDebugPrintErrorLevel=0xFFFFFF4F \
    --pcd gUefiCpuPkgTokenSpaceGuid.PcdCpuMaxLogicalProcessorNumber=128 \
    -p OvmfPkg/OvmfXen.dsc -n %{?_smp_flags}

cp Build/OvmfXen/DEBUG_GCC*/FV/OVMF.fd OVMF-debug.fd
python3 %{SOURCE1} Build/OvmfXen/DEBUG_GCC*/FV/PEIFV.Fv Build/OvmfXen/DEBUG_GCC*/FV/DXEFV.Fv > OVMF-debug.pcrs
rm -rf Build/OvmfXen/DEBUG_GCC*

%{?_cov_wrap} OvmfPkg/build.sh \
    -D SECURE_BOOT_ENABLE=TRUE \
    -D BGRT_ENABLE=FALSE \
    -D NETWORK_IP6_ENABLE=TRUE \
    -D IPXE_ENABLE=TRUE \
    -D NETWORK_HTTP_BOOT_ENABLE=FALSE \
    -D NETWORK_TLS_ENABLE=FALSE \
    -D NETWORK_ISCSI_ENABLE=FALSE \
    -D XEN_VARIABLE_ENABLE=TRUE \
    -D EXTRA_MODULES_ENABLE=FALSE \
    -D FD_SIZE_2MB \
    -D TPM1_ENABLE=FALSE \
    -D TPM2_ENABLE \
    -b DEBUG \
    --pcd gEfiMdePkgTokenSpaceGuid.PcdDebugPrintErrorLevel=0x80000000 \
    --pcd gUefiCpuPkgTokenSpaceGuid.PcdCpuMaxLogicalProcessorNumber=128 \
    -p OvmfPkg/OvmfXen.dsc -n %{?_smp_flags}

cp Build/OvmfXen/DEBUG_GCC*/FV/OVMF.fd OVMF-release.fd
python3 %{SOURCE1} Build/OvmfXen/DEBUG_GCC*/FV/PEIFV.Fv Build/OvmfXen/DEBUG_GCC*/FV/DXEFV.Fv > OVMF-release.pcrs


%install

install -m 755 -d %{buildroot}/%{_datadir}/%{name}
install -m 644 OVMF-debug.fd %{buildroot}/%{_datadir}/%{name}/OVMF-debug.fd
install -m 644 OVMF-release.fd %{buildroot}/%{_datadir}/%{name}/OVMF-release.fd
ln -sf OVMF-release.fd %{buildroot}/%{_datadir}/%{name}/OVMF.fd

install -m 644 OVMF-debug.pcrs %{buildroot}/%{_datadir}/%{name}/OVMF-debug.pcrs
install -m 644 OVMF-release.pcrs %{buildroot}/%{_datadir}/%{name}/OVMF-release.pcrs

cp OvmfPkg/License.txt License.ovmf
# cp CryptoPkg/Library/OpensslLib/openssl-xs/LICENSE LICENSE.openssl

%{?_cov_install}


%files
%license License.txt
%license License.ovmf
#license LICENSE.openssl
%{_datadir}/%{name}

%{?_cov_results_package}


%changelog
* Fri Jul 17 2026 Tu Dinh <ngoc-tu.dinh@vates.tech> - 202602-1
- Updated to edk2-stable202602, for XCP-ng 9
- All backports dropped except for 1 applicable one
- Added 1 backport for "Page align sections of DXE and UEFI images"
- XS patches for vGPU among others were dropped
- XCP-ng patches "Workaround page table allocation crash" and "Allocate more
  memory when PEI FV is shadowed" were dropped
- Added patch to suppress "HashAlg unsupported by PCR bank" error

* Fri Jul 17 2026 Tu Dinh <ngoc-tu.dinh@vates.tech> - 20220801-1.7.11.2
- Add XCP-ng branding to the boot logo

* Fri May 22 2026 Thierry Escande <thierry.escande@vates.tech> - 20220801-1.7.11.1
- Sync with edk2-20220801-1.7.11
- *** Upstream changelog ***
  * Mon Mar 09 2026 Ross Lagerwall <ross.lagerwall@citrix.com> - 20220801-1.7.11
  - CA-424506: Fix boot from physical CD/DVD drive

* Tue Jan 20 2026 Teddy Astie <teddy.astie@vates.tech> - 20220801-1.7.10.2
- Bump vCPU limit to 128 from 96

* Wed Aug 06 2025 anthony.perard@vates.tech - 20220801-1.7.10.1
- Sync with edk2-20220801-1.7.10
- *** Upstream changelog ***
- * Wed Jun 11 2025 Fei Su <fei.su@cloud.com> - 20220801-1.7.10
- - CA-410587 [XSI-1806] Fix 802.1Q Header Handling in OVS on XS8
- * Fri Mar 07 2025 Deli Zhang <deli.zhang@cloud.com> - 20220801-1.7.9
- - CP-53516: Add perl modules require for XS9 build
- * Thu Oct 31 2024 Deli Zhang <deli.zhang@cloud.com> - 20220801-1.7.8
- - CP-50542: Upgrade OpenSSL to 3.0.9
- - CP-52657: Fix CVE-2024-6119

* Wed Feb 26 2025 anthony.perard@vates.tech - 20220801-1.7.7.2
- Fix for ENOMEM error when allocating page tables.

* Fri Aug 09 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 20220801-1.7.7.1
- Sync with 20220801-1.7.7
- *** Upstream changelog ***
- * Thu Jun 06 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 20220801-1.7.7
- - CA-388489: Fix CVE-2023-45236, CVE-2023-45237, and additional fix for CVE-2023-45229
- * Tue May 14 2024 Deli Zhang <deli.zhang@cloud.com> - 20220801-1.7.6
- - CP-46076: Support xs9 build

* Tue Jun 18 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 20220801-1.7.5.1
- Sync with 20220801-1.7.5
- *** Upstream changelog ***
- * Wed Mar 20 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 20220801-1.7.5
- - CA-390410: Disable some unneeded modules
- - CA-390410: Downgrade some errors to warnings

* Mon Apr 08 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 20220801-1.7.4.1
- Sync with 20220801-1.7.4
- *** Upstream changelog ***
- * Fri Feb 02 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 20220801-1.7.4
- - CA-388017: Fix most PixieFail vulnerabilities
- - Fix CVE-2023-45229 - Integer underflow when processing IA_NA/IA_TA options in a DHCPv6 Advertise message
- - Fix CVE-2023-45230 - Buffer overflow in the DHCPv6 client via a long Server ID option
- - Fix CVE-2023-45231 - Out of Bounds read when handling a ND Redirect message with truncated options
- - Fix CVE-2023-45232 - Infinite loop when parsing unknown options in the Destination Options header
- - Fix CVE-2023-45233 - Infinite loop when parsing a PadN option in the Destination Options header
- - Fix CVE-2023-45234 - Buffer overflow when processing DNS Servers option in a DHCPv6 Advertise message
- - Fix CVE-2023-45235 - Buffer overflow when handling Server ID option from a DHCPv6 proxy Advertise message

* Wed Jan 31 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 20220801-1.7.3.2
- Rebuild for ipxe-efi-20180514gite7f67d5-1.0.3.xcpng8.3

* Wed Jan 24 2024 Thierry Escande <thierry.escande@vates.tech> - 20220801-1.7.3.1
- Add patch to workaround crash in page table allocation

* Tue Nov 28 2023 Alejandro Vallejo <alejandro.vallejo@cloud.com> - 20220801-1.7.3
- CP-46796: Allow booting up to 96 vCPUs

* Wed Sep 20 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 20220801-1.7.2
- CA-383046: Use the emulated RTC to implement time services
- CA-383095: Add a patch to fix an unusual Windows PXE boot hang
- CP-45175: Add debug messages to XenVariable
- CP-45175: Disable the BGRT
- CP-45175: Add extra debug statements
- CP-45175: Tweak debug levels
- Include g++ from devtoolset as a build requirement

* Fri May 26 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 20220801-1.7.1
- CA-377781: Set default resolution back to 1024x768

* Thu Mar 30 2023 Anthony PERARD <anthony.perard@citrix.com> - 20220801-1.7.0
- Update to newer version, edk2-stable202208 release.

* Wed Mar 29 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.6.0
- CP-41446: Enable IPv6 support

* Fri Nov 11 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.5.1
- CA-372205: OVMF: Shadow PEI for consistent measurements
- CA-372205: Calculate PCR 0 and 2 at build time

* Wed Aug 17 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.5.0
- Add TPM support

* Mon Aug 01 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.8
- CA-343027: Fix hang in OVMF

* Fri Jun 17 2022 Mark Syms <mark.syms@citrix.com> - 20180522git4b8552d-1.4.7
- Fix script typo which breaks static analysis

* Mon Feb 21 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.6
- CP-38416: Enable static analysis

* Mon Jan 18 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.5
- CA-350259: Fix PCI passthrough of devices with 64+ GB BARs

* Fri Dec 04 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.4
- CP-35517: Bump release to rebuild

* Wed Jun 24 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.3
- CA-337679: Fix triple fault while booting on a heavy loaded host

* Thu Jul 04 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.2
- CA-322248: Prevent guest attempting to accessing priv regsiters

* Wed Jun 19 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 20180522git4b8552d-1.4.1
- CA-321788: make vGPU console work with all NVidia cards
- Replace local fixes with upstream backports

* Thu May 30 2019 Edwin Török <edvin.torok@citrix.com> - 20180522git4b8552d-1.4.0
- CP-30786: Turn off cryptopkg when secure boot is off
- Fix typo in secure boot enable condition
- CA-314662: Don't accidentally switch off MTRRs in SEC phase
- CA-309841: reenable PV drivers in OVMF
- CP-30787: disable unneeded crypto code, simplify patchqueue

* Tue Dec 18 2018 Edwin Török <edvin.torok@citrix.com> - 20180522git4b8552d-1.2.0
- CA-293636: Add support for NVIDIA vGPU on top of UEFI
- CA-293633: Set default video resoultion to 1024x768
- CA-293634: Various fixes to make GPU/PCI passthrough work
- Add a config option to use iPXE drivers
- Cleanup XenVariable integration
- CA-297602: Speedup boot by enabling PV drivers
- CA-298449: Add basic support for Intel GVT-g on top of UEFI
- Fix race finding XenVariable PCI device
- CA-296489: Allow boot ordering to be set from XS
- CA-296489: Make Xen boot ordering work with normal boot entries
- Remove unused patches
- CA-296489: XenBootOrder: Fix a typo
- CP-28675: XenVariable: Make some functions static
- CP-28675: XenVariable: Move useful functions into a header file
- CP-28675: XenVariable: Notify dom0 on Security Violation
- CP-29100: Remove PCI Device and expose IO port for communication with varstored.
- CP-29100: Remove previous workaround related to Xen Variable PCI device
- CA-297688: Keep caching enabled during SEC phase

* Thu May 17 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 20170920git947f373-1.0.0
- Initial packaging.
