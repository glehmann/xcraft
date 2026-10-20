%global package_speccommit 45fa52c6df56ed969ee4a3337b2789b11d7e222f
%global usver 10.1.0
%global xsver 1
%global xsrel %{xsver}%{?xscount}%{?xshash}
%global package_srccommit v10.1.0

# submodule subprojects/keycodemapdb
%define keycodemapdb_cset f5772a62ec52591ff6870b7e8ef32482371f22c6
%define keycodemapdb_path subprojects%2fkeycodemapdb

# Control whether we build with the address sanitizer.
%define with_asan 0

Summary: qemu-dm device model
Name: qemu
Epoch: 0
Version: 10.1.0
Release: %{?xsrel}.3%{?dist}
License: GPL
Requires: xcp-clipboardd
## We broke an interface used by xenopsd-xc without version signalling
## so we have to carry a conflicts line to say we broke it.
Conflicts: xenopsd-xc < 0.123.0
Source0: qemu-10.1.0.tar.gz
Source2: keycodemapdb-f5772a62ec52591ff6870b7e8ef32482371f22c6.tar.gz
Patch0: 0001-hw-xen-pass-PCI-domain-to-xc_physdev_map_pirq_msi.patch
Patch1: hw-ide__check_null_block_before__cancel_dma_sync.patch
Patch2: xen-platform-add-device-id-property.patch
Patch3: xen-platform-add-class-id-property.patch
Patch4: xen-platform-add-revision-property.patch
Patch5: 0001-xen-platform-Handle-write-of-four-byte-build-number-.patch
Patch6: 0002-xen-platform-Provide-QMP-query-commands-for-XEN-PV-d.patch
Patch7: 0003-xen-platform-Emit-XEN_PLATFORM_PV_DRIVER_INFO-after-.patch
Patch8: dont-set-a20-on-xen.patch
Patch9: dont-init-cpus-on-xen.patch
Patch10: 0001-xen-Emit-RTC_CHANGE-upon-TIMEOFFSET-ioreq.patch
Patch11: remove-ioapic.patch
Patch12: ignore-rtc-century-changes.patch
Patch13: 0001-CP-33348-Allow-media-replace-qmp-command-to-take-a-n.patch
Patch14: token-bucket-event-throttling.patch
Patch15: handle-framebuffer-relocation.patch
Patch16: qmp-Fix-race-causing-events-to-be-sent-during-negotiation.patch
Patch17: disable-pcspk.patch
Patch18: protect-migration-vfio.patch
Patch19: ide-srst-compat.patch
Patch20: 0001-pc-Do-not-expect-to-have-a-fw_cfg-device.patch
Patch21: 0003-xen-apic-Implement-unrealize.patch
Patch22: 0004-hotplug-Implement-legacy-CPU-hot-unplug.patch
Patch23: igd-upt.patch
Patch24: check-unmigratable-devices-when-saving.patch
Patch25: query-migratable.patch
Patch26: nvme-close-backend.patch
Patch27: 0001-CP-20436-Introduce-a-config-option-for-machines-comp.patch
Patch28: pci-add-subsystem-id-properties.patch
Patch29: pci-add-revision_id-property.patch
Patch30: force-lba-geometry.patch
Patch31: 0001-CP-21767-Don-t-accidently-unplug-ourselves-if-PCI_CL.patch
Patch32: 0001-CP-21434-Implement-VBE-LFB-physical-address-register.patch
Patch33: 0001-CA-256542-Workaround-unassigned-accesses-caused-by-b.patch
Patch34: match-xen-pvdevice-location.patch
Patch35: 0001-CA-289906-Use-legacy-HID-descriptors-for-USB-Tablet-.patch
Patch36: 0001-Revert-target-i386-kvm-Remove-x86_cpu_change_kvm_def.patch
Patch37: 0001-Revert-hw-i386-pc-Unexport-PC_CPU_MODEL_IDS-macro.patch
Patch38: revert_hw-i386__remove_deprecated_machines_pc-0.10_and_pc-0.11.patch
Patch39: 0002-Revert-hw-i386-x86-Remove-X86MachineClass-save_tsc_k.patch
Patch40: 0003-Revert-hw-i386-pc-Remove-PCMachineClass-broken_reser.patch
Patch41: 0003-Revert-hw-usb-bus-Remove-the-full-path-property.patch
Patch42: 0004-Revert-target-i386-Remove-X86CPU-kvm_no_smi_migratio.patch
Patch43: 0005-Revert-pc-remove-short_root_bus-property.patch
Patch44: support_machines_pc-0.10.patch
Patch45: 0001-CP-17697-Initial-port-of-NVIDIA-VGPU-support-from-QEMU-trad.patch
Patch46: usb-batch-frames.patch
Patch47: 0001-CP-23753-Talk-to-new-clipboard-daemon.patch
Patch48: 0001-CA-420826-hw-cxl-add-fmws-stub.patch
Patch49: CA-423610-avoid-shareable-memory-file-descriptors.patch
Patch50: 0001-Revert-hw-block-fdc-Remove-the-check_media_rate-prop.patch
Patch51: CA-424241-Handle-strings-for-usb-hostbus-property.patch
Patch52: 0001-Revert-vnc-increase-max-display-size.patch
Patch53: allocate-guest-ram-reserved.patch
Patch54: fix-nvme-pci-exit.patch
Patch55: unplug-nvme-devices.patch
Patch56: do_not_register_xen_backend_for_qdisk.patch
Patch57: add-an-ide-read-cache.patch
Patch58: disable-dirty-vram-tracking.patch
Patch59: use-default-console.patch
Patch60: fix-mapcache-invalidate-performance.patch
Patch61: reduce-defined-machines.patch
Patch62: hw-nvme-guard-spdm.patch
Patch63: crypto-stubs.patch
Patch64: build-configuration.patch
Patch65: 81ef3d06c970c6b7ae4971ad552b2287af376f43.patch
Patch66: msix_pba_log.patch
Patch67: pci_passthrough.patch

# XCP-ng patches
Patch1000: 0001-xen-Fix-err-may-be-used-uninitialized.patch

BuildRequires: python3-devel
BuildRequires: libaio-devel, glib2-devel
BuildRequires: libjpeg-turbo-devel, libpng-devel, pixman-devel
BuildRequires: xen-dom0-libs-devel, xen-libs-devel, libusbx-devel
BuildRequires: libseccomp-devel
BuildRequires: ninja-build
%if %{with_asan} == 0
BuildRequires: jemalloc-devel
%else
BuildRequires: libasan
%endif
%{?_cov_buildrequires}

# XCP-ng: explicit build dep on gcc
BuildRequires: gcc

%description
This package contains Qemu.

%prep
%autosetup -p1
%{?_cov_prepare}

# submodule ui/keymapcodedb
tar xzf %{SOURCE2}

%build
%if %{with_asan}
extra_configure_argument+=('--enable-sanitizers')
extra_configure_argument+=('--enable-debug')
# Help to get better stack trace
extra_configure_argument+=('--extra-cflags=-fno-omit-frame-pointer')
# avoid: "WARNING: ASan doesn't fully support makecontext/swapcontext functions and may produce false positives in some cases!"
# extra_configure_argument+=('--with-coroutine=sigaltstack')
%endif

./configure --cc=gcc --cxx=/dev/null --enable-xen --target-list=i386-softmmu \
    --prefix=%{_prefix} --bindir=%{_libdir}/xen/bin --datadir=%{_datarootdir} \
    --localstatedir=%{_localstatedir} --libexecdir=%{_libexecdir} --sysconfdir=%{_sysconfdir} \
    --enable-werror --enable-libusb --enable-trace-backends=log \
    --disable-kvm --disable-docs --disable-guest-agent --disable-sdl \
    --disable-curses --disable-curl --disable-gtk --disable-bzip2 \
    --disable-strip --disable-gnutls --disable-nettle --disable-gcrypt \
    --disable-vhost-net --disable-vhost-user \
    --disable-lzo --disable-virtfs --disable-tcg --disable-tcg-interpreter \
    --disable-replication --disable-qom-cast-debug --disable-slirp \
    --audio-drv-list= --disable-coroutine-pool \
    --disable-bochs --disable-cloop --disable-dmg --disable-vvfat --disable-qed \
    --disable-parallels --disable-capstone --disable-fdt \
    --without-default-devices \
    --disable-download \
    --enable-seccomp "${extra_configure_argument[@]}"

%if %{with_asan}
# Check that address sanitizers is enabled, because QEMU's ./configure will not fail
grep -qe '-fsanitize=address' config-host.mak
%endif

%{?_cov_wrap} %{__make} %{?_smp_mflags} all

%install
mkdir -p %{buildroot}%{_libdir}/xen/bin

rm -rf %{buildroot}
%{__make} %{?_smp_mflags} install DESTDIR=%{buildroot}
rm -rf %{buildroot}/usr/include %{buildroot}%{_libdir}/pkgconfig %{buildroot}%{_libdir}/libcacard.*a \
       %{buildroot}/usr/share/locale
rm -rf %{buildroot}/usr/share/icons/
rm -rf %{buildroot}/usr/share/applications/

# QMP scripts
%{__install} -d -m 755 %{buildroot}%{python3_sitelib}/
cp -r python/qemu %{buildroot}%{python3_sitelib}/
cp -r scripts/qmp %{buildroot}%{_datarootdir}/qemu
%{?_cov_install}

%files
%{_libdir}/xen/bin
%{_datarootdir}/qemu
%{_libexecdir}/*
%{python3_sitelib}/qemu

%{?_cov_results_package}

%changelog
* Fri Oct 02 2026 Yann Dirson <yann.dirson@vates.tech> - 10.1.0-1.3
- Use comma not obsolete spaces as BuildRequires separator

* Thu Sep 10 2026 Thierry Escande <thierry.escande@vates.tech> - 10.1.0-1.2
- Rebuild against Xen v4.21

* Wed May 27 2026 Anthony PERARD <anthony.perard@vates.tech> - 10.1.0-1.1
- Sync with 10.1.0-1
- Remove all XCP-ng patches, as they are backport and already included.
- Add a build fix.
- *** Upstream changelog ***
  * Wed May 06 2026 Gerald Elder-Vass <gerald.elder-vass@citrix.com> - 10.1.0-1
  - CP-310167: Upgrade QEMU to 10.1.0

  * Mon Apr 27 2026 Andrew Cooper <andrew.cooper3@citrix.com> - 4.2.1-12
  - Rebuild against Xen 4.21

  * Thu Feb 26 2026 Frediano Ziglio <frediano.ziglio@citrix.com> - 4.2.1-11
  - CA-422649: xen: mapcache: Fix finding matching entry

  * Wed Jan 07 2026 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-10
  - CA-407410: qmp: Fix race causing events to be sent during negotiation

  * Tue Nov 18 2025 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-9
  - CA-420202: xen-hvm: Handle framebuffer relocation

  * Thu Oct 23 2025 Roger Pau Monné <roger.pau@citrix.com> - 4.2.1-8
  - Allow passthrough of devices from a PCI segment different than 0.

  * Thu Oct 02 2025 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-7
  - CA-417654: Fix NVME bug which causes WS2025 install failures

  * Wed Aug 27 2025 Andrew Cooper <andrew.cooper3@citrix.com> - 4.2.1-6
  - Rebuild against Xen 4.20

  * Mon May 19 2025 Frediano Ziglio <frediano.ziglio@cloud.com> - 4.2.1-5.3.5
  - CP-54031: Rebuild to pick up changes in Xen

  * Thu Apr 17 2025 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.3.4
  - CP-53474: Change QEMU to use new PCI config read/write domctl

  * Wed Jan 22 2025 Alex Brett <alex.brett@cloud.com> - 4.2.1-5.3.3
  - CP-53363: Update dependency for libjpeg-turbo

  * Wed Jan 22 2025 Alex Brett <alex.brett@cloud.com> - 4.2.1-5.3.2
  - CP-53238: Drop unused build requirement on libdrm

  * Tue Jan 14 2025 AshwinH <ashwin.h@cloud.com> - 4.2.1-5.3.1
  - CP-49883: Set epoch to 0 for XS9
  - CP-42926: Drop GVT-g support

* Mon Feb 02 2026 Quentin Casasnovas <quentin.casasnovas@vates.tech> - 4.2.1-5.2.17.1
- Sync with 4.2.1-5.2.17
- *** Upstream changelog ***
  * Thu Jan 08 2026 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.17
  - CA-407410: qmp: Fix race causing events to be sent during negotiation
  * Mon Dec 01 2025 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.16
  - CA-420202: xen-hvm: Handle framebuffer relocation

* Thu Jan 08 2026 Thierry Escande <thierry.escande@vates.tech> - 4.2.1-5.2.15.2
- Backport fixes for CVE-2021-3929

* Mon Dec 08 2025 Tu Dinh <ngoc-tu.dinh@vates.tech> - 4.2.1-5.2.15.1
- Sync with 4.2.1-5.2.15
- *** Upstream changelog ***
  * Thu Oct 23 2025 Roger Pau Monné <roger.pau@citrix.com> - 4.2.1-5.2.15
  - Allow passthrough of devices from a PCI segment different than 0.

* Mon Nov 17 2025 Tu Dinh <ngoc-tu.dinh@vates.tech> - 4.2.1-5.2.14.1
- Sync with 4.2.1-5.2.14
- Remove 0001-nvme-Don-t-check-NSID-in-NVME_VOLATILE_WRITE_CACHE.patch in favor of XenServer's fix
- *** Upstream changelog ***
  * Wed Oct 15 2025 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.14
  - CA-418876: Fix NVMe namespace indexing

  * Thu Oct 02 2025 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.13
  - CA-417654: Fix NVME bug which causes WS2025 install failures

* Thu Oct 02 2025 Tu Dinh <ngoc-tu.dinh@vates.tech> - 4.2.1-5.2.12.2
- Fix Server 2025 issue with NVMe volatile write cache feature

* Thu Feb 13 2025 Yann Dirson <yann.dirson@vates.tech> - 4.2.1-5.2.12.1
- Sync with xs8 4.2.1-5.2.12, no code change, only rebuild against libjemalloc.so.2:
  * Fri Aug 02 2024 Stephen Cheng <stephen.cheng@cloud.com> - 4.2.1-5.2.12
  - CP-46112: Rebuild after new version of jemalloc

  * Fri Aug 02 2024 Stephen Cheng <stephen.cheng@cloud.com> - 4.2.1-5.2.11
  - CP-46112: Rebuild with new version of jemalloc

* Tue Feb 11 2025 Lucas Ravagnier <lucas.ravagnier@vates.tech> - 4.2.1-5.2.10.1
- Add qemu-4.2.1-CVE-2023-3354.backport.patch to fix CVE-2023-3354

* Tue Jun 04 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 4.2.1-5.2.10
- CP-46254: Make PCI passthrough work in lockdown mode

* Mon Apr 08 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.9
- CA-391031: Reinstate rate limiting of RTC_CHANGE events
- CA-391069: Avoid livelock due to buffered ioreqs

* Mon Feb 12 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.8
- Disable capstone and fdt explicitly
- CP-42792: Backport a patch to avoid unncessary buffered ioreq polling

* Wed Jan 31 2024 Andrew Cooper <andrew.cooper3@citrix.com> - 4.2.1-5.2.7
- Rebuild against Xen 4.17

* Fri Jan 19 2024 Fei Su <fei.su@cloud.com> - 4.2.1-5.2.6
- CP-45970 remove qemu_trad_image.py

* Fri Jan 05 2024 Stephen Cheng <stephen.cheng@cloud.com> - 4.2.1-5.2.5
- CP-46162: Backport patches for building qemu with rawhide(xs9) toolchain
  - Do not ignore malloc value
  - Fix the null pointer errors in configure
  - Fix -Werror=maybe-uninitialized build failure
  - Fix net/eth.c compile errors
  - Compile with -Wno-array-bounds
  - Remove -no-pie linker flag

* Tue Nov 21 2023 Bernhard Kaindl <bernhard.kaindl@cloud.com> - 4.2.1-5.2.4
- CP-46102: Backport bugfixes for PCI passthrough using multifunction devices
  - hw/xen: set pci Atomic Ops requests for passthrough device
  - hw/xen/xen_pt: fix uninitialized variable
  - xen/pass-through: don't create needless register group
  - xen/pass-through: merge emulated bits correctly
  - Emulate the multifunction bit and set it based on the multifunction
    property of the PCIDevice (which can be set using QAPI).

* Tue Apr 18 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.2
- CA-376325: XSI-1393: Backport: xen-bus: reduce scope of backend watch

* Fri Mar 03 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.1
- CA-374355: Fix use of Lang1/Lang2 keys

* Wed Jan 25 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.2.0
- CP-41777: Migrate to Python 3

* Tue Dec 13 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.1.1
- CA-370278: tpm_crb: Avoid backend startup just before shutdown

* Wed Aug 17 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.1.0
- CP-40114: Remove mxGPU patches
- Enable TPM support

* Tue Jun 14 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.0.6
- CA-366527: Fix passthrough of multiple different devices

* Fri Feb 25 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.0.5
- CA-362592: Fix mapcache/iothread SIGBUS

* Thu Feb 10 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.0.4
- CP-38416: Enable static analysis

* Mon May 10 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.0.3
- CA-352456: Backport aio-posix: fix use after leaving scope in aio_poll()
- Fix patch Update-fd-handlers-to-support-sysfs_notify
- CA-352456: Fix QEMU memory corruption during migration

* Tue Apr 13 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.0.2
- CP-36580: Replace legacy xen-dom0-devel alias
- CA-352135: Fix CVE-2021-20257 - e1000 infinite loop
- CA-352998: Fix CVE-2021-3416 - infinite loop in net loopback mode

* Fri Feb 19 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 4.2.1-5.0.1
- CA-351961: Fix OOB accesses in ATAPI emulation

* Thu Jan 28 2021 Anthony PERARD <anthony.perard@citrix.com> - 4.2.1-5.0.0
- CP-33898: Upgrade QEMU to 4.2.1 upstream release

* Thu Sep 10 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.6.0
- CA-343524: XSA-335: Buffer overrun in QEMU USB subsystem
- CA-343531: CVE-2018-17598: Buffer overflow in rtl8139_do_receive

* Thu Apr 30 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.5.2
- CA-3216958: Move vgpu fifo to tempfs

* Mon Apr 20 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.5.1
- CA-337460 - Allow commit lists to be imported chronologically.
- CP-33051 - Forward port preopened sockets in monitor fdset.
- Fix patch context
- CP-33348 - Allow preopend nbd sockets to be passed to running qemu instance via qmp command.

* Thu Mar 26 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.5.0
- Fix patch context
- CA-336055: nvme: Disassociate block driver state during unplug
- CA-299551: Fix sporadic cpu_physical_memory_snapshot_get_dirty assertion

* Fri Feb 21 2020 Steven Woods <steven.woods@citrix.com> - 2.10.2-4.4.2
- CP33120: Add Coverity build macros

* Thu Nov 07 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.4.1
- CA-330047: Fix QEMU crash with discontiguous NVME namespaces

* Sat Aug 24 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.4.0
- CA-320079: Add NVME namespace support

* Wed Mar 27 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.3.0
- CA-309161: make the import script work with the new planex

* Tue Dec 18 2018 Edwin Török <edvin.torok@citrix.com> - 2.10.2-4.1.4
- CP-29626: Call blk_drain in NVMe reset code to avoid lockups
- CP-29898: Implement NVME migration workaround
- CP-29935: Add a command to allow querying whether the VM is migratable
- CP-29827: Don't conflate RTC_CENTURY bit with trad-compat
- CP-29827: Make legacy CPU unplug work for all machine types
- Fix patch context
- Backport NVMe fixes from upstream, including a CVE
- CA-303616: Expose LFB address when GVT-g is enabled

* Fri Nov 23 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-4.1.3
- CP-29194: Tidy up the patchqueue

* Tue Nov 06 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-4.1.2
- CP-28652: enable nvme for guest uefi

* Fri Oct 12 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-4.1.1
- Revert "CA-290135: Add debug patch to catch the unhandled access"

* Fri Sep 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.1.0
- CA-293487: Allocate VRAM in a reserved area
- CA-290135: Add debug patch to catch the unhandled access
- Enable the ISA debug device for OVMF logging

* Thu Aug 16 2018 Igor Druzhinin <igor.druzhinin@citrix.com> - 2.10.2-4.0.4
- CA-290647 50% regression in vm<->vm intrahost network throughput

* Fri Jul 27 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.0.3
- CA-290135: Make the debug message more obvious
- CA-294052: Recreate PCIBUS section properly on upgrade

* Thu Jul 12 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-4.0.2
- CA-290135 Reinforce unhandled unassigned access processing

* Tue Jul 03 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-4.0.1
- CA-293221: Fix upgrade from Clearwater

* Thu Jun 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-4.0.0
- CA-289906: Fix USB Tablet failure on every even migration during live-upgrade

* Mon Jun 25 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-3.0.8
- CA-286948: Fix resume when PV device location changes

* Wed May 30 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-3.0.7
- Print error code on foreign map failure

* Tue May 15 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-3.0.6
- CA-288638: Correct reporting of modified VRAM during migration
- CP-27696: Accomodate customised xen-pvdevice and NIC device ids into live-upgrade script
- CA-289321: Fix PCI passthrough edge cases
- CP-27572: vGPU should use shmem instead of xenstore+buffer hackery.

* Tue May 15 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-3.0.5
- CA-288638: Correct reporting of modified VRAM during migration
- CP-27696: Accomodate customised xen-pvdevice and NIC device ids into live-upgrade script
- CA-289321: Fix PCI passthrough edge cases
- CP-27572: vGPU should use shmem instead of xenstore+buffer hackery.

* Fri Apr 20 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-3.0.4
- CA-288286: Avoid built-in QEMU crypto

* Wed Apr 18 2018 marksy <mark.syms@citrix.com> - 2.10.2-3.0.3
- IDE read cache

* Mon Apr 16 2018 Simon Rowe <simon.rowe@citrix.com> - 2.10.2-3.0.2
- Return swallowed error messages
- CA-285493: Fix unmap issues with NVIDIA GPU passthrough

* Wed Mar 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.2-3.0.0
- Adjust dmops patch after backporting final version
- CA-266841: Improve VNC performance over long, thin pipes
- migration: Check return value of qemu_fclose
- CP-25629: Don't record QEMU's state in xenstore
- CA-267326: Backport VGA fixes including CVE-2018-5683
- CP-26998: Reintroduce QEMU vCPU hot-(un)plug patches
- CA-283664: Fix using QEMU upstream with libxl
- CP-23325: GVT-g: Save errno earlier
- CP-23325: Fix error handling issues in AMD GPU patch
- CP-23325: GVT-g: Check for allocation failure
- CP-23325: vgpu: Log sendto errors
- CP-24243: GVT-d: Use UPT instead of legacy mode
- CA-284366: Fix use of MSI-X with passthrough devices
- CP-23969 Introduce xen-pvdevice save state and upgrade into it
- CA-267326: Fix cirrus crash found by fuzzing
- CA-285409: Fix Windows RTC issues
- Enable e1000 device model for debug purposes
- CP-27303: Change QEMU vGPU communication with DEMU to fifo
- CA-285385: Backport qemu-trad vgpu-migrate patch, to allow vgpu migration
- CA-285493: Turn on PCI passthrough permissive mode

* Thu Aug 31 2017 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.10.0-1
- Update to QEMU v2.10.0.

* Tue Jun 16 2015 Ross Lagerwall <ross.lagerwall@citrix.com>
- Update for Xen 4.5.

* Tue Apr 8 2014 Frediano Ziglio <frediano.ziglio@citrix.com>
- First packaging
