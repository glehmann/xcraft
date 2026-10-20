%global package_speccommit 39584bc3190200fe5e4f8277686c314e2e5c95b9
%global usver 0.3.10
%global xsver 5
%global xsrel %{xsver}%{?xscount}%{?xshash}

Name:         biosdevname
Version:      0.3.10
Release: %{?xsrel}%{?dist}
Summary:      Helper for naming devices per BIOS names

Group:        System Environment/Base
License:      GPLv2
URL:          http://linux.dell.com/files/%{name}
Provides:     xenserver-biosdevname
# SMBIOS only exists on these arches.  It's also likely that other
# arches don't expect the PCI bus to be sorted breadth-first, or of
# so, there haven't been any comments about that on LKML.
ExclusiveArch: %{ix86} x86_64 ia64
Source0: biosdevname-0.3.10.tar.gz
Patch0: biosdevname-disable-udev.patch
Patch1: biosdevname-CA13344.patch
Patch2: biosdevname-revert-default-policy.patch
Patch3: biosdevname-remove-virtual-devices.patch
Patch4: biosdevname-disable-vm-check.patch
Patch5: biosdevname-fix-output-format.patch
Patch6: Fix-man-help-pages.patch
Patch7: Fix-command-line-parsing.patch

BuildRequires: gcc
BuildRequires: pciutils-devel
BuildRequires: zlib-devel
%{?_cov_buildrequires}

%define _default_patch_fuzz 0

%description
biosdevname in its simplest form takes a kernel device name as an
argument, and returns the BIOS-given name it "should" be.  This is necessary
on systems where the BIOS name for a given device (e.g. the label on
the chassis is "Gb1") doesn't map directly and obviously to the kernel
name (e.g. eth0).

%prep
%autosetup -p1
%{?_cov_prepare}

%build
%configure --disable-rpath --prefix=/ --sbindir=/sbin
%{?_cov_wrap} make %{?_smp_mflags}


%install
make install install-data DESTDIR=%{buildroot}
%{?_cov_install}

%files
%defattr(-,root,root,-)
%doc COPYING README
/sbin/%{name}
%{_mandir}/man1/%{name}.1*.gz

%{?_cov_results_package}


%changelog
* Thu Nov 14 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.3.10-5
- CA-392459: Accept the -x flag

* Mon Feb 21 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.3.10-4
- CP-38416: Enable static analysis

* Tue Dec 08 2020 Ross Lagerwall <ross.lagerwall@citrix.com> - 0.3.10-3
- CP-35517: Package for koji

* Fri Feb 18 2011 Andrew Cooper <Andrew.Cooper3@citrix.com> - 0.3.7-1@XS_RELEASE@
- Apply XenServer specific patches
- Remove udev rules
- Prevent bailing in a virtualized environment

* Thu Feb 17 2011 Matt Domsch <Matt_Domsch@dell.com> - 0.3.7-1
- drop dump_pirq, suggest use biosdecode instead
- don't use '#' in names, use 'p' instead, by popular demand
- properly look for SMBIOS, then $PIR, then recurse
- Add kernel command line parameter "biosdevname={0|1}" to turn off/on biosdevname
- Fix segfault when BIOS advertises zero sized PIRQ Routing Table
- Add 'bonding' and 'openvswitch' to the virtual devices list
- fail PIRQ lookups if device domain is not 0
- Don't suggest names if running in a virtual machine (Xen, KVM,
  VMware tested, but should work on others)
- Typo fixes

* Tue Jan 25 2011 Matt Domsch <Matt_Domsch@dell.com> - 0.3.6-1
- drop biosdevnameS, it's unused and fails to build on F15

* Tue Jan 25 2011 Matt Domsch <Matt_Domsch@dell.com> - 0.3.5-1
- install dump_pirq into /usr/sbin
- fix udev rule, skip running if NAME is already set
- move udev rule to /lib/udev/rules.d by default

* Thu Dec 16 2010 Matt Domsch <mdomsch@fedoraproject.org> - 0.3.4-1
- drop unnecessary explicit version requirement on udev
- bugfix: start indices at 1 not 0, to match Dell and HP server port designations
- bugfix: don't assign names to unknown devices
- bugfix: don't assign duplicate names

* Thu Dec  9 2010 Matt Domsch <Matt_Domsch@dell.com> - 0.3.3-1
- add back in use of PCI IRQ Routing Table, if info is not provided by
  sysfs or SMBIOS

* Thu Dec  2 2010 Matt Domsch <Matt_Domsch@dell.com> - 0.3.2-1
- fix for multi-port cards with bridges
- removal of code for seriously obsolete systems

* Mon Nov 29 2010 Matt Domsch <Matt_Domsch@dell.com> 0.3.1-1
- remove all policies except 'physical' and 'all_ethN'
- handle SR-IOV devices properly

* Wed Nov 10 2010 Matt Domsch <Matt_Domsch@dell.com> 0.3.0-1
- add --policy=loms, make it default
- read index and labels from sysfs if available

* Mon Jul 27 2009 Jordan Hargrave <Jordan_Hargrave@dell.com> 0.2.5-1
- fix mmap error checking

* Fri Jul 24 2009 Fedora Release Engineering <rel-eng@lists.fedoraproject.org> - 0.2.4-7
- Rebuilt for https://fedoraproject.org/wiki/Fedora_12_Mass_Rebuild

* Mon Feb 23 2009 Fedora Release Engineering <rel-eng@lists.fedoraproject.org> - 0.2.4-6
- Rebuilt for https://fedoraproject.org/wiki/Fedora_11_Mass_Rebuild

* Tue May 06 2008 Matt Domsch <Matt_Domsch@dell.com> 0.2.4-5
- use policy=all_names to find breakage

* Sun Feb 10 2008 Matt Domsch <Matt_Domsch@dell.com> 0.2.4-4
- rebuild for gcc43

* Fri Sep 21 2007 Matt Domsch <Matt_Domsch@dell.com> 0.2.4-3
- fix manpage entry in files

* Fri Sep 21 2007 Matt Domsch <Matt_Domsch@dell.com> 0.2.4-2
- rebuild with Requires: udev > 115-3.20070920git

* Fri Sep 21 2007 Matt Domsch <Matt_Domsch@dell.com> 0.2.4-1
- coordinate udev rules usage with udev maintainer
- fix crashes in pcmcia search, in_ethernet(), and incorrect command
  line parsing.

* Mon Aug 27 2007 Matt Domsch <Matt_Domsch@dell.com> 0.2.3-1
- eliminate libbiosdevname.*, pre and post scripts

* Fri Aug 24 2007 Matt Domsch <Matt_Domsch@dell.com> 0.2.2-1
- ExclusiveArch those arches with SMBIOS and PCI IRQ Routing tables
- eliminate libsysfs dependency, move app to / for use before /usr is mounted.
- build static

* Mon Aug 20 2007 Matt Domsch <Matt_Domsch@dell.com> 0.2.1-1
- initial release
