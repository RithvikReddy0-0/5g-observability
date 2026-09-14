# open5gs.Dockerfile — Open5GS built from a pinned commit.
#
# One image carries every network function; each container runs a different binary.
#
# Reproducibility, in the same spirit as manifest.lock:
#   * the base image is pinned by digest, not by tag
#   * Open5GS is fetched by commit SHA and the checkout is verified
#   * the meson subprojects are pinned by SHA too. Upstream's .wrap files point at BRANCHES
#     (freeDiameter r1.5.0, libtins r4.5, prometheus-client-c "open5gs"), so an unmodified
#     build silently picks up whatever those branches say on the day. The wraps are rewritten
#     below before meson ever reads them.
#
# Build (the Makefile passes the commit from manifest.lock):
#   docker build -f deployments/open5gs/images/open5gs.Dockerfile \
#     --build-arg OPEN5GS_COMMIT=<sha> -t o5gs/open5gs:v2.8.0 .

ARG UBUNTU=ubuntu@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254

# --------------------------------------------------------------------------- build
FROM ${UBUNTU} AS build

ARG OPEN5GS_COMMIT
ARG FREEDIAMETER_COMMIT=14725af3ba0edbf9ff61c4e3239ed42464423b2e
ARG LIBTINS_COMMIT=bf22438172d269e6db70e27246dffd8e1f0b96e3
ARG PROMCLIENT_COMMIT=a58ba25bf87a9b1b7c6be4e6f4c62047d620f402
ARG USRSCTP_COMMIT=07f871bda23943c43c9e74cc54f25130459de830

# Dependency list is upstream's own (docker/ubuntu/latest/base/Dockerfile at the pinned commit).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3-pip python3-setuptools python3-wheel ninja-build build-essential flex bison \
        git cmake meson libsctp-dev libgnutls28-dev libgcrypt-dev libssl-dev libmongoc-dev \
        libbson-dev libyaml-dev libmicrohttpd-dev libcurl4-gnutls-dev libnghttp2-dev \
        libtins-dev libtalloc-dev libidn-dev ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN test -n "${OPEN5GS_COMMIT}" || { echo "OPEN5GS_COMMIT build-arg is required"; exit 1; }

WORKDIR /src
RUN git init -q open5gs && cd open5gs \
    && git remote add origin https://github.com/open5gs/open5gs.git \
    && git fetch -q --depth 1 origin "${OPEN5GS_COMMIT}" \
    && git -c advice.detachedHead=false checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${OPEN5GS_COMMIT}" \
    && echo "open5gs checked out at ${OPEN5GS_COMMIT}"

# Replace branch revisions in the wraps with SHAs, and fail loudly if a wrap changed shape.
WORKDIR /src/open5gs/subprojects
RUN set -e; \
    pin() { f="$1.wrap"; sha="$2"; \
            grep -q '^revision = ' "$f" || { echo "unexpected wrap format: $f"; exit 1; }; \
            sed -i "s/^revision = .*/revision = ${sha}/" "$f"; \
            echo "pinned $f -> ${sha}"; }; \
    pin freeDiameter        "${FREEDIAMETER_COMMIT}"; \
    pin libtins             "${LIBTINS_COMMIT}"; \
    pin prometheus-client-c "${PROMCLIENT_COMMIT}"; \
    pin usrsctp             "${USRSCTP_COMMIT}"

WORKDIR /src/open5gs
RUN meson setup build --prefix=/opt/open5gs --buildtype=release \
    && ninja -C build -j"$(nproc)" \
    && ninja -C build install

# Work out exactly which runtime packages the binaries link against, so the final image
# carries libraries and nothing else (no compilers, no -dev headers).
#
# Two traps, both of which silently produced an EMPTY package list on the first build:
#   * Open5GS's own libraries live under /opt, so ldd needs LD_LIBRARY_PATH to see past them
#   * Ubuntu 24.04 is usr-merged: ldd reports /lib/x86_64-linux-gnu/..., but dpkg only knows
#     /usr/lib/x86_64-linux-gnu/..., so `dpkg -S` fails for every path. Map /lib -> /usr/lib.
RUN set -e; \
    export LD_LIBRARY_PATH=/opt/open5gs/lib/x86_64-linux-gnu; \
    find /opt/open5gs -type f \( -perm -u+x -o -name '*.so*' \) -exec ldd {} + 2>/dev/null \
      | awk '/=> \// {print $3}' | grep -v '^/opt/open5gs/' | sort -u \
      | xargs -r readlink -f | sed 's#^/lib/#/usr/lib/#' | sort -u \
      | xargs -r dpkg -S 2>/dev/null | cut -d: -f1 | sort -u > /opt/open5gs/runtime-packages.txt; \
    echo "runtime packages:"; cat /opt/open5gs/runtime-packages.txt; \
    test "$(wc -l < /opt/open5gs/runtime-packages.txt)" -gt 5 \
      || { echo "runtime package detection found too few packages — refusing to continue"; exit 1; }; \
    echo "${OPEN5GS_COMMIT}" > /opt/open5gs/COMMIT

# --------------------------------------------------------------------------- runtime
FROM ${UBUNTU}

COPY --from=build /opt/open5gs /opt/open5gs

# iproute2/iptables: the UPF creates TUN devices and NATs UE traffic.
# iputils-ping, iperf3, curl: used by the tests to prove the user plane carries traffic.
# python3 (full stdlib; python3-minimal has no http.server): runs the per-slice traffic exporter inside the UPF container (see
#   upf-entrypoint.sh for why it cannot be a sidecar).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        $(cat /opt/open5gs/runtime-packages.txt) \
        iproute2 iptables iputils-ping iperf3 curl ca-certificates netbase python3 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/open5gs/bin:$PATH \
    LD_LIBRARY_PATH=/opt/open5gs/lib/x86_64-linux-gnu

# Fail the build rather than ship an image whose binaries cannot load their libraries.
RUN for b in /opt/open5gs/bin/open5gs-*d; do \
        ldd "$b" | grep -q 'not found' && { echo "unresolved libs in $b"; ldd "$b"; exit 1; }; \
    done; echo "all NF binaries resolve their libraries"

LABEL org.opencontainers.image.source="https://github.com/open5gs/open5gs" \
      org.opencontainers.image.version="v2.8.0"

WORKDIR /opt/open5gs
