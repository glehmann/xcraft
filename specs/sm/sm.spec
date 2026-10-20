%global package_speccommit 961596c641e84de8fa5ed2f98768b4c7cbeb4b30
%global package_srccommit v4.1.12

# -*- rpm-spec -*-

Summary: sm - XCP storage managers
Name:    sm
Version: 4.1.12
Release: 1%{?xsrel}.1%{?dist}
License: LGPL
URL:  https://github.com/xapi-project/sm
Source0: sm-4.1.12.tar.gz

%define __python python3

BuildRequires: python3
BuildRequires: python3-devel
BuildRequires: python3-pylint
BuildRequires: python3-coverage
BuildRequires: python3-bitarray
# XCP-ng: used by dubious unit tests
BuildRequires: /usr/bin/systemctl

# XCP-ng: gcc must be explicitly required in our build system
BuildRequires: gcc

Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
Requires: sm-fairlock = %{version}-%{release}

%if 0%{xcpng}
# Use upstream packages, see later if it useful to follow XS forks
Requires: device-mapper-multipath
Requires(post): device-mapper-multipath
Requires: lvm2
%else
Requires: xenserver-multipath
Requires(post): xenserver-multipath
Requires: xenserver-lvm2 >= 2.02.180-11.xs+2.0.2
%endif

Obsoletes: lvm2-sm-config <= 7:2.02.180-15.xs8
Requires: python3-bitarray
Requires: sm-debugtools = %{version}-%{release}
Requires: python%{python3_pkgversion}-sm-libs = %{version}-%{release}
Requires: sm-compat = %{version}-%{release}
Requires: python%{python3_pkgversion}-sm-compat = %{version}-%{release}
# For cgclassify command
Requires: libcgroup-tools
Requires(post): xs-presets >= 1.3
Requires(preun): xs-presets >= 1.3
Requires(postun): xs-presets >= 1.3
Conflicts: kernel < 4.19.19-5.0.0
Conflicts: blktap < 4.0.0
Requires: sg3_utils

Obsoletes: sm-additional-drivers

# XCP-ng patches
# Generated from our sm repository
# git format-patch v3.2.12-xcpng..3.2.12-8.3 --no-signature --no-numbered
#TODO

# https://github.com/xapi-project/sm/pull/802
Patch2000: 0001-Install-relative-symlinks.patch

%description
This package contains storage backends used in XCP

%prep
%autosetup -p1

%build
make
make -C misc/fairlock

%install
make -C misc/fairlock install DESTDIR=%{buildroot}
make install DESTDIR=%{buildroot}
mkdir -p %{buildroot}%{_datadir}/%{name}/

%post
%systemd_post make-dummy-sr.service
%systemd_post mpcount.service
%systemd_post sm-mpath-root.service
%systemd_post xs-sm.service
%systemd_post storage-init.service
%systemd_post usb-scan.socket
%systemd_post mpathcount.socket
%systemd_post sr_health_check.timer
%systemd_post sr_health_check.service

# On upgrade, migrate from the old statefile to the new statefile so that
# storage is not reinitialized.
if [ $1 -gt 1 ] ; then
    grep -q ^success "%{_sysconfdir}/firstboot.d/state/10-prepare-storage" 2>/dev/null && touch /var/lib/misc/ran-storage-init || :
fi

rm -f "%{_sysconfdir}/lvm/cache/.cache"
touch "%{_sysconfdir}/lvm/cache/.cache"

systemctl enable sr_health_check.timer
systemctl start sr_health_check.timer

# # XCP-ng: enable linstor-monitor by default.
# # However it won't start without linstor-controller.service
# systemctl enable linstor-monitor.service

# # XCP-ng: We must reload the multipathd configuration without restarting the service to prevent
# # the opening of /dev/drbdXXXX volumes. Otherwise if multipathd opens a DRBD volume,
# # it blocks its access to other hosts.
# # This command is also important if our multipath conf is modified for other drivers.
# if [ $1 -gt 1 ]; then
#     multipathd reconfigure
# fi

%preun
%systemd_preun make-dummy-sr.service
%systemd_preun mpcount.service
%systemd_preun sm-mpath-root.service
%systemd_preun xs-sm.service
%systemd_preun storage-init.service
%systemd_preun usb-scan.socket
%systemd_preun mpathcount.socket
%systemd_preun sr_health_check.timer
%systemd_preun sr_health_check.service

# # XCP-ng
# %systemd_preun linstor-monitor.service

exit 0

%postun
%systemd_postun make-dummy-sr.service
%systemd_postun mpcount.service
%systemd_postun sm-mpath-root.service
%systemd_postun xs-sm.service
%systemd_postun storage-init.service
%systemd_postun sr_health_check.timer
%systemd_postun sr_health_check.service

# # XCP-ng
# %systemd_postun linstor-monitor.service

%check
tests/run_python_unittests.sh
cp .coverage %{buildroot}
cp coverage.xml %{buildroot}
cp -r htmlcov %{buildroot}/htmlcov

%files
%defattr(-,root,root,-)
%{_libexecdir}/sm
%exclude %{_libexecdir}/sm/debug
%{_sysconfdir}/udev/scripts/xs-mpath-scsidev.sh
%{_sysconfdir}/xapi.d/plugins/coalesce-leaf
%{_sysconfdir}/xapi.d/plugins/lvhd-thin
%{_sysconfdir}/xapi.d/plugins/nfs-on-slave
%{_sysconfdir}/xapi.d/plugins/on-slave
%{_sysconfdir}/xapi.d/plugins/tapdisk-pause
%{_sysconfdir}/xapi.d/plugins/testing-hooks
%{_sysconfdir}/xapi.d/plugins/intellicache-clean
%{_sysconfdir}/xapi.d/plugins/trim
%{_sysconfdir}/xapi.d/xapi-pre-shutdown/*
%{_sysconfdir}/xensource/master.d/02-vhdcleanup
%{_bindir}/blktap2
%{_bindir}/tapdisk-cache-stats
%{_unitdir}/make-dummy-sr.service
%{_unitdir}/xs-sm.service
%{_unitdir}/sm-mpath-root.service
%{_unitdir}/usb-scan.service
%{_unitdir}/usb-scan.socket
%{_unitdir}/mpathcount.service
%{_unitdir}/mpathcount.socket
%{_unitdir}/storage-init.service
%{_unitdir}/sr_health_check.timer
%{_unitdir}/sr_health_check.service
%{_unitdir}/SMGC@.service
%config %{_sysconfdir}/udev/rules.d/65-multipath.rules
%config %{_sysconfdir}/udev/rules.d/55-xs-mpath-scsidev.rules
%config %{_sysconfdir}/udev/rules.d/58-xapi.rules
%dir %{_sysconfdir}/multipath/conf.d
%config(noreplace) %{_sysconfdir}/multipath/conf.d/custom.conf
%config %{_sysconfdir}/logrotate.d/SMlog
%config %{_sysconfdir}/udev/rules.d/57-usb.rules
%config %{_sysconfdir}/udev/rules.d/99-purestorage.rules
%doc CONTRIB LICENSE MAINTAINERS README.md
# # XCP-ng
# /etc/systemd/system/drbd-reactor.service.d/override.conf
# /etc/systemd/system/linstor-satellite.service.d/override.conf
# /etc/systemd/system/var-lib-linstor.service
# /etc/xapi.d/plugins/linstor-manager
# /opt/xensource/bin/linstor-kv-tool
# /opt/xensource/libexec/fork-log-daemon
# /opt/xensource/libexec/linstor-monitord
# /opt/xensource/libexec/safe-umount
# /opt/xensource/sm/CephFSSR
# /opt/xensource/sm/CephFSSR.py
# /opt/xensource/sm/GlusterFSSR
# /opt/xensource/sm/GlusterFSSR.py
# /opt/xensource/sm/linstorjournaler.py
# /opt/xensource/sm/LinstorSR
# /opt/xensource/sm/LinstorSR.py
# /opt/xensource/sm/linstorvhdutil.py
# /opt/xensource/sm/linstorvolumemanager.py
# /opt/xensource/sm/MooseFSSR
# /opt/xensource/sm/MooseFSSR.py
# /opt/xensource/sm/XFSSR
# /opt/xensource/sm/XFSSR.py
# /opt/xensource/sm/ZFSSR
# /opt/xensource/sm/ZFSSR.py
# /opt/xensource/sm/LargeBlockSR
# /opt/xensource/sm/LargeBlockSR.py
# %{_unitdir}/linstor-monitor.service
# %{python3_sitelib}/__pycache__/sm_typing*pyc
# %{python3_sitelib}/sm_typing.py

%package testresults
Summary:  test results for SM package
BuildArch: noarch

%description testresults
The package contains the build time test results for the SM package

%files testresults
/.coverage
/coverage.xml
/htmlcov

%package -n python%{python3_pkgversion}-sm-test-plugins
Provides: sm-test-plugins = %{version}-%{release}
Summary:  System test fake key lookup plugin
BuildArch: noarch

%description -n python%{python3_pkgversion}-sm-test-plugins
The package contains a fake key lookup plugin for system tests

%files -n python%{python3_pkgversion}-sm-test-plugins
/opt/xensource/sm/plugins
%{_libexecdir}/sm/keymanagerutil
%{python3_sitelib}/sm/plugins/keymanagerutil.py
%{python3_sitelib}/sm/plugins/__pycache__/keymanagerutil.*

%package fairlock
Summary: Fair locking subsystem

%description fairlock
This package provides the fair locking subsystem using by the Storage
Manager and some other packages

%files fairlock
%{python3_sitelib}/__pycache__/fairlock*pyc
%{python3_sitelib}/fairlock.py
%{_unitdir}/fairlock@.service
%{_libexecdir}/fairlock

%post fairlock
## On upgrade, shut down existing lock services so new ones will
## be started. There should be no locks held during upgrade operations
## so this is safe.
if [ $1 -gt 1 ];
then
    /usr/bin/systemctl list-units fairlock@* --all --no-legend | /usr/bin/cut -d' ' -f1 | while read service;
    do
        /usr/bin/systemctl stop "$service"
    done
fi

%package debugtools
Summary: SM utilities for debug and testing

%description debugtools
Utilities for debug and testing purposes

%files debugtools
%{_libexecdir}/sm/debug


%package -n python%{python3_pkgversion}-sm-libs
Summary: SM core libraries
BuildArch: noarch
# sm-core-libs is currently at v1.1.3-1. These need updating if we end up
# putting out another release of it before this gets merged.
Provides: python%{python3_pkgversion}-sm-core-libs = 1.1.3-1
Obsoletes: python%{python3_pkgversion}-sm-core-libs < 1.1.3-2

%description -n python%{python3_pkgversion}-sm-libs
This package contains common core libraries for SM.

It obsoletes and replaces the old sm-core-libs package.

%files -n python%{python3_pkgversion}-sm-libs
%exclude %{python3_sitelib}/sm/plugins/keymanagerutil.py
%exclude %{python3_sitelib}/sm/plugins/__pycache__/keymanagerutil.*
%{python3_sitelib}/sm
%{_datadir}/sm

%package -n python%{python3_pkgversion}-sm-compat
Summary: SM compatibility files for older callers
BuildArch: noarch
Requires: sm = %{version}-%{release}

%description -n python%{python3_pkgversion}-sm-compat
This package contains compatibility wrappers left behind for older
callers which expect to find python files in /opt/xensource


%files -n python%{python3_pkgversion}-sm-compat
/opt/xensource/sm
/opt/xensource/bin/blktap2
/opt/xensource/bin/tapdisk-cache-stats
/opt/xensource/libexec/check-device-sharing
/opt/xensource/libexec/local-device-change
/opt/xensource/libexec/make-dummy-sr
/opt/xensource/libexec/usb_change
/opt/xensource/libexec/kickpipe
/opt/xensource/libexec/set-iscsi-initiator
/opt/xensource/libexec/storage-init
%exclude /opt/xensource/sm/plugins

%package compat
Summary: SM compatibility files for older callers

%description compat
This package contains arch-specific compatibility wrappers left
behind for older callers which expect to find libraries and binaries
in /opt/xensource

%files compat
/opt/xensource/debug/tp
/opt/xensource/libexec/dcopy

%changelog
* Tue May 05 2026 Yann Dirson <yann.dirson@vates.tech> - 4.1.12-1.1
- Rebase on 4.1.12-1
- Adjust deps for Almalinux 9
- Skipped all XCP-ng patches for now, drop BuildRequires: python-mock
- Dropped obsolete dependency on python3-future
- New patch: install relative symlinks
- HACK include systemctl in BuildRequires, some unit tests to strange things
- TEMP HACK remove dependency on device-mapper-multipath, which needs work
- TEMP HACK depend on lvm2 not xenserver-lvm2, which needs work
- TODO: XCP-ng patches were not ported yet to 4.x, XCP-ng-specific files commented out
- *** Upstream changelog ***
  * Mon Jan 26 2026 Mark Syms <mark.syms@citrix.com> - 4.1.12-1
  - Revert: use VHD header to fetch block size

  * Thu Jan 22 2026 Mark Syms  <mark.syms@citrix.com> - 4.1.11-1
  - Fix tests

  * Thu Jan 22 2026 Mark Syms <mark.syms@citrix.com> - 4.1.10-1
  - CA-420307: Construct synthetic page data as byte arrays
  - fix(cleanup): ensure VDI is active before relink
  - CA-411988 Fix TypeError in LVHDoHBASR.load() with dynamic module loading
  - CA-418775: ensure getsize and sg_readcap agree on resize
  - CA-420515: log errors when in foreground
  - CA-419706: update print calls
  - CA-416486: leaf coalesce wait for GC
  - LVHDSR: convert refvdi retured by get_snapshot_of to vdi_uuid
  - check that the snapshot_of field is not 'OpaqueRef:' format
  - use VHD header to fetch block size

  * Mon Dec 15 2025 Mark Syms <mark.syms@citrix.com> - 4.1.9-1
  - CA-421013: ensure cbt log removed on supporter after disable

  * Mon Dec 08 2025 Mark Syms <mark.syms@citrix.com> - 4.1.8-1
  - CP-310443: Drop legacy mpathutil tool
  - CA-413899: Rescan LVs whilst activating
  - remove flag gc_no_space from SR sm-config
  - improve error messages when vdi_type is missing
  - CP-309718: calculate a moving average of leaf size in GC

  * Thu Oct 02 2025 Mark Syms <mark.syms@citrix.com> - 4.1.7-1
  - Rebuild

  * Wed Oct 01 2025 Mark Syms <mark.syms@citrix.com> - 4.1.6-1
  - stop SMGC service on SR detach
  - CP-308803: enable SR multipathing by default for new installs
  - CA-417862: tolerate device not found on multipath flush

  * Tue Sep 16 2025 Tim Smith <tim.smith@cloud.com> - 4.1.5-1
  - CA-407343: do not remove the VHD parent property after leaf coalesce
  - CA-408452: remove vhd parent if it does not have one
  - Revert "CA-397084 Log any user of LV at deactivate"
  - CA-408105: add logging to _finishInterruptedCoalesceLeaf
  - CA-411163: verify SCSI ids for SR PVs
  - CA-405851: stop_all_gc failed to stop SMGC services

  * Mon Aug 18 2025 Mark Syms <mark.syms@cloud.com> - 4.1.4-1
  - CA-412080 Add lcache to SM_LIBS
  - CA-413209: remove dangling reference to deprecated rawhba
  - CP-51843: advertise SR_CACHING on LVHDoISCSI and HBA

  * Wed Jun 25 2025 Mark Syms <mark.syms@cloud.com> - 4.1.3-1
  - CA-411927: guard check with presence of ScsiId key
  - CA-395221: use systemd target for gc enable
  - CA-412336: treat inaccessible device as a soft failure

  * Tue Jun 03 2025 Tim Smith <tim.smith@cloud.com> - 4.1.2-1
  - CP-308219 Separate resetvdis script

  * Tue May 27 2025 Tim Smith <tim.smith@cloud.com> - 4.1.1-1
  - CP-54096 Path cleanup
  - CA-409231 report intellicache stats with nbd
  - CP-54434 Correction of python locations
  - CP-54351 Further correction of python locations
  - CP-54766 Use of /opt is now only for compatibility reasons

  * Tue Apr 01 2025 Tim Smith <tim.smith@cloud.com> - 4.1.0-1
  - Add udev rules for PureStorage
  - Remove 69-dm-lvm-metad.rules
  - CP-53808 Obsolete sm-core-libs with sm-libs
  - Move SM_ERRORCODES.xml to datadir
  - Remove excludes which are no longer required
  - CP-53810 Move all /opt/xensource file into compat package

  * Mon Jan 27 2025 Mark Syms <mark.syms@cloud.com> - 4.0.0-3
  - CA-392489: drop cgrules patch, moved to libcgroup
  - CA-392489: Requires libcgroup-tools
  - CP-53410: remove use of alternatives

  * Wed Jan 22 2025 Mark Syms <mark.syms@cloud.com> - 4.0.0-2
  - Drop patching cgrules as this is moved to libcgroup

  * Tue Jan 21 2025 Mark Syms <mark.syms@cloud.com> - 4.0.0-1
  - Remove sm-multipath, now moved to host-installer
  - Remove support for FCoE Storage Repositories
  - CP-35551: Support tapdisk >= 4 with no kernel blktap2 device

* Tue May 05 2026 Yann Dirson <yann.dirson@vates.tech> - 3.2.12-1.1
- Rebase on 3.2.12-1, based on 3.2.12-3.1 by Ronan
- Sync patches matching 3.2.12-3.1
- Remove patches merged upstream:
  - 0028-CA-398425-correctly-check-for-multiple-targets-in-iS.patch
  - 0030-fix-getAllocatedSize-is-incorrect-75.patch
  - 0047-Define-and-details-attr-on-Failure-mock.patch
  - 0064-feat-add-HPE-Nimble-multipath-configuration.patch
- *** Upstream changelog ***
  * Mon Jan 06 2025 Mark Syms <mark.syms@cloud.com> - 3.2.12-1
  - CA-400789: Do not exclude parentless VDIs from cacheing
  - CP-52844: allow for open session to be passed to sr_get_capability
  - CP-52852: add handler for xmlrpc ProtocolError
  - fix(cleanup.py): bad live coalesce check regarding FileSR
  * Tue Nov 19 2024 Mark Syms <mark.syms@cloud.com> - 3.2.11-1
  - CP-42675: send messages to Xapi if GC has insufficient space
  - CP-52620: enable read-through cache on persistent leaf
  * Tue Nov 05 2024 Mark Syms <mark.syms@cloud.com> - 3.2.10-1
  - CA-397084: SR scan tries to deactivate LV in use by tapdisk
  - CA-399644: if we make progress do not abort the GC or evaluate criteria
  - CA-400743: perform post snapshot rename in ioretry
  - CA-401068: if iSCSI device path is not found scan the bus
  * Wed Oct 09 2024 Mark Syms <mark.syms@cloud.com> - 3.2.9-3
  - CP-51658: install stop gc helper script
  - CA-399643 Use full paths when stopping fairlock on upgrade
  - CA-396655: check xapi is enabled before starting multipath reporting
  - CA-396658: check xapi is enabled before checking SR health
  - CA-400106: disable leaf coalesce with VDI snapshot secondary
  * Tue Sep 24 2024 Tim Smith <tim.smith@cloud.com> - 3.2.8-2
  - CA-399643 Use full paths when stopping fairlock on upgrade
  * Fri Sep 06 2024 Robin Newton <robin.newton@cloud.com> - 3.2.8-1
  - CA-398958 Cope with concurrent read-only activations
  * Wed Sep 04 2024 Mark Syms <mark.syms@cloud.com> - 3.2.7-1
  - CA-398425: correctly check for multiple targets in iSCSI
  * Wed Aug 21 2024 Mark Syms <mark.syms@cloud.com> - 3.2.6-1
  - CA-395560: Log exception details when LUN refresh fails
  - CA-396124: amend criteria under which the garbage collector aborts
  - CA-397084: Log any user of LV at deactivate
  - CP-51214: stop cgrules triggering errors on update
  * Tue Jul 23 2024 Tim Smith <tim.smith@cloud.com> - 3.2.5-1
  - CA-395554 Stop fairlock services on package upgrade
  * Mon Jul 22 2024 Mark Syms <mark.syms@cloud.com> - 3.2.4-1
  - Add missing sg3_utils dependency
  - Update multipath config for Dell, IBM and Nimble arrays

* Tue Feb 18 2025 Ronan Abhamon <ronan.abhamon@vates.tech> - 3.2.3-1.17
- Add 0065-fix-cleanup.py-resize-on-a-primary-host-82.patch

* Mon Jan 20 2025 Yann LE BRIS <yann.lebris@vates.tech> - 3.2.3-1.16
- Add 0061-Fix-LVHDSR.load-set-other_conf-in-cond-branch-to-pre.patch
- Add 0062-fix-cleanup.py-protect-LinstorSR-init-against-race-c.patch
- Add 0063-Fix-filter-to-reject-other-device-types-77.patch
- Add 0064-feat-add-HPE-Nimble-multipath-configuration.patch

* Thu Dec 19 2024 Ronan Abhamon <ronan.abhamon@vates.tech> - 3.2.3-1.15
- Fix missing mypy "@override" import in nfs-on-slave script

* Wed Dec 11 2024 Ronan Abhamon <ronan.abhamon@vates.tech> - 3.2.3-1.14
- Sync fork-load-daemon script with http-nbd-transfer (v1.5.0)
- Fix coalesce process for LINSTOR SRs
- Many code improvements for issues detected by mypy

* Wed Nov 27 2024 Damien Thenot <damien.thenot@vates.tech> - 3.2.3-1.13
- Replace 0030-fix-cleanup.py-bad-live-coalesce-check-regarding-Fil.patch with 0030-fix-getAllocatedSize-is-incorrect-75.patch
- Ensure correct allocatedSize for FileVDI in cleanup.py

* Tue Nov 26 2024 Damien Thenot <damien.thenot@vates.tech> - 3.2.3-1.12
- Add 0030-fix-cleanup.py-bad-live-coalesce-check-regarding-Fil.patch

* Mon Sep 09 2024 Ronan Abhamon <ronan.abhamon@vates.tech> - 3.2.3-1.7
- Import 8.2 LINSTOR changes on 8.3:
- Robustify HA: use a specific group with a replication count of 3
- Export helpers in linstor-manager regarding network interfaces
- Improve health-check helper: more details and simple API
- Fix pause/unpause: always load a valid VHD chain
- Robustify remote "vhdutil check" command
- Robustify SR destruction
- Prevent diskless destruction on master host
- Prevent tiebreaker destruction
- Reduce LINSTOR vhdutil queries

* Tue Sep 03 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.2.3-1.4
- Add 0028-CA-398425-correctly-check-for-multiple-targets-in-iS.patch
- Restore the sr_health_check service and the code which goes with it.

* Mon Aug 19 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.2.3-1.3
- %%preun: Move command above exit 0 so that it's executed
- Properly disable the removed sr_health_check.timer
- Also remove the dangling symlink if still present due to improper removal
  of the timer in sm-3.2.0-1.5

* Mon Aug 19 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.2.3-1.2
- Don't try to patch /etc/cgrules.conf when the patch was already applied
- Fixes update warning

* Tue Aug 13 2024 Benjamin Reis <benjamin.reis@vates.tech> - 3.2.3-1.1
- Rebase on 3.2.3-1
- Add 0028-reflect-upstream-changes-in-our-tests.patch
- *** Upstream changelog ***
- * Thu Jul 04 2024 Mark Syms <mark.syms@cloud.com> - 3.2.3-1
- - CA-393194: Fix pvremove failure
- * Mon Jun 24 2024 Mark Syms <mark.syms@cloud.com> - 3.2.2-1
- - CP-49689: remove reverse dependency on SR from xs_errors
- - CP-49775 convert SMGC to systemd service
- - CP-49720 Move LOCK_TYPE_RUNNING from cleanup.py to lock.py

* Mon Aug 12 2024 Benjamin Reis <benjamin.reis@vates.tech> - 3.2.1-1.1
- Rebase on 3.2.1-1
- *** Upstream changelog ***
- * Wed May 29 2024 Mark Syms <mark.syms@cloud.com> - 3.2.1-1
- - Use python3 rather than python3.6
- - CA-390937: fix conflict between GC and SR detach
- - CA-392989: improve diagnostics for tests

* Thu Aug 01 2024 Benjamin Reis <benjamin.reis@vates.fr> - 3.2.0-1.5
- Add 0027-Revert-CA-379329-check-for-missing-iSCSI-sessions-an.patch

* Tue Jul 30 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.2.0-1.4
- Add 0026-feat-LVHDSR-add-a-way-to-modify-config-of-LVMs-60.patch

* Tue Jul 30 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.2.0-1.3
- Reload automatically multipathd config after each update

* Fri Jun 28 2024 Ronan Abhamon <ronan.abhamon@vates.tech> - 3.2.0-1.2
- Fix 3.2.0 rebase which prevents VMs from starting

* Mon Jun 24 2024 Benjamin Reis <benjamin.reis@vates.tech> - 3.2.0-1.1
- Rebase on 3.2.0-1
- Drop 0001-XCP-ng-cherry-pick-of-CP-45750-get-storage-init-test.patch
- Drop 0026-Implement-correctly-fake_import-in-test_on_slave.py.patch
- Drop 0027-fix-NFSSR-ensure-we-can-attach-SR-during-attach_from.patch
- *** Upstream changelog ***
- * Fri May 17 2024 Mark Syms <mark.syms@cloud.com> - 3.2.0-1
- - CA-387861 Introduce fair locking subsystem
- - CA-384942: use resolved CD path for error checking
- - CA-392823: ensure no device mapper conflicts in LVHDSR detach
- * Wed Mar 27 2024 Mark Syms <mark.syms@citrix.com> - 3.1.0-1
- - CP-45750: Allow for alternative local storage SR types
- - Release 3.0.13
- * Tue Mar 26 2024 Mark Syms <mark.syms@citrix.com> - 3.0.13-2
- - CA-388353: Fix context in cgrules patch triggerin script
- - CA-379287 Cope with fs-encoded XMLRPC request on command line
- - CP-383791 Fix handling of UTF-8 in srmetadata.py
- - CP-45514: set ownership and permissions on backend
- - CA-371791: Fix world readable permissions on EXTSR
- - CA-384030 Ignore awkardly named images in ISO SRs
- - CP-45927: set multipath checker for Equalogic 100E-00 to tur
- - CA-377454: ensure that the iscsiadm running lock exists
- - CA-384783 Probe for NFS4 when rpcinfo does not include it
- - CA-253490 Add missing error codes
- - CP-46807: reduce logs from scheduler set errors
- - fix(NFSSR): ensure we can attach SR during attach_from_config call
- - CP-39600: Rework LVM locking to use fair lock queue
- - fix(ISOSR): type accepts 'nfs_iso' not 'nfs' as the docs claim
- - CA-386281 CIFS username can be omitted in ISO SR
- - CP-46863 Dump Multipath Status from Storage Manager
- - CA-386479: ensure we login to all iSCSI Target Portal Groups
- - CP-39600: remove stray print call
- - CA-385069: Remove unnecessary LvmContext wrap
- - CA-387770 Improve error message for readonly shares
- - CA-388451: ensure that xapi sessions are logged out
- - Always remove the RO/RW tag from VDIs in case of failure
- - CA-387770 increase NFSSR and SMBSR test coverage
- - CA-386316 Fix race condition between sr_detach and GC
- - CP-47841: update multipath configuration for PURE Storage
- - CA-387770: check for read-only shared fs at create
- - CP-48018: Update to systemd to manage services
- - CA-388933: rework GC Active lock to ensure GC starts

* Tue Apr 23 2024 Damien Thenot <damien.thenot@vates.tech> 3.0.12-12.3
- Updated 0028-feat-LargeBlock-introduce-largeblocksr-51.patch
- Add a clearer error message for multidevices config not being accepted

* Fri Apr 12 2024 Damien Thenot <damien.thenot@vates.tech> 3.0.12-12.2
- Add 0028-feat-LargeBlock-introduce-largeblocksr-51.patch

* Wed Apr 10 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.0.12-12.1
- Rebase on 3.0.12-12
- Add 0001-XCP-ng-cherry-pick-of-CP-45750-get-storage-init-test.patch
- Apply correctly: 0027-fix-NFSSR-ensure-we-can-attach-SR-during-attach_from.patch
- *** Upstream changelog ***
- * Fri Feb 16 2024 Mark Syms <mark.syms@citrix.com> - 3.0.12-12
- - CA-386316 Fix race condition between sr_detach and GC
- - CA-388808: only patch cgrules if our setting is missing
- - CA-388811 Don't tell xapi whether SR supports hard links when there's no session
- - CP-47841: update multipath configuration for PURE Storage
- * Tue Feb 13 2024 Mark Syms <mark.syms@citrix.com> - 3.0.12-11
- - CP-45750 Allow for alternative local storage SR types
- * Thu Feb 08 2024 Mark Syms <mark.syms@citrix.com> - 3.0.12-10
- - CA-387770 increase NFSSR and SMBSR test coverage
- - CA-383076 Before returning open files from /proc ensure PIDs are alive
- * Tue Feb 06 2024 Mark Syms <mark.syms@citrix.com> - 3.0.12-9
- - CA-387770 Improve error message for readonly shares
- - CA-388451: Ensure that xapi sessions are logged out
- * Mon Jan 29 2024 Mark Syms <mark.syms@citrix.com> - 3.0.12-8
- - CA-388353: fix patch context for cgrules.conf triggerin script
- - CP-46807: reduce logs from scheduler set errors
- * Mon Jan 15 2024 Mark Syms <mark.syms@citrix.com> - 3.0.12-7
- - CA-386281 CIFS username can be omitted in ISO SR
- - CA-386479: ensure we login to all iSCSI Target Portal Groups
- - CP-46863 Dump Multipath Status from Storage Manager

* Thu Feb 22 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.0.12-6.2
- Ensure we can always attach NFS SR during attach_from_config call
- Always activate LVs for LINSTOR if attach from config is asked
- Create VHD diskless chain for LINSTOR during snapshots
- Robustify LINSTOR resize when a volume has just been created

* Wed Jan 24 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.0.12-6.1
- Rebase on 3.0.12-6
- Drop 0022-Fix-vdi-ref-when-static-vdis-are-used.patch
- Drop 0024-Support-IPv6-for-NFS-ISO-SR.patch
- Import sm 8.2 LINSTOR fixes on 8.3:
- Make sure VDI.delete doesn't throw under specific conditions
- Add drbd in the blacklist of multipath.conf
- Autoselect destination host for clone
- Clone/snapshot without increasing volume size
- *** Upstream changelog ***
- * Wed Nov 22 2023 Tim Smith <tim.smith@citrix.com> - 3.0.12-6
- - Backport fix for CA-384030 from upstream
- - CA-377454: ensure iscsiadm lock file is present
- - CP-45927: Change the pathselector used on Equalogic 100E-00
- - CA-384783 Probe for NFS4 when rpcinfo does not include it
- * Mon Oct 30 2023 Mark Syms <mark.syms@citrix.com> - 3.0.12-5
- - rebuild
- * Wed Oct 18 2023 Mark Syms <mark.syms@citrix.com> - 3.0.12-4
- - CA-379287 Cope with fs-encoded XMLRPC request on command line
- - CA-383791 fix LVM SR issues with wide character names
- - CA-371791: ensure that file SR mounts are not world readable
- * Fri Sep 29 2023 Mark Syms <mark.syms@citrix.com> - 3.0.12-3
- - rebuild
- * Fri Sep 29 2023 Mark Syms <mark.syms@citrix.com> - 3.0.12-2
- - CP-45514: set ownership and perms on backend device
- * Tue Sep 26 2023 Mark Syms <mark.syms@citrix.com> - 3.0.12-1
- - Support IPv6 for NFS ISO SR
- - CA-339581: Report NFS version incompatibilities for ISO SRs
- - CA-381221: Increase NFS timeouts to the expected value
- * Wed Sep 06 2023 Mark Syms <mark.syms@citrix.com> - 3.0.11-1
- - CA-282738: fix bad exception thrown in mountOverSMB
- - CA-372064: storage-init don't try to make Local storage if it already exists.
- - CA-355289: if the SR is not plugged on GC startup wait a little while
- - CP-40871: use returned sector count to calculate GC speed
- - CP-40871: use VHD allocation size in checking canLiveCoalesce
- - CA-379315 Use XE_SR_ERRORCODES in the LVM journaller
- - Update multipath.conf with support for HP/HPE MSA storage appliances
- - Fix use of vdi-ref when static vdis are used

* Fri Jan 12 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.0.10-1.4
- Add 0023-Repair-coverage-to-be-compatible-with-8.3-test-env.patch
- Add 0024-Support-IPv6-for-NFS-ISO-SR.patch
- Add 0025-Support-IPv6-in-Ceph-Driver.patch
- Import sm 8.2 LINSTOR fixes on 8.3:
- Assume VDI is always a VHD when the info is missing during cleanup
- Remove SR lock during thin attach/detach
- Ensure database is mounted during scan
- Restart drbd-reactor in case of failure
- Retry in case of failure during mkfs call on database
- Avoid diskless creation when a new resource is added
- Remove diskless after VDI.detach calls
- Ignore volumes marked with a DELETED flag in GC
- Ensure detach never fails on plugin failure
- Don't try to repair persistent volumes in GC
- Wait for a "ready" state during attach to open the DRBD path
- GC support resized volumes
- Ensure we can deflate on any host after a journal rollback
- Robustify SR destroy

* Tue Sep 19 2023 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.0.10-1.2
- Import sm 8.2 LINSTOR fixes on 8.3:
- Ensure we always have a device path during leaf-coalesce calls
- Always use lock.acquire() during attach/detach
- Make sure hostnames are unique at SR creation
- Ensure we can attach non-special static VDIs
- Ensure we can detach when deflate call is not possible

* Tue Sep 19 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.0.10-1.1
- Rebase on 3.0.10-1
- Drop 0002-Add-TrueNAS-multipath-config.patch
- Drop 0020-Fix-blktap-error-mapping-in-python3.patch
- *** Upstream changelog ***
- * Tue Aug 01 2023 Mark Syms <mark.syms@citrix.com> - 3.0.10-1
- - CA-379329: install and enable sr_health_check_timer
- * Wed Jul 05 2023 Mark Syms <mark.syms@citrix.com> - 3.0.9-1
- - FileSR: fix error code
- - FileSR: get rid of unused loadLocked parameter in calls
- - drivers: removed use code in .vdi() and associated loadLocked arg
- - EXTSR: get rid of superfluous explicit line continuations
- - CA-379434: extend wait for multipath to 30 seconds
- - Support recent version of coverage tool (coverage 7.2.5)
- * Wed Jun 21 2023 Mark Syms <mark.syms@citrix.com> - 3.0.8-1
- - CA-374612: extend wait for multipath device arrival to 30 seconds
- - CA-378768: set scheduler on multipath device
- [... rebuilds]
- * Wed May 31 2023 Mark Syms <mark.syms@citrix.com> - 3.0.7-1
- - Set schedulers correctly on 6.x kernel
- [... rebuilds]
- * Wed Mar 29 2023 Mark Syms <mark.syms@citrix.com> - 3.0.6-1
- - remove uninstall operations now unrelated to this package
- - CA-375367 NFS timeout parameters not always set correctly
- - CP-27709: filter error messages about ioctl not supported in trim
- - CA-375968: multi session iSCSI updates
- - Upstream changes to harmonise multipath configuration
- * Tue Feb 21 2023 Mark Syms <mark.syms@citrix.com> - 3.0.4-2
- - More python3 string fixes
- * Mon Feb 20 2023 Mark Syms <mark.syms@citrix.com> - 3.0.4-1
- - Python3 string fixes

* Tue Aug 22 2023 Guillaume Thouvenin <guillaume.thouvenin@vates.tech> - 3.0.3-1.5
- Fix issues when running quicktest on ZFS and LVMoISCSI
- Add 0023-Remove-SR_PROBE-from-ZFS-capabilities-36.patch
- Add 0024-Fix-vdi-ref-when-static-vdis-are-used.patch

* Mon Jul 31 2023 Benjamin Reis <benjamin.reis@vates.fr> - 3.0.3-1.4
- Drop 0006-Re-add-the-ext4-driver-for-users-who-need-to-transit.patch

* Thu Jul 20 2023 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.0.3-1.3
- LINSTOR driver support for XCP-ng 8.3
- Add 0021-Fix-blktap-error-mapping-in-python3.patch
- Add 0022-feat-LinstorSR-import-all-8.2-changes.patch
- Add 0023-feat-LinstorSR-is-now-compatible-with-python-3.patch

* Thu May 04 2023 Yann Dirson <yann.dirson@vates.fr> - 3.0.3-1.1
- Rebase on sm 3.0.3
- Patches for python3 compat.
- Switch interpreter to python3 in drivers.
- Use python36-bitarray not python3-bitarray.
- Use python36-pylint not python3-pylint.
- BuildRequires: python36-mock for %check.
- *** Upstream changelog ***
- * Fri Jan 27 2023 Mark Syms <mark.syms@citrix.com> - 3.0.3-1
- - Include exportname in NBD data for attach
- * Tue Jan 17 2023 Mark Syms <mark.syms@citrix.com> - 3.0.2-1
- - CBT python3 fixes
- * Thu Jan 12 2023 Mark Syms <mark.syms@citrix.com> - 3.0.1-1
- - Check for open tapdisk NBD sockets before shutting down
- * Tue Oct 25 2022  <mark.syms@citrix.com> - 3.0.0-1
- - Migrate to Python3

* Thu Dec 15 2022 Ronan Abhamon <ronan.abhamon@vates.fr> - 2.46.16-1.1
- Rebase on sm 2.46.16
- *** Upstream changelog ***
- * Wed Sep 21 2022 Tim Smith <tim.smith@citrix.com> - 2.46.16-1
- - CA-370572: relinking is a transient property, do not copy to clones
- - CA-370696 Do not attempt to validate device or NFS server paths
- * Wed Aug 31 2022 Mark Syms <mark.syms@citrix.com> - 2.46.15-1
- - CA-353437: give coalesce tracker grace iterations to make progress
- - CA-353437: activate a FIST point in the coalesce tracker for test injection
- - Add multipath configuration for Dell ME4
- * Wed Aug 31 2022  <mark.syms@citrix.com> - 2.46.14-2
- - CA-368585: Remove dependency on lvm2-sm-config
- * Mon Aug 22 2022 Mark Syms <mark.syms@citrix.com> - 2.46.14-1
- - CA-370037: report errors from NFS correctly
- * Tue Aug 16 2022 Mark Syms <mark.syms@citrix.com> - 2.46.13-1
- - Multipath fixes
- * Mon Jul 25 2022 Mark Syms <mark.syms@citrix.com> - 2.46.12-1
- - CA-368769: extend the timeout on tap-ctl close

* Mon Sep 19 2022 Ronan Abhamon <ronan.abhamon@vates.fr> - 2.46.11-1.2
- Re-enable tests, test file coverage rate is now 100%

* Thu Sep 01 2022 Ronan Abhamon <ronan.abhamon@vates.fr> - 2.46.11-1.1
- Rebase on CH 8.3 Preview
- Remove patches merged upstream
- Keep other patches still necessary.

* Wed Jun 22 2022 Mark Syms <mark.syms@citrix.com> - 2.46.11-1
- Remove use of eval
- Raise explicit error in case of NFS mount failure

* Wed May  4 2022 Mark Syms <mark.syms@citrix.com> - 2.46.10-2
- Add requires for lvm2-sm-config

* Fri Mar 18 2022 Mark Syms <mark.syms@citrix.com> - 2.46.10-1
- CA-365359: cope with on_slave.is_open passing server=None
- Fix timeout_call: alarm must be reset in case of success
- timeout_call returns the result of user function now

* Mon Dec 13 2021 Mark Syms <mark.syms@citrix.com> - 2.46.9-1
- CP-38670: add dependency on python-monotonic
- Update product identification for QNAP iSCSI storage

* Thu Oct 07 2021 Mark Syms <mark.syms@citrix.com> - 2.46.8-1
- CA-359453: fallback to rename in snapshot if hardlinks are not supported
- CP-38316: Update pathchecker for EQL to be readsector0

* Mon Oct 04 2021 Mark Syms <mark.syms@citrix.com> - 2.46.7-1
- CP-38283: Support explicit NFS 4.0 version choice
- CP-38283: allow any NFS 4.x version
- CA-359621: fix regression in error handler

* Tue Sep 28 2021 Mark Syms <mark.syms@citrix.com> - 2.46.6-1
- CA-359304: ignore relinking and activating tags in update_sm_config

* Wed Sep  8 2021 Mark Syms <mark.syms@citrix.com> - 2.46.5-2
- Dummy build

* Tue Sep 07 2021 Mark Syms <mark.syms@citrix.com> - 2.46.5-1
- CA-327302: report signals in CommandException
- CA-352880: when deleting an HBA SR remove the kernel devices

* Mon Aug 09 2021 Mark Syms <mark.syms@citrix.com> - 2.46.4-1
- CA-356761: ensure that children are always reloaded on relink
- CA-334762: Fix VDI import with storage driver domains
- CA-356983: ignore more rules on td/nbd devices

* Thu Jul 15 2021 Mark Syms <mark.syms@citrix.com> - 2.46.3-1
- CA-356645: use "self.session is None" not "self.session == None"
- CP-37498: Fix all E711 errors in SM

* Tue Jul 13 2021 Mark Syms <mark.syms@citrix.com> - 2.46.2-1
- CA-356234: don't run repair if parent is raw
- CA-356102: retry LV commands when 'Incorrect checksum in metadata' reported
- CA-356411: correct check for session

* Wed Jun 16 2021 Mark Syms <mark.syms@citrix.com> - 2.46.1-1
- CA-355401: make post attach scan best effort and report errors
- CA-355289: ensure xapi is initialised before starting GC
- If not NFS ACLs provided, assume everyone (external contributor)

* Mon May 24 2021 Mark Syms <mark.syms@citrix.com> - 2.46.0-1
- CA-354692: check for device parameter in create/probe calls

* Tue May 18 2021 Mark Syms <mark.syms@citrix.com> - 2.45.0-1
- Remove OCFS SRs
- CA-354228: Reinstate load calls in _pathrefresh

* Wed Apr 14 2021 Mark Syms <mark.syms@citrix.com> - 2.44.0-2
- CP-36641: Fix build dependencies

* Tue Mar 02 2021 Mark Syms <mark.syms@citrix.com> - 2.44.0-1
- CA-352165: check for device key in dconf before referencing

* Tue Feb 23 2021 Mark Syms <mark.syms@citrix.com> - 2.43.0-1
- CA-351674: Reduce the frequency and cost of pathrefresh calls

* Tue Feb 16 2021 Mark Syms <mark.syms@citrix.com> - 2.42.0-1
- Support IPv6 in NFS

* Wed Feb 03 2021 Mark Syms <mark.syms@citrix.com> - 2.41.0-1
- Remove deprecated rawhba package

* Mon Jan 25 2021 Mark Syms <mark.syms@citrix.com> - 2.40.0-1
- CA-350871: optimise locking in LVHDSR snapshot

* Wed Jan 06 2021 Mark Syms <mark.syms@citrix.com> - 2.39.0-1
- New release
- PEP8 housekeeping cleanup
- CA-350437: simplify 02vhd-cleanup to only handle LVM refcounts

* Thu Dec 10 2020 Mark Syms <mark.syms@citrix.com> - 2.38.0-1
- New release

* Fri Dec 04 2020 Mark Syms <mark.syms@citrix.com> - 2.37.0-1
- Improved logging

* Mon Nov 30 2020 Mark Syms <mark.syms@citrix.com> - 2.36.0-2
- Enable sonarqube

* Wed Nov 25 2020 Mark Syms <mark.syms@citrix.com> - 2.36.0-1
- CA-349188: only check for the initator name, alias is optional

* Fri Nov 06 2020 Mark Syms <mark.syms@citrix.com> - 2.35.0-1
- EMC-75: add config for new DellEMC PowerStore array
- Add TrueNAS ALUA support

* Tue Oct 13 2020 Mark Syms <mark.syms@citrix.com> - 2.34.0-1
- Stage 1 futurize on SM
- CA-346583: make set-iscsi-initiator idempotent
- CA-346590: log when activating flag removed

* Wed Sep 30 2020 Mark Syms <mark.syms@citrix.com> - 2.33.0-1
- CA-333441 create directory for set-iscsi-initiator
- CA-344254: address race between GC and VDI activate

* Tue Sep 15 2020 Mark Syms <mark.syms@citrix.com> - 2.32.0-1
- Use object member session instead of creating a new one
- CA-344254: add unit tests for blktap2.activate
- CA-344254: add tests for coalesce

* Tue Sep 01 2020 Mark Syms <mark.syms@citrix.com> - 2.31.0-1
- CP-32998 - Use cgclassify to move tapdisk into pre configured vm.slice and vm-blktap.slice
- CA-343115: ensure device symlinks are created correctly even when path count not required

* Mon Jul 13 2020 Mark Syms <mark.syms@citrix.com> - 2.30.0-1
- CA-340203: remove name header from mpath map list
- CP-34042: Add unit tests for mpath utils and remove dead code
- CP-33617 Initial set of unit tests for mpathcount.py
- Add unit tests for blktap2.TapCtl
- CP-33629 - Unit tests for HBA
- CA-341777: pass args in the correct order

* Fri May 29 2020 Mark Syms <mark.syms@citrix.com> - 2.29.0-1
- CA-339329 firstboot scripts shouldn't sync DB when upgrading

* Wed May 20 2020 Mark Syms <mark.syms@citrix.com> - 2.28.0-1
- CA-338619: log the hostname when asking slaves
- CA-332978: Ensure that multipath reconnects after failure
- CA-332978: Only force reload multipath on start

* Tue Apr 21 2020 Mark Syms <mark.syms@citrix.com> - 2.27.0-1
- CA-337772: remove dead code handling qcow files.
- CP-33292: add read caching capability flags

* Tue Apr 07 2020 Mark Syms <mark.syms@citrix.com> - 2.26.0-1
- CA-337352: just idempotently unlink NBD symlinks on deactivate

* Wed Mar 25 2020 Mark Syms <mark.syms@citrix.com> - 2.25.0-1
- CA-331454: Handle interrupted SMGC during hotfix application

* Fri Mar 13 2020 Mark Syms <mark.syms@citrix.com> - 2.24.0-1
- CA-335092: after an update we use the new SM but the NBD link won't be there

* Wed Mar 11 2020 marksy <mark.syms@citrix.com> - 2.23.0-1
- CA-335771: call tap-ctl close with a timeout

* Mon Mar 09 2020 Mark Syms <mark.syms@citrix.com> - 2.22.0-1
- Filter out USB storage from HBA device, since USB storage is already managed by udev SM
- CA-335351: ensure async tasks are cleaned up
- CP-31856 - Return nbd socket as parameter to XAPI
- CA-335178: If we don't need tap we dont have NBD
- CA-335178: Fix unitialized variable
- CA-335721: Add missing case blktap2 for cached devices.

* Wed Jan  8 2020 Mark Syms <mark.syms@citrix.com> - 2.21.0-1
- CA-333441: add script to update iSCSI IQN and restart daemons

* Mon Jan 06 2020 Mark Syms <mark.syms@citrix.com> - 2.20.0-1
- CP-31089: Move storage firstboot scripts out of xenserver-firstboot
- CA-332890: trigger mpath count when multipath dm device added/removed
- CA-331453: Ensure all v1 GC processes are killed upon master promotion.
- CA-332806: Lock speedfile and use atomic write to prevent corruption during abort.
- Fix xe-mount-iso-sr command name in error message

* Thu Dec 12 2019 Mark Syms <mark.syms@citrix.com> - 2.19.0-1
- CA-332806 - Fix type mismatch when processing speed file
- CA-332801: use slightly longer timeout for testing connectivity

* Tue Dec 10 2019 Mark Syms <mark.syms@citrix.com> - 2.18.0-1
- CP-32433: refine the conditions under which mpath count will trigger
- CP-32433: kick the multipath and usb scanners at start
- CA-324971: lock LVM commands to avoid concurrency clashes
- CP-32204: Dynamic limits to leaf coalesce using storage speed estimate.

* Wed Nov  6 2019 Mark Syms <mark.syms@citrix.com> - 2.17.0-1
- CA-329845 - Remove usage of credentials file for CIF
- CA-329841 - Sanitise chaps incoming usage

* Tue Oct 29 2019 Mark Syms <mark.syms@citrix.com> - 2.16.0-1
- LUNperVDI supports multipathed LUN

* Fri Oct 11 2019 Mark Syms <mark.syms@citrix.com> - 2.15.0-1
- CA-328536: If we give up on leaf-coalesce make sure we do so until process exits

* Wed Sep 25 2019 Mark Syms <mark.syms@citrix.com> - 2.14.0-1
- CP-32203: report VHD sizes when abandoning leaf coalesce
- CA-327382: reap child processes

* Mon Sep 02 2019 Mark Syms <mark.syms@citrix.com> - 2.13.0-1
- CP-32027: remove snapwatchd

* Wed Aug 07 2019 Mark Syms <mark.syms@citrix.com> - 2.12.0-1
- CA-324815: lock the SR in GC before deleting orphans.

* Tue Aug 06 2019 Mark Syms <mark.syms@citrix.com> - 2.11.0-1
- CA-323702: fcntl locks are per process, so need to reference count.

* Mon Jul 22 2019 Mark Syms <mark.syms@citrix.com> - 2.10.0-1
- CA-315318: optimise mpathcount, we only need to call once per SCSI ID
- CA-315318: refine udev trigger conditions for mpath count

* Tue Jul 09 2019 Mark Syms <mark.syms@citrix.com> - 2.9.0-1
- CA-323050: check that at least one iSCSI session established
- CA-323394: Stop tapdisk process inheriting fds from sm process
- CP-23299: add test for HFX-651

* Mon Jun 03 2019 Mark Syms <mark.syms@citrix.com> - 2.8.0-1
- Avoid exceptions from LV existance checks
- CA-314822: use scanLocked in should_prempt to avoid SR contents changing during scan

* Wed May 08 2019 Mark Syms <mark.syms@citrix.com> - 2.7.0-1
- CA-316157: Check if any garbage collection needs to be done before going quiet
- CA-309979 Fix Storage Manager initialisation
- CA-315152: gc_force needs to take gc_active lock not running

* Tue Apr 16 2019 Mark Syms <mark.syms@citrix.com> - 2.6.0-1
- CA-314717 Explicit stdout and stderr for scan services

* Tue Apr 09 2019 Mark Syms <mark.syms@citrix.com> - 2.5.0-1
- CA-312605: Allow config of pause in _gcLoop.

* Mon Apr 01 2019 Mark Syms <mark.syms@citrix.com> - 2.4.0-1
- CA-313960: ensure that mpathcount trigger correctly

* Fri Mar 22 2019 Mark Syms <mark.syms@citrix.com> - 2.3.0-1
- CA-311551: do not trigger mpath count for nbd devices
- CA-298641: be more benign on removing host tag
- CA-273708: improve VHD scan for file-based SR
- CA-312608: Set scheduler to noop for tdX devices

* Fri Feb 15 2019 Mark Syms <mark.syms@citrix.com> - 2.2.0-1
- Update MAINTAINERS file
- CA-247723: wait for udevadm settle in LVM create

* Wed Feb 06 2019 Mark Syms <mark.syms@citrix.com> - 2.0.0-1
- Update NFS unit tests to make intent clearer
- Properly strip output of scsi_id
- NFS 4.1 Support
- CA-262506: Remove sec=ntlm from SMBSR mount option
- cifs.conf: Remove cifs.conf
- CP-30167: handle changed blkback kthread notification

* Wed Jan 23 2019 Mark Syms <mark.syms@citrix.com> - 1.37.0-1
- CA-285844: update mpathcount so that it can remove multipath information as well as add it
- CA-293816: convert OSError to CommandException
- CA-293816: make _clonecleanup safer
- CA-293816: stop and rollback earlier in case of errors

* Tue Jan 08 2019 Mark Syms <mark.syms@citrix.com> - 1.36.0-1
- Update MAX_VHD_SIZE to 2088960MiB (2040GiB).

* Wed Nov 28 2018 Mark Syms <mark.syms@citrix.com> - 1.35.0-1
- CA-303252 Generalise the pipe kicker
- CA-303252 Make the multipath count a kickable socket service
- CA-302773: move import of plugins until we need it.

* Mon Nov 26 2018 Tim Smith <tim.smith@citrix.com> - 1.34.0-2.0
- CA-303252 update mpathcount to use the kickpipe trigger

* Wed Nov 21 2018 Mark Syms <mark.syms@citrix.com> - 1.34.0-1
- Revert "CP-27709: suppress error if blkdiscard fails on full provisioned lun"

* Fri Nov 16 2018 Mark Syms <mark.syms@citrix.com> - 1.33.0-1
- CP-27709: suppress error if blkdiscard fails on full provisioned lun
- CP-29603: load plugins for key discovery
- CP-29688:encryption key lookup plugin
- CP-29688: Install the test plugin
- CP-29689: Integrate encryption key lookup into Storage Manager
- CP-29755: Implement VDI.create for encrypted VDIs
- If key_hash is present in sm_config then assign to variable
- CP-29778: detect encrypted VHDs during SR scan
- keymanagerutil: generate completely random keys & store them in base64
- Added alphanumeric key generator and removed key field from json
- CA-302505: Add key_hash to sm_config of snapshots if it exists
- CA-302770: Added key_hash key to sm_config_keep
- CP-29955: pass VDI UUID too to key lookup plugin
- CP-29955: Log failures in key lookup plugins

* Fri Nov 09 2018 Mark Syms <mark.syms@citrix.com> - 1.32.0-1
- CA-286144: add usb-scan systemd units
- CA-302514 increase default NFS timeouts

* Thu Nov  1 2018 Mark Syms <mark.syms@citrix.com> - 1.31.0-1.0
- CA-297628: Tweak mpath_dmp.py for using device-mapper-multipath-0.4.9
- Add versioned dependency for LVM2

* Tue Oct 30 2018 Liang Dai <liang.dai1@citrix.com> - 1.30.0-1
- CA-294319: CLI command sr-create failed: Error code: SR_BACKEND_FAILURE_77

* Mon Oct 15 2018 Mark Syms <mark.syms@citrix.com> - 1.29.0-1
- Remove obsolete 39-multipath.rules file from install rules
- CA-296534: if we give up on snapshot-coalesce, don't fall through to live coalesce

* Tue Oct 09 2018 Mark Syms <mark.syms@citrix.com> - 1.28.0-1
- CP-28924: Declare thin provisioning capability for file based SRs

* Mon Oct 01 2018 Mark Syms <mark.syms@citrix.com> - 1.27.0-1
- CA-292588 Add debug to header checking
- CA-297698 improve assert message

* Fri Sep 07 2018 Mark Syms <mark.syms@citrix.com> - 1.26.0-1
- CA-247723: Use fuser to report on who has the device open
- CA-295775 Fix handling of multipath events
- CA-295775 Escape from systemd-udevd's control group

* Tue Aug 14 2018 Mark Syms <mark.syms@citrix.com> - 1.25.0-1
- CA-294975: ensure chap settings are removed from discovery db when not used
- CA-295846: call vgs with --readonly
- CA-295861: extract processname correctly when there are no arguments

* Wed Jul 25 2018 Mark Syms <mark.syms@citrix.com> - 1.24.0-2.0
- Remove obsolete LVM config overrides, now in LVM package

* Mon Jul 16 2018 Mark Syms <mark.syms@citrix.com> - 1.24.0-1
- CA-292144 Avoid having an invalid leaf while taking a snapshot

* Fri Jun 29 2018 Mark Syms <mark.syms@citrix.com> - 1.23.0-1
- CA-292268: Retry tap-ctl.Spawn on failure
- Blacklist nbd devices

* Mon Jun 25 2018 Mark Syms <mark.syms@citrix.com> - 1.22.0-1
- CA-247723: extend timeout and log lsof if still busy afterwards

* Mon Jun 18 2018 Mark Syms <mark.syms@citrix.com> - 1.21.0-1
- CA-290491: do not leaf coalesce VDIs with allow_caching=true
- Add LIO-ORG iscsi device configuration
- Add RBD devices to blacklist
- CA-292103: do not scan for and clear signatures in new volumes

* Fri May 25 2018 marksy <mark.syms@citrix.com> - 1.20.0-1.0
- CA-247723: check lsof when EBUSY on device
- CA-287511: add ENOENT to acceptable IO retry errors in VDI load to avoid race
- CA-247723: refactor open exclusive and allow a single shot retry to counter new device tool race
- CA-277616: add retry into pathexists to diagnose underlying issue
- CA-288738: add dependency on multipathd.service
- CA-289543: get mpathcount to return the GFS2 multipath count
- CP-27874: Failed iSCSI paths are not reconnected to automatically
- CA-286622: fix logging issue
- CA-288100: in case the VDI is removed in between
- CA-288222: deactivate LV on slaves before changing the LVM layout
- CA-289979: Refine ENOENT error handling while deleting VDI

* Fri May 25 2018 marksy <mark.syms@citrix.com> - 1.19.0-1.0
- Release 1.19.0

* Tue Apr 17 2018 marksy <mark.syms@citrix.com> - 1.18.0-1.31
- CA-287884: reuse existing session in _get_pool_config
- CA-287883: Refresh CBT log LV on slave after snapshot

* Tue Apr 10 2018 marksy <mark.syms@citrix.com> - 1.18.0-1.30
- CA-283724: Don't attach CBT log when VDI attached in RO mode
- CA-274386: Ensure that get_supported_nfs_versions always returns a list
- CA-287504: Import Rackspace fix to Delete LUN on detach of RawISCSI
- CA-247723: add log trace for error on exclusive open to track reason
- CA-287286: correct error logging message read not write
- CA-277128: Remove broken RRD code from SM

* Tue Mar 27 2018 marksy <mark.syms@citrix.com> - 1.18.0-1.29
- CA-274822: SR-detach fails with parameter errors(add extra logs)
- CA-268337: have resetvdis.py request the GC abort before acquiring locks
- update series with upstream commit ids

* Fri Feb 16 2018 marksy <mark.syms@citrix.com> - 1.18.0-1.28
- Actually remove the patches which are now on github
- CA-267460: Make device multipath eligibility check more robust
- CA-273731: call _testHost before trying to mount ISO SR NFS share
- CA-283272: Skip multipath validity check if device is not up yet
- CA-283207: Modify mpathcount script to work for GFS2 SRs

* Tue Jan 30 2018 Mark Syms <mark.syms@citrix.com> - 1.18.0-1.27
- Release version 1.18.0

* Tue Jan 30 2018 marksy <mark.syms@citrix.com> - 1.17.1-1.26
- Move patches to github, update series and context in patches
- Merge CBT upstream, move patches and make minor corrections
- Update series with upstream commit ids
- Rationalise installation of pip packages
- Move the rest of Inverness patches to GH and update series references

* Wed Jan 10 2018 marksy <mark.syms@citrix.com> - 1.17.1-1.25
- CA-274584: this fixes reboot issues with iscsi sessions
- CA-276751: Make sure there are no left-overs in wwids file
- CA-277346: Fix flawed parsing of /proc/<pid>/cmdline, split on NUL

* Tue Dec 12 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.24
- CA-261907: cleanup refcounts when deleting unused volume
- CA-276601: update patch with correct ticket number

* Wed Dec 06 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.23
- CA-270011: Handle snapshotting a snapshot in a CBT chain
- Redo unit tests helpers for CBT chain setup and verification
- CA-274115: Handle RAW VDIs when getting block tracking status
- CA-257740: fix HA broken for NFS

* Tue Oct 31 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.22
- CA-268695: Do not strip bitmap strings
- CA-269654: Make tapdisk-unpause CBT aware
- CA-269988: Remove VDI from MGT metadata on data-destroy
- CA-269652: Live disk is not paused before bitmap coalesce on snapshot delete

* Wed Oct 25 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.21
- CA-267339: Race conditions when activating-deactiving cbtlog files
- CA-267032: add ESRCH to list of acceptable errors in pid search
- CP-24532: trigger pusb scan
- CP-24548: set usb path for VDI
- CP-25157: refine document of get_usb_node

* Thu Oct 19 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.20
- CA-269013: Check SR capability before querying CBT status
- CA-264210: Intellicache only supports a single base disk with deltas
- CA-269653: Make CBT changes only for user created snapshots

* Mon Oct 16 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.19
- CA-269166: remove bare print from error path

* Thu Oct 12 2017 marksy <mark.syms@citrix.com> - 1.17.1-1.18
- CA-255945: fix race on mkdir
- CP-24279: Use autospec=True on mocks for unit test
- CA-267352: Update local state of VDI after XAPI update
- CP-23681: [Unit test] Test snapshot creation on CBT VDI when OOS
- CP-23549: Make VDI._db_update use current CBT state of disk
- CP-25002: Update XAPI db with CBT state on snapshot

* Tue Oct 03 2017 marksy <mark.syms@citrix.com> - 1.17.1-xs.1+1.17
- CP-24893: test coverage improvements for refcount
- Reenable CP-23549__Update_VDI_CBT_status_on_SR_scan

* Wed Sep 27 2017 marksy <mark.syms@citrix.com> - 1.17.1-xs.1+1.16
- CP-23557: Extend ISOSR unit tests for SMB protocol
- CP-23535: Extend tap-ctl create to consdier CBT parameters
- CA-265461: Exclude source vdi whilst calculating changed blocks
- CP-22030: Unit tests for export-changed-blocks API
- CP-23547: On cbt error disable CBT and generate error for XenCenter
- CP-23405: Support calculation of changed blocks for resized VDIs
- CP-24536: Add support for CBT-specific messages to xapi and SM
- CP-24566: Change export_changed_blocks to list_changed_blocks
- CP-24593: Remove changes unrelated to CBT from patch introduced for CP-23919
- CP-24592: Resize in VDI should remain unimplemented
