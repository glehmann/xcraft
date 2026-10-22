%global package_speccommit f40573a915953b3b1a24591e90b15a1bb9a8ff80
%global usver 2.5.2
%global xsver 1
%global xsrel %{xsver}%{?xscount}%{?xshash}

Name:           opam
Version:        2.5.2
Release:        %{?xsrel}.1%{?dist}
Summary:        Source-based OCaml package manager
License:        LGPL-2.1-only WITH OCaml-LGPL-linking-exception
URL:            https://github.com/ocaml/opam
Source0: opam-full-2.5.2.tar.gz
BuildRequires:  make
BuildRequires:  curl
BuildRequires:  git
BuildRequires:  ocaml

%if 0%{?xenserver} < 9
BuildRequires:  devtoolset-11-gcc-c++
Requires:       devtoolset-11-gcc devtoolset-11-binutils
%else
BuildRequires:  gcc-c++
Requires:       gcc binutils
%endif

# Needed to install packages and run opam init.
Requires:       bubblewrap
Requires:       bzip2
Requires:       diffutils
Requires:       gzip
Requires:       make
Requires:       m4
Requires:       patch
Requires:       unzip
Requires:       tar

%description
Opam is a source-based package manager for OCaml. It supports multiple
simultaneous compiler installations, flexible package constraints, and
a Git-friendly development workflow.

%prep
%autosetup -n %{name}-full-%{version} -p1

%build

%if 0%{?xenserver} < 9
source /opt/rh/devtoolset-11/enable
%endif

%configure --with-vendored-deps
make

%install
# The makefile looks like it tries to invoke ocamlfind but only if DESTDIR
# isn't set. If it is set (which it is) LIBINSTALLDIR must be set too
# for installing opam-installer metadata.
%make_install LIBINSTALL_DIR=%{buildroot}/%{_libdir}/ocaml

# However it looks like some (extra) documentation gets
# installed in the wrong place so... delete it.
rm -rf %{buildroot}%{_prefix}/doc

%files
%doc README.md CHANGES AUTHORS CONTRIBUTING.md
%{_bindir}/opam
%{_bindir}/opam-installer
%license LICENSE
%{_mandir}/man1/*.1*

%changelog
* Tue Jun 23 2026 Andrii Sultanov <andriy.sultanov@vates.tech> - 2.5.2-1.1
- Update to 2.5.2

* Fri Nov 07 2025 Pau Ruiz Safont <pau.safont@vates.tech> - 2.4.1-1.1
- Update to 2.4.1

* Wed Aug 9 2023 Lin Liu <Lin.Liu01@cloud.com> - 2.1.4-4
- Use GCC to build opam for next platform

* Fri Jul 21 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 2.1.4-3
- Add dependencies for running opam init
- Use GCC 11 to build opam

* Mon Jul 17 2023 Edwin Török <edwin.torok@cloud.com> - 2.1.4-2
- Bump release and rebuild

* Fri May 05 2023 Pau Ruiz Safont <pau.ruizsafont@cloud.com> - 2.1.4-1
- Update to 2.1.4

* Thu Aug 04 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 2.0.10-5
- Bump release and rebuild

* Tue Feb 15 2022 Rob Hoes <rob.hoes@citrix.com> - 2.0.10-4
- Bump release and rebuild

* Wed Feb 02 2022 Pau Ruiz Safont <pau.safont@citrix.com> - 2.0.10-3
- Update to 2.0.10
- Fix packaging issues around versions
- Use correct SPDX identifier for the license

* Fri Feb 26 2021 Rob Hoes <rob.hoes@citrix.com> - 2.0.7-2
- Bump release to rebuild

* Fri Nov 20 2020 Pau Ruiz Safont <pau.safont@citrix.com> - 2.0.7-1
- Update to 2.0.7, include some comments from the fedora package

* Thu Nov 19 2020 Rob Hoes <rob.hoes@citrix.com> - 2.0.5-3
- Revision bump for koji migration

* Tue Jan 14 2020 Tim Smith <tim.smith@citrix.com> - 2.0.5-2
- Rebuild for ocaml-4.08

* Wed Dec 04 2019 Christian Lindig <christian.lindig@citrix.com> - 2.0.5-1
- update for compatibility with OCaml 4.08

* Wed Oct 03 2018 Christian Lindig <christian.lindig@citrix.com> - 2.0.0-2
- update License, use mirror for Source0

* Fri Sep 28 2018 Christian Lindig <christian.lindig@citrix.com> - 2.0.0-1
- First packaging of Opam 2

* Tue Sep 04 2018 Christian Lindig <christian.lindig@citrix.com> - 1.2.2-4
- Add patch to compile with -unsafe-string

* Tue Oct 3 2017 Edwin Török <edvin.torok@citrix.com> - 1.2.2-3
- CP-24976: Apply upstream patch to fix jbuilder install location of C stubs

* Mon Feb 27 2017 Christian Lindig <christian.lindig@citrix.com> - * 1.2.2-2
- Download sources from Artifactory

* Thu Dec 8 2016 Jon Ludlam <jonathan.ludlam@citrix.com> - 1.2.2-1
- Update to 1.2.2

* Fri Dec 26 2014 David Scott <dave.scott@citrix.com> - 1.2.0-1
- Update to 1.2.0

* Thu Oct 02 2014 Jon Ludlam <jonathan.ludlam@citrix.com> - 1.1.2-2
- Force a rebuild

* Fri Aug 01 2014 Euan Harris <euan.harris@citrix.com> - 1.1.2-1
- Initial package
