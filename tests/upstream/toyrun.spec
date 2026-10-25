# an interpreter, whose path moves to the subpackage toyrun-bin in version 2
Name: toyrun
Version: %{toyver}
Release: 1
Summary: An interpreter of the tests
License: MIT
BuildArch: noarch

%description
%{summary}.

%if %{toyver} >= 2
%package bin
Summary: The interpreter of toyrun
Requires: toyrun = %{version}-%{release}

%description bin
%{summary}.
%endif

%install
mkdir -p %{buildroot}/usr/bin %{buildroot}/usr/share/toyrun
printf '#!/bin/sh\nexec sh "$@"\n' > %{buildroot}/usr/bin/toyrun
chmod 755 %{buildroot}/usr/bin/toyrun
echo %{version} > %{buildroot}/usr/share/toyrun/version

%files
/usr/share/toyrun
%if %{toyver} < 2
/usr/bin/toyrun
%else

%files bin
/usr/bin/toyrun
%endif
