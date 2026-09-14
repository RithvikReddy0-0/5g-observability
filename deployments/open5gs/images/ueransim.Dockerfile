# ueransim.Dockerfile — UERANSIM built from the commit pinned in manifest.lock.
#
# The free5GC stack used the published free5gc/ueransim:latest image, whose tag moves and
# whose source commit is not recorded (an M0 open item). This builds the exact pinned SHA
# instead, so the RAN simulator is as reproducible as the core it is tested against.
#
# One local patch is applied on top of the pinned source, and the build fails if it no longer
# applies: patches/ueransim-udp-socket-buffers.patch sizes UDP socket buffers when
# UERANSIM_UDP_BUFFER_BYTES is set (docs/adr/ADR-012-ueransim-udp-buffers.md). The image is
# tagged v3.3.0-udpbuf so it is never mistaken for upstream.

ARG UBUNTU=ubuntu@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254

FROM ${UBUNTU} AS build
ARG UERANSIM_COMMIT
RUN test -n "${UERANSIM_COMMIT}" || { echo "UERANSIM_COMMIT build-arg is required"; exit 1; }

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential cmake git libsctp-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git init -q ueransim && cd ueransim \
    && git remote add origin https://github.com/aligungr/UERANSIM.git \
    && git fetch -q --depth 1 origin "${UERANSIM_COMMIT}" \
    && git -c advice.detachedHead=false checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${UERANSIM_COMMIT}"

COPY patches/ueransim-udp-socket-buffers.patch /patches/
RUN cd /src/ueransim && git apply --verbose /patches/ueransim-udp-socket-buffers.patch

WORKDIR /src/ueransim
RUN make -j"$(nproc)" && ls -la build/

FROM ${UBUNTU}
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libsctp1 iproute2 iputils-ping iperf3 curl ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/ueransim/build/nr-gnb /src/ueransim/build/nr-ue \
                  /src/ueransim/build/nr-cli /src/ueransim/build/nr-binder \
                  /src/ueransim/build/libdevbnd.so /opt/ueransim/
ENV PATH=/opt/ueransim:$PATH
LABEL org.opencontainers.image.version="v3.3.0" \
      io.5g-observability.patches="ueransim-udp-socket-buffers"
WORKDIR /opt/ueransim
