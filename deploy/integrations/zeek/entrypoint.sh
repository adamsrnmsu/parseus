#!/bin/sh
# Run Zeek live on ZEEK_IFACE, or once over ZEEK_PCAP, writing JSON logs to the
# current directory (/zeek/logs). Checksums are ignored (-C) because virtual and
# offloading NICs deliver packets with bad checksums that Zeek would drop.
set -eu

SCRIPT=/opt/parseus/zeek/parseus.zeek
# Spicy DNP3 analyzer (g40 / g2), compiled into the image by the Dockerfile builder stage.
HLTO=/opt/parseus/spicy/parseus_dnp3.hlto

if [ -n "${ZEEK_PCAP:-}" ]; then
  exec zeek -C -r "$ZEEK_PCAP" "$HLTO" "$SCRIPT" "Log::default_rotation_interval=0sec"
fi
IFACE="${ZEEK_IFACE:-eth0}"
if [ ! -e "/sys/class/net/$IFACE" ]; then
  echo "ZEEK_IFACE=$IFACE does not exist; set it in ingestor.env. Interfaces: $(ls /sys/class/net | tr '\n' ' ')" >&2
  exit 1
fi
# The ingestor mounts the log volume read-only, so Zeek's side prunes rotated logs
# (name.YYYY-MM-DD-HH-MM-SS.log) a day after rotation; the ingestor polls every 2 s.
(while sleep 3600; do find /zeek/logs -name '*.????-??-??-??-??-??.log' -mmin +1440 -delete; done) &
exec zeek -C -i "$IFACE" "$HLTO" "$SCRIPT"
