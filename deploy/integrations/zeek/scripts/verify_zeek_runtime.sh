#!/usr/bin/env bash
set -euo pipefail

ZEEK_CONTAINER="parseus_zeek"
INGESTOR_CONTAINER="parseus_zeek_ingestor"
LOG_DIR_IN_CONTAINER="/zeek/logs"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      cat <<'USAGE'
Usage:
  bash /opt/parseus/deploy/integrations/zeek/scripts/verify_zeek_runtime.sh

Checks that the Zeek and Zeek ingestor containers are running, that Zeek is
writing JSON logs, and that the ingestor can read them and is not logging errors.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "[FAIL] docker is not installed or not in PATH" >&2
  exit 1
fi

FAILURES=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1"; FAILURES=$((FAILURES + 1)); }

container_running() {
  docker ps --format '{{.Names}}' | grep -Fx "$1" >/dev/null 2>&1
}

for container in "$ZEEK_CONTAINER" "$INGESTOR_CONTAINER"; do
  if container_running "$container"; then
    pass "$container is running"
  else
    fail "$container is not running"
  fi
done

if container_running "$ZEEK_CONTAINER"; then
  if docker exec "$ZEEK_CONTAINER" test -s "$LOG_DIR_IN_CONTAINER/conn.log" \
    || docker exec "$ZEEK_CONTAINER" test -s "$LOG_DIR_IN_CONTAINER/packet_filter.log"; then
    pass "Zeek is writing logs to $LOG_DIR_IN_CONTAINER"
  else
    fail "no Zeek logs in $LOG_DIR_IN_CONTAINER (check ZEEK_IFACE and the container logs)"
  fi
  if docker exec "$ZEEK_CONTAINER" sh -c "head -c1 $LOG_DIR_IN_CONTAINER/packet_filter.log | grep -q '{'"; then
    pass "Zeek logs are JSON"
  else
    fail "Zeek logs are not JSON"
  fi
fi

if container_running "$INGESTOR_CONTAINER"; then
  if docker exec "$INGESTOR_CONTAINER" test -r "$LOG_DIR_IN_CONTAINER"; then
    pass "ingestor can read $LOG_DIR_IN_CONTAINER"
  else
    fail "ingestor cannot read $LOG_DIR_IN_CONTAINER"
  fi
  if docker logs --since 10m "$INGESTOR_CONTAINER" 2>&1 | grep -E "ERROR|Traceback" >/dev/null; then
    fail "ingestor logged errors in the last 10 minutes (docker logs $INGESTOR_CONTAINER)"
  else
    pass "no ingestor errors in the last 10 minutes"
  fi
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "$FAILURES check(s) failed" >&2
  exit 1
fi
echo "All checks passed"
