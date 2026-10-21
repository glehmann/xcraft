# The headers of the kernel's API, apart from the kernel, which all the packages are built with: they do not change
# with its patches, none of which changes the API, nor with its release. The version and the archive are the kernel's,
# whose build requires this version.
%global debug_package %{nil}

Name: kernel-headers
Version: 6.12.0
Release: 1%{dist}
License: GPLv2
Summary: Header files for the Linux kernel for use by glibc
URL: https://www.kernel.org/
Source0: linux-%{version}.tar.xz
BuildRequires: gcc
BuildRequires: make
BuildRequires: rsync
Obsoletes: glibc-kernheaders < 3.0-46
Provides: glibc-kernheaders = 3.0-46
Conflicts: kernel-headers < %{version}
# for the kernel's build, which requires these headers, not Alma's of the same version
Provides: kernel-headers-xcpng = %{version}

%description
Kernel-headers includes the C header files that specify the interface
between the Linux kernel and userspace libraries and programs.  The
header files define structures and constants that are needed for
building most standard programs and are also needed for rebuilding the
glibc package.

%prep
%autosetup -n linux-%{version}

%build

%install
make ARCH=x86 INSTALL_HDR_PATH=%{buildroot}/usr headers_install

%files
/usr/include/*

%changelog
* Wed Oct 21 2026 Gaëtan Lehmann <gaetan.lehmann@vates.tech> - 6.12.0-1
- The headers of the kernel, apart from it
