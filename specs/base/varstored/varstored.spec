%global package_speccommit a436001f700e5f647bd32b7055b18d3d78774ce5
%global package_srccommit v1.2.0
Name: varstored
Summary: EFI Variable Storage Daemon
Version: 1.2.0
Release: 2.3.1.ydi.2%{?xsrel}%{?dist}

License: BSD

# !!!! YOUR ATTENTION PLEASE !!!!
# Do not forget to run the script SOURCES/remove-certs-from-tarball.sh on new
# source archive. This will remove the pem files from the source archive as we
# are not supposed to distribute them in both RPM and SRPM. (This is why they
# are removed from archive and not just deleted from the buildroot as it used
# to be done)
Source0: varstored-1.2.0.tar.gz

# XCP-ng sources and patches
Source10: secureboot-certs
# Patch submitted upstream as https://github.com/xapi-project/varstored/pull/17
Patch1000: varstored-1.0.0-tolerate-missing-dbx-on-disk.XCP-ng.patch
# Patch submitted upstream as https://github.com/xapi-project/varstored/pull/21
Patch1001: varstored-1.2.0-fix-return-code-for-varstore-sb-state-user.XCP-ng.patch
# Patch submitted upstream as https://github.com/xapi-project/varstored/pull/23
Patch1002: 0001-Auth-Add-support-to-make-KEK-and-DB-files-optional.patch
Patch1003: 0002-Makefile-Add-EXTRA_CFLAGS-to-CFLAGS.patch
# Patch submitted upstream as https://github.com/xapi-project/varstored/pull/28
Patch1004: 0001-Use-memory-barriers-defined-by-xen-headers.patch

BuildRequires: xen-libs-devel xen-dom0-libs-devel openssl openssl-devel libxml2-devel
BuildRequires: glib2-devel
BuildRequires: libseccomp-devel
BuildRequires: gcc
%{?_cov_buildrequires}

# varstored now provides KEK.auth and db.auth that were
# previously provided by secureboot-certificates.
Conflicts: secureboot-certificates < 1.0.0-1

# Conflict with old XAPIs since the certificate directory moved.
Conflicts: xapi-core < 23.6.0-1

Requires: varstored-guard

# XCP-ng: transition from uefistored, starting with XCP-ng 8.3
Obsoletes: uefistored <= 1.3.0

# XCP-ng: secureboot-certs is a python script
Requires: python3

%description
A daemon for implementing variable services for UEFI guests.
It is responsible for storage, retrieval, and performing checks
when writing authenticated variables.


%package tools
Summary: Tools for manipulating a guest's EFI variables offline


%description tools
Provides a set of tools for manipulating a guest's EFI variables
when the guest is not running.


%prep
%autosetup -p1
%{?_cov_prepare}

# Check for pem files in the source archive
if find certs -name "*.pem" | grep -q pem; then
  echo "pem files are present in the source archive"
  echo "Please remove them using the script SOURCES/remove-certs-from-tarball.sh"
  echo "and push the updated source archive"
  false
fi

%build
%{?_cov_wrap} EXTRA_CFLAGS=-DAUTH_ONLY_PK_REQUIRED \
              make %{?_smp_mflags} varstored tools create-auth PK.auth

%{?_cov_make_model:%{_cov_make_model misc/coverity/model.c}}


%install
install -m 755 -d %{buildroot}/%{_sbindir}
install -m 755 %{name} %{buildroot}/%{_sbindir}/%{name}
install -m 755 -d %{buildroot}/%{_bindir}
install -m 755 tools/varstore-{ls,get,rm,set,sb-state} %{buildroot}/%{_bindir}
install -m 755 -d %{buildroot}/%{_datadir}/%{name}
install -m 644 PK.auth %{buildroot}/%{_datadir}/%{name}
mkdir -p %{buildroot}/opt/xensource/libexec/
install -m 755 create-auth %{buildroot}/opt/xensource/libexec/create-auth

# XCP-ng: add secureboot-certs script
install -m 755 %{SOURCE10} %{buildroot}/%{_sbindir}/secureboot-certs

%{?_cov_install}

%post
test "$(readlink %{_sharedstatedir}/%{name})" = %{_datadir}/%{name} || test -d %{_sharedstatedir}/%{name} || ln -sf -T %{_datadir}/%{name} %{_sharedstatedir}/%{name} || :

%check
make check


%files
%license LICENSE
%{_sbindir}/*
%{_datadir}/%{name}


%files tools
%license LICENSE
%{_bindir}/*
/opt/xensource/libexec/create-auth

%{?_cov_results_package}


%changelog
* Tue Nov 26 2024 Yann Dirson <yann.dirson@vates.tech> - 1.2.0-2.3.1.ydi.2
- switch secureboot-certs to python3
- add missing dependency on python interpreter
- add memory barrier definitions for aarch64

* Fri Apr 19 2024 Thierry Escande <thierry.escande@vates.tech> - 1.2.0-2.3
- Remove generation and installation of KEK and db files
- Add helper script to remove pem file from source archive
- Update source archive with pem files removed

* Wed Apr 17 2024 Thierry Escande <thierry.escande@vates.tech> - 1.2.0-2.2
- Auth: Add support to make KEK and DB files optional
- Auth: Enable AUTH_ONLY_PK_REQUIRED build macro

* Tue Apr 09 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.2.0-2.1
- Sync with 1.2.0-2
- *** Upstream changelog ***
- * Fri Jan 26 2024 Andrew Cooper <andrew.cooper3@citrix.com> - 1.2.0-2
- - Rebuild against libxenstore.so.4

* Wed Dec 13 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.2.0-1.3
- Update secureboot-certs script for recent UEFI cert handling in XAPI
- Remove KEK and db cert databases for now, pending legal advice.

* Wed Oct 25 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.2.0-1.2
- Revert part of XCP-ng specific changes, as upstream varstored now uses /var/lib/varstored
  and certs should be available by default now.
- Add varstored-1.2.0-fix-return-code-for-varstore-sb-state-user.XCP-ng.patch

* Fri Sep 15 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.2.0-1.1
- Update to 1.2.0-1
- Remove varstored-1.0.0-change-certs-directory.XCP-ng.patch
- *** Upstream changelog ***
- * Tue Feb 28 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.2.0-1
- - CP-41616: Move varstored auth dir to /var/lib/varstored
- - CA-369046: Log the error code if set_variable_from_auth fails
- - CA-362923: Change output format of create-auth
- - CP-40832: Add standard UEFI Secure Boot certificates

* Wed Dec 07 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.1.0-1.1
- Update from XS 8.3 prerelease updates
- *** Upstream changelog ***
- * Wed Aug 17 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.1.0-1
- - varstore-sb-state: only load auth data if needed
- - xapidb_init: Use BACKEND_INIT_FAILURE
- - CA-366706: Fix signal handling and possible segfault
- - CP-39854: Implement Platform Attack Reset Mitigation spec
- - CP-39864 / CP-40029: Implement PPI support
- * Tue Jul 26 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.0.0-3
- - Fix license of tools subpackage

* Fri Oct 28 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.0.0-2.4
- Update varstored-1.0.0-tolerate-missing-dbx-on-disk.XCP-ng.patch to match upstream PR

* Thu Oct 27 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.0.0-2.3
- Fix buggy varstored-1.0.0-tolerate-missing-dbx-on-disk.XCP-ng.patch

* Thu Oct 27 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.0.0-2.2
- Remove dependency to secureboot-certificates, for now
- Add varstored-1.0.0-tolerate-missing-dbx-on-disk.XCP-ng.patch

* Tue Oct 25 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.0.0-2.1
- Obsolete uefistored
- Create the /var/lib/varstored directory for XAPI to write into
- Add varstored-1.0.0-change-certs-directory.XCP-ng.patch to read certs from /var/lib/varstored
- Add the secureboot-certs script, formerly provided by uefistored
- Add /etc/xapi.conf.d/00-XCP-ng-varstore-dir.conf to change the certs dir in XAPI
- Add %post scriptlet to write PK.auth into /var/lib/varstored if missing

* Mon Mar 07 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.0.0-2
- Correct license field

* Fri Jun 25 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.0.0-1
- Switch upstream to GitHub

* Mon Mar 15 2021 Andrew Cooper <andrew.cooper3@citrix.com> - 0.9.5-1
- Switch to using stable Xen hypercalls only
- Don't configure bufioreq facilities
- Correct memory barriers

* Tue Mar 09 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.9.4-1
- CA-352332: Skip setting PK if KEK/db are missing

* Tue Feb 02 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.9.3-1
- CP-35896: varstore-get: Handle bad data_len
- CP-35896: Check count of variables before looping
- CP-35896: Check the return value of ASN1_get_object
- CP-35896: Initialize digest to zero
- CP-35896: Fix loading the certificate digest from serialized state
- CA-351587: Move crypto initialization earlier

* Wed Jan 27 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.9.2-1
- CA-351037: Use CC in the Makefile
- CA-351037: Fix issues reported by clang scan-build
- CP-35896: Add Coverity model and static analysis config
- CP-35896: Fix issues reported by Coverity

* Wed Jun 24 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.9.1-1
- CA-341597: Handle SIGTERM before the main loop is running
- CA-341597: Fix error message

* Mon Apr 20 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.9.0-1
- CA-333946: Make user mode == deployed mode
- CA-333944: Increase total size of variable storage to 128 KiB
- CA-333944: Set SignatureOwner to Microsoft's GUID for KEK and db

* Thu Sep 26 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.8.1-1
- CP-32192: Use func rather than offset to access EVP_PKEY

* Mon Jul 01 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.8.0-1
- Add a comment for commit a86b34eeac45
- Don't make auth target by default
- Cleanup initialize_settings()
- CA-322067: Use new resource mapping API

* Wed May 29 2019 Edwin Török <edvin.torok@citrix.com> - 0.7.0-1
- CP-30435 read feature flag in varstored to determine state of bootmode
- CP-30582 reverse setup keys
- Fix for CA-312835 and extra logging
- Add unit test for CA-312835

* Thu Feb 21 2019 Patrick Fox <patrick.fox@citrix.com> - 0.6.0-3
- Depend on SecureBoot Certificates Script for storing certificates in XAPI
- Build create-auth

* Fri Nov 23 2018 Edwin Török <edvin.torok@citrix.com> - 0.6.0-2
- Depend on varstored-guard for deprivileged operations

* Fri Nov 16 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.5.0-1
- Bump version to v0.5.0 since varstored is now functionally complete.

* Fri May 18 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.1.0-1
- Initial packaging
