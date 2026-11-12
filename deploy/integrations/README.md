# Optional Integrations

The `deploy/integrations/` directory contains optional Parseus ingestion components that are enabled through Docker Compose profiles by `manage_parseus.py`.

## Production architecture standard

Each integration should keep the same operational shape where practical:

- `README.md` documents purpose, runtime architecture, configuration, verification, operations, and troubleshooting.
- Runtime code/config is isolated under the integration directory and mounted into containers read-only unless state is required.
- `scripts/` contains operator utilities such as setup, replay, or runtime verification helpers.
- Runtime state is either stored in Docker volumes or explicitly ignored by source control.
- Configuration is environment-driven with safe defaults and validation.
- Long-running processes must be restart-safe, log to stdout/stderr, and avoid unbounded local files.
- Ingestion output must use date-partitioned indices and documented retention expectations.

## Current integrations

| Integration | Compose profile | Purpose | Verification helper |
| --- | --- | --- | --- |
| `gpib` | `gpib` | GPIB-over-Ethernet payload ingestion and local NDJSON capture. | `deploy/integrations/gpib/scripts/verify_gpib_runtime.sh` |
| `winlog` | `winlog` | Windows/Winlogbeat-compatible NDJSON file ingestion through Logstash. | `deploy/integrations/winlog/scripts/verify_winlog_runtime.sh` |
| `zeek` | `zeek` | Zeek deep packet inspection of Modbus and DNP3: register and point values indexed for the Registers views and analytics. | `deploy/integrations/zeek/scripts/verify_zeek_runtime.sh` |

Enable one or more integrations with:

```bash
sudo manage-parseus up --integrations gpib,winlog,zeek
```

Review each integration's README before production deployment; these integrations handle potentially sensitive operational data.
