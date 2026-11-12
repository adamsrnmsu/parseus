# Zeek Modbus / DNP3 Deep Packet Inspection (Parseus Optional Integration)

This directory contains the optional Zeek integration used by Parseus when the `zeek` integration is enabled. It decodes Modbus/TCP and DNP3 traffic and indexes the **register and point values** so the Registers views and the function-code analytics work without Malcolm.

The runtime processes are:

- `parseus_zeek` (this directory's image, stock `zeek/zeek` plus one script) sniffing a network interface
- `parseus_zeek_ingestor` (`python -m parseus.ingestors.zeek` in the Parseus image) tailing Zeek's JSON logs into the datastore

## Scope and Runtime Behavior

Zeek keeps its stock `modbus.log` and `dnp3.log` and `zeek/parseus.zeek` adds two logs built only from events the stock analyzers already raise (no ICSNPP, no `zkg`, so the image builds offline):

| Log | Content |
| --- | --- |
| `modbus_detailed.log` | One row per Modbus request/response: unit, function name, start address, quantity and `request_values` / `response_values`. Registers are counts; coils and discrete inputs are 0/1. Read responses are joined to their request to get the address. |
| `dnp3_points.log` | One row per DNP3 point: function code, group, variation, index, value and quality flags. |

DNP3 coverage is limited to what stock Zeek exposes:

- Binary input/output g1 and g10 (packed bits and flag byte).
- Counters g20 and frozen counters g21 (16/32-bit, with or without flag).
- Analog inputs g30 (16/32-bit and single-precision float) and analog input events g32 (non-time variants and `SPwoTime`).
- Not covered: binary input events g2 (stock Zeek raises no event with the flag byte), analog output status g40 and double-precision/double-bit variants, because stock Zeek raises no event for them.
- Index: `start + n` for range qualifiers `0x00`-`0x02`; the object prefix for prefixed qualifiers (`0x17`, `0x28`, ...).

The ingestor:

- converts each record to a Malcolm-style document (`@timestamp`, `source.ip`, `destination.ip`, `event.provider=zeek`, `event.dataset=<log>`, `zeek.<log>.<field>`), see `src/parseus/ingestors/zeek/normalize.py`
- uses a stable document `_id` derived from the record, so re-reading a log never duplicates documents
- saves per-file offsets keyed by inode (default `/home/webapp/.parseus/artifacts/logs/zeek_state.json`), advancing them only after a successful push, so restarts resume and hourly log rotation loses nothing
- writes to date-partitioned indices `arkime_sessions3-parseus-zeek-%y%m%d` (by event date) and installs an index template pinning the field types

## Directory Layout

```text
deploy/integrations/zeek/
├─ README.md
├─ Dockerfile
├─ entrypoint.sh
├─ zeek/
│  └─ parseus.zeek
└─ scripts/
   ├─ make_test_pcap.py
   ├─ replay_pcap.sh
   └─ verify_zeek_runtime.sh
```

## Compose Integration

In `compose.runtime.yaml`:

- `zeek`: container `parseus_zeek`, image `parseus-zeek` (built from `compose.build.yaml`, included in offline bundles), `network_mode: host`, `NET_RAW`/`NET_ADMIN`, JSON logs on the `zeek_log_data` volume, bounded Docker logging.
- `zeek_ingestor`: container `parseus_zeek_ingestor`, Parseus image, reads `zeek_log_data` read-only, state and logs on `app_log_data`, health check on the process.

Start with:

```bash
sudo manage-parseus up --integrations zeek
```

## Configuration

Set in `envs/ingestor.env` (or `[env.ingestor]` in the installer config):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ZEEK_IFACE` | `eth0` | Interface Zeek sniffs. Must be a SPAN/mirror or tap port to see other hosts' traffic. |
| `DATASTORE_HOST_URL` | from `datastore.env` | Datastore to write to. |
| `INGESTOR_ZEEK_INDEX` | `arkime_sessions3-parseus-zeek-%y%m%d` | strftime index pattern, lowercase. Keep the `arkime_sessions3-` prefix: the Parseus queries and analytics read that pattern. |
| `INGESTOR_ZEEK_LOG_DIR` | `/zeek/logs` | Log directory (set by Compose). |
| `INGESTOR_ZEEK_STATE_FILE` | `.../logs/zeek_state.json` | Offset state. |
| `INGESTOR_ZEEK_POLL_SECONDS` | `2` | Poll interval. |

Zeek ignores packet checksums (`-C`) because virtual and offloading NICs deliver bad ones. To analyze a capture instead of a live interface, set `ZEEK_PCAP` to a path mounted into the container.

## Runtime Validation

```bash
bash /opt/parseus/deploy/integrations/zeek/scripts/verify_zeek_runtime.sh
```

Checks that both containers run, Zeek is writing JSON logs, the ingestor can read them and has logged no errors.

Offline replay of a pcap (no Parseus stack needed) and a ground-truth self-check:

```bash
python3 deploy/integrations/zeek/scripts/make_test_pcap.py /tmp/truth.pcap
docker build -t parseus-zeek:dev deploy/integrations/zeek
deploy/integrations/zeek/scripts/replay_pcap.sh /tmp/truth.pcap /tmp/zeek-logs --check
```

`make_test_pcap.py` writes a Modbus/DNP3 capture with known values (negative analog, 16-bit max, float, prefixed index, packed bits); `--check` fails if Zeek's logs differ.

Then confirm in the datastore (Registers page, or):

```bash
curl -s 'localhost:9200/arkime_sessions3-parseus-zeek-*/_count?q=event.dataset:dnp3_points'
```

## Operations

- Zeek rotates logs hourly into the `zeek_log_data` volume; the Zeek container deletes rotated files a day after rotation. Inf/NaN float analog values are not logged.
- Index retention is not managed here; apply your datastore's retention policy to `arkime_sessions3-parseus-zeek-*`.
- Restarting either container is safe: offsets and document ids make ingestion resumable and idempotent.
- Upgrading Zeek means changing the tag in `Dockerfile` and re-running the replay check above.

## Troubleshooting

- **No logs**: check `ZEEK_IFACE` exists on the host (`ip link`) and `docker logs parseus_zeek`.
- **Logs but no Modbus/DNP3 rows**: Zeek only attaches the analyzers on port 502 (Modbus) and 20000 (DNP3), or by DPD signature for DNP3. Other ports need a `Modbus::ports` / `DNP3::ports` redef in `parseus.zeek`.
- **No register values for a Modbus read**: the response is only logged when its request was seen within one minute (the address lives in the request).
- **Documents missing in the UI**: check the ingestor log (`docker logs parseus_zeek_ingestor`) and that the datastore URL is reachable from the host network.
- **Reprocess everything**: delete `zeek_state.json`; existing documents are overwritten in place, not duplicated.
- **Sensitive data**: register values reveal process state; restrict access to the indices accordingly.
