#!/usr/bin/env bash
# provision_subscribers.sh — bulk-provision test subscribers into free5GC.
#
# Uses the WebUI (webconsole) REST API rather than writing MongoDB directly, so
# free5GC owns the document schema and we stay correct across version bumps.
#
# All values are tunables (ADR-001 guardrail: config/env, never baked in). The
# slice and credentials MUST match ran/config/free5gc-ue.yaml or registration
# will fail — see the slice-consistency audit in docs/runbooks/m0-report.md.
#
# Usage:
#   scripts/provision_subscribers.sh              # provision COUNT subscribers
#   COUNT=5 scripts/provision_subscribers.sh      # override count
#   scripts/provision_subscribers.sh --list       # list current subscribers
#   scripts/provision_subscribers.sh --delete-all # remove all subscribers
#
# Exit 0 when every requested subscriber is present, non-zero otherwise.

set -uo pipefail

COUNT="${COUNT:-20}"
# START lets subscribers be provisioned in ranges, so different ranges can sit on different
# slices (Phase 1.5). e.g. COUNT=10 provisions 1..10; COUNT=10 START=11 provisions 11..20.
START="${START:-1}"
WEBUI_URL="${WEBUI_URL:-http://localhost:5000}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-free5gc}"

MCC="${MCC:-208}"
MNC="${MNC:-93}"
PLMN="${MCC}${MNC}"

SST="${SST:-1}"
SD="${SD:-010203}"
DNN="${DNN:-internet}"

# Credentials shared by all test UEs (matches ran/config/free5gc-ue.yaml).
KEY="${KEY:-8baf473f2f8fd09487cccbd7097c6862}"
OPC="${OPC:-8e27b6af0e692e750f32667a3b14605d}"
AMF_FIELD="${AMF_FIELD:-8000}"
# Starting SQN must be LOW. free5GC's stock value (16f3b3f70fc2 ~ 2.5e13) sits far
# beyond the 5G-AKA acceptance window (~2^28) ahead of a fresh UE, whose SQN-MS starts
# at 0, so every first-time UERANSIM UE answers "Authentication Failure due to SQN out
# of range" — and free5GC's AUTS re-sync then fails with "Re-Sync MAC failed", so it
# never recovers. A small value authenticates cleanly on the first attempt.
SQN="${SQN:-000000000020}"

# GPSI (MSISDN) must be UNIQUE per subscriber — free5GC rejects duplicates with
# {"cause":"duplicate gpsi"}. Derived from the index, not shared like the key/OPc.
GPSI_BASE="${GPSI_BASE:-900000000}"

# free5GC keys slice-scoped maps by "<sst as 2-hex><sd>", e.g. sst=1 sd=010203 -> 01010203
SNSSAI_KEY="$(printf '%02x%s' "$SST" "$SD")"

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 2; }

login() {
  local resp
  resp="$(curl -s --max-time 15 -X POST "$WEBUI_URL/api/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")"
  printf '%s' "$resp" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4
}

supi_for() { printf 'imsi-%s%010d' "$PLMN" "$1"; }

subscriber_json() {
  local supi="$1" num="$2"
  local gpsi
  gpsi="msisdn-$(printf '%010d' $((GPSI_BASE + num)))"
  cat <<JSON
{
  "userNumber": $num,
  "ueId": "$supi",
  "plmnID": "$PLMN",
  "AuthenticationSubscription": {
    "authenticationManagementField": "$AMF_FIELD",
    "authenticationMethod": "5G_AKA",
    "milenage": { "op": { "encryptionAlgorithm": 0, "encryptionKey": 0, "opValue": "" } },
    "opc": { "encryptionAlgorithm": 0, "encryptionKey": 0, "opcValue": "$OPC" },
    "permanentKey": { "encryptionAlgorithm": 0, "encryptionKey": 0, "permanentKeyValue": "$KEY" },
    "sequenceNumber": "$SQN"
  },
  "AccessAndMobilitySubscriptionData": {
    "gpsis": ["$gpsi"],
    "nssai": { "defaultSingleNssais": [{ "sst": $SST, "sd": "$SD" }], "singleNssais": [] },
    "subscribedUeAmbr": { "downlink": "2 Gbps", "uplink": "1 Gbps" }
  },
  "SessionManagementSubscriptionData": [
    {
      "singleNssai": { "sst": $SST, "sd": "$SD" },
      "dnnConfigurations": {
        "$DNN": {
          "5gQosProfile": { "5qi": 9, "arp": { "preemptCap": "", "preemptVuln": "", "priorityLevel": 8 }, "priorityLevel": 8 },
          "pduSessionTypes": { "allowedSessionTypes": ["IPV4"], "defaultSessionType": "IPV4" },
          "sessionAmbr": { "downlink": "200 Mbps", "uplink": "200 Mbps" },
          "sscModes": { "allowedSscModes": ["SSC_MODE_2","SSC_MODE_3"], "defaultSscMode": "SSC_MODE_1" }
        }
      }
    }
  ],
  "SmfSelectionSubscriptionData": {
    "subscribedSnssaiInfos": { "$SNSSAI_KEY": { "dnnInfos": [{ "dnn": "$DNN" }] } }
  },
  "AmPolicyData": { "subscCats": ["free5gc"] },
  "SmPolicyData": {
    "smPolicySnssaiData": {
      "$SNSSAI_KEY": {
        "snssai": { "sst": $SST, "sd": "$SD" },
        "smPolicyDnnData": { "$DNN": { "dnn": "$DNN" } }
      }
    }
  },
  "FlowRules": [],
  "QosFlows": [],
  "ChargingDatas": []
}
JSON
}

count_subscribers() {
  local tok="$1"
  curl -s --max-time 15 "$WEBUI_URL/api/subscriber" -H "Token: $tok" \
    | grep -o '"ueId"' | wc -l | tr -d ' '
}

TOKEN="$(login)"
if [ -z "${TOKEN:-}" ]; then
  echo "ERROR: login to $WEBUI_URL failed (is the WebUI up? creds $ADMIN_USER/****)" >&2
  exit 1
fi

case "${1:-}" in
  --list)
    echo "subscribers currently provisioned: $(count_subscribers "$TOKEN")"
    curl -s "$WEBUI_URL/api/subscriber" -H "Token: $TOKEN" | grep -o '"ueId":"[^"]*"' | cut -d'"' -f4 | sort
    exit 0
    ;;
  --delete-all)
    echo "deleting all subscribers ..."
    for supi in $(curl -s "$WEBUI_URL/api/subscriber" -H "Token: $TOKEN" | grep -o '"ueId":"[^"]*"' | cut -d'"' -f4); do
      code="$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$WEBUI_URL/api/subscriber/$supi/$PLMN" -H "Token: $TOKEN")"
      echo "  deleted $supi (http $code)"
    done
    echo "remaining: $(count_subscribers "$TOKEN")"
    exit 0
    ;;
esac

END=$((START + COUNT - 1))
echo "==================================================================="
echo "provisioning $COUNT subscriber(s)  [index $START..$END]"
echo "  PLMN     : $MCC/$MNC"
echo "  slice    : SST $SST / SD $SD   (key $SNSSAI_KEY)"
echo "  DNN      : $DNN"
echo "  webui    : $WEBUI_URL"
echo "==================================================================="

ok=0; fail=0
for i in $(seq "$START" "$END"); do
  supi="$(supi_for "$i")"
  body="$(subscriber_json "$supi" "$i")"
  resp_body="$(mktemp)"
  code="$(printf '%s' "$body" | curl -s -o "$resp_body" -w '%{http_code}' \
            -X POST "$WEBUI_URL/api/subscriber/$supi/$PLMN" \
            -H 'Content-Type: application/json' -H "Token: $TOKEN" -d @-)"
  case "$code" in
    2*)  echo "  [ OK ] $supi (http $code)"; ok=$((ok+1)) ;;
    409) echo "  [ OK ] $supi (already provisioned)"; ok=$((ok+1)) ;;
    *)   echo "  [FAIL] $supi (http $code) $(head -c 200 "$resp_body")"; fail=$((fail+1)) ;;
  esac
  rm -f "$resp_body"
done

total="$(count_subscribers "$TOKEN")"
echo "-------------------------------------------------------------------"
echo "created/updated: $ok   failed: $fail   total now in DB: $total"

if [ "$fail" -eq 0 ] && [ "$total" -ge "$COUNT" ]; then
  echo "PROVISION: PASS"
  exit 0
fi
echo "PROVISION: FAIL"
exit 1
