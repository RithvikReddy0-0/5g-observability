#!/usr/bin/env bash
# start_jenkins.sh — start or manage the local Jenkins CI/CD service for the 5G stack.
#
# Usage:
#   scripts/start_jenkins.sh          # start Jenkins (builds image if needed)
#   scripts/start_jenkins.sh --status # show container status, port, and admin key
#   scripts/start_jenkins.sh --stop   # stop Jenkins
#
# Jenkins UI: http://localhost:8080

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
JENKINS_DIR="$REPO_ROOT/ci/jenkins"

get_admin_password() {
  if docker ps --format '{{.Names}}' | grep -q "^jenkins$"; then
    docker exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword 2>/dev/null || echo "(Jenkins is still initializing...)"
  else
    echo "(Jenkins container is not running)"
  fi
}

case "${1:-}" in
  --stop)
    echo "Stopping Jenkins CI..."
    cd "$JENKINS_DIR" && docker compose down && cd "$REPO_ROOT"
    echo "Jenkins stopped."
    exit 0
    ;;
  --status)
    if docker ps --format '{{.Names}}' | grep -q "^jenkins$"; then
      echo "Jenkins is RUNNING."
      echo "  URL:             http://localhost:8080"
      echo "  Admin Password:  $(get_admin_password)"
    else
      echo "Jenkins is NOT running."
    fi
    exit 0
    ;;
esac

echo "=== [1/2] Building and starting Jenkins CI container ==="
cd "$JENKINS_DIR"
docker compose up -d --build
cd "$REPO_ROOT"

echo "=== [2/2] Waiting for Jenkins to initialize ==="
echo "Polling http://localhost:8080 ..."
for _ in $(seq 30); do
  if curl -s -m 2 http://localhost:8080/login >/dev/null 2>&1; then
    echo "Jenkins is UP and ready!"
    break
  fi
  sleep 2
done

echo ""
echo "================================================================="
echo "  Jenkins CI/CD Service is live:"
echo "  URL:             http://localhost:8080"
echo "  Initial Password: $(get_admin_password)"
echo "================================================================="
