#!/usr/bin/env bash
# capture_pdu_session.sh — record one real PDU session establishment on the wire.
#
# Packet captures run inside the network namespaces of the AMF, SMF, eMBB UPF and eMBB gNB at
# the same time, while one UE releases its session and establishes a new one, then sends two
# pings through it. The merged, decoded capture is the source for
# diagrams/open5gs-pdu-session-establishment.mmd — the diagram is authored from what was
# observed, not from the specification.
#
# NAS is readable because the AMF prefers the null cipher (NEA0) in config/amf.yaml; messages
# are still integrity-protected.
#
# Output: docs/evidence/open5gs-pdu-session-capture/{capture.pcap, sequence.txt}
# Usage:  scripts/open5gs/capture_pdu_session.sh [imsi=208930000000003]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
OUT="$REPO_ROOT/docs/evidence/open5gs-pdu-session-capture"
IMSI="imsi-${1:-208930000000003}"
TOOLS=nicolaka/netshoot@sha256:a20c2531bf35436ed3766cd6cfe89d352b050ccc4d7005ce6400adf97503da1b  # v0.13
mkdir -p "$OUT"
rm -f "$OUT"/*.pcap

for nf in amf smf upf-embb gnb-embb; do
  docker run -d --rm --name "cap-$nf" --net "container:o5gs-$nf" -v "$OUT:/cap" "$TOOLS" \
    tcpdump -i any -s 0 -U -w "/cap/$nf.pcap" 'sctp or port 7777 or port 8805 or port 2152' >/dev/null
done
sleep 3

echo "releasing and re-establishing the PDU session of $IMSI"
docker exec o5gs-ue nr-cli "$IMSI" -e "ps-release 1"
sleep 3
docker exec o5gs-ue nr-cli "$IMSI" -e "ps-establish IPv4 --sst 1 --sd 0x010203 --dnn internet"
sleep 4
dev=$(docker exec o5gs-ue sh -c "sed -n 's/.*TUN interface\[\(uesimtun[0-9]*\), .*/\1/p' /tmp/ue-${IMSI#imsi-}.log | tail -1")
echo "user data: 2 pings through $dev"
docker exec o5gs-ue ping -I "$dev" -c 2 -W 2 10.53.0.51 | tail -2
sleep 2

for nf in amf smf upf-embb gnb-embb; do docker stop "cap-$nf" >/dev/null; done
sleep 1

docker run --rm -v "$OUT:/cap" "$TOOLS" sh -c '
  mergecap -w /cap/capture.pcap /cap/amf.pcap /cap/smf.pcap /cap/upf-embb.pcap /cap/gnb-embb.pcap &&
  rm /cap/amf.pcap /cap/smf.pcap /cap/upf-embb.pcap /cap/gnb-embb.pcap &&
  tshark -r /cap/capture.pcap -d tcp.port==7777,http2 \
    -Y "ngap or pfcp or (http2.type == 1) or (gtp and icmp)" \
    -T fields -E separator="|" \
    -e frame.time_relative -e ip.src -e ip.dst -e _ws.col.Protocol -e _ws.col.Info \
  | awk -F"|" "!seen[\$2 FS \$3 FS \$5]++" > /cap/sequence.txt'
echo "packets: $(docker run --rm -v "$OUT:/cap" "$TOOLS" capinfos -c /cap/capture.pcap | awk '/Number of packets/ {print $4}')"
echo "decoded sequence: $OUT/sequence.txt ($(wc -l < "$OUT/sequence.txt") distinct messages)"
