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
        bootstrap verify clean logs urls nuke

help: ## Show this help
	@echo ""
	@echo "  5G Observability — available commands"
	@echo "  ────────────────────────────────────────────────────────────────"
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
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
	@echo "▶ starting slice exporter ..."
	@bash scripts/start_slice_exporter.sh >/dev/null 2>&1 || true
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
	@echo "▶ stopping UEs and slice exporter ..."
	@bash scripts/start_slice_exporter.sh --stop >/dev/null 2>&1 || true
	@docker exec -i ueransim pkill nr-ue >/dev/null 2>&1 || true
	@echo "▶ stopping containers ..."
	@docker stop $(ALL) >/dev/null 2>&1 || true
	@echo "✓ stopped — data preserved, 'make up' brings it straight back"

restart: down up ## Stop then start everything

## ─────────────────────────── devices (UEs) ───────────────────────────────

ues: ## Re-provision SIMs and connect 20 devices across both slices
	@echo "▶ re-provisioning subscribers (resets the SQN counter — required) ..."
	@bash scripts/provision_subscribers.sh --delete-all >/dev/null 2>&1 || true
	@COUNT=$(N_A) START=1            SD=$(SLICE_A) bash scripts/provision_subscribers.sh | tail -1
	@COUNT=$(N_B) START=$$(($(N_A)+1)) SD=$(SLICE_B) bash scripts/provision_subscribers.sh | tail -1
	@echo "▶ making sure the slice-B config is inside the container ..."
	@docker cp $(COMPOSE_DIR)/config/uecfg-slice-b.yaml ueransim:/ueransim/config/uecfg-slice-b.yaml >/dev/null 2>&1 || true
	@echo "▶ connecting devices ..."
	@COUNT=$(N_A) COUNT_B=$(N_B) TEMPO=600 SETTLE=85 bash scripts/start_ues.sh

stop-ues: ## Disconnect all devices
	@bash scripts/start_ues.sh --stop

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
