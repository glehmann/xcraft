%global package_speccommit f92cdaa966ebdc2caa6cffb73b8b644276962a3f
%{!?xsrel: %global xsrel 2}
%global package_srccommit v1.1.1
Summary: A library of functions for running an emp server
Name: libempserver
Version: 1.1.1
Release: %{?xsrel}%{?dist}
License: BSD
Source0: libempserver-1.1.1.tar.gz

BuildRequires: gcc
BuildRequires: json-c-devel
%{?_cov_buildrequires}
Requires(post): /sbin/ldconfig
Requires(postun): /sbin/ldconfig

%description
libempserver is a library of functions for creating an
emp server, to allow an emp client (such as emu-manager) to connect
and control the process.  The emp protocol is intended to be used by
hardware emulators used to support a virtual machine.

%prep
%autosetup -p1
%{?_cov_prepare}

%build
%{?_cov_wrap} %{make_build}

%install
%{makeinstall}
%{?_cov_install}

%files
%{_libdir}/%{name}.so.1
%{_libdir}/%{name}.so.1.1

%{?_cov_results_package}

%post -p /sbin/ldconfig
%postun -p /sbin/ldconfig

%package devel
Summary: Development headers for libempserver
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
libempserver is a library of functions for creating an
emp server, to allow an emp client (such as emu-manager) to connect
and control the process.  The emp protocol is intended to be used by
hardware emulators used to support a virtual machine.

This package provides documentation and development headers for
libempserver

%files devel
%doc LICENSE
%{_includedir}/emp.h
%{_includedir}/%{name}.h
%{_libdir}/%{name}.a
%{_libdir}/%{name}.so

%changelog
* Wed Jan 22 2025 XenServer Rebuild <rebuild@xenserver.com> - 1.1.1-2
- CP-53310: XenServer 9 rebuild

* Thu Jun 27 2024 Lin Liu <Lin.Liu01@cloud.com> - 1.1.1-1
- CP-46114: Enable debuginfo to build with XS9

* Thu Feb 10 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.1.0-2
- CP-38416: Enable static analysis

* Thu Mar 19 2020 Jennifer Herbert <jennifer.herbert@citrix.com> - 1.1.0-1
- CA-326958 - Add function to provide default path

* Wed Jul 18 2018 Tim Smith <tim.smith@citrix.com> - 1.0.4-2
- Add ldconfig pre/post scriptlets

* Tue Jul 03 2018 Simon Rowe <simon.rowe@citrix.com> - 1.0.4-1
- CA-293082 - Build libempserver as a dynamic object
- CA-293082 - Set the library SONAME

* Thu Jun 28 2018 Andrew Cooper <andrew.cooper3@citrix.com> - 1.0.3-3
- Build libempserver as a dynamic library

* Wed Jun 20 2018 Tim Smith <tim.smith@citrix.com> - 1.0.3-2
- Create static and devel subpackages to reflect the fact that this is a static
  library used only at build time.

* Mon Apr 09 2018 Simon Rowe <simon.rowe@citrix.com> - 1.0.3-1
- CA-285283: Allow poll to be used in place of select.

* Wed Jan 03 2018 Bob Ball <bob.ball@citrix.com> - 1.0.2-1
- CP-25488: Improve libemp locking

* Wed Dec 13 2017 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.0.1-1
- Code tidy-ups

* Thu Nov 30 2017 Jennifer Herbert <jennifer.herbert@citrix.com> - 1.0.0-1
- Initial version of libempserver
