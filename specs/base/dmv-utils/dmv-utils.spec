%global package_speccommit 4ac4bd73423f30a2afb997b3cdca51413df9a75f
%global usver 1.0.3
%global xsver 2
%global xsrel %{xsver}%{?xscount}%{?xshash}

%define debug_package %{nil}

Name: dmv-utils
Version: 1.0.3
Release: %{?xsrel}%{?dist}
Summary: DMV utilities
License: GPL
Source0: driver-tool
Source1: dmv-info
Source2: gen-note

Requires: kmod
Requires: python3-xcp-libs >= 3.0.9-1

%description
Driver multi-version utilities to support building DMV drivers by generating the
required metadata and ELF note.
At runtime it supports in listing DMV modules and DMV selection.

%prep

%build

%install
mkdir -p %{buildroot}/usr/sbin/
install -m 755 %{SOURCE0} %{buildroot}/usr/sbin/driver-tool
install -m 755 %{SOURCE1} %{buildroot}/usr/sbin/dmv-info
install -m 755 %{SOURCE2} %{buildroot}/usr/sbin/gen-note

%files
/usr/sbin/driver-tool
/usr/sbin/dmv-info
/usr/sbin/gen-note

%changelog
* Thu Aug 07 2025 Chunjie Zhu <chunjie.zhu@cloud.com> - 1.0.3-2
- CP-54481: call dmv functions in xcp-python-libs

* Tue Jul 08 2025 Stephen Cheng <stephen.cheng@cloud.com> - 1.0.3-1
- CP-308676: Support a single driver with multiple .ko files

* Fri Jun 20 2025 Stephen Cheng <stephen.cheng@cloud.com> - 1.0.2-1
- CA-412339: Run weak-module command after the driver is selected

* Fri Jun 13 2025 Stephen Cheng <stephen.cheng@cloud.com> - 1.0.1-1
- CA-412335: driver-tool not work correctly with multiple kabi versions

* Thu May 15 2025 Chunjie Zhu <chunjie.zhu@cloud.com> - 1.0.0-3
- CA-411190: fix driver-tool fails to call dracut bug

* Sun Apr 27 2025 Chunjie Zhu <chunjie.zhu@cloud.com> - 1.0.0-2
- CA-409875: fix driver-tool list bug

* Tue Apr 01 2025 Stephen Cheng <stephen.cheng@cloud.com> - 1.0.0-1
- CP-53493: Bump to version 1.0.0

* Thu Feb 13 2025 Fouad Hilly <fouad.hilly@citrix.com> - 0.1
- CP-53434: First dmv-utils packaging release
