# XCP-ng: TO BE UPDATED FOR EACH NEW RELEASE
# TODO: use data from branding file instead
%define PRODUCT_VERSION 8.99.0
%define PRODUCT_VERSION_TEXT 8.99
%define PRODUCT_VERSION_TEXT_SHORT %{PRODUCT_VERSION_TEXT}
%define PLATFORM_VERSION 3.4.0
%define BUILD_NUMBER 8.99.0
%define xcpng_major 9
# XCP-ng: macro tested by some spec files inherited from XenServer
%define xenserver_major 9

# This package is special since the package version needs to
# match the product version. When making a change to the source
# repo, only the release should be changed, not the version.

%define debug_package %{nil}
%define base_release_version 10
%define upstream_rel_long 10.0-32.el10
%define upstream_rel 10.0-32.el10

%define replace_spaces() %(echo -n "%1" | sed 's/ /_/g')

%define _unitdir /usr/lib/systemd/system

Name:           xcp-ng-release
Version:        8.99.0
Release:        3%{?autorev}
Summary:        XCP-ng release file
Group:          System Environment/Base
License:        GPLv2
# XCP-ng: fixed from post to posttrans
Requires(posttrans): coreutils, grep
# XCP-ng: do not require system-config for now:
# it pulls a useless empty xenserver-config everywhere
#Requires:       system-config
Provides:       almalinux-release = %{upstream_rel_long}
Provides:       almalinux-release(x86-64) = %{upstream_rel_long}
Provides:       centos-release = %{base_release_version}
Provides:       redhat-release = %{upstream_rel_long}
Provides:       system-release = %{upstream_rel_long}
Provides:       system-release(releasever) = %{base_release_version}
Obsoletes:      almalinux-release
Obsoletes:      centos-release

# Metadata for the installer to consume
Provides:       product-brand = XCP-ng
Provides:       product-version = %{PRODUCT_VERSION}
Provides:       product-build = 0x
Provides:       platform-name = XCP
Provides:       platform-version = %{PLATFORM_VERSION}
Provides:       product-version-text = %replace_spaces %{PRODUCT_VERSION_TEXT}
Provides:       product-version-text-short = %replace_spaces %{PRODUCT_VERSION_TEXT_SHORT}

BuildRequires:  systemd
BuildRequires:  branding-xcp-ng
BuildRequires:  python3-devel
# XCP-ng: python dependencies for building branding files
BuildRequires:  python3-rpm-macros
URL:            https://github.com/xcp-ng/xcp-ng-release
# Before the tag of the final release, archives are exported this way from the source repository:
# export VER=8.99.0; git archive --format tgz master . --prefix xcp-ng-release-$VER/ -o /path/to/SOURCES/xcp-ng-release-$VER.tar.gz
Source0:        https://github.com/xcp-ng/xcp-ng-release/archive/v%{version}/xcp-ng-release-8.3.0.tar.gz

Source700:      macros.x86_64_v2

# XCP-ng Patches generated during maintenance period with `git format-patch --no-numbered --no-signature v8.3.0`
Patch1001: 0001-fix-curl-resolve-TLS-issue-caused-by-restrictive-con.patch
Patch1002: 0002-Sync-vm.slice-with-xenserver-release-v8.4.0-12.tar.g.patch
Patch1003: 0003-Sync-systemd-presets-with-xenserver-release-v8.4.0-1.patch
Patch1004: 0004-fix-prompt-s-c-e-p-xcp-ng-prompt.sh-Fix-prompt-on-te.patch
Patch1005: 0005-s-c-e-profile.d-history.sh-Add-timestamps-to-history.patch
Patch1006: 0006-fix-prompt-s-c-e-p-xcp-ng-prompt.sh-Align-to-RPM-pat.patch

%description
XCP-ng release files

%package        presets
Summary:        XCP-ng presets file
Group:          System Environment/Base
Provides:       xs-presets = 1.4
Requires(posttrans): systemd

%description    presets
XCP-ng presets file.

%package        config
Summary:        XCP-ng configuration
Group:          System Environment/Base
Requires:       grep sed coreutils patch systemd
# XCP-ng: no support for live patching yet
#Requires:       kernel-livepatch xen-livepatch

# XCP-ng: don't require xenserver-config-packages
### This gets xenserver-config-packages included only in a real install, because
### xenserver-release-config is only included in real installs
#Requires:	xenserver-config-packages
# XCP-ng v9: this causes a loop at runtime level
#Requires:	python3-xcp-libs
Requires(post): systemd xs-presets >= 1.4
Requires(preun): systemd xs-presets >= 1.4
Requires(postun): systemd xs-presets >= 1.4
Requires(post): sed

%description    config
Additional utilities and configuration for XCP-ng.


%prep
%autosetup -p1 -n %{name}-8.3.0
# XCP-ng: files XS9 moves to xapi.spec
rm src/common/etc/systemd/system/xapi.service.d/local.conf
rm src/common/etc/systemd/system/xenopsd-xc.service.d/local.conf
rmdir src/common/etc/systemd/system/xapi.service.d/
rmdir src/common/etc/systemd/system/xenopsd-xc.service.d/

# XCP-ng: copy LICENSES from branding package
cp %{_usrsrc}/branding/LICENSES .

%build

%install
rm -rf %{buildroot}

%{_usrsrc}/branding/brand-directory.py /usr/src/branding/branding src/common %{buildroot}
%{_usrsrc}/branding/brand-directory.py /usr/src/branding/branding src/xenserver %{buildroot}

install -d -m 755 %{buildroot}%{python3_sitelib}/xcp
%{_usrsrc}/branding/branding-compile.py --format=python > %{buildroot}%{python3_sitelib}/xcp/branding.py

install -m 644 %{_usrsrc}/branding/EULA %{buildroot}/

# create /etc/system-release and /etc/redhat-release
ln -s centos-release %{buildroot}%{_sysconfdir}/system-release
ln -s centos-release %{buildroot}%{_sysconfdir}/redhat-release

# create /etc/issue from /etc/issue.net
cp %{buildroot}%{_sysconfdir}/issue.net %{buildroot}%{_sysconfdir}/issue
echo >> %{buildroot}%{_sysconfdir}/issue
touch -r %{buildroot}%{_sysconfdir}/issue.net %{buildroot}%{_sysconfdir}/issue

# set up the dist tag macros
install -d -m 755 %{buildroot}%{_sysconfdir}/rpm
cat >> %{buildroot}%{_sysconfdir}/rpm/macros.dist << EOF
# dist macros.

%%centos_ver %{base_release_version}
%%centos %{base_release_version}
%%rhel %{base_release_version}
%%almalinux_ver %{base_release_version}
%%almalinux %{base_release_version}
%%dist %%{?autorev}.xcpng%{PRODUCT_VERSION_TEXT_SHORT}
%%el%{base_release_version} 1
%%xenserver %{xenserver_major}
%%xcpng %{xcpng_major}
EOF

# These variables should be set in the build environment to change rpm names
mkdir -p %{buildroot}%{_sysconfdir}/rpm
%ifarch x86_64_v2
install -p -m 0644 %{SOURCE700}  %{buildroot}%{_sysconfdir}/rpm/
%endif

# use unbranded datadir
install -d -m 755 %{buildroot}/%{_datadir}/centos-release
ln -s centos-release %{buildroot}/%{_datadir}/redhat-release

# use unbranded docdir
install -d -m 755 %{buildroot}/%{_docdir}/centos-release
ln -s centos-release %{buildroot}/%{_docdir}/redhat-release

# Prevent spawning gettys on tty1 and tty2
mkdir -p %{buildroot}%{_sysconfdir}/systemd/system
ln -s /dev/null %{buildroot}%{_sysconfdir}/systemd/system/getty@tty1.service
ln -s /dev/null %{buildroot}%{_sysconfdir}/systemd/system/getty@tty2.service
ln -s /dev/null %{buildroot}%{_sysconfdir}/systemd/system/autovt@tty1.service
ln -s /dev/null %{buildroot}%{_sysconfdir}/systemd/system/autovt@tty2.service

# move from yum to dnf
mv %{buildroot}%{_sysconfdir}/yum %{buildroot}%{_sysconfdir}/dnf

# HACK avoid overriding system setting with a broken one
sed -i '/^kernel.core_pattern=/ s/^/#/' %{buildroot}%{_sysconfdir}/sysctl.d/90-dom0.conf

# Don't install rsyslog config
rm -r %{buildroot}%{_sysconfdir}/rsyslog.d/

%posttrans
# XCP-ng 8.1: running this in posttrans instead of post because xcp-ng-release may be installed after
# coreutils, since they both require each other: no guaranteed order
# XCP-ng 8.2: CH 8.2 switched to posttrans too. I'm keeping my previous comment to document why.
/usr/bin/uname -m | grep -q 'x86_64'  && echo 'centos' >/etc/dnf/vars/contentdir || echo 'altarch' > /etc/dnf/vars/contentdir

%clean
rm -rf %{buildroot}

# %triggerin config -- rsyslog
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /etc/rsyslog.conf    2014-11-12 13:55:42.000000000 +0000
# +++ /etc/rsyslog.conf    2014-11-12 13:56:01.000000000 +0000
# @@ -7,8 +7,8 @@

#  # The imjournal module bellow is now used as a message source instead of imuxsock.
#  $ModLoad imuxsock # provides support for local system logging (e.g. via logger command)
# -$ModLoad imjournal # provides access to the systemd journal
# -#$ModLoad imklog # reads kernel messages (the same are read from journald)
# +#$ModLoad imjournal # provides access to the systemd journal
# +$ModLoad imklog # reads kernel messages (the same are read from journald)
#  #$ModLoad immark  # provides --MARK-- message capability

#  # Provides UDP syslog reception
# @@ -37,10 +37,10 @@

#  # Turn off message reception via local log socket;
#  # local messages are retrieved through imjournal now.
# -$OmitLocalLogging on
# +#$OmitLocalLogging on

#  # File to store the position in the journal
# -$IMJournalStateFile imjournal.state
# +#$IMJournalStateFile imjournal.state


#  #### RULES ####
# EOF

# %triggerin config -- setup
# # Replace /etc/motd with our branded version
# install -D -m 644 %{_sysconfdir}/motd.xs %{_sysconfdir}/motd

# %triggerin config -- iscsi-initiator-utils
# /usr/bin/systemctl -q disable iscsi.service
# /usr/bin/systemctl -q disable iscsid.socket

# %triggerin config -- iscsi-initiator-utils-iscsiuio
# /usr/bin/systemctl -q disable iscsiuio.socket

# %triggerin config -- xcp-networkd
# /sbin/chkconfig network off

# %triggerin config -- lvm2
# /usr/bin/systemctl -q mask lvm2-activation.service
# /usr/bin/systemctl -q mask lvm2-activation-early.service
# /usr/bin/systemctl -q mask lvm2-activation-net.service
# /usr/bin/systemctl -q disable lvm2-monitor.service

# %triggerin config -- device-mapper-event
# /usr/bin/systemctl -q disable dm-event.socket

# # default firewall rules, to be replaced by dynamic rule addition/removal
# # /!\ XCP-ng: if the following is updated, make sure to also adapt the rules
# # in the netdata package, because they depend on this
# %triggerin config -- iptables-services
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /etc/sysconfig/iptables    2014-06-10 06:02:35.000000000 +0100
# +++ /etc/sysconfig/iptables    2015-05-15 11:24:23.712024801 +0100
# @@ -5,10 +5,21 @@
#  :INPUT ACCEPT [0:0]
#  :FORWARD ACCEPT [0:0]
#  :OUTPUT ACCEPT [0:0]
# --A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
# --A INPUT -p icmp -j ACCEPT
# --A INPUT -i lo -j ACCEPT
# --A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
# --A INPUT -j REJECT --reject-with icmp-host-prohibited
# --A FORWARD -j REJECT --reject-with icmp-host-prohibited
# +:RH-Firewall-1-INPUT - [0:0]
# +-A INPUT -j RH-Firewall-1-INPUT
# +-A FORWARD -j RH-Firewall-1-INPUT
# +-A RH-Firewall-1-INPUT -i lo -j ACCEPT
# +-A RH-Firewall-1-INPUT -p icmp --icmp-type any -j ACCEPT
# +# DHCP for host internal networks (CA-6996)
# +-A RH-Firewall-1-INPUT -p udp -m udp --dport 67 --in-interface xenapi -j ACCEPT
# +-A RH-Firewall-1-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# +# Linux HA hearbeat (CA-9394)
# +-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m udp -p udp --dport 694 -j ACCEPT
# +-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m tcp -p tcp --dport 22 -j ACCEPT
# +-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m tcp -p tcp --dport 80 -j ACCEPT
# +-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m tcp -p tcp --dport 443 -j ACCEPT
# +# dlm
# +-A RH-Firewall-1-INPUT -p tcp -m tcp --dport 21064 -j ACCEPT
# +-A RH-Firewall-1-INPUT -p udp -m multiport --dports 5404,5405 -j ACCEPT
# +-A RH-Firewall-1-INPUT -j REJECT --reject-with icmp-host-prohibited
#  COMMIT
# EOF
# # XCP-ng: disabled for now. Will enable the change if/when we add the feature.
# #grep -q '^-A .* 5666 .*' /etc/sysconfig/iptables || sed -i '/^-A .* 443 .*/a # nrpe\n-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m tcp -p tcp --dport 5666 -j ACCEPT' /etc/sysconfig/iptables
# #grep -q '^-A .* 161 .*' /etc/sysconfig/iptables || sed -i '/^-A .* 443 .*/a # snmp\n-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m udp -p udp --dport 161 -j ACCEPT' /etc/sysconfig/iptables
# #systemctl try-restart iptables
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /etc/sysconfig/ip6tables    2014-06-10 06:02:35.000000000 +0100
# +++ /etc/sysconfig/ip6tables    2015-05-15 11:25:34.416370193 +0100
# @@ -5,11 +5,21 @@
#  :INPUT ACCEPT [0:0]
#  :FORWARD ACCEPT [0:0]
#  :OUTPUT ACCEPT [0:0]
# --A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
# --A INPUT -p ipv6-icmp -j ACCEPT
# --A INPUT -i lo -j ACCEPT
# --A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
# --A INPUT -d fe80::/64 -p udp -m udp --dport 546 -m state --state NEW -j ACCEPT
# --A INPUT -j REJECT --reject-with icmp6-adm-prohibited
# --A FORWARD -j REJECT --reject-with icmp6-adm-prohibited
# +:RH-Firewall-1-INPUT - [0:0]
# +-A INPUT -j RH-Firewall-1-INPUT
# +-A FORWARD -j RH-Firewall-1-INPUT
# +-A RH-Firewall-1-INPUT -i lo -j ACCEPT
# +-A RH-Firewall-1-INPUT -p icmpv6 -j ACCEPT
# +# DHCP for host internal networks (CA-6996)
# +-A RH-Firewall-1-INPUT -p udp -m udp --dport 67 --in-interface xenapi -j ACCEPT
# +-A RH-Firewall-1-INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
# +# Linux HA hearbeat (CA-9394)
# +-A RH-Firewall-1-INPUT -m state --state NEW -m udp -p udp --dport 694 -j ACCEPT
# +-A RH-Firewall-1-INPUT -m state --state NEW -m tcp -p tcp --dport 22 -j ACCEPT
# +-A RH-Firewall-1-INPUT -m state --state NEW -m tcp -p tcp --dport 80 -j ACCEPT
# +-A RH-Firewall-1-INPUT -m state --state NEW -m tcp -p tcp --dport 443 -j ACCEPT
# +# dlm
# +-A RH-Firewall-1-INPUT -p tcp -m tcp --dport 21064 -j ACCEPT
# +-A RH-Firewall-1-INPUT -p udp -m multiport --dports 5404,5405 -j ACCEPT
# +-A RH-Firewall-1-INPUT -j REJECT --reject-with icmp6-port-unreachable
#  COMMIT
# EOF
# # XCP-ng: disabled for now, will enable this change if/when we add the feature.
# #grep -q '^-A .* 5666 .*' /etc/sysconfig/ip6tables || sed -i '/^-A .* 443 .*/a # nrpe\n-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m tcp -p tcp --dport 5666 -j ACCEPT' /etc/sysconfig/ip6tables
# #grep -q '^-A .* 161 .*' /etc/sysconfig/ip6tables || sed -i '/^-A .* 443 .*/a # snmp\n-A RH-Firewall-1-INPUT -m conntrack --ctstate NEW -m udp -p udp --dport 161 -j ACCEPT' /etc/sysconfig/ip6tables

# # CA-38350
# %triggerin config -- dhclient
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /usr/sbin/dhclient-script    2015-11-19 21:28:36.000000000 +0000
# +++ /usr/sbin/dhclient-script    2016-01-22 17:15:09.000000000 +0000
# @@ -790,6 +790,8 @@
#              fi

#              flush_dev ${interface}
# +            # Remove IP address but leave up so that subsequent DHCPDISCOVERs are not dropped
# +            ip link set $interface up
#              exit_with_hooks 1
#          else
#              exit_with_hooks 1
# EOF

# %triggerin config -- smartmontools
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /etc/smartmontools/smartd.conf.orig    2015-09-24 09:13:05.000000000 +0100
# +++ /etc/smartmontools/smartd.conf    2015-09-24 09:12:19.000000000 +0100
# @@ -20,7 +20,7 @@
#  # Directives listed below, which will be applied to all devices that
#  # are found.  Most users should comment out DEVICESCAN and explicitly
#  # list the devices that they wish to monitor.
# -DEVICESCAN -H -m root -M exec /usr/libexec/smartmontools/smartdnotify -n standby,10,q
# +DEVICESCAN -H -n standby,10,q

#  # Alternative setting to ignore temperature and power-on hours reports
#  # in syslog.
# EOF

# %triggerin config -- tzdata
# ln -f %{_datadir}/zoneinfo/Asia/Shanghai %{_datadir}/zoneinfo/Asia/Beijing

# %triggerin config -- shadow-utils
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /etc/login.defs.orig    2016-02-26 11:11:20.000000000 +0000
# +++ /etc/login.defs 2016-02-26 11:11:57.000000000 +0000
# @@ -70,3 +70,4 @@
#  # Use SHA512 to encrypt password.
#  ENCRYPT_METHOD SHA512

# +MOTD_FILE /etc/motd.xs
# EOF

# %triggerin config -- yum
# ( patch -tsN -r - -d / -p1 || : ) >/dev/null <<'EOF'
# --- /etc/yum.conf.orig  2016-11-07 09:39:42.594325044 +0000
# +++ /etc/yum.conf   2016-11-07 09:39:58.478743042 +0000
# @@ -7,6 +7,7 @@
#  obsoletes=1
#  gpgcheck=1
#  plugins=1
# +installonlypkgs=
#  installonly_limit=5
#  bugtracker_url=http://bugs.centos.org/set_project.php?project_id=23&ref=http://bugs.centos.org/bug_report_page.php?category=yum
#  distroverpkg=centos-release
# EOF

# # XCP-ng: exclude fallback mirror from fastestmirror
# %triggerin config -- yum-plugin-fastestmirror
# FM_PATCH=$(cat <<'EOF'
# --- /etc/yum/pluginconf.d/fastestmirror.conf.orig     2019-07-10 18:51:55.253695553 +0200
# +++ /etc/yum/pluginconf.d/fastestmirror.conf  2019-07-10 18:52:57.774059664 +0200
# @@ -10,3 +10,6 @@
#  maxthreads=15
#  #exclude=.gov, facebook
#  #include_only=.nl,.de,.uk,.ie
# +
# +# Exclude fallback mirror so that it's used as last resort
# +exclude=updates.xcp-ng.org
# EOF
# )
# # Do not apply patch if it was already applied
# if ! echo "$FM_PATCH" | patch --dry-run -RsN -d / -p1 >/dev/null; then
#     # Apply patch. Output NOT redirected to /dev/null so that error messages are displayed
#     if ! echo "$FM_PATCH" | patch -tsN -r - -d / -p1; then
#         echo "Error: failed to apply patch:"
#         echo "$FM_PATCH"
#     fi
# fi

# # XCP-ng: chrony
# %triggerin config -- chrony
# if [ -f /etc/systemd/system/chrony-wait.service ]; then
#     # Since 8.2.1: remove our overriding service file
#     # Now chrony-wait's timeout is overriden through an override.conf
#     rm -f /etc/systemd/system/chrony-wait.service
# fi

# # XCP-ng 8.2.1: we switched back to using the official service file
# # instead of our replacement file in /etc/systemd/system/chrony-wait.service,
# # as we now use an override.conf to set the timeout to 120s.
# # In order to make the /etc/systemd/system/multi-user.target.wants/chrony-wait.service
# # symlink point to the right target (the official service /usr/lib/systemd/system/chrony-wait.service),
# # we disable then reenable the service.
# # TODO: remove in a future major release when update using yum from 8.2 or lower is not supported anymore.
# #       (but still enable the services if they need to be, or fix the preset file)
# systemctl disable chrony-wait >/dev/null 2>&1 || :
# systemctl enable chrony-wait >/dev/null 2>&1 || :


# # Hide previous 8.0 hotfixes from xapi
# %triggerun config -- %{name}-config = 8.0.0, %{name}-config = 8.1.0
# if [ -d /var/update/applied ]; then
#     shopt -s nullglob
#     for sfile in /var/update/applied/*; do
#         label=$(xmllint --xpath "string(//update/@name-label)" $sfile)
#         if [[ "$label" =~ ^XS8[01](E[0-9]{3}$|$) ]]; then
#             rm -f $sfile
#         fi
#     done
# fi

%post config
%systemd_post move-kernel-messages.service
%systemd_post update-issue.service
%systemd_post xs-fcoe.service
%systemd_post vm.slice

# XCP-ng: we update the BUILD_NUMBER here because in XS world
# it is updated separately by a script in the update-XSxx package.
# 
# Also, we update the file each time the package is installed, not just for upgrades, because in the past we renamed the package and then this wasn't run.
# Theoretically we shouldn't need that anymore, but it's not doing any harm
# and who knows, maybe we'll change the name again in the future.
if [ -f %{_sysconfdir}/xensource-inventory ]; then
    sed -i \
        -e "s@^\(PRODUCT_VERSION=\).*@\1'%{PRODUCT_VERSION}'@" \
        -e "s@^\(PLATFORM_VERSION=\).*@\1'%{PLATFORM_VERSION}'@" \
        -e "s@^\(PRODUCT_VERSION_TEXT=\).*@\1'%{PRODUCT_VERSION_TEXT}'@" \
        -e "s@^\(PRODUCT_VERSION_TEXT_SHORT=\).*@\1'%{PRODUCT_VERSION_TEXT_SHORT}'@" \
        -e "s@^\(BUILD_NUMBER=\).*@\1'%{BUILD_NUMBER}'@" \
        %{_sysconfdir}/xensource-inventory
    /sbin/update-issue || :
fi

# Add myhostname to the hosts line of nsswitch.conf if it is not there already.
# This needs to be kept until the next upgrade-only release after 8.0.
grep -q '^hosts:.*myhostname' %{_sysconfdir}/nsswitch.conf || sed -i 's/^hosts:.*/\0 myhostname/' %{_sysconfdir}/nsswitch.conf

# Add ntp configuration to /etc/sysconfig/network which is written
# normally by the installer.
# This needs to be kept until the next upgrade-only release after 8.0.
grep -q '^NTPSERVERARGS=' %{_sysconfdir}/sysconfig/network || echo 'NTPSERVERARGS="iburst prefer"' >> %{_sysconfdir}/sysconfig/network

%posttrans config
# We are shipping a file in depmod.d/, ensure it is used.
# Note: if depmod is not present yet, no cache was created before we add
# our config file, other packages will create it later, now with proper config.
if [ -x /sbin/depmod ]; then /sbin/depmod -a; fi


%preun config
%systemd_preun move-kernel-messages.service
%systemd_preun update-issue.service
%systemd_preun xs-fcoe.service
%systemd_preun vm.slice


%postun config
%systemd_postun move-kernel-messages.service
%systemd_postun update-issue.service
%systemd_postun xs-fcoe.service
%systemd_postun vm.slice

# XCP-ng: Not needed anymore but kept as documentation in case
# we rename the package again in the future
##%%triggerpostun config -- xenserver-release-config < 7.5
## XCP-ng: To be reviewed at each release to make sure the fixes are still valid.
## Fix upgrade from XCP-ng 7.4.x:
## when xenserver-release-config gets obsoleted by xcp-ng-release-config,
## its preun gets run last and disables the services.
## We don't want that, so we re-enable the services in this trigger
## which will be set off after postun has been executed.
#systemctl preset move-kernel-messages.service >/dev/null 2>&1 || :
#systemctl preset update-issue.service >/dev/null 2>&1 || :
#systemctl preset xs-fcoe.service >/dev/null 2>&1 || :
## Also restore changes to /etc/sysconfig/snmp, removed by a triggerun scriptlet
#if [ -f /etc/sysconfig/snmpd ]; then
#    grep -qs '^OPTIONS' /etc/sysconfig/snmpd || echo 'OPTIONS="-c /etc/snmp/snmpd.xs.conf"' >>/etc/sysconfig/snmpd
#fi

%posttrans presets
# Install or Upgrade, run when all new .service files got installed by other packages.
# Ensure that new service files installed by existing packages get appropriate defaults.
systemctl preset-all --preset-mode=enable-only || :

%files
%doc LICENSES
%defattr(0644,root,root,0755)
%{_sysconfdir}/redhat-release
%{_sysconfdir}/system-release
%{_sysconfdir}/centos-release
%config(noreplace) %{_sysconfdir}/os-release
%config(noreplace) %{_sysconfdir}/issue
%config(noreplace) %{_sysconfdir}/issue.net
%{_sysconfdir}/pki/rpm-gpg/
%config(noreplace) %{_sysconfdir}/dnf/vars/*
%{_sysconfdir}/rpm/macros.dist
%{_docdir}/redhat-release
%{_docdir}/centos-release
%{_datadir}/redhat-release
%{_datadir}/centos-release
%{_prefix}/lib/systemd/system-preset/90-default.preset
/EULA
%{python3_sitelib}/xcp/branding.py
%{python3_sitelib}/xcp/__pycache__
%{_sysconfdir}/depmod.d/00-xcpng-override.conf
%ifarch x86_64_v2
%config(noreplace) %{_sysconfdir}/rpm/macros.x86_64_v2
%endif

%files presets
%{_prefix}/lib/systemd/system-preset/89-default.preset

%files config
%defattr(0644,root,root,0755)
%config(noreplace) %{_sysconfdir}/modprobe.d/*.conf
%config(noreplace) %{_sysconfdir}/motd.xs
%config(noreplace) %{_sysconfdir}/profile.d/*.sh
%config(noreplace) %{_sysconfdir}/sysctl.d/*.conf
%{_sysconfdir}/logrotate.d/*
%{_sysconfdir}/udev/rules.d/*.rules
%{_sysconfdir}/systemd/system/*
# XCP-ng 8.3: we don't ship the xenserver.conf file
# If more useful files were added since in xapi.conf.d, re-enable this.
#%%{_sysconfdir}/xapi.conf.d/*.conf
%{_unitdir}/*
%attr(0755,-,-) /sbin/update-issue
%attr(0755,-,-) /opt/xensource/libexec/xen-cmdline
%attr(0755,-,-) /opt/xensource/libexec/bfs-interfaces
%attr(0755,-,-) /opt/xensource/libexec/iscsi-bfs-dhcp
%attr(0755,-,-) /opt/xensource/libexec/fcoe_driver
%attr(0755,-,-) %{_sysconfdir}/dhcp/dhclient.d/xs.sh

# kernel logging to VT2
%attr(0755,-,-) /opt/xensource/libexec/move-kernel-messages
%attr(0755,-,-) /opt/xensource/libexec/set-printk-console

# harden ciphers / TLS version used by curl/wget
/root/.curlrc
/root/.wgetrc

# Keep this changelog through future updates
%changelog
* Fri Sep 18 2026 Yann Dirson <yann.dirson@vates.tech> - 8.99.0-3
- Append autorev macro to Release

* Tue Jul 21 2026 Yann Dirson <yann.dirson@vates.tech> - 8.99.0-2
- Stop using space as separator in BuildRequires

* Tue Mar 17 2026 Yann Dirson <yann.dirson@vates.tech> - 8.99.0-1
- Bump versions to 8.99
- Set xenserver_major to 9
- Provide %xcpng macro in macros.dist
- provides/obsolete 9.x rpms
- Stop obsoleting epel-release
- Pick macros.x86_64_v2 from almalinux-release-10.0-32.el10
- Commented out all triggers
- Provide %almalinux macros in macros.dist
- Remove now-useless python2 build-deps
- Do not install 8.3 yum repo definitions (anyway moving to xcp-ng-config)
- Drop support for XCP-ng/XS 8.x compatibility
- Drop xapi and xenopsd snippets now provided by XAPI
- Stop obsoleting XS8 hotfixes
- Move /etc/yum to /etc/dnf
- Drop rsyslog support, we use journald
- Drop Obsoletes statements
- Remove runtime Requires: python3-xcp-libs
- Hack away broken sysctl setting for coredumps
- Add support for %%autorev macro to bump revision on rebuild
- Drop dependency of -release on -release-presets
- Drop dependency of -release on coreutils and grep

* Sun Feb 22 2026 Philippe Coval <philippe.coval@vates.tech> - 8.3.0-37
- Realign upstream to prompt patch from RPM
- Add timestamps to history

* Mon Jan 26 2026 Philippe Coval <philippe.coval@vates.tech> - 8.3.0-36
- Preserve /etc/rsyslog.d/xenserver.conf if present

* Wed Nov 19 2025 Philippe Coval <philippe.coval@vates.tech> - 8.3.0-35
- Fix prompt on testing tput output

* Fri Nov 14 2025 Philippe Coval <philippe.coval@vates.tech> - 8.3.0-33
- Remove noise from patches using git-format-patch options

* Thu May 08 2025 Andrii Sultanov <andriy.sultanov@vates.tech> - 8.3.0-32
- Fix patches that weren't properly generated and included in the specfile:
  - 0002-Sync-vm.slice-with-xenserver-release-v8.4.0-12.tar.g.patch
  - 0003-Sync-systemd-presets-with-xenserver-release-v8.4.0-1.patch

* Thu Apr 03 2025 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-31
- Rebuild for updated branding-xcp-ng
- Loosely sync with xenserver-release-8.4.0-15 (no real change, just a merge point)
  * Thu Jan 23 2025 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.4.0-15
  - CP-53338: Rebuild to pick up branding changes to 8.4

* Mon Mar 10 2025 Yann Dirson <yann.dirson@vates.tech> - 8.3.0-30
- Sync with xenserver-release-8.4.0-14:
  - 0001-Sync-vm.slice-with-xenserver-release-v8.4.0-12.tar.g.patch
  - 0002-Sync-systemd-presets-with-xenserver-release-v8.4.0-1.patch
  - Pull xcp-python-libs-compat (dropped from xapi deps between
    4.19 and 4.39)
- *** Upstream changelog ***
  * Thu Dec 05 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 8.4.0-14
  - CA-403415: Install xcp-python-libs-compat for XS8
  * Fri Oct 04 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.4.0-13
  - Enable new RRDD plugins
  * Fri Sep 20 2024 Mark Syms <mark.syms@cloud.com> - 8.4.0-12
  - CA-399511: Change systemd dependencies for vm.slice

* Mon Jan 20 2025 Lucas Ravagnier <lucas.ravagnier@vates.tech> - 8.3.0-29
- Add 0001-fix-curl-resolve-TLS-issue-caused-by-restrictive-con.patch
- This adds a cipher to '.curlrc' to fix a TLS Handshake Error with xoa.io

* Fri Aug 23 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-28
- Update repo files for CentOS and EPEL
- Point at repo.vates.tech for CentOS since mirrorlist.centos.org was cut
- Add "(EOL)" to repo descriptions for EOL repos
- Drop unused repos

* Tue Aug 13 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-27
- Loosely sync with xenserver-release-8.4.0-11
- *** Upstream changelog ***
- * Tue Jun 18 2024 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.4.0-11
- - CP-47026: Move mcelog config changes to mcelog.spec

* Tue Aug 06 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-26
- Don't require the empty kernel-livepatch and xen-livepatch packages
- Ensure rsyslog and systemd are installed in xcp-ng-release-config's %%post

* Fri Aug 02 2024 Thierry Escande <thierry.escande@vates.fr> - 8.3.0-25
- Move depmod call to posttrans to avoid ordering issues

* Thu Jun 20 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-24
- Loosely sync with xenserver-release-8.4.0-10
- *** Upstream changelog ***
- * Wed May 22 2024 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.4.0-10
- - CA-392433: Start dhclient on iSCSI BFS interfaces
- * Thu May 16 2024 Bernhard Kaindl <bernhard.kaindl@cloud.com> - 8.4.0-9
- - CA-392412: Disable the bpf syscall for unprivileged tasks (not used)
- * Sat May 11 2024 Deli Zhang <deli.zhang@cloud.com> - 8.4.0-8
- - CP-46084: Revert and move gnu-free-sans-fonts obsoletion to xs-obsolete-packages
- * Fri May 10 2024 Deli Zhang <deli.zhang@cloud.com> - 8.4.0-7
- - CP-46084: Obsolete unused package gnu-free-sans-fonts
- * Tue May 07 2024 Frediano Ziglio <frediano.ziglio@cloud.com> - 8.4.0-6
- - CP-49083: Do not patch logrotate configuration

* Wed Jun 19 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-23
- Loosely sync with xenserver-release-8.4.0-5
- *** Upstream changelog ***
- * Mon Apr 08 2024 Stephen Cheng <stephen.cheng@cloud.com> - 8.4.0-5
- - CP-47599: Update set-printk-console to Python3
- * Thu Mar 07 2024 Alex Brett <alex.brett@cloud.com> - 8.4.0-4
- - CA-389583: Update curlrc cipher specification to openssl format

* Wed Jun 12 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-22
- Rebuild for updated branding-xcp-ng
- Copyright year updated to 2024

* Mon Apr 29 2024 Thierry Escande <thierry.escande@vates.fr> - 8.3.0-21
- Remove sshd_config and ssh_config installation and triggers

* Fri Apr 26 2024 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-20
- xapi.service: do not pull qemuback.service unconditionally

* Tue Apr 16 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-19
- Loosely sync with xenserver-release-8.4.0-3
- *** Upstream changelog ***
- * Mon Feb 26 2024 Gerald Elder-Vass <Gerald.Elder-Vass@cloud.com> - 8.4.0-3
- - CP-47543: Increase pid max
- * Tue Jan 30 2024 Deli Zhang <deli.zhang@cloud.com> - 8.4.0-2
- - CP-44429: Move snmpd.xs.conf to net-snmp.spec
- - CP-44429: iptable: Accept snmp port 161
- * Tue Jan 09 2024 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.4.0-1
- - CP-44059: Update Product & Platform versions ahead of XenServer 8 GA
- * Fri Jan 05 2024 Lin Liu <lin.liu@cloud.com> - 8.3.60-12
- - In XS9, sshd manages the config itself, so remove config update from xenserver-release
- * Wed Jan 03 2024 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.3.60-11
- - CP-37929: Remove legacy logrotate mechanism
- - CA-385315: print both SHA1 and SHA256 fingerprints

* Wed Jan 24 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-18
- Loosely sync with xenserver-release-8.3.60-10
- *** Upstream changelog ***
- * Thu Dec 14 2023 Alex Brett <alex.brett@cloud.com> - 8.3.60-10
- - Revert of fcoe changes in 8.3.60-8 pending further testing
- * Wed Dec 13 2023 Alex Brett <alex.brett@cloud.com> - 8.3.60-9
- - CA-386077: Fixup filter and map diff between python2&3
- - CA-386658: Fix line wrapping of motd following rebranding
- - CP-46994: Install motd as /etc/motd
- * Mon Nov 27 2023 Lin Liu <lin.liu@cloud.com> - 8.3.60-8
- - CA-385236: Re-direct critical logs to /dev/hvc0
- - CP-46168: Some py2->py3 update and requires python3-xcp-libs
- * Thu Oct 26 2023 Lin Liu <lin.liu@cloud.com> - 8.3.60-6
- - CP-45918: XS9 does not build python2 site-packages
- * Wed Oct 25 2023 Alex Brett <alex.brett@cloud.com> - 8.3.60-5
- - CP-45884: Minor tweaks to /etc/issue text
- * Tue Oct 03 2023 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.3.60-4
- - CP-41302: Python 2&3 compatible versions of branding.py

* Mon Jan 22 2024 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-17
- Define xenserver macro in /etc/rpm/macros.dist. Some spec files use it.

* Fri Dec 08 2023 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-16
- Remove unused RPM-GPG-KEY-XenServer

* Wed Sep 27 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-15
- Remove dependency to system-config, added by XenServer and unneeded for now
- It just pulls an empty xenserver-config package everywhere

* Thu Sep 21 2023 Thierry Escande <thierry.escande@vates.tech> - 8.3.0-14
- Remove web server index files

* Wed Sep 20 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-13
- Update platform number to 3.4.0 for XenServer 8 to XCP-ng 8.3 migration
- Remove useless Obsoletes towards xenserver-release and xenserver-config-release
- Update BUILD_NUMBER to 8.3.0. The previous value, "cloud", doesn't mean anything anymore.
- Loosely sync with xenserver-release-8.3.60-3
- Don't require xenserver-config-packages. We have xcp-ng-deps already
- Keep CentOS, EPEL and XCP-ng yum repofiles
- Given the above, no useful changes in tarball, so it is unchanged
- Don't modify the iptables configuration for nrpe yet.
- *** Upstream changelog ***
- - * Fri Aug 18 2023 Alex Brett <alex.brett@cloud.com> - 8.3.60-3
- - CA-381674: Updates for launch of xenserver.com
- * Wed Aug 02 2023 Alex Brett <alex.brett@cloud.com> - 8.3.60-2
- - CP-44357: Update branding text
- * Wed Jul 26 2023 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.3.60-1
- - CP-42460: Update product and platform versions
- * Wed May 10 2023 Alex Brett <alex.brett@cloud.com> - 8.3.50-5
- - CP-43112: Update xapi configuration file for new GPG key and CDN domain
- - Remove redundant CentOS GPG keys
- * Tue May 09 2023 Sola Zhang <Sola.Zhang@cloud.com> - 8.3.50-4
- - CP-41889: Accept NRPE port 5666 in iptables
- * Wed May 03 2023 Deli Zhang <dzhang@tibco.com> - 8.3.50-3
- - CP-42642: Xapi: update config for sharing server certificate file to group users
- * Wed May 03 2023 Alex Brett <alex.brett@cloud.com> - 8.3.50-2
- - CP-41761: Replace CH GPG key with XS key
- - CA-377018: Require xenserver-config-packages for real installs
- * Fri Apr 28 2023 Gerald Elder-Vass <gerald.elder-vass@cloud.com> - 8.3.50-1
- - CP-42458: Update product and platform versions
- * Mon Apr 17 2023 Ming Lu <ming.lu@cloud.com> - 8.3.0-13
- - CP-41576: Enable xs-telemetry.timer and xs-telemetry.service
- * Wed Apr 05 2023 Tim Smith <tim.smith@citrix.com> - 8.3.0-12
- - CP-42031 Add dependency on system-config
- - Remove out of date yum repository definitions
- * Thu Mar 23 2023 Alex Brett <alex.brett@cloud.com> - 8.3.0-11
- - CP-42588: Rebuild to pick up new EUA
- * Thu Mar 09 2023 Gerald Elder-Vass <gerald.elder-vass@citrix.com> - 8.3.0-10
- - CP-42319: Rebuild to pick up further branding changes
- * Wed Feb 01 2023 Alex Brett <alex.brett@cloud.com> - 8.3.0-9
- - CP-41817: Correct console text in issue file
- * Thu Jan 26 2023 Alex Brett <alex.brett@cloud.com> - 8.3.0-8
- - CP-41789: Rebuild to pick up further branding changes
- * Fri Dec 02 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.3.0-7
- - CP-41365: Rebuild to pick up branding changes

* Thu Jun 15 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-12
- update BUILD_NUMBER

* Wed Jun 14 2023 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-11
- make sure depmod is run after touching depmod.d

* Thu Jun 08 2023 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-10
- Create file /etc/depmod.d/00-xcpng-override.conf instead of
  modifying /etc/depmod.d/dist.conf

* Wed May 10 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-9
- Update xcpng.repo for new repository structure

* Thu Mar 09 2023 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-8
- Restore python2 support in addition to python3

* Wed Feb 22 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-7
- Rebuild for updated branding-xcp-ng: COMPANY_NAME and COPYRIGHT_YEARS updated
- Update packaging for branding-xcp-ng's switch to python3
- Update /etc/motd.xs

* Thu Jan 05 2023 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-6
- Revert previous change: we'll fix the contents of the file instead

* Thu Dec 22 2022 Yann Dirson <yann.dirson@vates.fr> - 8.3.0-5
- Stop using /etc/motd.xs and modifying /etc/login.defs; try undo modification on upgrade

* Fri Dec 09 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-4
- Sync with xenserver-release-8.3.0-6
- *** Upstream changelog ***
- * Fri Oct 28 2022 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.3.0-6
- - CP-40640: Prefer PRODUCT_VERSION_TEXT over PRODUCT_VERSION
- * Mon Aug 22 2022 Lin Liu <lin.liu@citrix.com> - 8.3.0-5
- - CP-40419: Remove CBC from openssh ciphers
- * Wed Jul 20 2022 Ming Lu <ming.lu@citrix.com> - 8.3.0-4
- - CP-40176: Add dependencies on xen-livepatch and kernel-livepatch

* Mon Nov 14 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-3
- Update shell prompt (PS1) colors to match XCP-ng's new logo

* Fri Nov 04 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-2
- Replace host web page with a simple page that loads XO Lite

* Mon Sep 05 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.3.0-1
- Update to xcp-ng-release 8.3.0
- Sync with xenserver-release 8.3.0
- Removed /etc/xapi.conf.d/citrix-hypervisor.conf that was added by xenserver-release
- Removed upgrade scriptlets meant to make sure chronyd and chrony-wait are enabled. Now fixed in presets file.
- *** Upstream changelog (trimmed) ***
- - CA-362922: Outdated Ciphers used by Openssh when host is updated from hotfix
- - CA-366439: Silence critical warning from fcoeadm -i
- - CA-366439: Handle fipvlan failures properly
- - CP-39330 Provide signing pubkey
- - CP-38336: Remove elxocmcore trigger scripts
- - CA-362930: Replace space to underscore for provides

* Tue Feb 15 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-5
- Rebuild for updated branding-xcp-ng
- Sets "copyright" year to 2022

* Tue Feb 01 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-4
- Add inline patches to fix cipher lists in sshd and ssh config for people updating from an updated 8.2

* Mon Jan 31 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-3
- Add a comment about chronyd and chrony-wait services. In 8.2.1 we still need to enable them manually.
- Fix symlink to chrony-wait.service now that we removed our custom replacement from /etc

* Thu Jan 20 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-2
- Fix inverted test for the presence of an old replacement chrony-wait service file
- The file was not removed when it should.

* Tue Jan 11 2022 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.1-1
- Update to xcp-ng-release 8.2.1
- Sync with xenserver-release 8.2.1
- Remove patches whose changes are now included in the tarball
- Remove manual patching of EULA as the updated one is in 8.2.1's branding-xcp-ng RPM
- Switch to Citrix's better way of overriding the timeout in chrony-wait
- *** Upstream changelog ***
- * Tue Oct 26 2021 Igor Druzhinin <igor.druzhinin@citrix.com> - 8.2.1-8
- - CP-37372: Obsolete XS82E034 hotfix
- * Wed Oct 20 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.2.1-7
- - CA-339520: fcoe_driver: Only run "fcoeadm -i" when needed
- * Mon Sep 20 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.2.1-6
- - CP-34895: reduce verbosity of SM logs
- - CA-343416: write crit log message to host console
- - CA-343416: log corosync warning and above to console
- - CA-343759: Send HUP to rsyslogd after DHCP setup
- - CA-356624: Force log rotation when file size reaches 100 MiB
- - CA-358540: Fix secure.log typo
- * Tue Sep 14 2021 Christian Lindig <christian.linidg@citrix.com> - * 8.2.1-5
- - Add obsoltes for XS82E033
- * Fri Sep 10 2021 Igor Druzhinin <igor.druzhinin@citrix.com> - 8.2.1-4
- - CP-37666: Obsolete XS82E032 hotfix
- - CP-38080: Obsolete XS82E031
- * Fri Jul 30 2021 Ross Lagerwall <ross.lagerwall@citrix.com> - 8.2.1-3
- - CP-37202: Obsolete XS82E030 hotfix
- * Tue Jul 13 2021 Ming Lu <ming.lu@citrix.com> - 8.2.1-2
- - CP-36430: Update ciphers for SSH server and client
- * Fri Jun 25 2021 Pau Ruiz Safont <pau.safont@citrix.com> - 8.2.1-1
- - CP-36759: First 8.2.1 release

* Thu Dec 02 2021 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-8
- XOA quick deploy: warn when XOA IP is same as host IP

* Fri Sep 10 2021 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-7
- Don't reenable rsyslog.service when not needed
- Landing page: XOA deploy fixes
- Landing page: update jquery to 3.6.0

* Wed Mar 03 2021 Benjamin Reis <benjamin.reis@vates.fr> - 8.2.0-6
- Add xcp-ng-release-8.2.0-eula-shorter-lines.XCP-ng.patch

* Wed Feb 03 2021 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-5
- Sync with XS82E015
- Fix sshd and ssh config patching
- Handling of chronyd systemd unit override not synced yet, on purpose

* Fri Nov 13 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-4
- Show xe CLI in the host page instead of XCP-ng Center

* Thu Oct 01 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-2
- Update link to official doc in the HTML welcome page

* Wed Jul 01 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.2.0-1
- Update to XCP-ng 8.2

* Fri Apr 03 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.1.0-6
- Enable chronyd and chrony-wait service
- Reduce chrony-wait timeout from 600s to 120s
- This reduces boot time a lot for hosts that can't reach a ntp server

* Wed Mar 25 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.1.0-3
- Update console welcome message to invite using Xen Orchestra

* Mon Jan 20 2020 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.1.0-2
- Move POST scriptlet to POSTTRANS to avoid bugs due to ordering issues
- Avoids issue where uname is missing when executing the script
- See https://bugs.centos.org/view.php?id=14795

* Fri Dec 20 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.1.0-1
- Update to XCP-ng 8.1.0
- Add EULA and LICENSES back

* Thu Jul 18 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-13
- Add EPEL repos, disabled by default
- Warn against enabling CentOS and EPEL repos in .repo files

* Wed Jul 10 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-12
- Add fallback yum mirror in case mirrors.xcp-ng.org is down
- Includes updated repo file and updated fastestmirror configuration

* Thu Jun 27 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-11
- Update XOA quick deploy: workaround issue with xoa-updater

* Thu Jun 27 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-10
- Update XOA quick deploy: set XOA unix password

* Fri Jun 14 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-9
- Fix auto-add of first host in XOA during fast deploy

* Thu Jun 13 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-8
- Add custom bash prompt for XCP-ng

* Wed May 15 2019 Samuel Verschelde <stormi-xcp@ylix.fr> - 8.0.0-5
- Update to XCP-ng 8.0.0
- Update 'dist' macro to include the version number: ".xcpng8.0"
- Update web page and add XOA deployment

* Tue Nov 27 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 7.6.0-4
- Set the 'dist' macro to ".xcpng"

* Wed Oct 10 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 7.6.0-3
- Add XCP-ng's RPM GPG key

* Tue Sep 25 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 7.6.0-2
- Update to XCP-ng 7.6.0
- New welcome page

* Wed Jul 25 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 7.5.0-2
- Add triggerun scriptlet for smooth upgrade

* Fri Jul 06 2018 Samuel Verschelde <stormi-xcp@ylix.fr> - 7.5.0-1
- Rename to xcp-ng-release
- Update to XCP-ng 7.5.0

* Sun Apr 29 2018 John Else <john.else@gmail.com>
- Update packaging for XCP-ng

* Wed Nov 19 2014 Ross Lagerwall <ross.lagerwall@citrix.com>
- Initial xenserver-release packaging
