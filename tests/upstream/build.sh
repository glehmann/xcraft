# Build the upstream packages of the tests into /out/repo/<arch>/, from the specs in /specs, with their versions and
# architectures: toyarch 2 is for x86_64 only, and the repositories of x86_64 have toymulti for i686 too.
dnf -q -y install rpm-build createrepo_c
toy() { rpmbuild -bb --define "_topdir /tmp/rpmbuild" --define "_rpmdir /out" --define "toyver $1" "${@:2}"; }
toy 1 /specs/toyrun.spec && toy 2 /specs/toyrun.spec
toy 1 --target x86_64 /specs/toyarch.spec && toy 2 --target x86_64 /specs/toyarch.spec
toy 1 --target aarch64 /specs/toyarch.spec
for target in x86_64 i686 aarch64; do
    toy 1 --target $target /specs/toymulti.spec
done
for arch in x86_64_v2 x86_64 aarch64; do
    mkdir -p /out/repo/$arch
    cp /out/noarch/* /out/"${arch%_v2}"/* /out/repo/$arch
    if [ "${arch%_v2}" = x86_64 ]; then
        cp /out/i686/* /out/repo/$arch
    fi
    createrepo_c -q /out/repo/$arch
done
