"""Measure per-field MAVLink receipt age against live PX4 SITL.

The architecture document proposed max_age/max_skew values without evidence.
This measures what PX4 SITL default stream rates actually deliver, so policy
thresholds come from observation. Read-only: no command is ever sent.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter

FIELDS = ("position", "attitude", "battery", "gps", "flight_mode", "armed",
          "in_air", "home")


async def main(seconds=30.0):
    adapter = MavsdkAdapter(system_address="udpin://0.0.0.0:14540")
    await adapter.connect()
    ages = {name: [] for name in FIELDS}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await adapter.get_telemetry()
        observation = adapter.safety_snapshot()
        now = time.monotonic()
        metadata = observation.telemetry.metadata
        for name in FIELDS:
            field = metadata.fields.get(name)
            if field is not None:
                ages[name].append(now - field.received_monotonic)
        await asyncio.sleep(0.1)
    await adapter.disconnect()

    report = {}
    for name, values in ages.items():
        report[name] = ({"samples": 0} if not values else {
            "samples": len(values),
            "median_s": round(statistics.median(values), 3),
            "p95_s": round(sorted(values)[int(len(values) * 0.95) - 1], 3),
            "max_s": round(max(values), 3),
        })
    present = [v["max_s"] for v in report.values() if v["samples"]]
    report["_worst_field_max_age_s"] = max(present) if present else None
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
