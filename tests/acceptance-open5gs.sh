#!/usr/bin/env bash
# acceptance-open5gs.sh — SPEC §5 acceptance criteria, checked on the Open5GS stack.
#
# Same structure and vocabulary as tests/acceptance.sh (free5GC): PASS / FAIL / SKIP-ODE / GAP,
# plus OWNER for a step that is the project owner's decision rather than a technical check.
# The difference is the user plane. The criteria free5GC reports as SKIP-ODE only because gtp5g
# cannot load here — PDU session, UE addressing, end-to-end user plane, a PDU-session diagram
# from observed behaviour, teardown/rebuild — are TESTED here (ADR-010).
#
# Criteria that are about the Official Development Environment itself (the bare-metal
# VERSIONS.lock freeze) remain SKIP-ODE: a laptop cannot satisfy them for any core.
#
# Usage: tests/acceptance-open5gs.sh        (the stack must be running: make o5gs-up)

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
export NO_COLOR=1

pass=0; fail=0; skip=0; gap=0; owner=0
ok()    { printf '  [ PASS ] %s\n' "$1"; pass=$((pass+1)); }
no()    { printf '  [ FAIL ] %s\n' "$1"; fail=$((fail+1)); }
ode()   { printf '  [SKIP-ODE] %s\n' "$1"; skip=$((skip+1)); }
gapd()  { printf '  [ GAP  ] %s\n' "$1"; gap=$((gap+1)); }
own()   { printf '  [ OWNER] %s\n' "$1"; owner=$((owner+1)); }
metric() { docker exec o5gs-ue curl -s --max-time 5 "http://$1/metrics" 2>/dev/null; }

echo "==================================================================="
echo "Open5GS acceptance checks — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==================================================================="

echo; echo "-- Environment & reproducibility --"
pin=$(python3 -c "import json;print(next(d['commit'] for d in json.load(open('manifest.lock'))['dependencies'] if d['name']=='open5gs'))" 2>/dev/null)
[[ "$pin" =~ ^[0-9a-f]{40}$ ]] && ok "Open5GS pinned to a full SHA in manifest.lock ($pin)" || no "Open5GS not pinned to a 40-char SHA"
bash scripts/bootstrap.sh --verify-only >/dev/null 2>&1 && ok "bootstrap --verify-only: every upstream matches manifest.lock" || no "bootstrap --verify-only failed"
[ -z "$(git ls-files external/)" ] && ok "external/ is not tracked by git" || no "external/ has tracked files"
built=$(docker exec o5gs-amf cat /opt/open5gs/COMMIT 2>/dev/null)
[ "$built" = "$pin" ] && ok "running Open5GS binaries were built from the pinned commit" || no "running binaries report commit '$built', pin is $pin"
lbl=$(docker image inspect o5gs/ueransim:v3.3.0-udpbuf --format '{{index .Config.Labels "io.5g-observability.patches"}}' 2>/dev/null)
[ -n "$lbl" ] && [ -f deployments/open5gs/images/patches/ueransim-udp-socket-buffers.patch ] \
  && ok "UERANSIM image built from the pin with its recorded local patch ($lbl, ADR-012)" \
  || no "UERANSIM image or its patch record missing"
tuns=$(for u in o5gs-upf-embb o5gs-upf-urllc; do docker exec "$u" sh -c 'ls /sys/class/net | grep -c ogstun' 2>/dev/null; done | paste -sd+ | bc 2>/dev/null)
[ "${tuns:-0}" -ge 2 ] && ok "user plane runs with no kernel module (userspace UPF over TUN, one per slice)" || no "UPF TUN devices missing"
if [ -f VERSIONS.lock ] && ! grep -q "TO_BE_FILLED_ON_LAB_ODE" VERSIONS.lock; then ok "VERSIONS.lock frozen"
else ode "VERSIONS.lock environment freeze (bare-metal ODE values, not a property of the core)"; fi

echo; echo "-- Core bring-up --"
want="o5gs-mongodb o5gs-nrf o5gs-scp o5gs-ausf o5gs-udm o5gs-udr o5gs-pcf o5gs-bsf o5gs-nssf o5gs-amf o5gs-smf o5gs-upf-embb o5gs-upf-urllc o5gs-gnb-embb o5gs-gnb-urllc o5gs-ue o5gs-dn-embb o5gs-dn-urllc o5gs-prometheus o5gs-grafana"
down=""; for c in $want; do [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || down="$down $c"; done
[ -z "$down" ] && ok "all $(echo $want | wc -w) containers running" || no "not running:$down"
types=$(docker exec o5gs-ue sh -c 'for h in $(curl -s --http2-prior-knowledge --max-time 5 http://10.53.0.10:7777/nnrf-nfm/v1/nf-instances | grep -oE "nf-instances/[0-9a-f-]+"); do curl -s --http2-prior-knowledge --max-time 5 "http://10.53.0.10:7777/nnrf-nfm/v1/$h" | grep -oE "\"nfType\":\"[A-Z]+\""; done' 2>/dev/null | sort -u | grep -oE '[A-Z]{3,4}' | tr '\n' ' ')
missing=""; for t in AMF SMF AUSF UDM UDR PCF BSF NSSF; do echo " $types " | grep -q " $t " || missing="$missing $t"; done
[ -z "$missing" ] && ok "NRF holds registrations for AMF SMF AUSF UDM UDR PCF BSF NSSF (queried over the NRF API)" || no "NRF registrations missing:$missing (found: $types)"
gnb_ok=0; for g in o5gs-gnb-embb o5gs-gnb-urllc; do
  docker logs --since "$(docker inspect -f '{{.State.StartedAt}}' "$g")" "$g" 2>&1 | grep -c "NG Setup procedure is successful" | grep -qv '^0$' && gnb_ok=$((gnb_ok+1))
done
[ "$gnb_ok" -eq 2 ] && ok "both slice gNBs completed NG Setup with the AMF" || no "only $gnb_ok/2 gNBs completed NG Setup"
peers=$(metric 10.53.0.4:9090 | awk '/^pfcp_peers_active/ {print $2}')
[ "${peers:-0}" -ge 2 ] && ok "SMF associated over N4 with both slice UPFs ($peers PFCP peers)" || no "SMF PFCP peers: ${peers:-none}"
subs=$(docker exec o5gs-mongodb mongo open5gs --quiet --eval 'var r={}; db.subscribers.find().forEach(function(d){d.slice.forEach(function(s){if(s.default_indicator){var k=s.sst+"/"+s.sd; r[k]=(r[k]||0)+1;}})}); print(db.subscribers.count()+" "+JSON.stringify(r))' 2>/dev/null)
[ "${subs%% *}" = "20" ] && echo "$subs" | grep -q '"1/010203":10' && echo "$subs" | grep -q '"2/112233":10' \
  && ok "20 subscribers, 10 per home slice ($subs)" || no "subscribers: ${subs:-unreadable}"
targets=$(curl -s --max-time 5 http://localhost:9091/api/v1/targets | python3 -c "import json,sys
t=[x for x in json.load(sys.stdin)['data']['activeTargets'] if x['labels']['job']!='slice-orchestrator']
print(sum(x['health']=='up' for x in t), len(t))" 2>/dev/null)
set -- ${targets:-0 0}
[ "$1" -gt 0 ] && [ "$1" = "$2" ] && ok "Prometheus: $1/$2 stack targets up" || no "Prometheus targets up: ${1}/${2}"
gapd "structured JSON logs — Open5GS writes text logs; not configurable, same gap as free5GC (ADR-005, docs/logging.md)"

echo; echo "-- End-to-end function --"
reg=$(docker exec o5gs-ue sh -c "grep -l 'Initial Registration is successful' /tmp/ue-2089*.log 2>/dev/null | wc -l" | tr -d ' ')
[ "${reg:-0}" -ge 20 ] && ok "UE registration: $reg/20 UEs registered" || no "UE registration: ${reg:-0}/20"
live=$(docker exec o5gs-ue sh -c 'for f in /tmp/ue-2089*.log; do a=$(sed -n "s/.*TUN interface\[uesimtun[0-9]*, \([0-9.]*\)\].*/\1/p" $f | tail -1); [ -n "$a" ] && ip -4 -o addr show | grep -q " $a/" && echo "$a"; done' 2>/dev/null)
na=$(echo "$live" | grep -c '^10\.45\.'); nb=$(echo "$live" | grep -c '^10\.46\.')
[ "$na" -ge 10 ] && [ "$nb" -ge 10 ] && ok "PDU session established, UE addressed from its slice's pool: eMBB $na/10, URLLC $nb/10" \
  || no "PDU sessions with a live address: eMBB $na/10, URLLC $nb/10"
if bash scripts/open5gs/verify_user_plane.sh > /tmp/acc-up.txt 2>&1; then
  ok "end-to-end user plane: ping and iperf3 through the UPF on both slices, internet egress ($(grep -oE 'PASS=[0-9]+ +FAIL=[0-9]+' /tmp/acc-up.txt))"
else no "user-plane verification failed: $(grep 'FAIL ]' /tmp/acc-up.txt | head -2 | tr -s ' ')"; fi
s_embb=$(metric 10.53.0.7:9090 | awk '/^fivegs_upffunction_upf_sessionnbr/ {print $2}')
s_urllc=$(metric 10.53.0.8:9090 | awk '/^fivegs_upffunction_upf_sessionnbr/ {print $2}')
[ "${s_embb:-0}" -gt 0 ] && [ "${s_urllc:-0}" -gt 0 ] && ok "each slice's traffic is carried by its own UPF (sessions: eMBB UPF $s_embb, URLLC UPF $s_urllc — presence, ADR-011)" \
  || no "per-slice UPF sessions: eMBB ${s_embb:-none}, URLLC ${s_urllc:-none}"
python3 tools/check_open5gs_slices.py >/dev/null 2>&1 && ok "slices consistent across AMF, NSSF, gNBs, SMF routing and UPF pools" || no "slice configuration inconsistent (tools/check_open5gs_slices.py)"
if python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json > /tmp/acc-gate.txt 2>&1; then
  ok "KPI gate passes ($(grep -oE '[0-9]+ passed +[0-9]+ failed +[0-9]+ advisory' /tmp/acc-gate.txt))"
else no "KPI gate failed: $(grep -oE '[0-9]+ passed +[0-9]+ failed' /tmp/acc-gate.txt)"; fi

echo; echo "-- Understanding & documentation --"
[ -s docs/interfaces/interface-catalog.md ] && ok "Interface Catalog authored" || no "Interface Catalog missing"
d=diagrams/open5gs-pdu-session-establishment.mmd
if [ -s "$d" ] && ! grep -q "DESIGN ONLY" "$d" && [ -s docs/evidence/open5gs-pdu-session-capture/capture.pcap ]; then
  ok "PDU Session sequence diagram authored from observed behaviour, including the data path (capture kept)"
else no "Open5GS PDU Session diagram or its capture missing"; fi
[ -s docs/open5gs.md ] && ok "Open5GS runbook present (docs/open5gs.md)" || no "docs/open5gs.md missing"
latest=$(ls -1 docs/evidence/open5gs-rebuild/rebuild-*.txt 2>/dev/null | tail -1)
if [ -n "$latest" ] && grep -q "^RESULT: PASS" "$latest"; then
  ok "teardown (down -v) and rebuild return to a working stack — $(basename "$latest")"
else no "no passing teardown/rebuild evidence (run tests/open5gs-rebuild.sh --yes)"; fi

echo; echo "-- Freeze --"
tag=$(git tag --points-at HEAD | grep -E '^open5gs-baseline' | head -1)
if [ -n "$tag" ]; then ok "baseline tagged ($tag)"
else own "baseline not tagged — freezing a baseline is the project owner's decision (git tag open5gs-baseline-<date>)"; fi

echo
echo "==================================================================="
printf 'PASS=%d  FAIL=%d  SKIP-ODE=%d  GAP=%d  OWNER=%d\n' "$pass" "$fail" "$skip" "$gap" "$owner"
echo "==================================================================="
if [ "$fail" -gt 0 ]; then echo "RESULT: FAIL — runnable criteria did not all pass."; exit 1; fi
echo "RESULT: PASS (non-baseline host) — every runnable criterion passed."
[ $((skip + gap + owner)) -gt 0 ] && echo "        Not complete: $skip need the ODE, $gap documented gap(s), $owner owner decision(s)."
exit 0
