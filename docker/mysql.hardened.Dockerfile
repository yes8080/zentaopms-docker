FROM golang:1.27.4-alpine3.23@sha256:f70850ef6ce341e8b6427f677049145d63c0b7ce002d4d7e3d464828b1b96dcd AS gosu-build

# Upstream gosu 1.20 includes the patched user/group database parser.
ADD --checksum=sha256:2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51 https://codeload.github.com/tianon/gosu/tar.gz/29b939aa5bd4dc588af765b8248f6e39150d2565 /tmp/gosu.tar.gz
WORKDIR /src
# Pin the x/sys update and its published Go checksum database records.
RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    set -eu; \
    tar -xzf /tmp/gosu.tar.gz --strip-components=1; \
    if [ -s /run/secrets/apt_ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/apt_ca_bundle; fi; \
    go mod edit -go=1.25.0 -require=golang.org/x/sys@v0.44.0; \
    printf '%s\n' \
        'golang.org/x/sys v0.44.0 h1:ildZl3J4uzeKP07r2F++Op7E9B29JRUy+a27EibtBTQ=' \
        'golang.org/x/sys v0.44.0/go.mod h1:4GL1E5IUh+htKOUEOaiffhrAeqysfVGipDYzABqnCmw=' >> go.sum; \
    go mod download; \
    go mod verify; \
    CGO_ENABLED=0 go build -mod=readonly -trimpath -buildvcs=false -o /out/gosu .

FROM mysql:8.4@sha256:a81d043930e51745522bd5c430961cf8286f0a23f0a9a2a6e0108f7ea71b0902
ENV GOSU_VERSION=1.20

# The official entrypoint and health check use mysql and mysqladmin.
RUN rpm -e mysql-shell

ARG ZENTAO_REFRESH_OS_PACKAGES=0
RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 <<'SH'
set -eu
case "$ZENTAO_REFRESH_OS_PACKAGES" in
    0) exit 0 ;;
    1) ;;
    *) echo "ZENTAO_REFRESH_OS_PACKAGES must be 0 or 1" >&2; exit 1 ;;
esac

# Keep the MySQL LTS binaries from the pinned image. Update the operating system
# using only the two Oracle Linux repositories and the signing key in that image.
mysql_package=$(rpm -q mysql-community-server-minimal)
set -- '--disablerepo=*' --setopt=install_weak_deps=0 --setopt=allow_vendor_change=0
for repo in ol9_baseos_latest ol9_appstream; do
    set -- "$@" "--enablerepo=$repo" "--setopt=$repo.gpgcheck=1" \
        "--setopt=$repo.sslverify=1" "--setopt=$repo.skip_if_unavailable=0" \
        "--setopt=$repo.gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-oracle"
    if [ -s /run/secrets/apt_ca_bundle ]; then
        set -- "$@" "--setopt=$repo.sslcacert=/run/secrets/apt_ca_bundle"
    fi
done
microdnf "$@" --refresh --best -y upgrade
microdnf clean all
if [ "$(rpm -q mysql-community-server-minimal)" != "$mysql_package" ]; then
    echo "Oracle Linux OS updates changed the pinned MySQL server package" >&2
    exit 1
fi

# A stale but reachable mirror must not pass the November 2026 errata baseline.
check_floor() {
    installed=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' "$1")
    comparison=$(INSTALLED="$installed" REQUIRED="$2" rpm --eval '%{lua:print(rpm.vercmp(os.getenv("INSTALLED"), os.getenv("REQUIRED")))}')
    if [ "$comparison" -lt 0 ]; then
        echo "$1 $installed is older than the required $2" >&2
        exit 1
    fi
}
check_floor krb5-libs 0:1.21.1-10.0.1.el9_9.2
for pkg in libblkid libmount libsmartcols libuuid; do check_floor "$pkg" 0:2.37.4-28.0.1.el9_9.1; done
for pkg in glibc glibc-common glibc-minimal-langpack; do check_floor "$pkg" 0:2.34-290.0.1.el9_9; done
for pkg in rpm rpm-libs; do check_floor "$pkg" 0:4.16.1.3-43.el9_9; done
SH
COPY --from=gosu-build /out/gosu /usr/local/bin/gosu
RUN gosu mysql id
