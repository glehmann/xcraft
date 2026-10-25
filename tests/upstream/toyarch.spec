# a package of an architecture, whose version 2, with the new subpackage toyarch-new, is for x86_64 only
%global debug_package %{nil}
Name: toyarch
Version: %{toyver}
Release: 1
Summary: A package of the tests
License: MIT

%description
%{summary}.

%if %{toyver} >= 2
%package new
Summary: A subpackage of toyarch 2

%description new
%{summary}.
%endif

%install
mkdir -p %{buildroot}/usr/share/toyarch
echo %{version} > %{buildroot}/usr/share/toyarch/version

%files
/usr/share/toyarch
%if %{toyver} >= 2

%files new
%endif
