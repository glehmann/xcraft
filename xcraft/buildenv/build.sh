# Build a package in the root of the build environment, /buildroot, in its build tree mounted on /build, sourced by
# xcraft after setting:
#   spec: the file name of its spec, in /build/SPECS/
#   stage: the stage of rpmbuild -b<stage>, like a for all, or p for %prep
#   repo: the id of the repository of our builds
#   lock: the prefix of the dnf commands, which the containers run one at a time
#   autorev: the release suffix of the build, %autorev: +b<n> of its build ID, and ~<counter>.<ID> for a development one
#   replace: the upstream packages of the root to replace by ours, or nothing
#   nocheck: --nocheck, for rpmbuild to skip %check, or nothing
#   arch: the RPM architecture of the root, which the tools around it run natively, or through qemu when another
# The dnf of the image installs the build dependencies in the root, including the dynamic ones, from the repository of
# the pinned upstream packages on /pinned and the one of our builds only, then rpmbuild runs in the root, see
# chroot.py, with stdin from /dev/null like koji and mock, so that interactive steps of a build don't wait forever, and
# without gaining privileges, so that sudo cannot change the build root once recorded. The container has no network,
# but its loopback interface, like the builds of koji and mock: the build only uses what it is given, and the root
# only has the packages of the builds, not the tools of the image, like dnf or python3.
# shellcheck shell=bash disable=SC2086 # $lock, $replace and $nocheck are lists of words
: "${spec:?}" "${stage:?}" "${repo:?}" "${lock:?}" "${autorev?}" "${replace?}" "${nocheck?}" "${arch:?}"
set -euo pipefail
export CCACHE_DIR=/ccache PATH=/usr/lib64/ccache:$PATH
spec=/build/SPECS/$spec
root=/buildroot
defines=(-D "_topdir /build")
# a dynamic BuildRequires not pinned yet is not installed: xcraft pins it, and builds again; in the root, with the
# repositories of the image rather than those of the root
installed=(--disablerepo='*' --enablerepo=xcraft-pinned --enablerepo="$repo" --installroot="$root"
    --forcearch="$arch" --setopt=reposdir=/etc/yum.repos.d --setopt=cachedir=/var/cache/dnf)
if [ -n "$autorev" ]; then
    defines+=(-D "autorev $autorev")
fi
chrooted() {
    python3 /xcraft/chroot.py "$@"
}
time {
    # dnf may not see that the local repository changed when it is regenerated within a second
    $lock dnf -q makecache --forcearch="$arch" --repo "$repo" --refresh
    if [ -n "$replace" ]; then
        # ours have their own versions, older ones for kernel-headers: only from our repository, of a higher
        # priority, with the pinned packages they need that the root does not have
        $lock dnf -y "${installed[@]}" --setopt="$repo.priority=1" distro-sync $replace
    fi
    # the static BuildRequires of the spec as the root reads it, with its macros: those of its source package, made
    # without %prep, nor compressing the sources
    chrooted rpmbuild -bs --nodeps -D '_source_payload w0.ufdio' "${defines[@]}" "$spec" </dev/null >/dev/null
    $lock dnf -y "${installed[@]}" builddep /build/SRPMS/*.src.rpm
    rm -f /build/SRPMS/*.src.rpm
    # the directories of the sources of a package developed from a tree, synchronized with its archive before %prep
    python3 /xcraft/develop.py "$spec" "$arch-linux"
    if grep -q '^%generate_buildrequires' "$spec"; then
        # rpmbuild -br exits with 11 while some dynamic BuildRequires are missing
        while rc=0; chrooted rpmbuild -br "${defines[@]}" "$spec" </dev/null || rc=$?; [ $rc -eq 11 ]; do
            # all the BuildRequires, the dynamic ones included, which xcraft records, even when they cannot be installed
            rpm -qp --requires /build/SRPMS/*.buildreqs.nosrc.rpm >/build/buildrequires.txt
            $lock dnf -y "${installed[@]}" builddep /build/SRPMS/*.buildreqs.nosrc.rpm
            rm /build/SRPMS/*.buildreqs.nosrc.rpm
        done
        [ $rc -eq 0 ]
        rm -f /build/SRPMS/*.buildreqs.nosrc.rpm
        # the last ones: rpmbuild -br wrote the source package
        rpm -qp --requires /build/SRPMS/*.src.rpm >/build/buildrequires.txt
    fi
    # with the compilers it wraps, like the build roots of resolve.py; its package makes its group after installing
    # the files of the group, which get root's, unused as the cache is /ccache: not a warning of the build
    if rpm --root "$root" -q --quiet gcc || rpm --root "$root" -q --quiet clang; then
        $lock dnf -y "${installed[@]}" install ccache 2>&1 | grep -v '^warning: group ccache does not exist'
    fi
    $lock python3 /xcraft/record.py /build/buildroot.json "$root" "$arch"
    # like mock's login shell, for the environment of build dependencies, like opam's, which are not written for set -eu
    # shellcheck disable=SC2016 # expanded in the root
    chrooted bash -c 'for profile in /etc/profile.d/*.sh; do . "$profile"; done; exec "$@"' bash \
        rpmbuild -b"$stage" $nocheck "${defines[@]}" "$spec" </dev/null
    if [ "$stage" = a ]; then
        python3 /xcraft/made.py /build/made.json /build/RPMS
    fi
}
