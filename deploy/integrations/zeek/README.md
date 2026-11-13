# Zeek Modbus / DNP3 Deep Packet Inspection (Parseus Optional Integration)

This directory contains the optional Zeek integration used by Parseus when the `zeek` integration is enabled. It decodes Modbus/TCP and DNP3 traffic and indexes the **register and point values** so the Registers views and the function-code analytics work without Malcolm.

The runtime processes are:

- `parseus_zeek` (this directory's image: stock `zeek/zeek` plus one script and one compiled Spicy analyzer) sniffing a network interface
- `parseus_zeek_ingestor` (`python -m parseus.ingestors.zeek` in the Parseus image) tailing Zeek's JSON logs into the datastore

## Scope and Runtime Behavior

Zeek keeps its stock `modbus.log` and `dnp3.log` and `zeek/parseus.zeek` adds two logs built from the events the stock analyzers raise plus one event from the Parseus Spicy DNP3 analyzer (no ICSNPP, no `zkg`):

| Log | Content |
| --- | --- |
| `modbus_detailed.log` | One row per Modbus request/response: unit, function name, start address, quantity and `request_values` / `response_values`. Registers are counts; coils and discrete inputs are 0/1. Read responses are joined to their request to get the address. |
| `dnp3_points.log` | One row per DNP3 point: function code, group, variation, index, value and quality flags. |

DNP3 coverage, by source:

- Stock Zeek events:
  - Binary input/output g1 and g10 (packed bits and flag byte).
  - Counters g20 and frozen counters g21 (16/32-bit, with or without flag).
  - Analog inputs g30 and analog input events g32 (16/32-bit, single and double precision; inf/NaN are skipped, the index still advances).
- Parseus Spicy analyzer (`spicy/parseus_dnp3.spicy`), for what stock Zeek raises no usable event for:
  - Analog output status g40v1-v4 (32/16-bit, float, double; inf/NaN skipped).
  - Binary input events g2v1-v3. The value is the state bit (bit 7) of the flag byte, with the whole flag byte in `flags`, the same as g1v2/g10v2. Stock Zeek only raises a generic object event for g2 whose value is not the point's flag byte, so `parseus.zeek` ignores it and nothing is logged twice.
- Index: `start + n` for range qualifiers `0x00`-`0x02`; the object prefix for prefixed qualifiers (`0x17`, `0x28`, ...). Both sources use the same rule and the same `dnp3_points` columns; `function_code` is `RESPONSE` or `UNSOLICITED_RESPONSE`.

### Spicy analyzer

It is attached to TCP/20000 next to the stock DNP3 analyzer (both appear in the connection's `service` in `conn.log`), compiled to `/opt/parseus/spicy/parseus_dnp3.hlto` and loaded by `entrypoint.sh`. It parses the wire format itself: link frames (start `0x0564`, header and every 16-byte data block checked against their CRC-16, bad frames dropped), transport FIR/FIN/sequence reassembly, the application header (IIN on `0x81`/`0x82` responses) and then every object header, sizing each object from a table of the common static and event objects (g1-g4, g10-g13, g20-g23, g30-g34, g40-g43, g50-g52, g80, g110-g113) so objects it does not log are skipped and g40/g2 later in the same fragment are still reached.

Limits:

- Responses only (`0x81` and `0x82`). Request objects are not walked.
- An object whose size it cannot know (unlisted group/variation, absolute-address ranges, `0x0B` formats other than a size prefix) ends that fragment: points before it are logged, nothing after it is guessed. Objects with an object-size prefix (qualifiers `0x4x`-`0x6x`, such as g120 authentication) are skipped by their prefix whatever they are.
- One reassembly state per TCP direction. A segment out of sequence (loss, or a gateway interleaving fragments from several outstations on one connection) drops that fragment. A fragment cut off mid-object keeps the items that are fully present.
- No resync inside a frame: after a bad frame header the parser scans on for the next `0x0564`.
- DNP3 over serial/UDP, and TCP on ports other than 20000, are not attached (add the port to `parseus_dnp3.evt` and `DNP3::ports`).

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
├─ spicy/
│  ├─ parseus_dnp3.spicy
│  └─ parseus_dnp3.evt
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

`make_test_pcap.py` writes a Modbus/DNP3 capture with known values (negative analog, 16-bit max, float, prefixed index, packed bits, g40v1-v4, g2v1-v3, a fragment split over four link frames, and a second connection with a bad CRC, an unknown object, a segment gap and a truncated fragment); `--check` fails if Zeek's logs differ.

Then confirm in the datastore (Registers page, or):

```bash
curl -s 'localhost:9200/arkime_sessions3-parseus-zeek-*/_count?q=event.dataset:dnp3_points'
```

## Operations

- Zeek rotates logs hourly into the `zeek_log_data` volume; the Zeek container deletes rotated files a day after rotation. Inf/NaN float analog values are not logged.
- Building the image compiles the Spicy analyzer in a throwaway builder stage (`apt-get install g++`, so the build needs network access to the Debian mirrors once, or a proxy via `HTTP_PROXY`/`HTTPS_PROXY`). The running image has no compiler; offline bundles ship the built image.
- Index retention is not managed here; apply your datastore's retention policy to `arkime_sessions3-parseus-zeek-*`.
- Restarting either container is safe: offsets and document ids make ingestion resumable and idempotent.
- Upgrading Zeek means changing the tag on both `FROM` lines in `Dockerfile` (the `.hlto` must be built by the same Zeek/Spicy version it runs on) and re-running the replay check above.

## Troubleshooting

- **No logs**: check `ZEEK_IFACE` exists on the host (`ip link`) and `docker logs parseus_zeek`.
- **Logs but no Modbus/DNP3 rows**: Zeek only attaches the analyzers on port 502 (Modbus) and 20000 (DNP3), or by DPD signature for DNP3. Other ports need a `Modbus::ports` / `DNP3::ports` redef in `parseus.zeek` (and, for DNP3 g40/g2, the port in `spicy/parseus_dnp3.evt`).
- **No register values for a Modbus read**: the response is only logged when its request was seen within one minute (the address lives in the request).
- **Documents missing in the UI**: check the ingestor log (`docker logs parseus_zeek_ingestor`) and that the datastore URL is reachable from the host network.
- **Reprocess everything**: delete `zeek_state.json`; existing documents are overwritten in place, not duplicated.
- **Sensitive data**: register values reveal process state; restrict access to the indices accordingly.
