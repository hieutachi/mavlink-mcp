# Installation

Three levels, each self-contained. Stop at the one you need.

| Level | What you get | Needs |
| --- | --- | --- |
| A. Tests | 744 tests, no vehicle of any kind | Python 3.10+ |
| B. Kinematic demo | Full approval flow over real MCP stdio | Level A |
| C. PX4 SITL | The MAVSDK adapter against a real autopilot | Linux or WSL2, ~2 GB disk |

Everything here was run on the host recorded in
[BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md): Windows 11, Python 3.13.3, and
Ubuntu 22.04 under WSL2 for level C. Every command below assumes the virtual
environment is activated and works unchanged on Linux, macOS and Windows.

## A. Environment and tests

```bash
git clone https://github.com/hieutachi/mavlink-mcp.git
cd mavlink-mcp
git checkout safemav/p08-p09-gateway

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
```

Dependencies, all permissively licensed: `fastmcp>=2,<3` (Apache-2.0),
`mavsdk>=2,<4` (BSD-3-Clause), `cryptography>=42,<51` (Apache-2.0/BSD). The
project deliberately avoids pymavlink (LGPL-3).

```bash
python -m pytest -q
```

Expected: `744 passed, 1 warning`. The warning is an Authlib deprecation
notice from inside FastMCP, not from this project.

No test opens a network socket or connects to MAVSDK. One test spawns a real
MCP server subprocess over stdio against the kinematic simulator.

**If tests fail.** A wrong interpreter is the usual cause — `pytest` from a
global install will not see the package. Activate the venv first, or call its
interpreter directly (`.venv/bin/python` on Linux/macOS,
`.venv\Scripts\python.exe` on Windows). `cryptography` must import successfully; without it
approval signing cannot work and roughly 300 tests fail.

## B. Kinematic demo, no autopilot

The bundled simulator is a network-free kinematic model. It is a demonstration
vehicle, not evidence about flight safety.

```bash
python examples/patrol_sim.py
```

This launches its own MCP server subprocess, generates a throwaway operator
key, and runs eight approved actions plus three refusals. Expected tail:

```json
{
  "passed": true,
  "backend": "sim",
  "tool_count": 12,
  "hardware_contacted": false,
  "audit": {"event_count": 37, "hash_chain_valid": true, "acknowledged": 8}
}
```

Runtime is a few seconds. If it hangs, a previous run's MCP child may still
hold the ledger lock; see Troubleshooting.

### Configuring a server for an MCP client

Without `SAFEMAV_CONFIG` the server exposes telemetry only and refuses every
write. That is the default, and `MAVLINK_MCP_READONLY=false` does not change it.

Create an operator keypair. The private half never goes near the server:

```bash
python -m mavlink_mcp.operator_cli keygen \
    --output operator.key --key-id demo-key --operator-id alice
```

It prints the public half and writes 32 private bytes to `operator.key`:

```json
{"key_id": "demo-key", "operator_id": "alice",
 "public_key": "2NsfkoDPStiE7yRIUx1iKHi7RO0cgzi55kk19nVxpug"}
```

Store `operator.key` somewhere the server cannot read, ideally on a different
machine. Put only the public key in the launch config, `safemav.json`:

```json
{
  "actor_id": "demo-agent",
  "vehicle_id": "demo-vehicle",
  "gateway_id": "demo-gateway",
  "environment": "kinematic",
  "readonly": false,
  "bootstrap": true,
  "ledger_path": "ledger.sqlite3",
  "policy": {
    "readonly": false,
    "max_altitude_m": 50,
    "max_radius_m": 200,
    "min_battery_percent": 30
  },
  "trust_keys": [{
    "key_id": "demo-key",
    "operator_id": "alice",
    "roles": ["human_operator"],
    "vehicles": ["demo-vehicle"],
    "actions": ["arm", "disarm", "takeoff", "land", "hold",
                "return_to_launch", "goto", "fly_mission"],
    "public_key": "2NsfkoDPStiE7yRIUx1iKHi7RO0cgzi55kk19nVxpug"
  }]
}
```

Every field is required; unknown fields are rejected. `bootstrap: true` permits
creating the ledger and should be `false` once it exists, so a deleted ledger
fails closed instead of silently starting a fresh one. `environment` must match
the backend: `kinematic` for `sim`, `px4_sitl` for `mavsdk`. Relative paths
resolve against the config file's directory. A key with empty `vehicles` or
`actions` is unrestricted, so list them explicitly.

Claude Desktop or any MCP client:

```json
{
  "mcpServers": {
    "safemav": {
      "command": "C:\\path\\to\\mavlink-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mavlink_mcp"],
      "env": {
        "MAVLINK_MCP_BACKEND": "sim",
        "SAFEMAV_CONFIG": "C:\\path\\to\\safemav.json"
      }
    }
  }
}
```

On Linux or macOS the interpreter is `/path/to/mavlink-mcp/.venv/bin/python`.

Twelve tools appear: `get_capabilities`, `get_telemetry`, `get_flight_mode`,
`get_connection_status`, `arm`, `disarm`, `takeoff`, `land`, `hold`,
`return_to_launch`, `goto_location`, `fly_mission`.

## C. PX4 SITL

Needed only to exercise the MAVSDK adapter against a real autopilot. No
Gazebo, no GPU, no Docker: PX4's built-in SIH dynamics are enough.

```bash
# in WSL2 / Linux
git clone --depth 1 --recursive https://github.com/PX4/PX4-Autopilot.git
cd PX4-Autopilot
bash ./Tools/setup/ubuntu.sh --no-nuttx --no-sim-tools
make px4_sitl_default          # ~10 min on 32 cores
```

Start it with the helper, which also reports the MAVLink ports it bound:

```bash
bash tools/run_sitl.sh
```

Expect `INFO [commander] Ready for takeoff!`. PX4 streams onboard MAVLink to
UDP 14540, which is where the adapter listens.

`run_sitl.sh` deletes PX4's persisted parameters first. PX4 otherwise restores
the simulated vehicle armed and airborne between runs, which silently
invalidates any preflight-state test.

The evidence scripts need MAVSDK 3.x specifically. MAVSDK 4 is a different,
incompatible API:

```bash
python3 -m venv ~/sitlenv
~/sitlenv/bin/pip install "mavsdk==3.17.2" "cryptography>=42"
```

Then, from the repository root inside WSL:

```bash
~/sitlenv/bin/python tools/measure_rates.py    # per-field telemetry age, read-only
~/sitlenv/bin/python tools/sitl_evidence.py    # signed flight + refusal matrix
```

`sitl_evidence.py` writes `docs/SITL_EVIDENCE.json` and exits 0 only when all
ten refusals matched and all four flight actions were acknowledged and
observed. See [SITL_EVIDENCE.md](SITL_EVIDENCE.md) for the recorded run.

For an MCP server against SITL, use `MAVLINK_MCP_BACKEND=mavsdk`,
`"environment": "px4_sitl"`, and set `max_age_ms`/`max_skew_ms` to at least
3000 — measured PX4 stream rates make tighter bounds reject healthy telemetry.

## Benchmark

```bash
python benchmark/verify_baseline.py    # provenance check
python benchmark/runner.py             # ~2 min
python tools/verify_benchmark.py       # recompute from raw data
```

The first verifies the reconstructed baseline against git and exits non-zero if
it has drifted. The third recomputes all 18 rates from `trials.jsonl` with an
independent oracle and exits non-zero on any disagreement.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `SAFEMAV_CONFIG` | unset | Trusted launch config. Unset means read-only, no gateway. |
| `MAVLINK_MCP_BACKEND` | `mavsdk` | `sim` or `mavsdk`. Unknown values fail closed. |
| `MAVLINK_MCP_URL` | `udpin://0.0.0.0:14540` | MAVLink endpoint for the `mavsdk` backend. |
| `MAVLINK_MCP_READONLY` | `true` | Legacy guardrail flag. **Not** an authorization switch; it cannot grant write access. |
| `SAFEMAV_RUNTIME_DIR` | platform default | Where the ledger lives when `ledger_path` is relative and unset. |

Policy limits (altitude, radius, battery, freshness) live in the launch config,
not in environment variables, so they cannot be changed by whatever launched
the process.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ModuleNotFoundError: mavlink_mcp` | Wrong interpreter. Use the venv's `python` explicitly. |
| Every write returns `READONLY` | `SAFEMAV_CONFIG` is unset or `readonly` is true in the config or policy. Both must be false. |
| `StoreBusy: ledger already has an owner` | Another process holds the ledger. Single-owner locking is intentional. Find it with `Get-CimInstance Win32_Process` filtered on `mavlink_mcp`, or wait for it to exit. Never delete the `.lock` sidecar. |
| `StoreUnavailable: established database is missing` | `bootstrap` is false and the ledger does not exist. Correct behavior: a vanished ledger is not silently recreated. Set `bootstrap: true` once, deliberately. |
| `APPROVAL_EXPIRED` immediately | The approval TTL is 60 s and the clock is wall time. Check system clock sync; signing must finish inside the window. |
| `TELEMETRY_STALE` against SITL | `max_age_ms`/`max_skew_ms` are too tight. PX4 sends battery and home at 1 Hz; use 3000 ms or more. |
| `interactive confirmation required` | `safemav-operator approve` refuses a piped or redirected stdin by design. Run it at a terminal. |
| `EOFError` from `approve` | Same cause, different path: on Git Bash `isatty()` can report true for `/dev/null`, so the prompt raises instead of printing the message. No token is written either way. |
| `TypeError: System is not constructed directly` | MAVSDK 4 is installed. This project needs `mavsdk<4`. |
| PX4 starts armed and airborne | Stale persisted parameters. `tools/run_sitl.sh` wipes them; if you start PX4 by hand, delete `build/px4_sitl_default/rootfs/eeprom`. |
| No MAVLink port bound by PX4 | You started a model that waits for an external simulator (for example `none_iris`). Use `sihsim_quadx`. |

## Verifying the install is honest

Three checks a reviewer can run that are designed to fail when they should:

```bash
# 1. the baseline reconstruction matches git
python benchmark/verify_baseline.py

# 2. reported rates reproduce from raw trial data
python tools/verify_benchmark.py

# 3. the documented bypass gap is still exactly one case
python -m pytest -q tests/safety/test_bypass_attempts.py -v
```

Check 3 includes `test_known_gap_same_process_issuer_theft_is_not_prevented`,
which asserts a limitation rather than a feature. It is there so the gap
described in [THREAT_MODEL.md](THREAT_MODEL.md) cannot quietly disappear from
the documentation.
