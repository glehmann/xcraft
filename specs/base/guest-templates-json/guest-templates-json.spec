%global package_speccommit c8c991da5882168a03079348f0b0ad29a9e22c2f
%global package_srccommit v2.1.1
Name:    guest-templates-json
Summary: Creates the default guest templates
Version: 2.1.1
Release: 1%{?xsrel}.1%{?dist}
License: BSD
Source0: guest-templates-json-2.1.1.tar.gz

# XCP-ng patches
Source1000: centos-stream-8.json
Source1001: debian-13.json
Source1002: oel-9.json
Source1003: oel-10.json
Source1004: rocky-10.json
Source1005: generic-linux-bios.json
Source1006: generic-linux-uefi.json
BuildArch: noarch

Requires: xapi-core
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
BuildRequires: python3-devel
BuildRequires: python-setuptools
BuildRequires: systemd-devel
Obsoletes: guest-templates-json-data-xenapp

%description
Creates the default guest templates during first boot or package
install/upgrade.

%package data-pv
Summary: Contains the PV guest templates
Requires(post): %{name} = %{version}-%{release}

%description data-pv
Contains the PV guest templates.

%package data-linux
Summary: Contains the default Linux guest templates
Requires(post): %{name} = %{version}-%{release}

%description data-linux
Contains the default Linux guest templates.

%package data-windows
Summary: Contains the default Windows guest templates
Requires(post): %{name} = %{version}-%{release}

Obsoletes: %{name}-data-windows-11 < 2.0.0-2
Conflicts: %{name}-data-windows-11 < 2.0.0-2

%description data-windows
Contains the default Windows guest templates.

%package data-other
Summary: Contains the default other guest templates
Requires(post): %{name} = %{version}-%{release}

%description data-other
Contains the default other guest templates.

%define templatedir %{_datadir}/xapi/vm-templates

%prep
%autosetup -p1

%build
%{__python3} setup.py build

%install
%{__python3} setup.py install --root %{buildroot}

install -d %{buildroot}%{templatedir}
install -m 644 json/*.json %{buildroot}%{templatedir}
install -m 644 %{SOURCE1000} %{SOURCE1001} %{SOURCE1002} %{SOURCE1003} %{SOURCE1004} %{SOURCE1005} %{SOURCE1006} %{buildroot}%{templatedir}
install -d %{buildroot}%{_sysconfdir}/xapi.d/vm-templates

install -m 755 service/create-guest-templates-wrapper %{buildroot}%{_bindir}
install -d %{buildroot}%{_unitdir}
install -m 644 service/create-guest-templates.service %{buildroot}%{_unitdir}

%check
# instead of make check, which needs jsonlint, from python-demjson, not available in EL10:
# python's json.tool checks the syntax, but not the absence of duplicate keys
for json in $(find json -name '*.json'); do %{__python3} -m json.tool "$json" > /dev/null; done
%{__python3} ./check.py

%define statefile %{_localstatedir}/lib/rpm-state/recreate-guest-templates

%post
> %{statefile}
%systemd_post create-guest-templates.service

# On upgrade, migrate from the old statefile to the new statefile so that the
# guest templates are not unnecessarily recreated.
if [ $1 -gt 1 ] ; then
    grep -q ^success /etc/firstboot.d/state/62-create-guest-templates 2>/dev/null && touch /var/lib/misc/ran-create-guest-templates || :
fi

%preun
%systemd_preun create-guest-templates.service

%postun
%systemd_postun create-guest-templates.service

%post data-pv
> %{statefile}

%post data-linux
> %{statefile}

%post data-windows
> %{statefile}

%post data-other
> %{statefile}

%posttrans
if [ -e %{statefile} ]; then
    rm %{statefile}
    /usr/bin/create-guest-templates-wrapper > /dev/null ||:
fi

%posttrans data-pv
if [ -e %{statefile} ]; then
    rm %{statefile}
    /usr/bin/create-guest-templates-wrapper > /dev/null ||:
fi

%files
%{_bindir}/*
%{python3_sitelib}/*
%dir %{templatedir}
%{_sysconfdir}/xapi.d/*
%{_unitdir}/*

%files data-pv
%{templatedir}/base-el-[56]-32bit.json
%{templatedir}/base-el-[56]-64bit.json
%{templatedir}/base-el-pv.json
%{templatedir}/base-kylin-6-64bit.json
%{templatedir}/base-pvlinux.json
%{templatedir}/base-sle-12-pv-64bit.json
%{templatedir}/base-sle-pv-32bit.json
%{templatedir}/base-sle-pv-64bit.json
%{templatedir}/base-sle-pv.json
%{templatedir}/centos-6-32bit.json
%{templatedir}/centos-6-64bit.json
%{templatedir}/kylin-6-64bit.json
%{templatedir}/oel-[56]-32bit.json
%{templatedir}/oel-[56]-64bit.json
%{templatedir}/rhel-[56]-32bit.json
%{templatedir}/rhel-[56]-64bit.json
%{templatedir}/sl-6-32bit.json
%{templatedir}/sl-6-64bit.json
%{templatedir}/sled-11-sp3-64bit.json
%{templatedir}/sled-12-64bit.json
%{templatedir}/sled-12-sp[12]-64bit.json
%{templatedir}/sles-11-sp[34]-32bit.json
%{templatedir}/sles-11-sp[34]-64bit.json
%{templatedir}/sles-12-64bit.json
%{templatedir}/sles-12-sp[12]-64bit.json

%files data-linux
%{templatedir}/alma-[89].json
%{templatedir}/alma-10.json
%{templatedir}/base-el-7.json
%{templatedir}/base-hvmlinux.json
%{templatedir}/base-sle-hvm-64bit.json
%{templatedir}/base-sle-hvm.json
%{templatedir}/base-linux-uefi.json
%{templatedir}/centos-stream-8.json
%{templatedir}/centos-9.json
%{templatedir}/centos-10.json
%{templatedir}/debian*.json
%{templatedir}/oel-[89].json
%{templatedir}/oel-10.json
%{templatedir}/rhel-[89].json
%{templatedir}/rhel-10.json
%{templatedir}/sle-15-64bit.json
%{templatedir}/sles-12-sp[4-5]-64bit.json
%{templatedir}/ubuntu*.json
%{templatedir}/gooroom-2.json
%{templatedir}/rocky-[89].json
%{templatedir}/rocky-10.json
%{templatedir}/generic-linux-bios.json
%{templatedir}/generic-linux-uefi.json

%files data-windows
%{templatedir}/base-windows*.json
%{templatedir}/windows*.json

%files data-other
%{templatedir}/other-install-media.json

%changelog
* Fri Aug 07 2026 Yann Dirson <yann.dirson@vates.tech> - 2.1.1-1.1
- Rebase on 2.1.1-1
- Replace our AlmaLinux templates with XenServer's
- Replace our RHEL 10 template with XenServer's
- Stop using whitespace as separator in *Requires fields
- *** Upstream changelog ***
  * Tue Jul 07 2026 Lunfan Zhang <Lunfan.Zhang@cloud.com> - 2.1.1-1
  - CP-311170 Remove kylin Linux 7 Guest templates
  - CP-312320 Support New Guest Templates AlmaLinux 8/9/10
  - CP-312320 Remove 'preview' label from RHEL 10

  * Thu Apr 30 2026 Frediano Ziglio <frediano.ziglio@citrix.com> - 2.1.0-2
  - CP-312646: Add missing dependency for PV guest templates

  * Thu Nov 13 2025 Lunfan Zhang <Lunfan.Zhang@cloud.com> - 2.1.0-1
  - CP-310014 Support New Guest Template RHEL 10
  - CP-309456 Remove EOL templates from XS9

* Tue Sep 02 2025 Gael Duperrey <gduperrey@vates.tech> - 2.0.14-1.1
- Rebase on 2.0.14-1
- Add templates for almalinux 10, rocky linux 10, debian 13, oracle linux 10, redhat 10
- *** Upstream changelog ***
  * Tue Jan 21 2025 Lunfan Zhang <Lunfan.Zhang@cloud.com> - 2.0.14-1
  - CP-51980: Add CentOS Stream 10 template
  - CP-51980: Remove 'preview' label from Ubuntu24.04

* Tue Mar 04 2025 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.0.13-1.1
- Rebase on 2.0.13-1
- *** Upstream changelog ***
  * Tue Nov 19 2024 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 2.0.13-1
  - CP-50305: Remove '(preview') from Windows Server 2025 template
  * Thu Sep 19 2024 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 2.0.12-1
  - CP-50304 Add Windows Server 2025 preview template
  * Tue Sep 03 2024 Lunfan Zhang <Lunfan.Zhang@cloud.com> - 2.0.11-1
  - CP-47949 Add New Guest template Ubuntu 24.04
  - CP-47949 Remove 'preview' label from RHEL9, Rocky9, CentOS Stream 9 and Debian 12

* Tue Jul 23 2024 Gael Duperrey <gduperrey@vates.tech> - 2.0.10-1.3
- Add generic templates for Linux BIOS and UEFI

* Tue Jul 16 2024 Gael Duperrey <gduperrey@vates.tech> - 2.0.10-1.2
- Remove "preview" from templates Centos 9, Debian 12, rhel 9, Rocky 9.

* Tue Jun 18 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.0.10-1.1
- Sync with 2.0.10-1
- *** Upstream changelog ***
- * Mon Mar 25 2024 Lunfan Zhang <Lunfan.Zhang@cloud.com> - 2.0.10-1
- - CP-47662 Remove deprecated guest templates SUSE Linux 12 SP4 and Ubuntu 18.04
- - CP-48082 Update Guest Templates to support UEFI Secure boot

* Wed Apr 10 2024 Gael Duperrey <gduperrey@vates.tech> - 2.0.9-1.1
- Sync with 2.0.9-1
- *** Upstream changelog ***
- * Tue Feb 13 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.0.9-1
- - CP-46356 Remove 'preview' from Windows 11 template

* Mon Jan 22 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.0.6-1.1
- Update to 2.0.6-1
- Remove our Debian 12, RHEL 9, Rocky Linux 9, and Centos Stream 9 templates, now provided upstream
- *** Upstream changelog ***
- * Thu Oct 19 2023 Lunfan Zhang <Lunfan.Zhang@citrix.com> - 2.0.6-1
- - CP-44378: Remove 'preview' From Ubuntu 22.04 Guest template
- * Thu Sep 14 2023 Lunfan Zhang <Lunfan.Zhang@citrix.com> - 2.0.5-1
- - CP-44127: Add New Guest templates Rocky Linux 9, CentOS Stream 9 and Debian 12
- * Fri Sep 8 2023 Lunfan Zhang <Lunfan.Zhang@citrix.com> - 2.0.4-1
- - CP-44042: Add Red Hat Enterprise Linux 9 (preview) template
- * Wed Sep 6 2023 Lin Liu<Lin.Liu01@cloud.com> - 2.0.3-2
- - BuildRequires python-setuptools to support xs9

* Fri Sep 15 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.0.3-1.1
- Update to 2.0.3-1
- Remove our Debian 11 template, as upstream's one is not marked experimental anymore
- *** Upstream changelog ***
- * Fri Jul 14 2023 Alex Brett <alex.brett@cloud.com> - 2.0.3-1
- - CP-43952: Remove 32-bit Windows 10
- * Fri May 05 2023 Xihuan Yang <xihuan.yang@citrix.com> - 2.0.2-1
- - CP-41630: Support up to 64 vCPUs for HVM guests
- * Thu Feb 16 2023 Minghui Hu <minghui.hu@citrix.com> - 2.0.1-1
- - CP-41650: Remove CoreOS templates from Next
- * Tue Feb 14 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.0.0-2
- - CP-42132: Install Windows 11 template by default

* Mon Jul 24 2023 Gael Duperrey <gduperrey@vates.fr> - 2.0.0-1.2
- Add template for Debian 12

* Tue Feb 21 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 2.0.0-1.1
- Update to 2.0.0-1
- *** Upstream changelog ***
- * Thu Jan 19 2023 Ross Lagerwall <ross.lagerwall@citrix.com> - 2.0.0-1
- Switch upstream to GitHub
- - Build for Python 3 and drop Python 2 support
- - CP-41654: Check for duplicate uuids and reflabels in templates

* Wed Jan 18 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.11.2-1.1
- Update to 1.11.2-1
- *** Upstream changelog ***
- * Tue Nov 29 2022 Xihuan Yang <xihuan.yang@citrix.com> - 1.11.2-1
- - CP-39690: Add Ubuntu Jammy 11 (experimental) template

* Fri Jan 06 2023 Gael Duperrey <gduperrey@vates.fr> - 1.11.1-1.2
- Add templates for rhel 9, CentOS Stream 8 and 9, Almalinux 9, Rockylinux 9, Oracle linux 9

* Wed Dec 07 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.11.1-1.1
- Update from XS 8.3 pre-release updates
- Keep our debian 11 template until the upstream one is not displayed as experimental anymore
- *** Upstream changelog ***
- * Tue Sep 13 2022 Xihuan Yang <xihuan.yang@citrix.com> - 1.11.1-1
- - CP-40462: Remove EOL templates from Next
- * Wed Aug 31 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.11.0-1
- - Add Windows 11 preview template
- * Wed Jun 01 2022 Xihuan Yang <xihuan.yang@citrix.com> - 1.10.2-1
- - CP-39797: Add Debian Bullseye 11 (experimental) template

* Fri Aug 26 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.10.1-2.1
- Re-sync with CH 8.3 preview (including changelog)
- Re-add our patches (almalinux 8 and debian 11 templates).

* Mon Aug 23 2021 Xihuan Yang <xihuan.yang@citrix.com> - 1.10.1-2
- CP-37115: Update file size and sha256 in CHECKSUMS after include Windows Server 2022

* Tue Aug 10 2021 Xihuan Yang <xihuan.yang@citrix.com> - 1.10.1-1
- CP-37115: Add Rocky Linux 8 template

* Thu Jul 08 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.10.0-1
- Add Windows Server 2022 template

* Fri Nov 06 2020 Xihuan Yang <xihuan.yang@citrix.com> - 1.9.2-1
- CP-33714: add template for gooroom os

* Thu Sep 10 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.9.1-1
- CA-342292: Skip running if xensource-inventory does not exist

* Wed Jun 24 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.9.0-1
- CA-340484 improve error message when template import fails

* Fri May 29 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.8.19-1
- CP-33714: add template for gooroom os
- CA-336730 create-guest-templates only after network-init
- CA-339329 firstboot scripts shouldn't sync DB when ugprading
- Revert "CP-33714: add template for gooroom os"

* Sun May 10 2020 Xihuan Yang <xihuan.yang@citrix.com> - 1.8.17-1
- CP-32907: Add new guest for Ubuntu 20.04 (64-bit)

* Tue Mar 31 2020 Fei Su <fei.su@citrix.com> - 1.8.16-1
- CP-32727: Add new guest for SUSE Linux Enterprise Server 12 SP5 (64-bit)

* Mon Mar 30 2020 Sergey Dyasli <sergey.dyasli@citrix.com> - 1.8.15-1
- CA-333172: Remove viridian from PV guest template
- Revert "CP-32458: make PV guests to be "plain PV" again"

* Fri Mar 20 2020 Fei Su <fei.su@citrix.com> - 1.8.14-1
- CP-32728 Remove Windows 7 for Stockholm
- CP-32707 Remove Windows Server 2008 and Windows Server 2008 R2

* Tue Feb 11 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.8.13-1
- CA-334955: Connect to XAPI using a UNIX domain socket

* Fri Nov 22 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.8.11-1
- CP-31095: Convert the firstboot script into a standalone service

* Thu Nov 14 2019 Sergey Dyasli <sergey.dyasli@citrix.com> - 1.8.10-1
- CP-32458: make PV guests to be "plain PV" again

* Thu Oct 31 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.8.9-1
- CP-32392: Set domain_type to pv_in_pvh for base-pvlinux.json

* Thu Sep 26 2019 Fei Su <fei.su@citrix.com> - 1.8.8-1
- CP-31388 Centos 8 template add

* Mon Sep 23 2019 Fei Su <fei.su@citrix.com> - 1.8.7.3-1
- CP-31827: Remove Ubuntu 14.04 template

* Tue Sep 10 2019 Xihuan Yang <xihuan.yang@citrix.com> - 1.8.7.2-1
- CP-31826: Remove centos5 templates

* Mon Sep 09 2019 Fei Su <fei.su@citrix.com> - 1.8.7.1-2
- CP-32028: Stop PV guests appearing in the default template set in Quebec

* Tue Sep 03 2019 Xihuan Yang <xihuan.yang@citrix.com> - 1.8.7.1-1
- CA-31821: Add new template for debian 10

* Thu Aug 22 2019 Xihuan Yang <xihuan.yang@citrix.com> - 1.8.6-1
- CP-31830: Add support to Oracle Linux 8

* Wed Jul 10 2019 Jennifer Herbert <jennifer.herbert@citrix.com> - 1.8.5-1
- CA-321550: Revert "CA-310465 Use of reference_tsc appears to cause problems across migration"

* Thu Jun 27 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.8.4-2
- CA-322182: Recreate guest-templates only once per transaction

* Wed Jun 26 2019 Fei Su <fei.su@citrix.com> - 1.8.4-1
- CP-31415: Create Redhat 8 Template

* Wed Jun 12 2019 Fei Su <fei.su@citrix.com> - 1.8.2-1
- CP-31330: Create SLES 12 SP4 and SLED 12 SP4 template for plymouth

* Tue Jun 4 2019 Xihuan Yang <xihuan.yang@citrix.com> - 1.8.1-1
- CP-31406: Modify disk size to 32G for window 10 template

* Thu May 30 2019 Edwin Török <edvin.torok@citrix.com> - 1.8.0-1
- CP-29856: set minimum number of vCPUs to 2
- CP-30443: set secureboot mode to auto
- REQ-396: Windows 10, 2016, 2019 64-bit default to UEFI boot mode
- REQ-396: prefer booting from disk first for Windows guest to avoid install loop

* Thu Feb 21 2019 Sergey Dyasli <sergey.dyasli@citrix.com> - 1.7.20-1
- CA-310465: disable reference_tsc as it appears to cause problems across migration

* Fri Feb 15 2019 Alex Brett <alex.brett@citrix.com> - 1.7.19-1
- CP-30588: Remove Asianux, Kylin 5, Linx, GreatTurbo and Yinhe Kylin templates for Naples.

* Thu Feb 14 2019 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.7.18-1
- CA-310681: disable remote TLB flush hypercall by default

* Wed Feb 06 2019 jenniferhe <jennifer.herbert@citrix.com> - 1.7.17-1
- CA-307828: Enabled more viridian enlightenments for windows guests

* Mon Jan 14 2019 Yuan Ren <yuan.ren@citrix.com> - 1.7.16-1
- CP-30013: Update CoreOS, SLES/SLED 15 template for Naples.
- CP-30191: Remove Debian 6, Ubuntu 12.04 and legacy window templates for Naples.

* Tue Dec 18 2018 Edwin Török <edvin.torok@citrix.com> - 1.7.15-1
- Revert changes to boot order for BIOS

* Tue Dec 18 2018 Edwin Török <edvin.torok@citrix.com> - 1.7.14-1
- Flip the boot order between HDD and CD
- Default some templates to UEFI + SB
- Windows 10 32-bit and 64-bit will have to be derived from different base templates (64-bit uses UEFI)
- Windows {10,Server 2016, Server 2019} 64-bit are all derived from UEFI
- CP-29857: do not set device-model in UEFI mode and add unit test
- CP-29856: set minimum number of vCPUs to 2
- fixup! CP-29856: set minimum number of vCPUs to 2
- fixup! CP-29857: do not set device-model in UEFI mode and add unit test
- CA-303990: Default Windows 10, 2016, 2019 to BIOS boot
- CP-30222: remove 'secureboot: true'
- Revert "CP-29856: set minimum number of vCPUs to 2"

* Fri Sep 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.7.13-1
- CP-28737 : Add Windows Server 2019 template for Naples
- Replace a per-template user_version with a single version
- CP-28658 update templates to contain recommendations that they support BIOS, UEFI or Secure Boot

* Thu Aug 30 2018 Simon Rowe <simon.rowe@citrix.com> - 1.7.12-1
- Add JSON check rule
- Remove legacy spec file
- Add a README

* Thu Jun 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.7.11-1
- CA-292656: Fix device-model typo

* Mon Jun 25 2018 Simon Rowe <simon.rowe@citrix.com> - 1.7.10-1
- CP-28459: Add new Ubuntu 18.04 template for Lima

* Wed May 30 2018 Simon Rowe <simon.rowe@citrix.com> - 1.7.9-1
- CA-290004: Change SLE.12 PV templates to use 'hvc' as their console

* Wed Mar 28 2018 Ross Lagerwall <ross.lagerwall@citrix.com> - 1.7.8-1
- CP-17721: Use qemu-upstream-compat for new VMs
- Increment user_version for QEMU upstream changes

* Tue Mar 27 2018 Samson Danziger <samson.danziger@citrix.com> - 1.7.7-1
- Increase user version on sle 12.3 templates
- Remove unnecessary config, and generate new uuid
- Add HVM templates for SLE.123 HVM

* Mon Mar 26 2018 Simon Rowe <simon.rowe@citrix.com> - 1.7.6-1
- CP-23798: Add template support for Network SR-IOV

* Mon Mar 26 2018 Simon Rowe <simon.rowe@citrix.com> - 1.7.5-1
- Bump user version
- CP-27447: Rename Windows 8 templates to Windows 8.1
- CA-286130: Set Debian 9 min memory to 256 MiB

* Mon Mar 26 2018 Simon Rowe <simon.rowe@citrix.com> - 1.7.4-1
- Add the user_version to the oel6 x64 template
- Add crashkernel=no as an extra PV arg for oel6 x64 guests
- CA-275747 Remove old, untested/supported guests
- CP-27055: Remove templates for removed guests

* Mon Oct 23 2017 Simon Rowe <simon.rowe@citrix.com> - 1.7.3-1
- CP-24360: Initialise VM.platform['device-model'] values - Stage 1

* Tue Oct 10 2017 Simon Rowe <simon.rowe@citrix.com> - 1.7.2-1
- CA-268286: Make all EL7 guests have at least 2G RAM

* Wed Sep 27 2017 Simon Rowe <simon.rowe@citrix.com> - 1.7.1-1
- CA-267449: fix typo in SLES template

* Wed Sep 20 2017 Simon Rowe <simon.rowe@citrix.com> - 1.7.0-1
- Add Debian Stretch 9 guest template
- Update CoreOS (Container Linux) disk and RAM requirements for v1465.7.0
- Add SLES 12.2 template assuming PV
- Add SLED 12.2 template
- Add SLES and SLED 12 SP3 for PV

* Fri Aug 18 2017 Deli Zhang <deli.zhang@citrix.com> - 1.6.0-1
- CP-22495: Add Linux guest template for NeoKylin Linux Security OS 5.0 (Update8) x64

* Thu Jun 08 2017 Wei Xie <wei.xie@citrix.com> - v1.5.0-1
- CA-256319: Rocky6 configured with 8 vCPUs installation failure via XenCenter
- CP-22352: Add support for Yinhe Kylin 4.
- CA-255653: Change Linx 6 to HVM mode.
- CP-22351: Add support for Linx Linux 6 and 8.

* Fri Jun 02 2017 Wei Xie <wei.xie@citrix.com> - v1.4.0-1
- CP-22097: Add Linux guest template for Turbo Linux.
- CP-21799: And new VM templates for Asianux 4 Linux.

* Thu May 04 2017 Simon Rowe <simon.rowe@citrix.com> - 1.2.1-1
- CP-251550: Update legacy windows template

* Wed Apr 26 2017 Simon Rowe <simon.rowe@citrix.com> - 1.2.0-1
- Update base Windows template to use 2 vCPUs
- CP-21446: Remove old templates from XenServer.
- CP-21988 Remove Vista template
- CP-21988 Add legacy windows template

