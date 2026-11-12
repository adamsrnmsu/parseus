#!/usr/bin/env bash
# Run the Parseus Zeek image over a pcap and leave the JSON logs in OUT_DIR.
#   replay_pcap.sh capture.pcap [OUT_DIR]
# Add `--check` as the third argument to compare the logs with make_test_pcap.py's
# expected values (only meaningful for the pcap that script generates).
set -euo pipefail

PCAP="${1:?usage: replay_pcap.sh capture.pcap [OUT_DIR] [--check]}"
OUT="${2:-./zeek-logs}"
IMAGE="${ZEEK_IMAGE:-parseus-zeek:${PARSEUS_VERSION:-dev}}"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
PCAP_DIR="$(cd "$(dirname "$PCAP")" && pwd)"

docker run --rm \
  -e "ZEEK_PCAP=/pcap/$(basename "$PCAP")" \
  -v "$PCAP_DIR:/pcap:ro" -v "$OUT:/zeek/logs" "$IMAGE"

for log in modbus_detailed dnp3_points; do
  [[ -f "$OUT/$log.log" ]] && echo "$log.log: $(wc -l <"$OUT/$log.log") rows"
done

if [[ "${3:-}" == "--check" ]]; then
  python3 "$(dirname "$0")/make_test_pcap.py" --check "$OUT"
fi
