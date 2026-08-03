#!/usr/bin/env bash
# collect_evidence.sh — capture reproducible proof of the deployment's state.
#
# Writes a timestamped folder under docs/evidence/ containing raw captures plus a
# human-readable summary. Intended as the artifact trail for milestone sign-off
# (SPEC ADR-008: "understanding produces artifacts").
#
# Usage:
#   scripts/collect_evidence.sh            # capture into docs/evidence/run-<ts>/
#   OUT_DIR=/tmp/x scripts/collect_evidence.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
COMPOSE_DIR="$REPO_ROOT/deployments/compose"
TS="$(date -u +%Y%m%d-%H%M%SZ)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/docs/evidence/run-$TS}"
mkdir -p "$OUT_DIR"

dc() { (cd "$COMPOSE_DIR" && docker compose "$@" 2>/dev/null); }
curl_net() { docker run --rm --network compose_privnet curlimages/curl:latest -s --max-time 8 "$@" 2>/dev/null; }

echo "collecting evidence into: $OUT_DIR"

# --- 1. environment -------------------------------------------------------
{
  echo "captured (UTC): $TS"
  echo "kernel        : $(uname -r)"
  echo "os            : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
  echo "cpu threads   : $(nproc)"
  echo "memory        : $(awk '/MemTotal/{printf "%.1f GiB", $2/1024/1024}' /proc/meminfo)"
  echo "docker        : $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  echo "compose       : $(docker compose version --short 2>/dev/null)"
  echo
  echo "--- ODE conformance (scripts/verify_env.sh) ---"
  bash "$SCRIPT_DIR/verify_env.sh" 2>&1
} > "$OUT_DIR/01-environment.txt"

# --- 2. containers --------------------------------------------------------
dc ps --format "table {{.Name}}\t{{.Status}}" > "$OUT_DIR/02-containers.txt"

# --- 3. NRF registrations -------------------------------------------------
dc exec -T db mongo free5gc --quiet --eval \
  'db.NfProfile.find({},{_id:0,nfType:1,nfStatus:1}).toArray()' \
  > "$OUT_DIR/03-nrf-registrations.txt"

# --- 4. subscribers -------------------------------------------------------
{
  echo "--- counts by collection ---"
  dc exec -T db mongo free5gc --quiet --eval '
    print("amData              : " + db["subscriptionData.provisionedData.amData"].count());
    print("authSubscription    : " + db["subscriptionData.authenticationData.authenticationSubscription"].count());
    print("smData              : " + db["subscriptionData.provisionedData.smData"].count());
    print("distinct PLMN       : " + db["subscriptionData.provisionedData.amData"].distinct("servingPlmnId"));
    print("unique GPSIs        : " + db["subscriptionData.provisionedData.amData"].distinct("gpsis").length);
    print("slice(s)            : " + JSON.stringify(db["subscriptionData.provisionedData.amData"].distinct("nssai.defaultSingleNssais")));
  '
  echo
  echo "--- provisioned SUPIs ---"
  dc exec -T db mongo free5gc --quiet --eval \
    'db["subscriptionData.provisionedData.amData"].find({},{_id:0,ueId:1}).toArray().map(function(x){return x.ueId}).join("\n")'
} > "$OUT_DIR/04-subscribers.txt"

# --- 5. RAN / UE registration --------------------------------------------
{
  echo "--- gNB NG setup ---"
  dc logs --no-log-prefix ueransim 2>/dev/null | grep -E "NG Setup|SCTP connection" | tail -5
  echo
  echo "--- registered UEs (from UE log) ---"
  dc exec -T ueransim sh -c \
    "grep 'Initial Registration is successful' /tmp/ue.log 2>/dev/null | grep -oE '20893[0-9]+' | sort -u"
  echo
  echo -n "count registered: "
  dc exec -T ueransim sh -c \
    "grep 'Initial Registration is successful' /tmp/ue.log 2>/dev/null | grep -oE '20893[0-9]+' | sort -u | wc -l"
  echo
  echo "--- per-UE state via nr-cli (GROUND TRUTH — see note in 00-summary.md) ---"
  reg=0; conn=0
  for i in $(seq 1 "${UE_COUNT:-20}"); do
    supi="$(printf 'imsi-20893%010d' "$i")"
    out="$(dc exec -T ueransim ./nr-cli "$supi" -e status 2>/dev/null | tr -d '\r')"
    rm_state="$(printf '%s' "$out" | awk -F': ' '/^rm-state/{print $2}')"
    cm_state="$(printf '%s' "$out" | awk -F': ' '/^cm-state/{print $2}')"
    printf '%s  rm=%s  cm=%s\n' "$supi" "${rm_state:-?}" "${cm_state:-?}"
    [ "$rm_state" = "RM-REGISTERED" ] && reg=$((reg+1))
    [ "$cm_state" = "CM-CONNECTED" ] && conn=$((conn+1))
  done
  echo
  echo "RM-REGISTERED : $reg / ${UE_COUNT:-20}"
  echo "CM-CONNECTED  : $conn / ${UE_COUNT:-20}"
  echo "$reg" > "$OUT_DIR/.reg_count"
} > "$OUT_DIR/05-ue-registration.txt"

# --- 6. NF Prometheus metrics --------------------------------------------
for nf in amf smf nssf pcf ausf udm udr nrf; do
  curl_net "http://$nf.free5gc.org:9091/metrics" > "$OUT_DIR/06-metrics-$nf.txt"
done

# --- 7. observability stack (if running) ---------------------------------
{
  echo "--- prometheus target health ---"
  curl -s --max-time 8 "http://localhost:9090/api/v1/targets?state=active" \
    | tr ',' '\n' | grep -E '"(nf|health|scrapeUrl)"' || echo "(prometheus not reachable on :9090)"
  echo
  echo "--- up{job=free5gc} ---"
  curl -sG --max-time 8 --data-urlencode 'query=count(up{job="free5gc"} == 1)' \
    http://localhost:9090/api/v1/query 2>/dev/null || true
  echo
  echo "--- selected dashboard queries ---"
  for q in 'free5gc_amf_business_ue_connectivity{access_type="3GPP_ACCESS"}' \
           'sum(free5gc_nas_msg_received_total{name="RegistrationRequest"})' \
           'sum by (nf_type) (free5gc_sbi_inbound_request_total)'; do
    echo "  query: $q"
    curl -sG --max-time 8 --data-urlencode "query=$q" http://localhost:9090/api/v1/query 2>/dev/null | head -c 600
    echo; echo
  done
} > "$OUT_DIR/07-prometheus.txt" 2>&1

{
  echo "--- grafana health ---"
  curl -s --max-time 8 http://localhost:3000/api/health || echo "(grafana not reachable on :3000)"
  echo
  echo "--- provisioned dashboards ---"
  curl -s --max-time 8 "http://localhost:3000/api/search?query=free5gc" || true
  echo
  echo "--- datasources ---"
  curl -s --max-time 8 http://localhost:3000/api/datasources | head -c 400 || true
} > "$OUT_DIR/08-grafana.txt" 2>&1

# --- 8. summary -----------------------------------------------------------
REG_COUNT="$(dc exec -T ueransim sh -c "grep 'Initial Registration is successful' /tmp/ue.log 2>/dev/null | grep -oE '20893[0-9]+' | sort -u | wc -l" | tr -d ' \r')"
SUB_COUNT="$(dc exec -T db mongo free5gc --quiet --eval 'print(db["subscriptionData.provisionedData.amData"].count())' | tr -d ' \r')"
NF_COUNT="$(dc exec -T db mongo free5gc --quiet --eval 'print(db.NfProfile.count())' | tr -d ' \r')"
CM_CONNECTED="$(grep 'state="cm-connected"' "$OUT_DIR/06-metrics-amf.txt" 2>/dev/null | grep '3GPP_ACCESS' | head -1 | awk '{print $NF}')"
UE_CONNECTIVITY="$(grep '^free5gc_amf_business_ue_connectivity{access_type="3GPP_ACCESS"}' "$OUT_DIR/06-metrics-amf.txt" 2>/dev/null | awk '{print $NF}')"
REG_TRUTH="$(cat "$OUT_DIR/.reg_count" 2>/dev/null || echo '?')"
TARGETS_UP="$(grep -c '"health":"up"' "$OUT_DIR/07-prometheus.txt" 2>/dev/null || echo 0)"
rm -f "$OUT_DIR/.reg_count"

cat > "$OUT_DIR/00-summary.md" <<EOF
# Deployment evidence — $TS

Captured by \`scripts/collect_evidence.sh\` on a **non-baseline** host (WSL2; see
\`01-environment.txt\`). The UPF/data path is intentionally absent — gtp5g requires a
conforming ODE (SPEC ADR-003). Registration is the success criterion here.

| Metric | Value | Source |
|---|---|---|
| NF profiles registered in NRF | $NF_COUNT | MongoDB \`NfProfile\` |
| Subscribers provisioned | $SUB_COUNT | MongoDB \`amData\` |
| **UEs RM-REGISTERED (ground truth)** | **$REG_TRUTH** | \`nr-cli … -e status\` |
| UEs logging "Initial Registration is successful" | $REG_COUNT | UE log |
| Prometheus targets healthy | $TARGETS_UP | Prometheus API |
| AMF \`ue_connectivity{3GPP}\` | ${UE_CONNECTIVITY:-n/a} | AMF exporter (unreliable — see below) |
| AMF \`ue_cm_gmm_state_count{cm-connected}\` | ${CM_CONNECTED:-n/a} | AMF exporter (unreliable — see below) |

## Files

| File | Contents |
|---|---|
| \`01-environment.txt\` | host, versions, ODE conformance report |
| \`02-containers.txt\` | running containers + status |
| \`03-nrf-registrations.txt\` | every NF's NRF registration status |
| \`04-subscribers.txt\` | subscriber counts, PLMN, slice, SUPI list |
| \`05-ue-registration.txt\` | gNB NG setup, registered SUPIs, per-UE \`nr-cli\` state |
| \`06-metrics-<nf>.txt\` | raw Prometheus exposition from each NF's \`:9091/metrics\` |

| \`07-prometheus.txt\` | target health + selected dashboard queries |
| \`08-grafana.txt\` | Grafana health, provisioned dashboards, datasources |

## Notes

- **The AMF business gauges do not match ground truth on free5GC v4.2.0.**
  \`ue_connectivity\` undercounts and \`ue_cm_gmm_state_count{cm-connected}\` overcounts; both
  leak when procedures abort (T3510/T3580) or the AMF restarts. Use the \`nr-cli\` figure in
  \`05-ue-registration.txt\` as the UE population. Counter series
  (\`*_msg_received_total\`, \`*_sbi_inbound_request_total\`) and \`up\` are reliable.
  See \`observability/README.md\`.
- PDU session establishment fails by design here (T3580) — no UPF, so
  \`/nsmf-pdusession/v1/sm-contexts\` returns 500. Expected on a non-baseline host.
EOF

echo "done."
echo "summary: $OUT_DIR/00-summary.md"
echo "  NFs registered   : $NF_COUNT"
echo "  subscribers      : $SUB_COUNT"
echo "  UEs registered   : $REG_COUNT"
