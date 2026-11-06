# a package of several architectures in the repositories of x86_64, x86_64 and i686, like the multilib ones
%global debug_package %{nil}
Name: toymulti
Version: %{toyver}
Release: 1
Summary: A package of the tests
License: MIT

%description
%{summary}.

%install
mkdir -p %{buildroot}/usr/share/toymulti
echo %{_target_cpu} > %{buildroot}/usr/share/toymulti/arch

%files
/usr/share/toymulti
