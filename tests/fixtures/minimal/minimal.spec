Name: minimal
Version: 0.1.0
Release: 1%{?dist}
Summary: A minimal test package
License: MIT
Source0: minimal-0.1.0.tar.gz

BuildRequires: gcc
Requires: glibc

%description
A minimal package for testing the build pipeline.

%prep
%setup -q

%build
echo build > /dev/null

%install
mkdir -p %{buildroot}/usr/bin
echo "hello" > %{buildroot}/usr/bin/minimal

%files
/usr/bin/minimal

%changelog
* Mon Jul 10 2026 Test User <test@example.com> - 0.1.0-1
- Initial package
