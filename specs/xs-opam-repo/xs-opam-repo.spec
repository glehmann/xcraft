%global package_speccommit 3897985874efa761a47528d2831f4b210975b71e
%global usver 6.99.0
%global xsver 1
%global xsrel %{xsver}%{?xscount}%{?xshash}
## This has to match the declaration in xs-opam-src, which
## creates the directory and makes it WORLD WRITABLE
%global _opamroot %{_libdir}/opamroot

# The following has no effect on XS 8, only on XS 9.
# However, something needs to be fixed on XS 9 to not need it anymore.
%global _debugsource_template %{nil}

%global _version 6.99.0

# When building an untagged version, add the number of commits and hash after
# the variable _version. e.g. -34-gab48a58c for 6.77.0-34-gab48a58c
%global _version_full %{_version}

Name: xs-opam-repo
Version: %{_version}
Release: %{?xsrel}.2%{?dist}
Summary: Build and install OCaml libraries from Opam repository
# The license field is produced by running print-license.sh
# Please update licenses.txt on every new version and then run the script to
# keep these in sync.
License: Apache-1.0 and Apache-2.0 and BSD-2-Clause and BSD-3-Clause and curl and GPL-1.0-or-later and GPL-2.0-only and GPL-3.0-or-later and ISC and LGPL-2.0-only WITH OCaml-LGPL-linking-exception and LGPL-2.0-or-later WITH OCaml-LGPL-linking-exception and LGPL-2.1-only and LGPL-2.1-only WITH OCaml-LGPL-linking-exception and LGPL-2.1-or-later WITH OCaml-LGPL-linking-exception and LGPL-2.1-or-later WITH OpenSSL-linking-exception and LGPL-3.0-only and MIT and PSF-2.0
URL:     https://github.com/xapi-project/xs-opam
Source0: xs-opam-repo-6.99.0.tar.gz
Source1: host-arch-arm64.1.opam
# To "pin" a package during development, see below the example
# where ezxenstore is pinned to an internal master branch.
# You need the Source1 line, and the below 'tar' and 'opam pin' lines, and comment-out the OPAMFETCH
# Replace YOURUSER below with your CITRITE userid where you put the repo
# (make sure you gave 'stash-users' read permissions on it)
# Note that Jenkins will likely not pick up commits on this repo, and you have to hit 'Build Now'
# when you want a new build.
# Source1: https://code.citrite.net/rest/archive/latest/projects/~YOURUSER/repos/ezxenstore/archive?at=master&format=tar.gz&prefix=ezxenstore#/ezxenstore.tar.gz

BuildRequires: xs-opam-src >= 5.1.0

Requires:      opam >= 2.0.0
Requires:      ocaml
Requires:      gmp
Requires:      libev-devel

BuildRequires: autoconf
%if 0%{?xcpng}
BuildRequires: libcurl-devel
%else
BuildRequires: curl-devel
%endif
# XCP-ng: remove dlm, which is only required by proprietary xapi-clusterd
#BuildRequires: dlm-devel
BuildRequires: git
BuildRequires: gmp
BuildRequires: gmp-devel
BuildRequires: hwdata
BuildRequires: libffi-devel
BuildRequires: libnl3-devel
BuildRequires: ocaml
%if 0%{?xcpng}
BuildRequires: ocaml-ocamldoc
%else
BuildRequires: ocamldoc
%endif
BuildRequires: opam > 2.1.4-1
BuildRequires: openssl-devel
BuildRequires: pam-devel
BuildRequires: pciutils-devel
BuildRequires: perl-interpreter
BuildRequires: python3
BuildRequires: rsync
BuildRequires: systemd-devel
BuildRequires: which
%if 0%{?xcpng}
BuildRequires: zlib-ng-devel
%else
BuildRequires: zlib-devel
%endif
BuildRequires: libev-devel

# needed for OCaml 5
%global __ocaml_requires_opts -i Dynlink_cmo_format -i Dynlink_cmxs_format

%description
Opam repository that contains all libraries necessary to compile the
Toolstack components of the Citrix Hypervisor.

%prep
%autosetup -p1 -n xs-opam-repo-%{_version_full}
# XCP-ng: remove dlm, which is only required by proprietary xapi-clusterd
rm -r packages/dlm/dlm.*
%ifarch aarch64
mkdir -p packages/host-arch-arm64/host-arch-arm64.1
cp %{SOURCE1} packages/host-arch-arm64/host-arch-arm64.1/opam
rm -r packages/host-arch-x86_64
%endif

%build

%install

# install into the real opam root to avoid problems with
# embedded paths.
export OPAMROOT=%{_opamroot}
opam init --bare --no-setup -k local xs-opam . -y
opam switch create ocaml-system
# XCP-ng: avoid hard-to-interpret errors while depext is still not stable
#opam option depext=false

# take note of installed packages, along with their versions
PKG_EXISTING=$(opam list --installed --short --separator=. --columns=name,version | tr -d ' ' | tr '\n' ',')

# take note of packages that need to be installed. They have to be installable with already installed package versions, in particular with the current OCaml version.
PKG=$(opam exec -- opam list --installable --short --coinstallable-with=$PKG_EXISTING)

export OPAMFETCH=/bin/false
opam exec -- opam install %{?_smp_mflags} -y $PKG

mkdir -p %{buildroot}/etc/profile.d
mkdir -p %{buildroot}%{_opamroot}
echo 'export OPAMROOT=%{_opamroot}' > %{buildroot}/etc/profile.d/opam.sh
echo 'eval `opam config env`' >> %{buildroot}/etc/profile.d/opam.sh
echo 'export OCAMLPATH=%{_libdir}/ocaml' >> %{buildroot}/etc/profile.d/opam.sh

rm -rf %{_opamroot}/ocaml-system/.opam-switch/sources
rm -rf %{_opamroot}/download-cache/*
rm -rf %{_opamroot}/repo/local/cache/*
# remove log as it contains $RPM_BUILD_ROOT env value
rm -rf %{_opamroot}/log
find   %{_opamroot}/ocaml-system/lib -type f -name '*.cmt*' -delete

rsync -aW %{_opamroot}/ %{buildroot}%{_opamroot}/
strip %{buildroot}%{_opamroot}/ocaml-system/bin/* || true

mkdir -p "%{buildroot}%{_rpmconfigdir}/macros.d"
echo '%%_opamroot %%{_libdir}/opamroot' >> "%{buildroot}%{_rpmconfigdir}/macros.d/macros.opam"

%files
%attr(644, root, root) /etc/profile.d/opam.sh
%defattr(-, root, wheel, 775)
%exclude %{_opamroot}/download-cache
%exclude %{_opamroot}/repo/local/cache
%{_rpmconfigdir}/macros.d/macros.opam
%{_opamroot}

%changelog
* Fri Aug 21 2026 Yann Dirson <yann.dirson@vates.tech> - 6.99.0-1.2
- Use Alma10 package names in BuildRequires for xcpng
- Drop unused devtoolset-11 for xenserver < 9

* Fri Mar 20 2026 Yann Dirson <yann.dirson@vates.tech> - 6.99.0-1.1.1
- Hack in the definition for host-arch-arm64, from
  https://raw.githubusercontent.com/ocaml/opam-repository/e64203035b54812a6e33cfa9f390fd0f17c02e07/packages/host-arch-arm64/host-arch-arm64.1/opam
- Document why we comment out the depext line from XS
- Adjust BuildRequires: perl to perl-interpreter

* Wed Feb 18 2026 Pau Ruiz Safont <pau.safont@vates.tech> - 6.99.0-1.1
 - *** Upstream changelog ***
 * Tue Jan 13 2026 Rob Hoes <rob.hoes@cloud.com> - 6.99.0-1
 - packages: Use ocamlformat 0.28.1

 * Tue Jan 13 2026 Rob Hoes <rob.hoes@cloud.com> - 6.98.0-1
 - vhd-format: Sync with xapi
 - packages: prepare for an ocamlformat compatible with ocaml 5.4 (#750)
 - packages: update ocamlformat from 0.27.0 to 0.28.1
 - packages: patch notty 0.2.3 for ocaml 5.4
 - packages: update num to 1.6
 - packages: drop bisect_ppx from testing dependencies
 - packages: update xen-api's metadata

* Fri Jan 30 2026 Pau Ruiz Safont <pau.safont@vates.tech> - 6.97.0-1.1
- *** Upstream changelog ***
  * Thu Nov 27 2025 Rob Hoes <rob.hoes@cloud.com> - 6.97.0-1
  - qcow: update to 0.12.2
  - packages: define dlm's depext for centos-based distributions

* Wed Nov 19 2025 Pau Ruiz Safont <pau.safont@vates.tech> - 6.96.0-1.1
- *** Upstream changelog ***
  * Fri Oct 03 2025 Rob Hoes <rob.hoes@cloud.com> - 6.96.0-1
  - Add missing oxenstored dependencies

  * Wed Oct 01 2025 Rob Hoes <rob.hoes@cloud.com> - 6.95.0-1
  - Package updates for OCaml 5.3 compatibility

* Wed Aug 06 2025 Andrii Sultanov <andriy-sultanov@vates.tech> - 6.91.0-1.1
- Update to 6.91.0-1
- *** Upstream changelog ***
  * Fri Jul 18 2025 Rob Hoes <rob.hoes@cloud.com> - 6.91.0-1
  - Add qcow-stream and its dependencies
  - packages: update xen-api's metadata

  * Tue Jun 10 2025 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.90.0-1
  - packages: update dune packages to 3.18.2
  - packages: update ocaml-xenstore to 2.4.0
  - packages: update mirage-crypto to 2.0.1

  * Mon Jun 09 2025 Bengang Yuan <bengang.yuan@cloud.com> - 6.89.0-2
  - Bump release and rebuild

  * Thu May 22 2025 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.89.0-1
  - packages: round of non-breaking updates (dune, ssl, others)
  - packages: update xenstore_transport to 1.4.0

  * Fri Mar 28 2025 Rob Hoes <rob.hoes@cloud.com> - 6.88.0-1
  - New HTTP packages
  - Non-breaking updates of existing packages

  * Tue Jan 21 2025 Deli Zhang <deli.zhang@cloud.com> - 6.87.0-2
  - CP-52964: Build with OpenSSL3 for XS8

* Mon Mar 31 2025 Samuel Verschelde <stormi-xcp@ylix.fr> - 6.87.0-1.1
- Update to 6.87.0-1
- *** Upstream changelog ***
  * Tue Dec 03 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.87.0-1
  - Drop dependency on async and core packages
  - Non-breaking updates of packages

* Tue Jan 21 2025 Yann Dirson <yann.dirson@vates.tech> - 6.86.0-1.1
- Update to 6.86.0-1
- Refresh removal of dlm package (location changed in source tree)
- Don't pass "-f" to "rm" while removing packages, to better catch upstream changes
- Fix typo generating /etc/profile.d/opam.sh
- Reformat changelog to allow diffing with upstream
- *** Upstream changelog ***
  * Wed Oct 09 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.86.0-1
  - Include crowbar library
  
  * Tue Oct 08 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.85.0-1
  - Update Uuidm to 0.9.9
  - Use upstream folder structure, use opam-directed tools for managing it
  
  * Mon Sep 23 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.84.0-1
  - CA-399172: fix potential crash in Uri.of_string
  
  * Fri Jul 26 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.83.0-1
  - Patch Uri packages to add path_unencoded
  - jst-config: fix build on Fedora40
  - Metadata refresh, breaking update of upstream libraries
  - Remove systemd library
  
  * Tue Jul 02 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.81.0-1
  - Update crc to 2.2.0
  - Add qcheck and qcheck-alcotest
  
  * Wed May 22 2024 Rob Hoes <rob.hoes@cloud.com> - 6.80.0-1
  - Add psq 0.2.1

* Fri Aug 09 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 6.80.0-1.1
- Rebase on 6.80.0-1
- *** Upstream changelog ***
  * Wed May 22 2024 Rob Hoes <rob.hoes@cloud.com> - 6.80.0-1
  - Add psq 0.2.1

  * Mon May 13 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.79.0-1
  - ocaml: add 4.14.2 packages
  - upstream: update non-breaking packages

* Tue Jun 18 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 6.78.0-1.1
- Rebase on 6.78.0-1
- *** Upstream changelog ***
  * Thu Mar 07 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.78.0-1
  - Add patch for rpclib to accept empty variants
  - Non-breaking update of upstream dependencies

* Wed Apr 10 2024 Benjamin Reis <benjamin.reis@vates.tech> - 6.77.0-1.1
- Rebase on 6.77.0-1
- Drop xs-opam-repo-6.74.0-metajson.XCP-ng.patch
- *** Upstream changelog ***
  * Wed Jan 31 2024 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.77.0-1
  - Remove xapi-rd, xapi-inventory and xapi-stext-* packages

  * Wed Dec 20 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.76.0-2
  - Bump release

  * Tue Dec 19 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.76.0-1
  - upstream: update ocamlformat to the latest version
  - update inotify to 2.5.0 and uri to 4.4.0

  * Fri Nov 17 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.75.0-1
  - xs: update xapi-rrd to 1.11.0

* Wed Feb 14 2024 Yann Dirson <yann.dirson@vates.tech> - 6.74.0-1.2
- backport xapi-project/xen-api#5456

* Mon Jan 22 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 6.74.0-1.1
- Update to 6.74.0-1
- Drop xs-opam-repo-6.72.0-fix-ipv6-uri.XCP-ng.patch, applied by XS
- *** Upstream changelog ***
  * Thu Nov 02 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.74.0-1
  - xs: update xapi-stext packages to 4.23.0
  - licenses: update package licenses

  * Fri Oct 27 2023 Lin Liu <Lin.Liu01@cloud.com> - 6.73.0-2
  - Build with gcc for xs9

  * Wed Sep 27 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.73.0-1
  - xs: update polly and qmp
  - upstream: update many packages, there are no breaking changes

* Fri Sep 22 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 6.72.0-1.1
- Update to 6.72.0-1
- Rediff xs-opam-repo-6.72.0-fix-ipv6-uri.XCP-ng.patch
- *** Upstream changelog ***
  * Fri Jul 28 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.72.0-1
  - upstream: patch rpclib to support python3, and start using it
  - upstream: patch uri packages to parse ipv6 addresses correctly
  - maintenance: Fix issues spotted by the opam linter

  * Mon Jul 17 2023 Edwin Török <edwin.torok@cloud.com> - 6.71.0-2
  - Bump release and rebuild

  * Tue Jul 11 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.71.0-1
  - xs: update xapi-rrd to 1.9.2 (memory leak fix)
  - xs: add goblint for static analysis of C stubs

  * Mon Jul 10 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.70.0-2
  - Remove build dependencies, they are opam's normal dependencies

  * Wed Jun 07 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.70.0-1
  - xs: update xapi-stdext packages
  - upstream: remove unused dependencies, do not depend on xen
  - xs-extra: update metadata from upstream
  - vhd-format: move from upstream to xs-extra

  * Tue Jun 06 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.69.0-2
  - Bump release and rebuild

  * Fri May 05 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.69.0-1
  - ocaml: Use correct file contents in ocaml-system.4.14.1
  - upstream: Remove old ocamlformat and odoc-parser

  * Mon Apr 24 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.68.0-1
  - xs: do not package ezxenstore, it's a part of xen-api
  - ocaml: add 4.14

  * Wed Mar 22 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.67.0-1
  - xs: xenctrl.dummy: do not rsync Xen packages we do not use
  - Make xs-opam self-consistent and able to run unit tests
  - upstream: enforce only known licenses
  - upstream: update dune packages to 3.7.0

* Fri Jul 07 2023 Benjamin Reis <benjamin.reis@vates.fr> - 6.66.0-1.2
- Patch `ocaml-uri` for IPv6 URI parsing

* Thu Mar 16 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 6.66.0-1.1
- Remove dlm, which is only required by proprietary xapi-clusterd
- Drop the dlm-devel BuildRequires

* Thu Feb 02 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.66.0-1
- upstream: Add opentelemetry libraries for testing, includes ocurl and ezcurl

* Fri Jan 27 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.65.0-1
- CP-40716, General update of dependencies:
- upstream: update conduit packages to 6.1.0
- upstream: update bisect_ppx to 2.8.1
- upstream: update io-page packages to 3.0.0
- upstream: update conduit packages to 5.1.1
- upstream: update x509 to 0.16.0
- upstream: update mirage-crypto packages to 0.10.7
- upstream: update tar packages to 2.0.1
- upstream: update mirage-clock packages to 4.2.0
- upstream: update ctypes to 0.20.1
- upstream: update mirage-time packages to 3.0.0
- upstream: update xenstore to 2.2.0
- upstream: update lwt packages to 5.6.1
- upstream: update vhd-format packages to 0.12.3
- upstream: update ezjsonm to 1.3.0
- upstream: update eqaf to 0.9
- upstream: update hex to 1.5.0
- upstream: update ounit packages to 2.2.6
- upstream-extra: update react to 1.2.2
- upstream: update ipaddr packages to 5.3.1
- upstream: update bigarray-compat to 1.1.0
- upstream: update integers to 0.7.0
- upstream: update ocaml-version to 3.5.0
- upstream: update domain-name to 0.4.0
- upstream-extra: update odoc to 2.1.1
- upstream: update qcheck packages to 0.19.1
- upstream: update ocamlbuild to 0.14.1
- upstream: update bigstringaf to 0.9.0
- upstream: update inotify to 2.4
- upstream: update ocaml-migrate-parsetree to 2.4.0
- upstream: update cppo to 1.6.9
- upstream: update re to 1.10.4
- upstream: update sha to 1.15.2
- upstream: update Janestreet libraries to v0.15.x
- upstream: update cohttp packages to 2.5.6
- upstream: update fix to 20220121
- upstream: update menhir libraries to 20220210
- upstream: update alcotest packages to 1.6.0
- upstream: update uutf to 1.0.3
- upstream: update xmlm to 1.4.0
- upstream: update uuidm to 0.9.8
- upstream: update topkg to 1.0.5
- upstream: update ptime to 1.0.0
- upstream: update conf packages
- upstream: update ssl to 0.5.13
- upstream: update yojson to 2.0.0 and dependencies
- upstream-extra: update utop to 2.10.0
- upstream-extra: update merlin to 4.5
- upstream: update to ppx_tools to 6.5
- upstream: update dune to 3.4.1
- upstream: update metadata from opam-repository

* Thu Jan 26 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.64.0-2
- Bump release and rebuild

* Tue Jan 10 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.64.0-1
- xs: update polly to 0.3.0

* Tue Dec 06 2022 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.63.0-1
- xs: update ezxenstore to 0.4.2

* Thu Dec 01 2022 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 6.62.0-1
- xs: update xapi-stdext to 4.21.0

* Fri Nov 11 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.61.0-1
- upstream: add crowbar to use it for testing in xapi

* Fri Oct 28 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.60.0-1
- CA-371780: update xapi-rrd to 1.9.1
- xs-extra: update xapi package metadata
- xs-extra: add dependencies to new packages
- xs-extra: update with the upstream metadata
- tools: add xapi-log and xapi-open-uri to metadata generator
- gitignore: ignore cache.log
- xs-extra: xen-api packages don't need to set XAPI_VERSION anymore
- CA-367236: update xapi-rrd to 1.9.0
- CI: deactivate stockholm / 8.2.0 builds
- xs-extra: xapi-xenopsd now uses zstd for compression

* Mon Aug 08 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.59.0-2
- Bump release and rebuild

* Mon Jul 04 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.59.0-1
- xs-extra: add merge-fmt 0.2
- CP-39805: update utop to 2.9.2
- CP-39805: update xapi-stdext packages to 4.19.0
- CP-39805: update xapi-inventory to 1.2.3
- CP-39805: update qmp to 0.19.0
- CP-39805: update dlm to 0.3.3
- CP-39805: update cmdliner to 1.1.1 and rpclib to 9.0.0
- CP-39805: update odoc to 2.1.0
- CP-39805: update ocaml-lsp-server to 1.10.3
- xs-extra: sync metadata with xen-api
- upstream-extra: update depext to latest version
- CP-34028: refresh xs-extra metadata for uuid changes

* Mon Mar 07 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.58.0-1
- xs: add ounit as dependency to dlm
- update ocamlfind to 1.9.3
- upstream: add memtrace 0.2.1.2
- update logs-syslog to 0.2.2
- upstream: update metadata for existing packages
- update rresult to 0.7.0
- update rpclib packages to 8.1.1
- update re to 1.10.3
- update ipaddr packages to 5.2.0
- update buenzli's unicode packages to 14.0.0
- update mtime to 1.3.0
- update conf-libev to 4-12
- update ocplib-endian to 1.2
- update topkg to 1.0.4
- update fmt to 0.9.0
- update asn1-combinators to 0.2.6
- update lwt-dllist to 1.0.1
- update sha to 1.15.1
- update domain-name to 0.3.1
- update duration to 0.2.0
- update uri packages to 4.2.0
- update tyxml to 4.5.0
- update lwt packages to 5.5.0
- update ocaml-compiler-libs to v.0.12.4
- update ocaml-version to 3.1.0
- update magic-mime to 1.2.0
- update cppo to 1.6.8
- update eqaf to 0.8
- update bigstringaf to 0.8.0
- update integers to 0.5.1
- update cstruct packages to 6.0.1
- update menhir libraries to 20211128
- Update qcheck packages to 0.18
- update janestreet packages to their latest 0.14.x version
- upstream: update alcotest to 1.5.0
- CP-38617: expose xentoollog as a package and use it
- CA-359981: Replace 'typeof OCaml system' with a recognized SPDX
- ci: package and publish tarball to github on tag
- maintenance: drop previous ocaml versions
- CA-359981: Tweak licenses to avoid duplicates

* Fri Feb 18 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.57.0-3
- CA-359981: Include licenses.txt with all the libraries' licenses
- CA-359981: Populate license field with all the pertinent licenses

* Tue Feb 15 2022 Rob Hoes <rob.hoes@citrix.com> - 6.57.0-2
- Bump release and rebuild with OCaml 4.13.1 compiler.

* Fri Feb 04 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 6.57.0-1
- update libraries to be compatible with OCaml 4.13:
- upstream: update ocamlformat to 0.19.0
- upstream: update vhd-format to 0.12.2
- upstream: update JST libraries for ocaml 4.13.1
- upstream: update ppx_tools to 6.4
- upstream: update ppxlib to 0.22.2
- upstream: upgrade ocaml-migrate-parsetree to 2.3.0

* Tue Jan 25 2022 Christian Lindig <christian.lindig@citrix.com> - 6.56.0-1
- CA-359981: Use correct SPDX identifiers
- CI: add stockholm LCM badge
- ci: detect unused packages
- CI: update status badge
- ci: use updated container images
- conduit: allow turning TLS hostname verification off by setting..
- CP-34942: Update dune to 2.9.0
- CP-34942: Update dune to 2.9.1
- CP-34942: Update ocamlfind to 1.9.1
- CP-34942: update unix-errno
- CP-37034: add tyre: library for typed regular expressions
- CP-37368: merge xapi-idl into message-switch
- CP-37369: merge squeezed into xenopsd
- CP-37931: merge forkexecd into message-switch
- CP-38206: transitional libs are in the xapi's main repo
- maintenance: fix depexts
- maintenance: remove mentions of docker in the readme
- maintenance: remove unused conf-m4
- maintenance: remove unused upstream packages
- maintenance: schedule builds for the yangtze branch
- remove xapi-libs-transitional metapackage
- specsavers: merge xapi-storage(-script) into message-switch
- specsavers: merge xcp-networkd into xen-api
- specsavers: merge xen-api-client into xen-api
- specsavers: merge xenopsd into xen-api
- specsavers move message-switch code to xen-api
- specsavers: move remaining independent daemons to xen-api
- specsavers move xen-api-sdk code to xen-api
- specsavers: relocate vhd-tool
- specsavers: remove tapctl
- specsavers: rrdd-plugins -> xcp-rrdd
- specsavers: xcp-rrdd -> xapi
- upstream: update ctypes to 0.19.1
- upstream: update python2-7 to 1.2
- upstream: update unix-errno to 0.6.0
- xs-extra: stop using archive/master/master.tar.gz
- xs-extra: update metadata from xen-api repo
- xs-extra: use -j parameter to run tests
- xs: update dlm to 0.3.2
- xs: update xapi-inventory to 1.2.2
- Use the official cache as a mirror
- repo: use local folder as well for cache

* Tue Jan 11 2022 Rob Hoes <rob.hoes@citrix.com> - 6.54.0-5
- Bump release and rebuild

* Mon Dec 06 2021 Rob Hoes <rob.hoes@citrix.com> - 6.54.0-4
- Bump release and rebuild

* Thu Sep 16 2021 Rob Hoes <rob.hoes@citrix.com> - 6.54.0-3
- Bump release and rebuild

* Thu Jun 24 2021 Edwin Török <edvin.torok@citrix.com> - 6.54.0-2
- CP-37034: rebuild with new xenctrl

* Wed Jun 23 2021 Pau Ruiz Safont <pau.safont@citrix.com> - 6.54.0-1
- CP-34942: update lwt to 5.4.1
- CP-34942: update pci to 1.0.4
- CP-34643: Update xapi-stdext packages to 4.18.0
- CP-34942: Update ppxlib to 0.22.1
- xs-extra: add lwt to xapi-idl's dependencies
- CP-36097 REQ-403 patch ocaml-conduit
- maintenance: point to correct versions of conduit

* Thu May 20 2021 Pau Ruiz Safont <pau.safont@citrix.com> - 6.53.0-1
- Update toolstack packages to use nbd-unix
- CP-34942: Update ppxlib and mirage ecosystem
- fix: broken links from inria's gitlab
- Upgrade upstream-extra packages
- upstream-extra: add charInfo_width, required by zed
- CP-34942: Update alcotest packages to 1.3.0
- CP-34942: update asn1-combinators to 0.2.5
- CP-34942: update ocaml-lsp-server to 1.4.1
- CP-34942: update fix to 20201120
- CP-34942: update menhir packages to 20210310
- CP-34942: update csexp to 1.5.1
- CP-34942: Update odoc to 1.5.2
- CP-34942: update sha to 1.14
- CP-34942: update zarith to 1.12
- CP-34942: update conf-libssl and conf-pkg-config packages
- CP-34942: update polly to 0.2.2
- CP-34942: update dune packages to 2.8.5
- CP-34942: update base64 to 3.5.0
- CP-34675: Update default OCaml to 4.10.1
- CP-34942: update dune packages to 2.8.4
- xs-extra: update xapi.master to use external pci library
- xs: update xapi-stdext packages to v4.17.0

* Fri Feb 26 2021 Rob Hoes <rob.hoes@citrix.com> - 6.52.0-2
- Bump release to rebuild

* Mon Feb 15 2021 Christian Lindig <christian.lindig@citrix.com> - 6.52.0-1
- fix: downgrade ocaml-ssl to 0.5.9
- fix: update cstruct-sexp to 5.2.0

* Mon Feb 15 2021 Christian Lindig <christian.lindig@citrix.com> - 6.50.0-1
- CP-34942: update dune packages to 2.8.2
- CP-34942:  update ocaml-compiler-libs to v0.12.3
- CP-34942: update stdlib-shims to 0.3.0
- CP-34942: update integers to 0.4.0
- CP-34942: update ctypes to 0.17.1
- CP-34942: update num to 1.4
- CP-34942: update zarith to 1.11
- CP-34942: update bigstringaf to 0.7.0
- CP-34942: update magic-mime to 1.1.3
- CP-34942: update cppo to 1.6.7
- CP-34942: update ppx_tools to 6.3
- CP-34942: update ocaml-migrate-parsetree to 1.8.0
- CP-34942: update ssl to 0.5.10
- CP-34942: update lwt to 5.4.0
- CP-34942: update ezjsonm to 1.2.0
- CP-34942: update menhir packages to 20201216
- CP-34942: update ounit packages
- CP-34942: update uri packages to 4.1.0
- CP-34942: update conf packages
- CP-34942: update base and core to v0.14.1
- CP-34942: update cstruct packages
- CP-34942: refresh ctypes-foreign
- xs-extra-dummy: add ocaml-lsp-server
- xs-extra-dummy: alcotest-lwt is a build-dep
- upstream-extra: update ocaml-lsp-server to 1.4.0
- upstream-extra: update merlin packages to 3.4.2


* Mon Jan 25 2021 Christian Lindig <christian.lindig@citrix.com> - 6.49.0-1
- tools: include script used for syncing metadata
- maintenance: sync packages from default repo
- CA-350872: update mirage-crypto packages
- fixup! Update xenstore_transport to 1.3.0
- Update xenstore_transport to 1.3.0
- Update xenstore_transport to 1.2.0
- detect failed downloads
- detect failed downloads

* Tue Jan 5 2021 Ben Anson <ben.anson@citrix.com> - 6.48.0-1
-  maintenance: bump stdext

* Thu Dec 17 2020 Pau Ruiz Safont <pau.safont@citrix.com> - 6.47.0-2
- Added BuildRequires for the conf-* packages added in 6.47
- Remove special case to ignore outdated ppx_tools packages, it's not used anymore.

* Wed Dec 16 2020 Christian Lindig <christian.lindig@citrix.com> - 6.47.0.1
- maintenance: use profile release for http-svr
- maintenance: bump stdext
- xs: update xapi-rrd to 1.8.2
- xs: update nbd packages to 4.0.3
- CP-34942: update opam-depext to 1.1.5
- maintenance: cleanup depexts
- xs-extra: update opam metadata
- ci: detect if there are packages with multiple versions
- upstream: add conf-libnl3 and conf-xen

* Tue Dec 01 2020 Christian Lindig <christian.lindig@citrix.com> - 6.46.0-1
- fix: remove ppxlib 0.13.0, since a newer version is available

* Tue Dec 01 2020 Christian Lindig <christian.lindig@citrix.com> - 6.45.0-1
- ci: drop Travis
- ci: enable caching for versioned packages
- ci: fix failure to download apt packages
- ci: refresh opam metadata before trying to upgrade packages
- ci: stop using container-based workflow
- CP-33121: remove stdext package metadata
- CP-34942: update angstrom to 0.15.0 (breaking change)
- CP-34942: Update bisect_ppx to 2.5.0
- CP-34942: update dbuenzli packages
- CP-34942: update dune packages to 2.7.1
- CP-34942: update jst packages
- CP-34942: update lwt to 5.3.0
- CP-34942: update mirage crypto libraries and libssl
- CP-34942: update mirage's addresses library
- CP-34942: update testing frameworks
- maintenance: sync toolstack package metadata
- maintenance: update github actions dependency
- readme: replace ci instructions to use github actions
- tools: change distro variable for travis CI
- upstream-extras: add ocaml-lsp-server package

* Tue Nov 17 2020 Edwin Török <edvin.torok@citrix.com> - 6.44.0-2
- Re-enabled automatic ocaml dependency generator

* Thu Nov 05 2020 Christian Lindig <christian.lindig@citrix.com> - 6.44.0-1
- travis: create switch for 4.10.1
- CP-34675: use ocaml 4.10.1 instead of 4.10.0
- CP-32673: replace ocaml-rrdd-plugin with xcp-rrdd

* Tue Oct 06 2020 Christian Lindig <christian.lindig@citrix.com> - 6.43.0-1
- fix: update cstruct-sexp to 5.1.1

* Mon Oct 05 2020 Christian Lindig <christian.lindig@citrix.com> - 6.42.0-1
- Update rpclib to 8.0.0
- xs-extra: use jobs for compiling and testing when using dune
- Revert "xs-extra: pin fixes for rpclib 7"
- xs-extra: pin fixes for rpclib 7
- Use ocaml 4.10 instead of 4.09 in CI builds
- maintenance: bump ocaml-pci
- Add xs/polly.0.2.0

* Thu Aug 13 2020 Christian Lindig <christian.lindig@citrix.com> - 6.41.0-1
- Update ezxenstore to 0.4.1 for CA-342986

* Tue Aug 11 2020 Christian Lindig <christian.lindig@citrix.com> - 6.40.0-1
- Update stdext to 4.14.0
- Update xapi-stdext to 4.13.0 for time handling
- CP-32672 add dune-build-info lib to xapi-rrdd
- CP-32672 merge rrd-transport into xcp-rrdd
- add dune-build-info

* Tue Jul 28 2020 Christian Lindig <christian.lindig@citrix.com> - 6.39.0-1
- Move uucp to upstream as required by uuseg
- maintenance: drop opasswd

* Mon Jul 27 2020 Christian Lindig <christian.lindig@citrix.com> - 6.38.0-1
-  maintenance: remove unused packages
-  maintenance: refresh packages to run cleanup tool
-  ocamlformat: move dependencies to upstream
-  CP-34356: update xs-extra packages
-  CP-33121: Update stdext modules
-  Update README for Docker use case (#497)
-  Add nolicense target to report pkgs w/o license
-  Add licenses target to report them
-  CP-34439: merge rrd2csv into xen-api
-  maintenance: replace xenops-cli with xapi-xenopsd-cli
-  xs: update xapi-test-utils to 1.4.0
-  ci: do not limit builds to master branch
-  xs-extra: sync metadata with repositories
-  Move ocamlformat to upstream/ so we build it by default
-  maintenance: remove packages hosted in the xenops repository
-  xs-extra: update opam metadata from repositories
-  ci: schedule jobs for stockholm lcm branch

* Mon Jul 13 2020 Christian Lindig <christian.lindig@citrix.com> - 6.37.0-1
- xs: update nbd packages to 4.0.2
- xs: do not specify version in file for crc
- ci: get notified when 4.09 breaks
- upstream: update base metadata ocaml bounds
- maintenance: remove ocaml 4.07.1 from repo
- maintenance: use 4.09.1 instead of 4.09.0
- ci: choose container depending on ocaml version
- updated alcotest to 1.1.0
- maintenance: remove unmaintained xenctrlext library
- ci: enable github action for stockholm branch
- ci: run daily to detect breaking changes in xs-extra
- CI: run only jobs with tests
- ci: remove job for testing all upstream packages
- Travis: use debian-10-ocaml-4.08
- github actions: enable builds on pull requests
- CI: use valid names whe nexporting env vars
- ci: enable github actions on PRs and tags
- CI: ensure env vars are picked correctly on github actions

* Wed Jun 24 2020 Edwin Török <edvin.torok@citrix.com> - 6.36.0-1
- CA-341597: add conf-libev
- maintenance: remove obsolete packages
- upstream opam file and CI changes

* Tue May 12 2020 Christian Lindig <christian.lindig@citrix.com> - 6.35.0-1
- CA-338596: Update xe to use fpath
- CA-338596: Move fpath to upstream
- CP-33121: remove unused stdext-bigbuffer
- Sync opam dependencies for xs-extra/ from source repos
- upstream-extras: add ocamlformat

* Fri Apr 24 2020 Christian Lindig <christian.lindig@citrix.com> - 6.34.0-1
- CA-338243 bump stdext to fix date/time parsing and unparsing

* Mon Apr 20 2020 Christian Lindig <christian.lindig@citrix.com> - 6.32.0-1
- CA-333908 bump stdext with datetime changes
- CP-32669: add randomconv to be able to test mirage-crypto-pk
- CP-32669: update ppx_tools to 6.0
- CP-32669: update mirage-types packages to 3.6.0
- CP-3266: update mirage-protocols packages to 3.1.0
- CP-32669: synchronize ssl package with upstream
- CP-32669: update x509 to 0.11.0
- CP-32669: update menhir to 20200211
- CP-32669: update upstream-extra packages
- CP-32669: update ppx_tools_versioned to 5.3.0
- CP-32669: update base64 to 3.4.0
- CP-21669: update ocaml-migrate-parsetree to 1.7.1
- CP-32669: update bigstringaf to 0.6.1
- CP-32669: update ocplib-endian to 1.1
- CP-32669: update dune packages to 2.5.0
- CP-32669: update sha to use 1.13
- CP-32669: Add pci v1.0.2
- Don't use COPY --chown in Dockerfile

* Fri Apr 03 2020 Christian Lindig <christian.lindig@citrix.com> - 6.31.0-1
- Fix name of dependency

* Fri Apr 03 2020 Christian Lindig <christian.lindig@citrix.com> - 6.30.0-1
- CP-33380: Replace nocypto with mirage-crypto
- CP-32669: update xs-extra opam files

* Thu Apr 02 2020 Christian Lindig <christian.lindig@citrix.com> - 6.29.0-1
- fixup! REQ-811 pin nbd, and all opam files to xs
- REQ-811 pin nbd, and all opam files to xs

* Mon Mar 30 2020 Christian Lindig <christian.lindig@citrix.com> - 6.27.0-1
- CP-33380: Fix installation of dlm with dune 2
- CP-33354 bump xcp-rrd

* Fri Mar 27 2020 Christian Lindig <christian.lindig@citrix.com> - 6.26.0-1
- CP-33380: Update dune to 2.4.0
- travis: follow validator's suggestions
- CP-33380: update message-switch tests for dune 2
- CP-32669: remove craml package
- CP-32669: update travis-opam to 1.5.0
- CP-32669: update qtest to 2.10.1
- CP-32669: Update ppx_deriving_rpc to 6.1.0
- mainteance: update stdext's opam files
- merge rrddump with xcp-rrdd
- Print more log lines on build failures
- maintenance: rrddump pointing at incorrect repo
- maintenance: remove vncproxy
- maintenance: remove lindig from generate-opam-sources.sh
- maintenance: move xenctrl repo to xapi-project
- maintenance: remove rrd.transition

* Tue Mar 10 2020 Christian Lindig <christian.lindig@citrix.com> - 6.25.0-1
- CP-32663: Update x509 to 0.9.0
- CP-27904: nuke sha1 out of orbit
- maintenance: upgrade Dockerfile to ocaml 4.08

* Wed Feb 12 2020 Christian Lindig <christian.lindig@citrix.com> - 6.24.0-1
- maintenance: update metadata for xs-extra packages
- maintenance: update travis build instructions
- CP-29837: use odig so docs can be built
- CP-32669: update ctypes to 0.16.0
- CP-32669: update ounit packages
- CP-32669: update asn1-combinators

* Mon Jan 06 2020 Christian Lindig <christian.lindig@citrix.com> - 6.23.0-1
- CP-32669: update shared-block-ring to 2.5.0
- CP-32669: update ounit to 2.2.1
- CP-32669: update dune 1.11.4
- CP-32669: update xenstore to 2.1.1
- CP-32669: update uri packages to 3.1.0
- CP-32669: update rpclib packages to 6.1.0
- CP-32669: update lwt to 4.5.0
- CP-32669: update duration to 0.1.3
- CP-32669: update cstruct packages to 5.1.1
- CP-32669: update ocaml-migrate-parsetree to 1.5.0
- CP-32669: update num to 1.3
- CP-32669: update octavius to 1.2.2
- CP-32055: update x509 to 0.8.1
- maintenance: update merlin for ocaml 4.08.1
- Use OCaml 4.08 by default, 4.09 speculatively
- wsproxy: fix dependencies, add lwt_log
- Add fmt >= 0.8.8 to xapi-xenopsd

* Wed Dec 04 2019 Christian Lindig <christian.lindig@citrix.com> - 6.22.0-2
- Don't install ppx_tools.5.1 for OCaml 4.08 compatibility

* Mon Nov 04 2019 Pau Ruiz Safont  <pau.safont@citrix.com> - 6.22.0-1
- xs: bump xcp-rrd

* Tue Oct 29 2019 Pau Ruiz Safont <pau.safont@citrix.com> - 6.21.0-1
- xs-extra: sync metadata with repos
- xs: update xapi-rrd to v1.7.0

* Tue Oct 29 2019 Pau Ruiz Safont <pau.safont@citrix.com> - 6.20.0-1
- Add xenops-cli
- CP-32138: Add logs-syslog and dependencies
- upstream-extra: update ocp-indent checksum
- upstream-extra: add crowbar for testing
- xs-extra: sync metadata with repo

* Mon Sep 30 2019 Christian Lindig <christian.lindig@citrix.com> - 6.19.0-1
- CP-32055: Update angstrom to 0.12.1
- CP-32055: remove unused packages
- CP-32055: update diet to 0.4
- CP-32055: update opam-ed
- CP-32055: keep toolstack packages up-to-date
- CP-32055: update ssl to 0.5.9
- CP-32055: update lwt to 4.3.1
- CP-32055: update menhir to 20190924
- CP-32055: update ppxlib to 0.8.1
- CP-32055: update bigstringaf to 0.6.0
- CP-32055: update utop and ocp-indent
- CP-32055: update janestreet packages
- CP-32055: update utop to 2.4.1
- CP-32055: update camomile to 1.0.2
- CP-32055: update lwt_react to 1.1.3

* Fri Sep 06 2019 Christian Lindig <christian.lindig@citrix.com> - 6.18.0-1
- CP-32055: update x509 to 0.7.1
- CP-32055: update zarith to 1.9.1
- CP-32055: update ctypes to 0.15.1

* Tue Sep 03 2019 Christian Lindig <christian.lindig@citrix.com> - 6.16.0-2
- Avoid installing ppx_tools.5.3+4.08.0

* Tue Sep 03 2019 Christian Lindig <christian.lindig@citrix.com> - 6.16.0-1
- add ppx_tools 5.3+4.08.0 for OCaml 4.08
- Add 4.08 for testing
- CP-32055: move core_kernel to correct folder
- CP-32055: update bigstringaf to 0.5.3
- CP-32055: update magic-mime to 1.1.2
- CP-32055: update lwt_ssl to 1.1.3
- CP-32055: update lwt_log to 1.1.1
- CP-32055: update lwt to 4.3.0
- CP-32055: update menhir to 20190626
- CP-32055: update logs to 0.7.0
- CP-32055: update dune to 1.11.3
- CP-32055: update fmt to 0.8.8
- CP-32055: update mirage-protocol packages to 3.0.0
- CP-32055: update mirage-profile to 0.9.1
- CP-32055: update mirage-console packages to 2.4.3
- CP-32055: update cohttp packages to 2.3.0
- CP-32055: update mirage-types packages to 3.5.2
- CP-32055: update mirage-time packages to 1.3.0*

* Fri Aug 23 2019 Edwin Török <edvin.torok@citrix.com> - 6.15.0
- CP-32055: update Jane Street ecosystem to v0.12
- CP-32055: update ppx_deriving to 4.4
- CP-32055: update ppx_tools_versioned to 5.2.3
- CP-32055: update mtime to 1.2.0
- CP-32055: update io-page packages to 2.3.0
- CP-32055: update ocam-migrate-parsetree to 1.4.0
- CP-32055: update ocamlfind to 1.8.1
- CP-32055: update biniou to 1.2.1
- CP-32055: update num to 1.2
- CP-32055: update topkg to 1.0.1
- CP-32055: update bigstringaf to 0.5.2
- CP-32055: update easy-format to 1.3.2
- CP-32055: update cmdliner to 1.0.4
- CP-32055: update dune to 1.11.1

* Wed Aug 07 2019 Christian Lindig <christian.lindig@citrix.com> - 6.14.0-1
- xcp-rrd update to 1.6.0 for CA-322008
- xs-toolstack: remove unused xapi-netdev dependency
- Remove unused xapi-netdev package
- xapi: remove unused netdev dependency
- Add a build that also runs the tests

* Fri Jul 26 2019 Rob Hoes <rob.hoes@citrix.com> - 6.13.0-1
- Fix xapi-test-utils opam dependencies

* Fri Jul 26 2019 Rob Hoes <rob.hoes@citrix.com> - 6.12.0-1
- Update xapi-test-utils to 1.3.0
- Update ezxenstore to 0.4.0
- Drop rpc completely from opam files, do not keep it as optional
- Update http-svr opam file from repo
- update dummy xapi-clusterd to latest
- Do depext after switching to 4.07
- use rpclib instead of rpc when available

* Mon Jul 01 2019 Christian Lindig <christian.lindig@citrix.com> - 6.11.0-1
- Update xenstore_transport to 1.1.0, fixes CA-289145

* Mon Jun 10 2019 Christian Lindig <christian.lindig@citrix.com> - 6.10.0-1
- Remove obsolete packages: cow, omd, caml2html
- CP-30756: update cstruct packages to 5.0.0
- CP-30756: update mirage-block-unix to 2.11.2
- CP-30756: update dune to 1.10.0
- CP-30756: update ppx_tools_versioned to 5.2.2
- CP-30756: update ppxlib to 0.8.0
- CP-30756: update ocaml-migrate-parsetree to 1.3.1
- CP-30756: update result to 1.4
- CP-30756: update uri to 2.2.1
- CP-30756: update ocaml-compiler-libs to v0.12.0
- CP-39756: update stringext to 1.6.0
- CP-30756: update cppo packages to 1.6.6
- CP-30756: update sapwn to v0.13.0
- CP-30756: update xen-gnt packages to 4.0.0

* Wed Jun 05 2019 Christian Lindig <christian.lindig@citrix.com> - 6.9.0-1
- Update stdext to 4.7.0
- CP-30756: Update base64
- maintenance: ignore files created for archiving

* Tue May 28 2019 Christian Lindig <christian.lindig@citrix.com> - 6.8.0-2
- explicitly require xs-opem-src > 5.1.0 which defines _opamroot

* Wed May 22 2019 Christian Lindig <christian.lindig@citrix.com> - 6.8.0-1
- CA-318579 update qmp to 0.18.0 for "query-chardev"
- CP-30756: update tar packages to 1.1.0
- CP-30756: update cstruct packages to 4.0.0
- CP-30756: update nocrypto to follow opam-repository
- xapi-rrd: update to 1.5.0 for CA-315952 XSI-335

* Thu May 16 2019 Christian Lindig <christian.lindig@citrix.com> - 6.7.0-1
- CP-30756: update hex to 1.4.0
- CP-30756: update mirage-console packages to 2.4.2
- CP-30756: upgrade mustache to 3.1.0
- CP-30756: upgrade ppxlib to 0.6.0
- CP-30756: update logs to 0.6.3
- CP-30756: update mirage-flow packages to 1.6.0
- CP-30756: update ezjsonm to 1.1.0
- CP-30756: update ppxfind to 1.3
- CP-30756: update fmt to 0.8.6
- CP-30756: update ptime to 0.8.5
- CP-30756: update io-page packages to 2.2.0
- CP-30756: update octavius to 1.2.1
- CP-30756: update lwt to 4.2.1
- CP-30756: update cpuid to 0.1.2
- CP-30756: update ppx_derivers to 1.2.1
- CP-30756: Update opam-depext to 1.1.3
- CP-30756: Update mmap to 1.1.0
- CP-30756: Update dune to 1.9.3
- CP-30756: Update re to 1.9.0
- rrd-transport: add ezjsonm dependency
- CP-30136: qmp.0.17.0
- Travis: use "opam switch 4.07"
- Travis: use debian-unstable
- xapi.master: add libxxhash-dev external deps
- xapi-test-utils: update to 1.2.0

* Tue Apr 02 2019 Christian Lindig <christian.lindig@citrix.com> - 6.6.0-1
- CA-314001: xapi-stdext 4.6.0
- CP-30756: update mirage-types packages to 3.5.0
- CP-30756: Update ipaddr to 3.1.0 and dependents
- CP-30756: Update mirage-clock packages to 2.0.0
- CP-30756: Update angstrom to 0.11.2
- CP-30756: Update lwt to 4.2.0
- CP-30756: Update bisect_ppx to 1.4.1
- Don't package upstream-extra packages

* Tue Mar 19 2019 Christian Lindig <christian.lindig@citrix.com> - 6.5.0-1
- CP-30756: update mirage-device to 1.2.0
- CP-30756: update mirage-block-unix to 2.11.1
- CP-30756: update xen-gnt packages to 3.1.0
- CP-30756: update mirage-stack packages to 1.4.0
- CP-30756: update vhd-format packages to 0.12.0
- CP-30756: update uuidm to 0.9.7
- CP-30756: update mirage-block packages to 2.4.1
- CP-30756: update mirage-console packages to 2.4.1
- CP-30756: update angstrong to 0.11.1
- CP-30756: Update mirage-channel packages to 3.2.0
- CP-30756: update bigstringaf to 0.5.0
- CP-30756: update xenstore to 2.1.0
- CP-30756: update uutf to 1.0.2
- CP-30756: update uri to 2.2.0
- CP-30756: update tar libraries to 1.0.1
- CP-30756: update ppxlib to 0.5.0
- CP-30756: update cstruct packages to 3.3.0
- CP-30756: Update ocaml-migrate-parsetree to 1.2.0
- CP-30756: update ocamlbuild to 0.14.0
- CP-30756: update magic-mime to 1.1.1
- CP-30756: update ipaddr to 2.9.0
- CP-30756: update io-page to 2.1.0
- CP-30756: update hex to 1.3.0
- CP-30756: update ezjsonm to 1.0.0
- CP-30756: update duration to 0.1.2
- CP-30756: updater alcotest to 0.8.5
- CP-30756: update dune to 1.8.2
- CP-30756: update yojson to 1.7.0
- fix: copy files as user opam instead of root

* Fri Mar 15 2019 Christian Lindig <christian.lindig@citrix.com> - 6.4.0-1
- stdext 4.5.0
- updated opam files in upstream/
- add back patch for CA-297060

* Tue Jan 22 2019 Christian Lindig <christian.lindig@citrix.com> - 6.3.0-1
- Update Dune to 1.6.3
- Use new xenctrl.master for Travis builds

* Tue Jan 15 2019 Christian Lindig <christian.lindig@citrix.com> - 6.2.0-1
- Update nbd to 4.0.0+beta3 for CA-307773

* Thu Jan 10 2019 Konstantina Chremmou <konstantina.chremmou@citrix.com> - 6.1.0-1
- Catch up with upstream, Lwt 4.1
- xenctrl: add missing dependency
- ezxenstore.0.3.0 -> ezxenstore.0.3.1
- fd-send-recv.2.0.0 -> fd-send-recv.2.0.1
- opasswd.1.3.0 -> opasswd.1.3.1
- netlink 0.3.3 -> netlink.0.3.4
- qmp.0.15.0 -> qmp.0.15.1
- rrd.1.4.0 -> rrd.transition and xapi-rrd.1.4.0 -> xapi-rrd.1.4.1
- Some packages ported to Dune

* Fri Dec 14 2018 Christian Lindig <christian.lindig@citrix.com> - 5.7.0-1
- vhd-format.0.10.0 => vhd-format.0.11.0
- cdrom.0.9.3 => cdrom.0.9.4
- dlm.0.3.0 => dlm.0.3.1
- nbd.4.0.0+beta1 => nbd.4.0.0
- rrd.1.3.0 => rrd.1.4.0
- xapi-rrd.1.3.0 => xapi-rrd.1.4.0
- Many packages ported to Dune

* Fri Dec 07 2018 Christian Lindig <christian.lindig@citrix.com> - 5.6.1-2
- Block Opam from downloading packages; only use cache

* Thu Dec 06 2018 Christian Lindig <christian.lindig@citrix.com> - 5.6.1-1
- fix missing cache entries in 5.6.0

* Mon Dec 03 2018 Christian Lindig <christian.lindig@citrix.com> - 5.6.0-1
- Update xapi-inventory to 1.2.1'
- Fix Dockerfile
- Deprecate xcp and xcp-inventory

* Fri Nov 30 2018 Christian Lindig <christian.lindig@citrix.com> - 5.5.0-1
- menhir.20181026 => menhir.20181113
- crc.2.0.0 => crc.2.1.0
- ezxenstore.0.2.0 => ezxenstore.0.3.0
- netlink.0.3.1 => netlink.0.3.2
- mirage-random.1.1.0 => mirage-random.1.2.0
- various small changes

* Thu Nov 15 2018 Jon Ludlam <jonathan.ludlam@citrix.com> - 5.4.0-1
- bigstringaf 0.3.0 -> 0.4.0
- menhir 20181006 -> 20181026
- ppx_bin_prot v0.11.0 -> v0.11.1
- ppx_compare v0.11.0 -> v0.11.1
- ppx_enumerate v0.11.0 -> v0.11.1
- ppx_hash v0.11.0 -> v0.11.1
- ppx_sexp_conv v0.11.1 -> v0.11.2
- ppx_typerep_conv v0.11.0 -> v0.11.1
- ppx_variants_conv v0.11.0 -> v0.11.1
- ppxlib 0.2.2 -> 0.3.1
- rpclib 5.9.0 -> 6.0.0
- qmp 0.14.0 -> 0.15.0
- added utop and its dependencies
- removed ppx_type_conv and ppx_driver

* Mon Nov 05 2018 Christian Lindig <christian.lindig@citrix.com> - 5.3.0-1
- xapi-backtrace: v0.6 -> v0.7
- Sync opam files from component repositories
- Restored mustache in xapi-datamodel deps.
- Ported xen-api-sdk to dune.
- ssl.0.5.6 -> ssl.0.5.7


* Wed Oct 24 2018 Christian Lindig <christian.lindig@citrix.com> - 5.2.0-1
- catching up with upstream libraries: alcotest cohttp cohttp-async
  cohttp-lwt cohttp-lwt-unix conduit conduit-async conduit-lwt
  conduit-lwt-unix dune menhir rresult ssl topkg uri

* Tue Oct 09 2018 Christian Lindig <christian.lindig@citrix.com> - 5.1.0-1
- CA-297060 speculative patch for time functions in Core lib
- make opam depext work again in Travis
- simplify files section

* Tue Oct 02 2018 Christian Lindig <christian.lindig@citrix.com> - 5.0.0-1
- First xs-opam based on Opam 2 tooling
