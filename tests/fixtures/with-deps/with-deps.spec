Name: with-deps
Version: 1.0
Release: 1%{?dist}
Summary: Package with a dependency
License: MIT
BuildArch: noarch
BuildRequires: trivial

%description
Package that depends on trivial for testing dependency resolution.

%prep
rm -rf %{name}-%{version}
mkdir %{name}-%{version}

%build
cd %{name}-%{version}
echo "with-deps" > with-deps.txt

%install
mkdir -p %{buildroot}/usr/share/%{name}
cp %{name}-%{version}/with-deps.txt %{buildroot}/usr/share/%{name}/

%files
/usr/share/%{name}/with-deps.txt

%changelog
* Mon Jul 10 2026 Test User <test@example.com> - 1.0-1
- Initial package
