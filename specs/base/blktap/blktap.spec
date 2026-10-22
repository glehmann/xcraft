%global package_speccommit 22717df73a3c1a3fad92fc1fc268b37bc45b4eeb
%global package_srccommit v4.0.8

Summary: blktap user space utilities
Name: blktap
Version: 4.0.8
Release: 1%{?xsrel}.1%{?dist}
License: BSD
Group: System/Hypervisor
URL: https://github.com/xapi-project/blktap
Source0: blktap-4.0.8.tar.gz

BuildRoot: %{_tmppath}/%{name}-%{release}-buildroot
Obsoletes: xen-blktap < 4
BuildRequires: e2fsprogs-devel, libaio-devel, systemd, autoconf, automake, libtool, libuuid-devel
BuildRequires: xen-libs-devel, zlib-devel, libcmocka-devel, git, lcov
BuildRequires: openssl-devel >= 3.0.0
BuildRequires: gcc
BuildRequires: binutils
BuildRequires: liblsan-static
%{?_cov_buildrequires}
Requires(post): systemd
Requires(post): /sbin/ldconfig
Requires(preun): systemd
Requires(postun): systemd
Requires(postun): /sbin/ldconfig

Conflicts: sm < 4.0.0

Provides: blktap(nbd) = 2.0

# # XCP-ng patches
# # Required by XOSTOR. Upstream PR: https://github.com/xapi-project/blktap/pull/378
# Patch1001: 0001-Add-an-option-to-never-resolve-parent-path-when-vhd-.patch
# # Required by sm (qcow2). Upstream PR: https://github.com/xapi-project/blktap/pull/417
# Patch1002: 0002-Add-an-option-to-use-backup-footer-when-vhd-util-que.patch

# cmocka doesn't play nice with LTO
%global _lto_cflags %{nil}

%description
Blktap creates kernel block devices which realize I/O requests to
processes implementing virtual hard disk images entirely in user
space.

Typical disk images may be implemented as files, in memory, or
stored on other hosts across the network. The image drivers included
with tapdisk can map disk I/O to sparse file images accessed through
Linux DIO/AIO and VHD images with snapshot functionality.

This packages includes the control utilities needed to create
destroy and manipulate devices ('tap-ctl'), the 'tapdisk' driver
program to perform tap devices I/O, and a number of image drivers.

%package devel
Summary: BlkTap Development Headers and Libraries
Requires: blktap = %{version}
Group: Development/Libraries

%description devel
Blktap and VHD development files.

%prep
%autosetup -p1 -S git
%{?_cov_prepare}

%build
%{?_cov_make_model:%{_cov_make_model misc/coverity/model.c}}
echo -n %{version} > VERSION
sh autogen.sh
# The following can be used for leak tracing
#%%configure LDFLAGS="$LDFLAGS -lrt -static-liblsan" CFLAGS="$CFLAGS  -Wno-stringop-truncation -fsanitize=leak -ggdb -fno-omit-frame-pointer"
#%%configure CFLAGS="$CFLAGS -Wno-stringop-truncation -Wno-error=analyzer-malloc-leak -Wno-error=analyzer-use-after-free -Wno-error=analyzer-double-free -Wno-error=analyzer-null-dereference -fanalyzer"
%configure CFLAGS="$CFLAGS -Wno-stringop-truncation"
%{?_cov_wrap} make %{?coverage:GCOV=true}

%check
make clean
make check GCOV=true || (find mockatests -name \*.log -print -exec cat {} \; && false)
mkdir -p %{buildroot}/testresults
./collect-test-results.sh %{buildroot}/testresults

%install
rm -rf %{buildroot}
make install DESTDIR=%{buildroot}
%{?_cov_install}
mkdir -p %{buildroot}%{_localstatedir}/log/blktap
%if 0%{?coverage:1}
cd ../ && find -name "*.gcno" | grep -v '.libs/' | xargs -d "\n" tar -cvjSf %{buildroot}/%{name}-%{version}.gcno.tar.bz2
%endif
## Remove libtool archive files; they should never be installed
rm -f %{buildroot}%{_libdir}/*.la
## Remove static libraries; they should not be used by other packages
rm -f %{buildroot}%{_libdir}/*.a

%files
%defattr(-,root,root,-)
%docdir /usr/share/doc/%{name}
/usr/share/doc/%{name}
%{_libdir}/*.so.*
%{_bindir}/vhd-util
%{_bindir}/vhd-index
%{_bindir}/tapback
%{_bindir}/cpumond
%{_sbindir}/cbt-util
%{_sbindir}/lvm-util
%{_sbindir}/tap-ctl
%{_sbindir}/td-util
%{_sbindir}/td-rated
%{_libexecdir}/tapdisk
%{_sysconfdir}/logrotate.d/blktap
%{_sysconfdir}/cron.daily/prune_tapdisk_logs
%{_sysconfdir}/xensource/bugtool/tapdisk-logs.xml
%{_sysconfdir}/xensource/bugtool/tapdisk-logs/description.xml
%{_localstatedir}/log/blktap
%{_unitdir}/tapback.service
%{_unitdir}/cpumond.service

%files devel
%defattr(-,root,root,-)
%doc
%{_libdir}/*.so
%{_includedir}/vhd/*
%{_includedir}/blktap/*
%if 0%{?coverage:1}
/%{name}-%{version}.gcno.tar.bz2
%endif

%post
/sbin/ldconfig
%systemd_post tapback.service
%systemd_post cpumond.service

%preun
%systemd_preun tapback.service
%systemd_preun cpumond.service

%postun
/sbin/ldconfig
%systemd_postun tapback.service
%systemd_postun cpumond.service
if [ $1 -eq 0 ]; then
    rm -f %{_sysconfdir}/udev/rules.d/65-md-incremental.rules
fi

# The posttrans invocation of ldconfig is needed because older
# versions of blktap did not have ldconfig in their postun script.
%posttrans -p /sbin/ldconfig

%{?_cov_results_package}

%package testresults
Group:    System/Hypervisor
Summary:  test results for blktap package

%description testresults
The package contains the build time test results for the blktap package

%files testresults
/testresults

%package -n vhd-util-standalone
Group:   System/Hypervisor
Summary: Standalone vhd-util binary
Conflicts: blktap

%description -n vhd-util-standalone
The package contains a standalone vhd-util binary which can be installed
without requiring other libraries

%files -n vhd-util-standalone
%{_bindir}/vhd-util
%{_libdir}/libvhd.so.*
%{_libdir}/libblockcrypto.so.*

%changelog
* Fri Jun 26 2026 Yann Dirson <yann.dirson@vates.tech> - 4.0.8-1
- Sync with 4.0.8-1
- Disable application of unported XCP-ng patches for now
- *** Upstream changelog ***
  * Wed Feb 4 2026 Lunfan Zhang <lunfan.zhang@cloud.com> - 4.0.8-1
  - revert "CP-311026: advertise flush cache as no-op to blktap"

  * Wed Jan 28 2026 Mark Syms <mark.syms@citrix.com> - 4.0.7-1
  - CP-311026: advertise flush cache as no-op to blktap
  - Prevent segfault of vhd-util scan on VHD with corrupt footer

  * Thu Nov 13 2025 Mark Syms <mark.syms@citrix.com> - 4.0.6-1
  - Use libc API for xattr
  - Use memory barriers defined by xen headers
  - CA-420659: don't fail on unknown options in NBD

  * Thu Aug 28 2025 Mark Syms <mark.syms@cloud.com> - 4.0.5-1
  - CA-416464: return BLKIF_RSP_EOPNOTSUPP for EOPNOTSUPP

  * Mon Aug 11 2025 Mark Syms <mark.syms@cloud.com> - 4.0.4-1
  - Fix coalesced size conversion in vhd-util-coalesce
  - CA-414626: initialize cpumon even if lowmem initialization failed
  - CA-400404: remove low memory mode

  * Tue Jun 24 2025 Mark Syms <mark.syms@cloud.com> - 4.0.3-1
  - CA-412802: drop redundant udev rule override
  - CP-308276: vhd_batmap_header_offset only returns 0 thus is void.
  - CP-308382: fix sign conversion in coalesce

  * Tue Apr 29 2025 Mark Syms <mark.syms@cloud.com> - 4.0.2-1
  - Fix several Coverity detected errors

  * Tue Apr 15 2025 Mark Syms <mark.syms@cloud.com> - 4.0.1-1
  - CP-54256: log errors when reporting EOPNOTSUP
  - CA-408175: distinguish logging for long NBD operations

  * Tue Jan 21 2025 Mark Syms <mark.syms@cloud.com> - 4.0.0-1
  - CP-35551: remove use of kernel blkatp2 driver
  - CA-404370: enable NBD client only after completing handshake

  * Mon Jan 06 2025 Mark Syms <mark.syms@cloud.com> - 3.55.5-1
  - Add an option to never resolve parent path when vhd-util query is called

* Tue Mar 04 2025 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.55.4-1.1
- Sync with 3.55.4-1
- *** Upstream changelog ***
  * Tue Dec 03 2024 Mark Syms <mark.syms@cloud.com> - 3.55.4-1
  - CA-392151 lcache.c uses wrong buffer size definition
  - CA-403297: when storing read-through do not mirror
  * Mon Nov 18 2024 Mark Syms <mark.syms@cloud.com> - 3.55.3-1
  - CP-52620: enable local caching after opening all other images
  * Tue Nov 05 2024 Tim Smith <tim.smith@cloud.com> - 3.55.2-1
  - CA-387575 Remove static libraries and libtool archives from devel package
  - Correct name of exposed NBD server
  - CA-401174 Filedescriptor leak in NBD option negotiation
  * Mon Sep 16 2024 Tim Smith <tim.smith@cloud.com> - 3.55.1-1
  - Redo the revert of an incorrect NBD FD leak fix
  * Fri Sep 13 2024 Tim Smith <tim.smith@cloud.com> - 3.55.0-1
  - CP-46765 - Fix asan issues in unit tests
  - CA-396659: rationalise bitops and fix overflow issues
  - CA-396659: delete legacy debt utility vhd-update
  - Handle scheduler uuid overflow explictly
  - CP-50952 Autodetect NBD server protocol version on client connect
  - Fix use of incorrect label in error case
  - CA-397082 Properly fix FD leak in NBD server
  - CP-50955 Add a new-style NBD server for each VDI
  * Tue Aug 06 2024 Mark Syms <mark.syms@cloud.com> - 3.54.10-1
  - CA-391882: fix nbd connection fd leak bug
  - Remove some unnecessary build dependencies

* Wed Dec 18 2024 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.54.9-1.2
- Add 0002-Add-an-option-to-use-backup-footer-when-vhd-util-que.patch

* Tue Jun 18 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.54.9-1.1
- Sync with 3.54.9-1
- *** Upstream changelog ***
- * Thu Apr 25 2024 Mark Syms <mark.syms@citrix.com> - 3.54.9-1
- - Address various memory management issues
- * Wed Mar 27 2024 Mark Syms <mark.syms@citrix.com> - 3.54.8-1
- - CP-48048: Blktap fix code coverage (XS8 only)
- - tap-ctl: Improve list output filtering
- - CP-46763: When VBD queue is not ready call the driver completion
- - CP-46778: always issue request via issue_requests
- - CP-46771: separate queue checks for completed and new requests.
- - CP-46764: mask ring event handlers before quiescing the queue
- - CP-46764: When the VBD is quiesced skip checking for requests to submit
- - Fix setting of CFLAGS for mockatests
- - Do not free iconv cd if allocation fails
- - Fix the order of linker options in CBT
- - Fixup various bits of incorrect automake usage
- - CA-384162: Send completions for all partial responses
- - CA-390490: allocate vhd request before locking bat
- - CP-48057: Add unit tests for drivers/scheduler.c

* Mon Apr 08 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.54.6-3.1
- Sync with 3.54.6-3
- *** Upstream changelog ***
- * Fri Jan 26 2024 Mark Syms <mark.syms@citrix.com> - 3.54.6-3
- - Rebuild against libxenstore.so.4

* Mon Jan 22 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.54.6-2.1
- Update to 3.54.6-2
- *** Upstream changelog ***
- * Thu Sep 28 2023 Mark Syms <mark.syms@citrix.com> - 3.54.6-2
- - CP-40871: Enable rate limit control on unpause
- * Thu Sep 07 2023 Mark Syms <mark.syms@citrix.com> - 3.54.6-1
- - CP-40871: return the number of sectors coalesced from vhd-util
- - CP-45025: Avoid retries except in a few specific cases
- - CA-382087: Only write the bitmap if the contents have changed

* Fri Sep 15 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 3.54.5-1.1
- Update to 3.54.5-1
- *** Upstream changelog ***
- * Tue Aug 01 2023 Mark Syms <mark.syms@citrix.com> - 3.54.5-1
- - Report control timeouts correctly
- * Wed Jun 21 2023 Mark Syms <mark.syms@citrix.com> - 3.54.4-1
- - Remove unused and broken libvhdio
- - CA-377624: Guard against NULL tiocb in lio event handler
- * Wed Apr 12 2023 Mark Syms <mark.syms@citrix.com> - 3.54.3-2
- - Rebuild
- * Wed Mar 29 2023 Mark Syms <mark.syms@citrix.com> - 3.54.3-1
- - Rebuild
- * Mon Feb 27 2023 Mark Syms <mark.syms@citrix.com> - 3.54.2-1
- - Rebuild
- * Fri Feb 17 2023 Mark Syms <mark.syms@citrix.com> - 3.54.1-1
- - Rebuild
- * Thu Feb 16 2023 Mark Syms <mark.syms@citrix.com> - 3.54.0-1
- - Release 3.54.0
- - Update compiler set
- - Various NBD server improvements

* Thu Jul 13 2023 Ronan Abhamon <ronan.abhamon@vates.fr> - 3.53.0-1.1
- Add 0001-Add-an-option-to-never-resolve-parent-path-when-vhd-.patch

* Thu Jan 12 2023 Mark Syms <mark.syms@citrix.com> - 3.53.0-1
- Use NBD in IntelliCache
- Use optimized SMP barriers

* Mon Oct 03 2022 Mark Syms <mark.syms@citrix.com> - 3.52.0-1
- CP-40769: make NBD server listen correctly after open

* Tue Sep 20 2022 Mark Syms <mark.syms@citrix.com> - 3.51.7-1
- Remvoe deprecated support for Nutanix HC storage.

* Wed May 18 2022 Mark Syms <mark.syms@citrix.com> - 3.51.6-1
- CA-366761: fix off by one size calculation in snprintf

* Fri May 13 2022 Mark Syms <mark.syms@citrix.com> - 3.51.5-1
- CA-366761: fix coverity issues

* Tue May 03 2022 Mark Syms <mark.syms@citrix.com> - 3.51.4-1
- CA-366614: prune old blktap logs

* Wed Mar 09 2022 Mark Syms <mark.syms@citrix.com> - 3.51.3-1
- Various bugfixes from OpenXT

* Wed Nov 17 2021 Mark Syms <mark.syms@citrix.com> - 3.51.2-4
- Rebuild

* Mon Aug 23 2021 Mark Syms <mark.syms@citrix.com> - 3.51.2-3
- Revert "CA-356983: move udev rules for td devices to sm"

* Thu Aug 12 2021 Mark Syms <mark.syms@citrix.com> - 3.51.2-2
- CA-356983: move udev rules for td devices to sm

* Thu Jul 15 2021 Mark Syms <mark.syms@citrix.com> - 3.51.2-1
- CA-356180: ensure queue is valid before debug dumping it
- CA-356215: Add more error logs to vhd-util and report on stderr
- CA-356508: handle EINTR in tap-ctl IPC

* Mon Jun 07 2021 Tim Smith <tim.smith@citrix.com> - 3.51.1-1
- CA-353265 Disable posix AIO backend

* Wed Jun 02 2021 Mark Syms <mark.syms@citrix.com> - 3.51.0-1
- CA-355145: guard vbd_stats
- CA-183182: Don't error if there are no tapdisks to signal

* Thu Mar 11 2021 Mark Syms <mark.syms@citrix.com> - 3.50.0-1
- CA-35250 - Fix Coverity issues introduced by CP-32853

* Thu Mar 04 2021 Mark Syms <mark.syms@citrix.com> - 3.49.0-1
- CP-32853 - Add posixaio io backend for read only files.

* Mon Jan 18 2021 Mark Syms <mark.syms@citrix.com> - 3.48.0-1
- CP-35115: implement "New fixed style" nbd option negotiation.

* Mon Jan 04 2021 Mark Syms <mark.syms@citrix.com> - 3.47.0-1
- Update to v3.47.0
- CA-350300: remove duplicated and unchecked call to canonpath

* Fri Dec 11 2020 Mark Syms <mark.syms@citrix.com> - 3.46.0-1
- More coverity fixes

* Fri Dec 04 2020 Mark Syms <mark.syms@citrix.com> - 3.45.0-1
- Various coverity fixes

* Fri Nov 06 2020 Mark Syms <mark.syms@citrix.com> - 3.44.0-1
- CA-347165: validate the VHD header blocksize

* Wed Sep 30 2020 Mark Syms <mark.syms@citrix.com> - 3.43.0-1
- CA-345193: allocate sufficient space in normalize_path
- CA-345193: add unit test to exercise memory allocation error

* Tue Sep 15 2020 Mark Syms <mark.syms@citrix.com> - 3.42.0-1
- CA-343797: (Coverity) fix set of Unintentional integer overflow issues
- CA-343796: (Coverity) exit when insufficient args supplied
- Errno model removal.
- CP-34900: remove command lookup by environment
- feat(vhd/canonpath): add support for DRBD resource path
- feat(libvhd): try to open DRBD devices using the LVM layer
- feat(libvhd): when a DRBD device path is given, do not use LVM layer if device is not up to date
- libvhd: properly check data alignment
- CA-344119: return correct error, snprintf doesn't set errno
- CA-344424: ensure we don't get division by 0

* Tue Sep 01 2020 Mark Syms <mark.syms@citrix.com> - 3.41.0-1
- CP-34221 - Remove unused obsolete MD5
- CP-32988 - In CA-225067 it was discovered cgexec which used to wrap
- CA-343704: Correct the error return for vhd_footer_offset_at_eof
- CP-34867: Reduce use of lseek by storing the current offset in the vhd context

* Tue Jul 21 2020 Mark Syms <mark.syms@citrix.com> - 3.40.0-1
- CA-342553: don't try to read encryption key from raw parent
- CA-342578: fix switch case fall through error

* Mon Jul 13 2020 Mark Syms <mark.syms@citrix.com> - 3.39.0-1
- CP-34313: Fix Coverity Nesting_Indent_Mismatch error
- CP-34313: Coverity: correctly check for non-writeable file
- CP-34313: Coverity: refactor to remove dead code
- CP-34313: Coverity: #if out unused code
- CP-34313: Coverity: remove dead code for ctx_match
- CP-34313: Coverity: call driver td_validate function
- CP-34313: Coverity: remove unreachable code in block_cache_hash
- CP-34313: Coverity: fix STACK_USE errors
- CP-34313: Coverity: ensure nbd id is terminated
- CP-34313: Coverity: use PAGESIZE instead of reading sysconf on each request
- CP-34313: Coverity: use valid printf format string
- CP-34313: Coverity: check that event->fd is valid
- CP-34313: Coverity: close sock in error path
- CP-34313: Coverity: ensure sfd is closed
- CP-34313: Coverity: fix several unused value errors
- Coverity: add model for posix_memalign
- CP-34313: free prv->remote in error path

* Wed Jun 17 2020 Mark Syms <mark.syms@citrix.com> - 3.38.0-1
- CA-340619 - Propagate errors from snaphot creation

* Wed May 20 2020 Mark Syms <mark.syms@citrix.com> - 3.37.0-1
- CA-338603: fix out of band write in tapdisk-control

* Tue Apr 21 2020 Mark Syms <mark.syms@citrix.com> - 3.36.0-1
- CA-338080: prevent segfault if stats have been freed
- CA-338180: Ensure string is terminated
- CA-338180: Correct return type of tapdisk_stats_length
- CA-338176: fix resource leaks in vhd-util copy

* Tue Apr 07 2020 Mark Syms <mark.syms@citrix.com> - 3.35.0-1
- Add some unit tests for tap-ctl
- CA-337493: Fix bug in find minors.

* Wed Mar 25 2020 Mark Syms <mark.syms@citrix.com> - 3.34.0-1
- feat(vhd-util-scan): '-m' supports style brace expressions and pattern list
- fix(vhd-util-scan): compare fnmatch return to FNM_NOMATCH instead of FNM_PATHNAME
- CA-335771: timeouts are deltas not absolute deadlines

* Wed Mar 11 2020 marksy <mark.syms@citrix.com> - 3.33.0-1
- CA-335771: correct missing usage info

* Wed Feb 19 2020 Ming Lu <ming.lu@citrix.com> - 3.32.0-1
- CP-32123: Integrate blktap with CCM openssl 1.1.1

* Tue Dec 10 2019 Mark Syms <mark.syms@citrix.com> - 3.31.0-1
- CP-32675 Use the stable Xen interfaces

* Tue Oct 29 2019 Mark Syms <mark.syms@citrix.com> - 3.30.0-1
- CA-327558: add user-space io_getevents() implementation
- CA-327558: switch to user-space io_getevents
- CA-327558: Do not call fadvise on read cached FD
- Drop libaio-compat.h: libaio.h has everything we need now
- Drop tapdisk-filter.c: dead code
- Drop rwio dead code
- Fix debug mode
- Fix build when TEST is defined
- CA-327558: save and restore entire iocb
- CA-327558: iocb union helper functions
- CA-327558: io merge using pread/pwritev

* Fri Oct 11 2019 Mark Syms <mark.syms@citrix.com> - 3.29.0-1
- Unify definition of LIBBLOCKCRYPTO_NAME
- Add major version to LIBBLOCKCRYPTO_NAME
- CP-30739: enable multi-page rings for tapdisk
- CP-30739: mask the ring events while in polling mode

* Wed Sep 11 2019 Mark Syms <mark.syms@citrix.com> - 3.28.0-1
- CA-326370 Check return code from io_getevents()

* Mon Jun 17 2019 Mark Syms <mark.syms@citrix.com> - 3.27.0-1
- CA-318654: set dependency on xenstored

* Mon Jun 10 2019 Mark Syms <mark.syms@citrix.com> - 3.26.0-1
- CA-320241: remove NBD mirror timeouts, they just add fragility
- CP-27247: Move tapdisk to a blkio cgroup

* Mon May 13 2019 Mark Syms <mark.syms@citrix.com> - 3.25.0-1
- CA-314653: add tracing to nbd failure paths
- CA-314653: log if nbd requests take over 20 seconds to complete

* Tue Apr 16 2019 Mark Syms <mark.syms@citrix.com> - 3.24.0-1
- fix vhd-util keypath validation

* Tue Apr 09 2019 Mark Syms <mark.syms@citrix.com> - 3.23.0-1
- Optimise allocate-and-fill of full blocks

* Wed Mar 06 2019 Mark Syms <mark.syms@citrix.com> - 3.22.0-1
- CA-312256: remove extraneous tracing from libvhd

* Fri Feb 08 2019 Mark Syms <mark.syms@citrix.com> - 3.21.0-1
- Address coverity issue in vhd_cache_load
- CA-307886: add tracing for failure cases
- Fix coverity issues in tests
- CA-310477: fix race in create tap-ctl directory

* Wed Jan 23 2019 Mark Syms <mark.syms@citrix.com> - 3.20.0-1
- CA-306397: amend tracing in physical_device_changed.
- CA-306397: consider empty string read from xs_read to be equiv to NULL

* Tue Jan 08 2019 Mark Syms <mark.syms@citrix.com> - 3.19.0-1
- CA-305951: change xattr_get to tolerate ENOTSUP error

* Mon Dec 17 2018 Mark Syms <mark.syms@citrix.com> - 3.18.0-1
- CA-304648: add guard asserts to prevent overwriting the header
- CA-304653: round up the required number of BAT entries so that the entire data size is accomodated
- CA-304945: Only load crypto library when encryption is used
- CA-304562 Stats use-after-free

* Wed Nov 28 2018 Mark Syms <mark.syms@citrix.com> - 3.17.0-1
- CA-303668: check that both name and new name are passed to vhd-util copy
- CA-303671: initialise variable so that -B functions
- CA-302889: Correct vhd-util copy help strings

* Tue Nov 27 2018 Bob Ball <bob.ball@citrix.com> - 3.16.0-2
- CP-30093: Separate sub-package for vhd-util and dependencies

* Fri Nov 16 2018 Mark Syms <mark.syms@citrix.com> - 3.16.0-1
- CA-299175 Guard against NULL devices
- VHD keyhash support
- VHD encryption support
- remove unused functions
- Only set the image keyhash if we've loaded it
- CP-29602:  remove replication of check for vhd encrpyption
- CP-29690: Update tap-ctl open to take encryption key
- CP-29690: Pass encryption key data down to block-crypto
- CP-29777: VDI.snapshot for encrypted VDIs
- CP-29812: split crypto routines into shared library and dynamically load
- CP-29890: Implement vhd-util copy with optional encryption.

* Mon Oct 15 2018 Mark Syms <mark.syms@citrix.com> - 3.15.0-1
- CP-24320: fix resource leak in vhd-journal.c
- CP-24319: fix resource leak in tapdisk_vbd_add_block_cache
- CP-24321: fix resource leak in vhd_w2u_decode_location
- CP-24323: fix resource leak in vhd_journal_read_batmap_header
- CA-299656: use sizeof(struct sockaddr_in) not the pointer
- Address coverity issues in tapdisk-control.c

* Mon Oct 01 2018 Mark Syms <mark.syms@citrix.com> - 3.14.0-1
- CA-295527: more logging for tap-ctl allocate failures

* Fri Sep 07 2018 Mark Syms <mark.syms@citrix.com> - 3.13.0-1
- CA-295527: Add some more diagnostics so that we know which bit of allocate errors

* Mon Aug 06 2018 Mark Syms <mark.syms@citrix.com> - 3.12.0-1
- CA-227096 Allow stashed passed fds to overwrite with the same name

* Wed Jul 25 2018 Mark Syms <mark.syms@citrix.com> - 3.11.0-1
- CA-294079 Revert "CA-205513: Removing bad fds from the fdset passed to select call."

* Thu Jul 19 2018 Tim Smith <tim.smith@citrix.com> - 3.10.0-2.0
- Add ldconfig in posttrans to support update from older versions

* Mon Jul 16 2018 Mark Syms <mark.syms@citrix.com> - 3.10.0-1
- valve: fix duplicated forwarding

* Wed Jul 11 2018 Mark Syms <mark.syms@citrix.com> - 3.9.0-1
- XOP-944 Fix crashes and errors in stats
- CP-24318: Closed resource leak in vhd_macx_decode_location()
- CA-293563 prevent race when creating blktap/control device node
- Update coverage profile dirs so that product and test code match
- make O_DIRECT optional

* Thu Jun 28 2018 Tim Smith <tim.smith@citrix.com> - 3.8.0-2.0
- Run ldconfig on install/uninstall

* Mon Jun 18 2018 Mark Syms <mark.syms@citrix.com> - 3.8.0-1
- Update specfile template
- Remove VERSION and WHATS_NEW files, this information is in git history and tags
- Remove WHATS_NEW from Makefile.am

* Fri May 25 2018 marksy <mark.syms@citrix.com> - 3.7.0-1.0
- CA-285194: ensure that tapdisk logs if it exits and also on opening its control

* Fri May 25 2018 marksy <mark.syms@citrix.com> - 3.6.0-1.0
- Release 3.6.0

* Tue Apr 10 2018 marksy <mark.syms@citrix.com> - 3.5.0-1.17
- CA-277128: remove redundant, broken RRD code from tapdisk

* Tue Mar 27 2018 marksy <mark.syms@citrix.com> - 3.5.0-1.16
- Gather the gcov coverage files during build

* Fri Feb 16 2018 marksy <mark.syms@citrix.com> - 3.5.0-1.15
- CP-26852: Support building with upstream Linux

* Tue Jan 30 2018 marksy <mark.syms@citrix.com> - 3.5.0-1.14
- Convert patch to use tabs for merge to github
- Reorder patchqueue with patches commited to github
- CA-220042: Add missing half of pull request 191 to patchqueue
- Update patchqueue patch to match the github pull request

* Wed Dec 06 2017 marksy <mark.syms@citrix.com> - 3.5.0-1.13
- CP-20541 Enable conditional coverage build

* Thu Oct 12 2017 marksy <mark.syms@citrix.com> - 3.5.0-1.12
- Patch cleanup

* Wed Oct 11 2017 marksy <mark.syms@citrix.com> - 3.5.0-1.11
- CA-268288: Send logpath as an additional write

* Wed Sep 27 2017 marksy <mark.syms@citrix.com> - 3.5.0-xs.1+1.10
- CP-23545: Extend tap-ctl create to consider CBT parameters
- CP-23920: [Unit test] Increase test coverage for cbt-util coalesce
- CP-24547: [Unit test] Increase test coverage for cbt-util set
