# Do not strip binaries. We need this for good stacktraces in production.
%global debug_package %{nil}
%global __os_install_post /usr/lib/rpm/brp-compress

Name:           xcp-emu-manager
Version:        1.2.1
Release:        3%{?dist}
Summary:        Tool used for managing xenguest
License:        GPLv3
URL:            https://github.com/xcp-ng/xcp-emu-manager
Source0:        https://github.com/xcp-ng/xcp-emu-manager/archive/v%{version}/%{name}-%{version}.tar.gz

BuildRequires:  cmake3
BuildRequires:  make
BuildRequires:  gcc
BuildRequires:  json-c-devel
BuildRequires:  libempserver-devel
BuildRequires:  xcp-ng-generic-lib-devel

Provides: emu-manager
Obsoletes: emu-manager

%description
Simple host networking management service for the xapi toolstack.

Handles suspend, resume and migrate.

%prep
%autosetup -p1

%build
%cmake3
%cmake3_build

%install
%cmake3_install

%files
%{_libdir}/xen/bin/emu-manager

%changelog
* Tue Sep 22 2026 Julian Vetter <julian.vetter@vates.tech> - 1.2.1-3
- Fix cmake macros. No need to pull in epel-rpm-macros

* Fri Jul 17 2026 Lucas Pottier <lucas.pottier@vates.tech> - 1.2.1-2
- New version 1.2.1
- Add support for --mem_pnode xenopsd argument
- Remove unnecessary or duplicate abort calls to xenopsd
- Send pause command to xenguest before suspending the domain

* Tue Jan 21 2025 Thierry Escande <thierry.escande@vates.tech> - 1.2.0-2
- Fix build that was failing because of cmake3 update

* Fri Oct 21 2022 Ronan Abhamon <ronan.abhamon@vates.fr> - 1.2.0-1
- New version 1.2.0
- Correctly report error codes when opening an emu stream using a non-sock file
- Streams support pipes

* Fri Sep 16 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.1.3-2
- Rebuild for XCP-ng 8.3 alpha

* Thu Jul 16 2020 Ronan Abhamon <ronan.abhamon@vates.fr> - 1.1.3-1
- New version 1.1.3
- Fix suspend action for XCP-ng 8.2

* Wed Jul 01 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.1.2-3
- Rebuild for XCP-ng 8.2

* Fri Dec 20 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.1.2-2
- Rebuild for XCP-ng 8.1

* Mon Jun 03 2019 Ronan Abhamon <ronan.abhamon@vates.fr> - 1.1.2-1
- Fix suspend action

* Fri May 31 2019 Ronan Abhamon <ronan.abhamon@vates.fr> - 1.1.1-1
- Abort migration (if possible) and log better stacktrace in case of unexpected end of process

* Wed May 29 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.1.0-3
- Do not require the debuginfo package anymore
- Do not strip binaries nor produce the debuginfo package at all for now

* Tue May 28 2019 Ronan Abhamon <ronan.abhamon@vates.fr> - 1.1.0-1
- New version 1.1.0
- Supports non-live migration
- Fix live migration of PV guests

* Tue May 21 2019 Ronan Abhamon <ronan.abhamon@vates.fr> - 1.0.0-2
- New version 1.0.0 (new C version)

* Thu May 09 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.1.0-1
- New version 0.1.0 for XCP-ng 8.0

* Thu Jan 10 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.9-1
- New version 0.0.9
- Better logs in case of unexpected end of process
- Migration of PV guests fixed

* Tue Jan 08 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.8-2
- Fix live migration of PV guests

* Wed Dec 19 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.8-1
- Live migration fixes, with new loop logic.

* Mon Nov 19 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.7-1
- Live migration fixes.

* Thu Nov 08 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.6-2
- Update to 0.0.6 to improve migration of VMs under load.
- In most cases migration will now resume when the VM's CPU and/or IO usage decreases.

* Tue Oct 02 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.5-1
- New version 0.0.5 to fix suspend, resume and migrate in XCP-ng 7.6

* Thu Sep 13 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.4-1
- New version 0.0.4 to fix build for XCP-ng 7.6.0

* Mon Jul 23 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 0.0.3-1
- Update to 0.0.3 with migration support!

* Mon Jul 23 2018 John Else <john.else@gmail.com> - 0.0.2-1
- Initial package
