%global package_speccommit fc6660be6dedf78939d0c2a115aee72009704c84
%global usver 2.17.7
%global xsver 2
%global xsrel %{xsver}%{?xscount}%{?xshash}
%global package_srccommit refs/tags/v2.17.7
%global __python %{_bindir}/python3

# Control whether we build with the address sanitizer.
# Default enabled: (to override: --without asan)
#%%define with_asan  %%{?_without_asan: 0} %%{?!_without_asan: 1}
# Default disabled: (to override: --with asan)
%define with_asan  %{?_with_asan: 1} %{?!_with_asan: 0}

Name: openvswitch
Summary: Virtual switch
URL: http://www.openvswitch.org/
Version: 2.17.7
License: ASL 2.0 and GPLv2
Release: %{?xsrel}.1.0.ydi.1%{?dist}
Source0: openvswitch-2.17.7.tar.gz
Patch0: CA-72973-hack-to-strip-temp-dirs-from-paths.patch
Patch1: CP-15129-Convert-to-use-systemd-services.patch
Patch2: CA-78639-dont-call-interface-reconfigure-anymore.patch
Patch3: CA-153718-md5-verification-dvsc.patch
Patch4: CP-9895-Add-originator-to-login_with_password-xapi-call.patch
Patch5: CP-13181-add-dropping-of-fip-and-lldp.patch
Patch6: use-old-db-port-6632-for-dvsc.patch
Patch7: CA-243975-Fix-openvswitch-service-startup-failure.patch
Patch8: CP-23098-Add-IPv6-multicast-snooping-toggle.patch
Patch9: CA-265107-When-enable-igmp-snooping-cannot-receive-ipv6-multicast-traffic.patch
Patch10: 0001-xenserver-fix-Python-errors-in-Citrix-changes.patch
Patch11: 0002-O3eng-applied-patch-on-top-of-the-NSX-OVS.patch
Patch12: 0003-update-bridge-fail-mode-settings-when-bridge-comes-up.patch
Patch13: CP-23607-inject-multicast-query-msg-on-bond-port.patch
Patch14: mlockall-onfault.patch
Patch15: hide-logrotate-script-error.patch

# upstream backports
Patch100: 0001-ovs-atomic-Fix-inclusion-of-Clang-header-by-GCC-14.patch
Patch101: 0001-tests-Fix-compatibility-issue-with-Python-3.13-in-vl.patch
Patch102: 0001-tests-Fix-non-portable-plus-match-in-python-vlog-tes.patch

# XCP-ng patches
Patch1000: openvswitch-2.17.7-comment-failing-tests.XCP-ng.patch
Patch1001: openvswitch-2.17.7-add-pythonpath-ipsec.XCP-ng.patch
Patch1002: openvswitch-2.17.7-CVE-2023-3966-netdev-offload-tc-Check-geneve-metadata-length.backport.patch
Patch1003: openvswitch-2.17.7-CVE-2023-5366-odp-ND-Follow-Open-Flow-spec-converting-from-OF-to-DP.backport.patch
Patch1004: ovs-ctl-set-default-ssl-ciphers-and-protocols.patch
Patch1005: 0001-manpage-check-enable-more-error-diagnostics.patch
Patch1006: 0002-manpage-check-be-explicit-that-it-fails-because-of-a.patch
Patch1007: 0003-manpage-check-disable.patch
Patch1008: 0004-HACK-Address-python-warning-in-xenserver-tests.patch

Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
BuildRequires: systemd
BuildRequires: openssl, openssl-devel, python3
BuildRequires: autoconf, automake, libtool
%if %{with_asan}
BuildRequires: libasan
%endif
%{?_cov_buildrequires}

%if %undefined module_dir
%define module_dir updates
%endif
%if %undefined modules_suffix
%define modules_suffix modules
%endif

# Only build the modules for a 3.10 kernel.
%define build_modules %(if echo %{kernel_version} | grep -q "^3\.10\."; then
                            echo 1
                        else
                            echo 0
                        fi)

%if %build_modules
BuildRequires: kernel-devel
%define with_linux --with-linux=/lib/modules/%{kernel_version}/build
%endif

%description
The vswitch provides standard network bridging functions augmented with
support for the OpenFlow protocol for remote per-flow control of
traffic.

%prep
%autosetup -p1
%{?_cov_prepare}

%build
sh boot.sh

%if %{with_asan}
# Extend RPM's defaults to include address sanitizer
CFLAGS="-O2 -g -pipe -Wall -Wp,-D_FORTIFY_SOURCE=2 -fexceptions -fstack-protector-strong --param=ssp-buffer-size=4 -grecord-gcc-switches   -m64 -mtune=generic -fsanitize=address"
CXXFLAGS="$CFLAGS"
LDFLAGS="-Wl,-z,relro -fsanitize=address"
export CFLAGS CXXFLAGS LDFLAGS
%endif

%configure --enable-ssl --without-pcre --without-ncurses --with-logdir=/var/log/openvswitch --with-dbdir=/run/openvswitch %{?with_linux}

%{?_cov_wrap} %{__make} %{_smp_mflags}

%install
rm -rf %{buildroot}
%{?_cov_wrap} %{__make} install DESTDIR=%{buildroot}

%if %build_modules
%{?_cov_wrap} %{__make} INSTALL_MOD_PATH=%{buildroot} modules_install
# mark modules executable so that strip-to-file can strip them
find %{buildroot}/lib/modules/%{kernel_version} -name "*.ko" -type f | xargs chmod u+x
%endif

install -d -m 755 %{buildroot}/var/lib/openvswitch
install -d -m 755 %{buildroot}/var/log/openvswitch
install -d -m 755 %{buildroot}/var/xen/openvswitch

install -d -m 755 %{buildroot}/%{_sysconfdir}/openvswitch

install -d -m 755 %{buildroot}/%{_sysconfdir}/sysconfig
install -m 755 xenserver/usr_share_openvswitch_scripts_sysconfig.template \
         %{buildroot}/%{_sysconfdir}/sysconfig/openvswitch

install -d -m 755 %{buildroot}/%{_sysconfdir}/logrotate.d
install -m 644 xenserver/etc_logrotate.d_openvswitch \
         %{buildroot}/%{_sysconfdir}/logrotate.d/openvswitch

install -d -m 755 %{buildroot}/%{_sysconfdir}/xapi.d/plugins
install -m 755 xenserver/etc_xapi.d_plugins_openvswitch-cfg-update \
         %{buildroot}/%{_sysconfdir}/xapi.d/plugins/openvswitch-cfg-update

install -d -m 755 %{buildroot}/%{_datadir}/openvswitch
install -m 644 vswitchd/vswitch.ovsschema \
         %{buildroot}/%{_datadir}/openvswitch/vswitch.ovsschema

install -d -m 755 %{buildroot}/%{_datadir}/openvswitch/scripts
install -m 755 xenserver/usr_share_openvswitch_scripts_ovs-xapi-sync \
         %{buildroot}/%{_datadir}/openvswitch/scripts/ovs-xapi-sync
install -m 755 xenserver/usr_share_openvswitch_scripts_ovs-start \
         %{buildroot}/%{_datadir}/openvswitch/scripts/ovs-start

install -d -m 755 %{buildroot}/%{_unitdir}
install -m 644 xenserver/usr_lib_systemd_system_openvswitch.service \
         %{buildroot}/%{_unitdir}/openvswitch.service
install -m 644 xenserver/usr_lib_systemd_system_openvswitch-xapi-sync.service \
         %{buildroot}/%{_unitdir}/openvswitch-xapi-sync.service
install -m 644 rhel/usr_lib_systemd_system_openvswitch-ipsec.service \
         %{buildroot}/%{_unitdir}/openvswitch-ipsec.service

install -d -m 755 %{buildroot}/%{_datadir}/openvswitch/python/ovs/__pycache__
install -d -m 755 %{buildroot}/%{_datadir}/openvswitch/python/ovs/compat/__pycache__
install -d -m 755 %{buildroot}/%{_datadir}/openvswitch/python/ovs/compat/sortedcontainers/__pycache__
install -d -m 755 %{buildroot}/%{_datadir}/openvswitch/python/ovs/db/__pycache__
install -d -m 755 %{buildroot}/%{_datadir}/openvswitch/python/ovs/unixctl/__pycache__

#install python/compat/uuid.py %{buildroot}/%{_datadir}/openvswitch/python
#install python/compat/argparse.py %{buildroot}/%{_datadir}/openvswitch/python

# Get rid of stuff we don't want to make RPM happy.
(cd "$RPM_BUILD_ROOT" && rm -f usr/lib64/lib*)

%{?_cov_install}

%check
make check

%post
%systemd_post openvswitch.service
%systemd_post openvswitch-xapi-sync.service

%preun
%systemd_preun openvswitch.service
%systemd_preun openvswitch-xapi-sync.service

%postun
%systemd_postun openvswitch.service
%systemd_postun openvswitch-xapi-sync.service

%files
#%doc COPYING README
%config %{_sysconfdir}/sysconfig/openvswitch
%config %{_sysconfdir}/logrotate.d/openvswitch
%{_sysconfdir}/xapi.d/plugins/openvswitch-cfg-update
%dir %{_sysconfdir}/openvswitch
%dir %{_var}/xen/openvswitch
%dir %{_var}/lib/openvswitch
%dir %{_var}/log/openvswitch
%{_sysconfdir}/bash_completion.d/ovs-appctl-bashcomp.bash
%{_sysconfdir}/bash_completion.d/ovs-vsctl-bashcomp.bash
%{_bindir}/ovs-docker
%{_bindir}/ovs-testcontroller
%{_bindir}/ovs-appctl
%{_bindir}/ovs-dpctl
%{_bindir}/ovs-dpctl-top
%{_bindir}/ovs-l3ping
%{_bindir}/ovs-ofctl
%{_bindir}/ovs-pcap
%{_bindir}/ovs-tcpdump
%{_bindir}/ovs-tcpundump
%{_bindir}/ovs-vlan-test
%{_bindir}/ovs-vsctl
%{_bindir}/ovsdb-client
%{_bindir}/ovsdb-tool
%{_bindir}/ovs-test
%{_sbindir}/ovs-bugtool
%{_sbindir}/ovs-vswitchd
%{_sbindir}/ovsdb-server
%{_datadir}/openvswitch/local-config.ovsschema
%{_datadir}/openvswitch/bugtool-plugins/kernel-info/openvswitch.xml
%{_datadir}/openvswitch/bugtool-plugins/network-status/openvswitch.xml
%{_datadir}/openvswitch/bugtool-plugins/system-configuration.xml
%{_datadir}/openvswitch/bugtool-plugins/system-configuration/openvswitch.xml
%{_datadir}/openvswitch/bugtool-plugins/system-logs/openvswitch.xml
%{_datadir}/openvswitch/python/ovs/__init__.py*
%{_datadir}/openvswitch/python/ovs/compat/__init__.py*
%{_datadir}/openvswitch/python/ovs/compat/sortedcontainers/__init__.py*
%{_datadir}/openvswitch/python/ovs/compat/sortedcontainers/sorteddict.py*
%{_datadir}/openvswitch/python/ovs/compat/sortedcontainers/sortedlist.py*
%{_datadir}/openvswitch/python/ovs/compat/sortedcontainers/sortedset.py*
%{_datadir}/openvswitch/python/ovs/daemon.py*
%{_datadir}/openvswitch/python/ovs/db/__init__.py*
%{_datadir}/openvswitch/python/ovs/db/data.py*
%{_datadir}/openvswitch/python/ovs/db/error.py*
%{_datadir}/openvswitch/python/ovs/db/idl.py*
%{_datadir}/openvswitch/python/ovs/db/parser.py*
%{_datadir}/openvswitch/python/ovs/db/schema.py*
%{_datadir}/openvswitch/python/ovs/db/types.py*
%{_datadir}/openvswitch/python/ovs/db/custom_index.py*
%{_datadir}/openvswitch/python/ovs/fcntl_win.py*
%{_datadir}/openvswitch/python/ovs/unixctl/__init__.py*
%{_datadir}/openvswitch/python/ovs/unixctl/client.py*
%{_datadir}/openvswitch/python/ovs/unixctl/server.py*
%{_datadir}/openvswitch/python/ovs/dirs.py*
%{_datadir}/openvswitch/python/ovs/fatal_signal.py*
%{_datadir}/openvswitch/python/ovs/json.py*
%{_datadir}/openvswitch/python/ovs/jsonrpc.py*
%{_datadir}/openvswitch/python/ovs/ovsuuid.py*
%{_datadir}/openvswitch/python/ovs/poller.py*
%{_datadir}/openvswitch/python/ovs/process.py*
%{_datadir}/openvswitch/python/ovs/reconnect.py*
%{_datadir}/openvswitch/python/ovs/socket_util.py*
%{_datadir}/openvswitch/python/ovs/stream.py*
%{_datadir}/openvswitch/python/ovs/timeval.py*
%{_datadir}/openvswitch/python/ovs/util.py*
%{_datadir}/openvswitch/python/ovs/vlog.py*
%{_datadir}/openvswitch/python/ovs/version.py*
%{_datadir}/openvswitch/python/ovs/winutils.py
%{_datadir}/openvswitch/python/ovs/__pycache__
%{_datadir}/openvswitch/python/ovs/compat/__pycache__
%{_datadir}/openvswitch/python/ovs/compat/sortedcontainers/__pycache__
%{_datadir}/openvswitch/python/ovs/db/__pycache__
%{_datadir}/openvswitch/python/ovs/unixctl/__pycache__
#%%{_datadir}/openvswitch/python/argparse.py*
#%%{_datadir}/openvswitch/python/uuid.py*
%{_datadir}/openvswitch/vswitch.ovsschema
%{_datadir}/openvswitch/scripts/ovs-lib
%{_datadir}/openvswitch/scripts/ovs-bugtool-*
%{_datadir}/openvswitch/scripts/ovs-check-dead-ifs
%{_datadir}/openvswitch/scripts/ovs-ctl
%{_datadir}/openvswitch/scripts/ovs-kmod-ctl
%{_datadir}/openvswitch/scripts/ovs-monitor-ipsec
%{_datadir}/openvswitch/scripts/ovs-save
%{_datadir}/openvswitch/scripts/ovs-start
%{_datadir}/openvswitch/scripts/ovs-xapi-sync
%{_mandir}/man1/ovsdb-client.1.gz
%{_mandir}/man1/ovsdb-tool.1.gz
%{_mandir}/man1/ovsdb-server.1.gz
%{_mandir}/man1/ovs-pcap.1.gz
#%%{_mandir}/man1/ovs-tcpundump.1.gz
%{_mandir}/man5/ovsdb.local-config.5.gz
%{_mandir}/man5/ovsdb-server.5.gz
%{_mandir}/man5/ovs-vswitchd.conf.db.5.gz
%{_mandir}/man5/vtep.5.gz
%{_mandir}/man7/ovs-fields.7.gz
%{_mandir}/man8/vtep-ctl.8.gz
%{_mandir}/man8/ovs-dpctl.8.gz
%{_mandir}/man8/ovs-dpctl-top.8.gz
%{_mandir}/man8/ovs-ofctl.8.gz
%{_mandir}/man8/ovs-kmod-ctl.8.gz
%{_mandir}/man8/ovs-vsctl.8.gz
%{_mandir}/man8/ovs-vswitchd.8.gz
%{_mandir}/man8/ovs-bugtool.8.gz

%{_unitdir}/openvswitch.service
%{_unitdir}/openvswitch-xapi-sync.service

%exclude /usr/include/openflow/*
%exclude /usr/include/openvswitch/*
%exclude /usr/lib64/pkgconfig/libofproto.pc
%exclude /usr/lib64/pkgconfig/libopenvswitch.pc
%exclude /usr/lib64/pkgconfig/libovsdb.pc
%exclude /usr/lib64/pkgconfig/libsflow.pc
%exclude /usr/share/man/man8/ovs-testcontroller.8.gz
%exclude %{_bindir}/ovs-l3ping
%exclude %{_bindir}/ovs-parse-backtrace
%exclude %{_bindir}/ovs-pki
%exclude %{_bindir}/vtep-ctl
%exclude %{_mandir}/man5/vtep.5.gz
%exclude %{_mandir}/man8/vtep-ctl.8.gz
%exclude %{_datadir}/openvswitch/python/ovstest
%exclude %{_datadir}/openvswitch/scripts/ovs-vtep
%exclude %{_datadir}/openvswitch/vtep.ovsschema

%{?_cov_results_package}

%if %build_modules

%package modules
Summary: Open vSwitch kernel module
Version: %(echo "%{kernel_version}" | tr - .)
License: GPLv2
Provides: %{name}-modules = %{kernel_version}
%if 0%{?fedora} >= 17 || 0%{?rhel} >= 7
Requires(post): /usr/sbin/depmod
Requires(postun): /usr/sbin/depmod
%else
Requires(post): /sbin/depmod
Requires(postun): /sbin/depmod
%endif

%description modules
Open vSwitch Linux kernel module compiled against kernel version
%{kernel_version}.

%files modules
%defattr(-,root,root)
/lib/modules/%{kernel_version}/extra/*.ko
%exclude /lib/modules/%{kernel_version}/modules.*

%post modules
/sbin/depmod -a %{kernel_version}

%postun modules
/sbin/depmod -a %{kernel_version}

%endif


%package ipsec
Summary: Open vSwitch IPsec package
Requires: %{name} = %{version}-%{release}
Requires: libreswan >= 3.26

%description ipsec
Provides an Open vSwitch extension allowing to use encrypted
tunnels using IPsec.

%files ipsec
%{_unitdir}/openvswitch-ipsec.service

%post ipsec
%systemd_post openvswitch-ipsec.service

%preun ipsec
%systemd_preun openvswitch-ipsec.service

%postun ipsec
%systemd_postun openvswitch-ipsec.service

%changelog
* Mon Jul  7 2025 Yann Dirson <yann.dirson@vates.tech> - 2.17.7-2.1.0.ydi.1
- Upstream patches:
  - ovs-atomic: Fix inclusion of Clang header by GCC 14.
  - tests: Fix compatibility issue with Python 3.13 in vlog.at.
  - tests: Fix non-portable plus match in python vlog test.
- Added patches to reveal problematic manpage-check command
- Fix python warning in xenserver tests

* Mon Aug 12 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.17.7-2.1
- Sync with 2.17.7-2
- *** Upstream changelog ***
- * Thu Jun 13 2024 Lin Liu <Lin.Liu01@cloud.com> - 2.17.7-2
- - CP-46117: Refresh patches to build with XS9

* Fri Aug 09 2024 David Morel <david.morel@vates.tech> - 2.17.7-1.5
- link back to openssl to avoid issues with XO sdn-controller
- use ciphers that works with XO sdn-controller and should not be considered as weak-ciphers

* Thu Jul 11 2024 David Morel <david.morel@vates.tech> - 2.17.7-1.4
- Disable TLSv1.3 as it makes ovsdb-server spam error in logs

* Fri Jun 21 2024 David Morel <david.morel@vates.tech> - 2.17.7-1.3
- Link with xs-openssl instead of openssl
- Tweak ovs-ctl script that is used to start ovsdb-server, passing it
  --ssl-ciphers and --ssl-protocols command line parameters to disable weak
  ciphers.

* Fri Mar 01 2024 David Morel <david.morel@vates.tech> - 2.17.7-1.2
- Apply fix for CVE-2023-3966, provided in advisory
- Apply fix for CVE-2023-5366, provided in advisory

* Mon Jan 22 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.17.7-1.1
- Update to 2.17.7-1
- Add openvswitch-2.17.7-comment-failing-tests.XCP-ng.patch (Benjamin Reis)
- Add openvswitch-2.17.7-add-pythonpath-ipsec.XCP-ng.patch (Benjamin Reis)
- Get ipsec script and service from new sources (Benjamin Reis)
- *** Upstream changelog ***
- * Wed Aug 23 2023 Chunjie Zhu <chunjie.zhu@cloud.com> - 2.17.7-1
- - CP-44181: upgrade to OVS LTS 2.17.7

* Fri Sep 15 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.5.3-2.3.14.1
- Update to 2.5.3-2.3.14
- Drop openvswitch-2.5.3-CVE-2023-1668.backport.patch, now patched in XenServer's RPM
- *** Upstream changelog ***
- * Tue Apr 18 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.14
- - CA-376367: Fix CVE-2023-1668

* Mon Jun 05 2023 David Morel <david.morel@vates.fr> - 2.5.3-2.3.13.2
- Backport fix for CVE-2023-1668: Remote traffic denial of service via crafted packets with IP proto 0
- Comment out tests that fail
- Enable make check in spec file

* Tue Aug 30 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.5.3-2.3.13.1
- Rebase on latest package from CH 8.3 Preview
- Re-add changes to produce openvswitch-ipsec subpackage

* Tue Jun 14 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.13
- CA-367973: Backport bond-related patches to fix XSI-1198

* Tue Mar 15 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.12
- CA-364343: Honour updelay when LACP is used

* Mon Feb 14 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.11
- CA-357261: Hide expected errors from logrotate script
- Drop unneeded BuildRequires
- CP-38416: Enable static analysis

* Fri Feb 19 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.10
- CA-351960: CVE-2020-35498: Support extra padding length

* Mon May 18 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.9
- CA-339588: vswitchd: Use MCL_ONFAULT

* Tue May 12 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.8
- CP-33375: Turn off the address sanitizer

* Mon Apr 20 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.7
- CP-33305: Try and protect OVS from OOM
- Roll CP-33305-dont-kill-ovs.patch into CP-15129-Convert-to-use-systemd-services.patch
- CA-331103: Fix crash in mbundle_lookup()

* Tue Mar 24 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.6
- CA-331103: Turn on the address sanitizer temporarily

* Fri Mar 20 2020 Xin Li <xin.li@citrix.com> - 2.5.3-2.3.5
- CP-32957: remove the open vSwitch configure dialog from xsconsole

* Fri Feb 21 2020 Steven Woods <steven.woods@citrix.com> - 2.5.3-2.3.4
- CP33120: Add Coverity build macros

* Fri Jul 19 2019 Deli Zhang <deli.zhang@citrix.com> - 2.5.3-2.3.3
- CA-320193: Revert OVS from CCM rpath link

* Wed Jun 19 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.2
- CA-321784: Fix LACP bond interface flapping

* Wed May 08 2019 Jennifer Herbert <jennifer.herbert@citrix.com> - 2.5.3-2.3.1
- CA-318197: Correct logging path

* Wed Mar 27 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.5.3-2.3.0
- CA-308221: Add OVS patches required by Rackspace

* Mon Apr 23 2018 Simon Rowe <simon.rowe@citrix.com> - 2.5.3-2.2.3
- CA-288424: Fix sporadic output of incoming packets back to the same port

* Mon Apr 16 2018 Simon Rowe <simon.rowe@citrix.com> - 2.5.3-2.2.2
- CA-281351: Stop services that require network before openvswitch

* Fri Sep 22 2017 Yang Qian <yang.qian@citrix.com> - 2.5.3-2.2.1
- REQ-230 Multicast support

