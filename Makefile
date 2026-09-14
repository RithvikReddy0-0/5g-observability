# Makefile — one-word commands for the whole stack.
#
# Run from WSL/Linux:   make up      make ues      make status      make down
#
# Every target encodes the traps this project hit, so you don't have to remember them:
#   * the SMF must start with --no-deps or Compose drags in the UPF (which can't work here)
#   * the gNB exits if the AMF isn't resolvable yet, so it gets retried
#   * subscribers MUST be re-provisioned right before launching UEs (SQN drift)
#   * Docker Desktop drops the `docker compose` plugin when it restarts, so anything that
#     can be done with plain `docker` is done with plain `docker`

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE_DIR := deployments/compose
OBS_DIR     := observability

# Core containers, in start order. These are container names, not compose service names.
CORE_DB   := mongodb nrf
CORE_NF   := amf ausf udm udr pcf nssf smf webui
OBS       := prometheus grafana
ALL       := $(CORE_DB) $(CORE_NF) ueransim $(OBS)

# `docker compose` vanishes whenever Docker Desktop restarts; detect it per-invocation.
HAVE_COMPOSE = $$(docker compose version >/dev/null 2>&1 && echo yes || echo no)

# Slice layout
SLICE_A := 010203
SLICE_B := 112233
N_A     := 10
N_B     := 10

.PHONY: help up create down restart stop-ues ues status test evidence screenshots report \
        bootstrap verify clean logs urls nuke gate gate-test \n        o5gs-build o5gs-up o5gs-ues o5gs-status o5gs-ping o5gs-traffic o5gs-gate o5gs-evidence o5gs-urls \n        o5gs-down o5gs-logs o5gs-clean

help: ## Show this help
	@echo ""
	@echo "  5G Observability — available commands"
	@echo "  ────────────────────────────────────────────────────────────────"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Typical session:   make up  →  make ues  →  make status"
	@echo ""

## ─────────────────────────── running the stack ───────────────────────────

up: ## Start everything (core + monitoring + slice exporter)
	@echo "▶ starting database and NRF ..."
	@docker start $(CORE_DB) >/dev/null 2>&1 || $(MAKE) --no-print-directory create
	@sleep 12
	@echo "▶ starting network functions ..."
	@docker start $(CORE_NF) >/dev/null 2>&1 || true
	@sleep 25
	@echo "▶ starting monitoring ..."
	@docker start $(OBS) >/dev/null 2>&1 || true
	@sleep 5
	@echo "▶ starting gNB (retrying past the DNS race) ..."
	@for i in 1 2 3 4; do \
	    docker start ueransim >/dev/null 2>&1; sleep 14; \
	    if docker logs --tail 40 ueransim 2>&1 | grep -q "NG Setup procedure is successful"; then \
	        echo "  gNB connected to the AMF"; break; \
	    fi; \
	    echo "  attempt $$i: gNB not up yet"; \
	done
	@echo "▶ starting slice exporter and orchestrator ..."
	@bash scripts/start_slice_exporter.sh >/dev/null 2>&1 || true
	@bash scripts/start_orchestrator.sh >/dev/null 2>&1 || true
	@echo ""
	@$(MAKE) --no-print-directory status

create: ## First-time creation of containers (needs the docker compose plugin)
	@if [ "$(HAVE_COMPOSE)" != "yes" ]; then \
	    echo "✗ 'docker compose' is unavailable — start Docker Desktop and wait for it to finish loading."; \
	    exit 1; \
	fi
	@echo "▶ creating containers ..."
	cd $(COMPOSE_DIR) && docker compose up -d db free5gc-nrf
	@sleep 10
	cd $(COMPOSE_DIR) && docker compose up -d free5gc-amf free5gc-ausf free5gc-udm \
	    free5gc-udr free5gc-pcf free5gc-nssf free5gc-webui
	@sleep 15
	cd $(COMPOSE_DIR) && docker compose up -d --no-deps free5gc-smf ueransim
	cd $(OBS_DIR) && docker compose up -d

down: ## Stop everything (keeps all data)
	@echo "▶ stopping UEs, slice exporter and orchestrator ..."
	@bash scripts/start_slice_exporter.sh --stop >/dev/null 2>&1 || true
	@bash scripts/start_orchestrator.sh --stop >/dev/null 2>&1 || true
	@docker exec -i ueransim pkill nr-ue >/dev/null 2>&1 || true
	@echo "▶ stopping containers ..."
	@docker stop $(ALL) >/dev/null 2>&1 || true
	@echo "✓ stopped — data preserved, 'make up' brings it straight back"

restart: down up ## Stop then start everything

## ─────────────────────────── devices (UEs) ───────────────────────────────

ues: ## Re-provision SIMs and connect 20 devices across both slices
	@echo "▶ re-provisioning subscribers (resets the SQN counter — required) ..."
	@bash scripts/provision_subscribers.sh --delete-all >/dev/null 2>&1 || true
	@set -a; . deployments/slices.env; set +a; \
	 COUNT=$$SLICE_A_SUBSCRIBERS START=1 SST=$$SLICE_A_SST SD=$$SLICE_A_SD \
	   ALSO_NSSAI="{\"sst\":$$SLICE_B_SST,\"sd\":\"$$SLICE_B_SD\"}" \
	   FIVEQI=$$SLICE_A_5QI ARP_PRIORITY=$$SLICE_A_ARP_PRIORITY \
	   ARP_PREEMPT_CAP="$$SLICE_A_ARP_PREEMPT_CAP" ARP_PREEMPT_VULN="$$SLICE_A_ARP_PREEMPT_VULN" \
	   SESSION_AMBR_DL="$$SLICE_A_SESSION_AMBR_DL" SESSION_AMBR_UL="$$SLICE_A_SESSION_AMBR_UL" \
	   UE_AMBR_DL="$$SLICE_A_UE_AMBR_DL" UE_AMBR_UL="$$SLICE_A_UE_AMBR_UL" \
	   bash scripts/provision_subscribers.sh | tail -1; \
	 COUNT=$$SLICE_B_SUBSCRIBERS START=$$(($$SLICE_A_SUBSCRIBERS+1)) SST=$$SLICE_B_SST SD=$$SLICE_B_SD \
	   ALSO_NSSAI="{\"sst\":$$SLICE_A_SST,\"sd\":\"$$SLICE_A_SD\"}" \
	   FIVEQI=$$SLICE_B_5QI ARP_PRIORITY=$$SLICE_B_ARP_PRIORITY \
	   ARP_PREEMPT_CAP="$$SLICE_B_ARP_PREEMPT_CAP" ARP_PREEMPT_VULN="$$SLICE_B_ARP_PREEMPT_VULN" \
	   SESSION_AMBR_DL="$$SLICE_B_SESSION_AMBR_DL" SESSION_AMBR_UL="$$SLICE_B_SESSION_AMBR_UL" \
	   UE_AMBR_DL="$$SLICE_B_UE_AMBR_DL" UE_AMBR_UL="$$SLICE_B_UE_AMBR_UL" \
	   bash scripts/provision_subscribers.sh | tail -1
	@echo "▶ making sure the slice-B config is inside the container ..."
	@docker cp $(COMPOSE_DIR)/config/uecfg-slice-b.yaml ueransim:/ueransim/config/uecfg-slice-b.yaml >/dev/null 2>&1 || true
	@echo "▶ connecting devices ..."
	@COUNT=$(N_A) COUNT_B=$(N_B) TEMPO=600 SETTLE=85 bash scripts/start_ues.sh

stop-ues: ## Disconnect all devices
	@bash scripts/start_ues.sh --stop

## ─────────────────────── slice allocation (orchestrator) ─────────────────

traffic: ## Send traffic demands and watch them routed to slices by requirement
	@bash scripts/start_orchestrator.sh >/dev/null 2>&1 || true
	@cd tools/slice-orchestrator && DURATION=$${DURATION:-30} RATE=$${RATE:-5} python3 traffic_gen.py

slices: ## Show slice capacity, utilisation and how traffic was allocated
	@bash scripts/start_orchestrator.sh >/dev/null 2>&1 || true
	@echo "── slice definitions & load ───────────────────────────────────"
	@curl -s --max-time 20 http://localhost:9110/metrics 2>/dev/null \
	    | grep -E '^slice_(capacity|allocated|utilization)' | sed 's/^/  /' \
	    || echo "  (orchestrator not reachable)"
	@echo "── allocation decisions ───────────────────────────────────────"
	@curl -s --max-time 20 http://localhost:9110/metrics 2>/dev/null \
	    | grep -E '^slice_(admitted|rejected)' | sed 's/^/  /' || true

## ─────────────────────────── checking things ─────────────────────────────

status: ## Show what's running and how many devices are connected
	@echo "── containers ─────────────────────────────────────────────────"
	@docker ps --format '  {{.Names}}' 2>/dev/null | sort | tr '\n' ' '; echo
	@echo "── network functions registered ───────────────────────────────"
	@docker exec -i mongodb mongo free5gc --quiet \
	    --eval 'print("  " + db.NfProfile.distinct("nfType").sort().join(" "))' 2>/dev/null \
	    || echo "  (database not reachable)"
	@echo "── devices ────────────────────────────────────────────────────"
	@COUNT=$(N_A) COUNT_B=$(N_B) bash scripts/start_ues.sh --status 2>/dev/null | sed 's/^/  /' \
	    || echo "  (not running)"
	@echo "── slice metrics ──────────────────────────────────────────────"
	@curl -s --max-time 60 http://localhost:9105/metrics 2>/dev/null \
	    | grep '^free5gc_slice_provisioned' | sed 's/^/  /' || echo "  (exporter not running)"
	@echo ""
	@$(MAKE) --no-print-directory urls

test: ## Run the acceptance checks
	@bash tests/acceptance.sh

gate: ## Check the running deployment against the KPI thresholds (fails if breached)
	@python3 tools/kpi-gate/kpi_gate.py --wait 20

gate-test: ## Prove the KPI gate can fail (no running stack needed)
	@python3 tools/kpi-gate/kpi_gate.py --self-test

verify: ## Check the machine and that pinned versions haven't drifted
	@bash scripts/verify_env.sh || true
	@echo ""
	@bash scripts/bootstrap.sh --verify-only

urls: ## Show the web addresses
	@echo "  Grafana        http://localhost:3000   (no login needed)"
	@echo "  Prometheus     http://localhost:9090"
	@echo "  free5GC WebUI  http://localhost:5000   (admin / free5gc)"
	@echo "  Slice metrics  http://localhost:9105/metrics"

logs: ## Follow the AMF log (Ctrl-C to stop). Use: make logs C=smf
	@docker logs -f --tail 50 $${C:-amf}

## ─────────────────────────── setup & artifacts ───────────────────────────

bootstrap: ## Download the pinned free5GC / UERANSIM / gtp5g sources
	@bash scripts/bootstrap.sh

evidence: ## Save a timestamped evidence bundle
	@bash scripts/collect_evidence.sh

screenshots: ## Capture PNG screenshots of the dashboards
	@bash scripts/capture_screenshots.sh

report: ## Rebuild the shareable PDF report
	@docker run --rm -v "$$(pwd)/docs:/work" --entrypoint chromium-browser \
	    zenika/alpine-chrome:latest --headless --disable-gpu --no-sandbox \
	    --no-pdf-header-footer --virtual-time-budget=30000 \
	    --print-to-pdf=/work/5G-Observability-Report.pdf file:///work/report.html >/dev/null 2>&1
	@ls -la docs/5G-Observability-Report.pdf

## ─────────────────────────── Open5GS core (ADR-010) ──────────────────────
# A second, independent stack. Runs beside free5GC on its own network (10.53.0.0/24) with
# its own database; nothing below touches the free5GC containers.

O5GS_DIR      := deployments/open5gs
O5GS_COMMIT    = $$(python3 -c "import json;print(next(d['commit'] for d in json.load(open('$(CURDIR)/manifest.lock'))['dependencies'] if d['name']=='open5gs'))")
UERANSIM_COMMIT = $$(python3 -c "import json;print(next(d['commit'] for d in json.load(open('$(CURDIR)/manifest.lock'))['dependencies'] if d['name']=='ueransim'))")
O5GS_NF       := o5gs-mongodb o5gs-nrf o5gs-scp o5gs-ausf o5gs-udm o5gs-udr o5gs-pcf o5gs-bsf o5gs-nssf o5gs-upf o5gs-smf o5gs-amf o5gs-gnb o5gs-ue \n                 o5gs-prometheus o5gs-grafana

o5gs-build: ## Open5GS: build core + UERANSIM images from the SHAs in manifest.lock
	@cd $(O5GS_DIR)/images && \
	  docker build -f open5gs.Dockerfile  --build-arg OPEN5GS_COMMIT=$(O5GS_COMMIT)   -t o5gs/open5gs:v2.8.0 . && \
	  docker build -f ueransim.Dockerfile --build-arg UERANSIM_COMMIT=$(UERANSIM_COMMIT) -t o5gs/ueransim:v3.3.0 .

o5gs-up: ## Open5GS: start the core and gNB, then attach 20 UEs with PDU sessions
	@if [ "$(HAVE_COMPOSE)" = "yes" ]; then \
	    cd $(O5GS_DIR) && docker compose up -d; \
	else \
	    docker start $(O5GS_NF) >/dev/null || { echo "containers not created yet and the compose plugin is unavailable (it is provided by Docker Desktop's WSL integration) — start Docker Desktop, then retry"; exit 1; }; \
	fi
	@bash scripts/open5gs/start_ues.sh
	@echo ""
	@echo "  Keep this WSL terminal OPEN while the stack runs. Docker runs inside the Ubuntu distro,"
	@echo "  and WSL shuts the distro down ~30-60 s after the last terminal closes, stopping every"
	@echo "  container with it (measured: 11 engine stops in 12 min idle, 0 in 15 min held open)."
	@echo "  The UEs re-attach on their own when it comes back. Grafana: http://localhost:3001"

o5gs-ues: ## Open5GS: re-provision and re-attach the 20 UEs
	@bash scripts/open5gs/start_ues.sh

o5gs-status: ## Open5GS: containers, UEs and their slice addresses
	@docker ps -a --filter name=o5gs- --format "  {{.Names}}\t{{.Status}}" | sort
	@echo ""
	@docker exec o5gs-ue sh -c "ip -4 -o addr show | awk '/uesimtun/ {print \"  \" \$$2 \"  \" \$$4}'" 2>/dev/null || echo "  (no UEs attached)"

o5gs-ping: ## Open5GS: prove the user plane — ping through the UPF from both slices
	@bash scripts/open5gs/verify_user_plane.sh

o5gs-traffic: ## Open5GS: real traffic through both slices at once. Use: make o5gs-traffic T=60 N=3
	@bash scripts/open5gs/traffic.sh $(or $(T),60) $(or $(N),3) $(or $(DIR),down)

o5gs-gate: ## Open5GS: KPI gate against the live stack (fails if a threshold is breached)
	@python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json --wait 20

o5gs-evidence: ## Open5GS: save a timestamped proof bundle (traffic + isolation test, ~4 min)
	@bash scripts/open5gs/collect_evidence.sh $(or $(T),30)

o5gs-urls: ## Open5GS: web addresses
	@echo "  Grafana     http://localhost:3001   (no login; dashboard 'Open5GS — slices, sessions and traffic')"
	@echo "  Prometheus  http://localhost:9091"

o5gs-logs: ## Open5GS: follow an NF log. Use: make o5gs-logs C=smf
	@docker logs -f --tail 50 o5gs-$(or $(C),amf)

o5gs-down: ## Open5GS: stop the stack (keeps subscribers)
	@docker stop $(O5GS_NF) >/dev/null 2>&1 || true
	@echo "✓ Open5GS stopped — 'make o5gs-up' to resume"

o5gs-clean: ## Open5GS: delete its containers and database (free5GC untouched)
	@read -p "Delete the Open5GS containers and subscriber DB? Type yes: " a; [ "$$a" = "yes" ] || { echo "cancelled"; exit 1; }
	@docker rm -f $(O5GS_NF) >/dev/null 2>&1 || true
	@docker volume rm open5gs_o5gs-db open5gs_o5gs-prom open5gs_o5gs-grafana >/dev/null 2>&1 || true
	@docker network rm o5gs_net >/dev/null 2>&1 || true
	@echo "✓ removed"

## ─────────────────────────── destructive ─────────────────────────────────

clean: ## Delete containers AND data, so the next 'make up' rebuilds from empty
	@echo "This deletes all subscribers, dashboards and metric history."
	@read -p "Type yes to continue: " a; [ "$$a" = "yes" ] || { echo "cancelled"; exit 1; }
	@if [ "$(HAVE_COMPOSE)" = "yes" ]; then \
	    cd $(COMPOSE_DIR) && docker compose down -v; \
	    cd $(OBS_DIR) && docker compose down -v; \
	else \
	    docker rm -f $(ALL) >/dev/null 2>&1 || true; \
	    docker volume rm compose_dbdata observability_promdata observability_grafanadata >/dev/null 2>&1 || true; \
	fi
	@echo "✓ removed — run 'make create && make up && make ues' to rebuild"

nuke: clean ## Alias for clean
