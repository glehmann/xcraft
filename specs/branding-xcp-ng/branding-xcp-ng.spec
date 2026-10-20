Name:           branding-xcp-ng
Version:        8.99.0
# When increasing the first number, also update the hash if needed
Release:        2.219ab5b%{?autorev}
Summary:        XCP-ng branding
License:        ISC
URL:            https://github.com/xcp-ng/branding-xcp-ng

# As we don't tag for each change, because we want the release number to remain fixed for a given XCP-ng release,
# archives are exported this way from the source repository:
# export VER=8.3.0; git archive --format tgz master . --prefix branding-xcp-ng-$VER/ -o /path/to/SOURCES/branding-xcp-ng-$VER.tar.gz
# Document the git hash of the commit corresponding to the tarball in the Release tag. Example: 2.d5ffe4d.
Source0:        https://github.com/xcp-ng/branding-xcp-ng/archive/v%{version}/branding-xcp-ng-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3

BuildRequires:  python3-devel
# Make brp-python-bytecompile use python3
%define __python /usr/bin/python3

%description
This package contains branding information for XCP-ng.

%prep
%autosetup -p1

%build

%install
%{__install} -D -m 0644 branding/branding            %{buildroot}%{_usrsrc}/branding/branding
%{__install} -D -m 0755 branding/brand-directory.py  %{buildroot}%{_usrsrc}/branding/brand-directory.py
%{__install} -D -m 0755 branding/branding-compile.py %{buildroot}%{_usrsrc}/branding/branding-compile.py
%{__install} -D -m 0644 branding/EULA                %{buildroot}%{_usrsrc}/branding/EULA
%{__install} -D -m 0644 branding/LICENSES            %{buildroot}%{_usrsrc}/branding/LICENSES

%files
%{_usrsrc}/branding/branding
%{_usrsrc}/branding/brand-directory.py
%{_usrsrc}/branding/branding-compile.py
%{_usrsrc}/branding/EULA
%{_usrsrc}/branding/LICENSES

%changelog
* Fri Sep 18 2026 Yann Dirson <yann.dirson@vates.tech> - 8.99.0-2.219ab5b
- Append autorev macro to Release

* Thu Mar 12 2026 Yann Dirson <yann.dirson@vates.tech> - 8.99.0-1.219ab5b
- Official 8.99.0 release to prepare for XCP-ng v9
- Don't ship __pycache__ any more

* Wed Apr 09 2025 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-5.74d9cc1
- COPYRIGHT_YEARS up to 2025
- Improved EULA text (formatting, wording, URLs)

* Wed Jun 12 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-4.2637627
- COPYRIGHT_YEARS up to 2024

* Wed Sep 20 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-3.5930aef
- Update PLATFORM_VERSION to 3.4.0

* Thu Feb 02 2023 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-2.73c8834
- Switch to python3
- COMPANY_NAME update to "Vates, XenServer and others"
- COPYRIGHT_YEARS up to 2023

* Thu Sep 01 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-1
- Update for XCP-ng 8.3.0

* Tue Feb 15 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-2
- Update year to 2022

* Tue Jan 04 2021 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-1
- Update for XCP-ng 8.2.1

* Tue Jun 30 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-1
- Update for XCP-ng 8.2.0

* Fri Dec 20 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.1.0-1
- Update for XCP-ng 8.1.0
- Add EULA and LICENSES files

* Thu Apr 25 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-2
- Update for XCP-ng 8.0.0
- Clean spec file: remove Group and BuildRoot definitions
- Remove 'dist' macro since it's built before xcp-ng-release

* Tue Dec 25 2018 Robin Lee <cheeselee@fedoraproject.org> - 7.6.0-2
- Change to noarch

* Wed Sep 12 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 7.6.0-1
- Update for XCP-ng 7.6.0

* Thu Jun 21 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 1.0.1-1
- Update for XCP-ng 7.5.0

* Sun Apr 29 2018 John Else <john.else@gmail.com> - 1.0.0-1
- Initial package
