%global package_speccommit ec99887d98ef5e88281ce0eecdca058c94897d18
%global package_srccommit v1.2.1
%{!?xsrel: %global xsrel 1}

Name: vmss
Version: 1.2.1
Release: %{?xsrel}%{?dist}
Summary: REQ-277 schedule snapshots feature
License: GNU / GPLv2
Source0: vmss-1.2.1.tar.gz
BuildArch: noarch
BuildRequires: python3-devel


%description
This package contains scheduled snapshots feature plugins and the associated cron job

%prep

%autosetup -p1

%build
DESTDIR=$RPM_BUILD_ROOT make

%install
DESTDIR=$RPM_BUILD_ROOT make install

%clean
rm -rf $RPM_BUILD_ROOT

%post

%preun

%files
%defattr(-,root,root,-)
/etc/xapi.d/plugins/vmss
/etc/cron.d/vmss.cron
/etc/xensource/bugtool/VM-snapshot-schedule/vmss.xml
/etc/xensource/bugtool/VM-snapshot-schedule.xml

%config /etc/logrotate.d/VMSSlog

%changelog
* Wed Mar 20 2024 Deli Zhang <deli.zhang@cloud.com> - 1.2.1-1
- CP-47868: Replace deprecated imp module use

* Wed Sep 20 2023 Lin Liu <lin.liu@citrix.com> - 1.2.0-1
- CP-44269: Update python2 to python3

* Wed Nov 30 2022 Mark Syms <mark.syms@citrix.com> - 1.1.2-1
- Remove implicit dependencies on SM code

* Thu Apr 28 2022 Rob Hoes <rob.hoes@citrix.com> - 1.1.1-2
- Bump release and rebuild

* Tue Jul 28 2020 Ben Sims <ben.sims@citrix.com> - 1.1.1-1
- CA-342874 Correct error warning levels.

* Mon Feb 20 2017 Sharath Babu <sharath.babu@citrix.com> 1.0.0-1
- Initial Commit
