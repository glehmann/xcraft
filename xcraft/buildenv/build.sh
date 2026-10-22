# Build a package in the build environment, in its build tree mounted on /build, sourced by xcraft after setting:
#   spec: the file name of its spec, in /build/SPECS/
#   stage: the stage of rpmbuild -b<stage>, like a for all, or p for %prep
#   repo: the id of the repository of our builds
#   lock: the prefix of the dnf commands, which the containers run one at a time
#   autorev: the suffix of the release of a test build, or nothing
#   replace: the upstream packages of the image to replace by ours, or nothing
# It installs the build dependencies, including the dynamic ones, then runs rpmbuild, with stdin from /dev/null like
# koji and mock, so that interactive steps of a build don't wait forever.
# shellcheck shell=bash disable=SC2086 # $lock and $replace are lists of words
: "${spec:?}" "${stage:?}" "${repo:?}" "${lock:?}" "${autorev?}" "${replace?}"
set -euo pipefail
export CCACHE_DIR=/ccache PATH=/usr/lib64/ccache:$PATH
spec=/build/SPECS/$spec
defines=(-D "_topdir /build")
if [ -n "$autorev" ]; then
    defines+=(-D "autorev $autorev")
fi
time {
    # dnf may not see that the local repository changed when it is regenerated within a second
    $lock dnf -q makecache --repo "$repo" --refresh
    if [ -n "$replace" ]; then
        # ours have their own versions, older ones for kernel-headers, and are not in the versionlock
        $lock dnf -y --disableplugin=versionlock --repo "$repo" distro-sync $replace
    fi
    $lock dnf -y builddep "${defines[@]}" "$spec"
    if grep -q '^%generate_buildrequires' "$spec"; then
        # rpmbuild -br exits with 11 while some dynamic BuildRequires are missing
        while rc=0; rpmbuild -br "${defines[@]}" "$spec" </dev/null || rc=$?; [ $rc -eq 11 ]; do
            $lock dnf -y builddep /build/SRPMS/*.buildreqs.nosrc.rpm
            rm /build/SRPMS/*.buildreqs.nosrc.rpm
        done
        [ $rc -eq 0 ]
        rm -f /build/SRPMS/*.buildreqs.nosrc.rpm
    fi
    $lock python3 /xcraft/record.py /build/buildroot.json
    # like mock's login shell, for the environment of build dependencies, like opam's, which are not written for set -eu
    set +eu +o pipefail
    for profile in /etc/profile.d/*.sh; do
        # shellcheck source=/dev/null
        . "$profile"
    done
    set -eu -o pipefail
    rpmbuild -b"$stage" "${defines[@]}" "$spec" </dev/null
}
