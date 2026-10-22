%global package_speccommit da356c77e70ef4ff96579e1e077b452ed85af5e9
%global package_srccommit v10.2.2
Summary: vncterm tty to vnc utility
Name: vncterm
Version: 10.2.2
Release: 1%{?xsrel}%{?dist}
License: GPL
Group: System/Hypervisor
Source0: vncterm-10.2.2.tar.gz
BuildRequires: xen-libs-devel systemd
BuildRequires: gcc
%{?_cov_buildrequires}
Requires: qemu
Requires(pre): shadow-utils
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
This package contains the vncterm utility

%prep
%autosetup -p1
%{?_cov_prepare}

%build
%{?_cov_wrap} %{__make}

%install
%{__rm} -rf %{buildroot}
%{__install} -d %{buildroot}%{_libdir}/xen/bin
%{__install} -d %{buildroot}/opt/xensource/libexec
%{__install} -d %{buildroot}%{_unitdir}
%{__install} -m 755 %{name} %{buildroot}%{_libdir}/xen/bin
%{__install} -m 755 dom0term/dom0term.sh %{buildroot}/opt/xensource/libexec
%{__install} -m 755 dom0term/%{name}-wrapper %{buildroot}/opt/xensource/libexec
%{__install} -m 644 dom0term/dom0term.service %{buildroot}%{_unitdir}
%{__install} -d %{buildroot}%{_var}/xen/%{name}
%{?_cov_install}

%clean
%{__rm} -rf %{buildroot}

%pre
/usr/bin/getent passwd vncterm >/dev/null 2>&1 || /usr/sbin/useradd \
    -M -U -r \
    -s /sbin/nologin \
    -d / \
    vncterm >/dev/null 2>&1 || :
/usr/bin/getent passwd vncterm_base >/dev/null 2>&1 || /usr/sbin/useradd \
    -M -U -r \
    -s /sbin/nologin \
    -d / \
    -u 131072 \
    vncterm_base >/dev/null 2>&1 || :

%post
grep -xq 'pts/0' /etc/securetty || echo 'pts/0' >>/etc/securetty
%systemd_post dom0term.service

%preun
%systemd_preun dom0term.service

%postun
%systemd_postun_with_restart dom0term.service

%files
%defattr(-,root,root,-)
%{_libdir}/xen/bin/%{name}
/opt/xensource/libexec/dom0term.sh
/opt/xensource/libexec/%{name}-wrapper
%{_unitdir}/dom0term.service
%dir %{_var}/xen/%{name}

%{?_cov_results_package}

%changelog
* Fri Aug 30 Lin Liu <Lin.Liu01@cloud.com> - 10.2.2-1
- CP-50733: Fix misleading-indentation

* Fri Jan 26 2024 Andrew Cooper <andrew.cooper3@citrix.com> - 10.2.1-2
- Rebuild against libxenstore.so.4

* Thu Jun 29 2023 Per Bilse <per.bilse@citrix.com> - 10.2.1-1
- CP-41049: Safely remove /proc/xen from dom0

* Tue Feb 15 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.2.0-3
- CP-38416: Enable static analysis

* Fri Feb 21 2020 Steven Woods <steven.woods@citrix.com> - 10.2.0-2
- CP33120: Add Coverity build macros

* Wed Mar 27 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 10.2.0-1
- Use xenstored.service rather than socket
- Specify coredump limit in unit rather than wrapper script
- CA-308916: Remove special coredump handling

* Fri Jan 18 2019 Edwin Török <edvin.torok@citrix.com> - 10.1.0-1
- CA-308198: qemu-trad was dropped, update vncterm to use keymaps from upstream qemu instead

