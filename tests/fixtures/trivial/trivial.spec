Name: trivial
Version: 1.0
Release: 1%{?dist}
Summary: Trivial test package
License: MIT
BuildArch: noarch

%description
Trivial package for functional testing of the build pipeline.

%prep
rm -rf %{name}-%{version}
mkdir %{name}-%{version}

%build
cd %{name}-%{version}
echo "hello" > hello.txt

%install
mkdir -p %{buildroot}/usr/share/%{name}
cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/

%files
/usr/share/%{name}/hello.txt

%changelog
* Mon Jul 10 2026 Test User <test@example.com> - 1.0-1
- Initial package
