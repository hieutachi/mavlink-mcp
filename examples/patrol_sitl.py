"""Takeoff → waypoint → land against PX4 SITL, using the Python surface.

This is the same capability layer the MCP server exposes to AI agents,
used directly as a library. Start a simulator first (see
docs/simulator-quickstart.md), e.g.:

    docker run --rm -it jonasvautherin/px4-gazebo-headless:1.14.3

then run:

    python examples/patrol_sitl.py

The script opts out of readonly mode explicitly (we are the human operator
here, and the target is a simulator). The altitude ceiling and soft
geofence stay active — try setting TARGET_OFFSET_DEG ten times larger and
watch the goto get rejected before anything reaches the autopilot.
"""

import asyncio

from mavlink_mcp import Drone, GuardrailConfig
from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter

TAKEOFF_ALTITUDE_M = 15.0
CRUISE_ALTITUDE_M = 20.0
# ~110 m north / ~75 m east of home — inside the 200 m default geofence.
TARGET_OFFSET_DEG = (0.001, 0.001)


async def wait_until(drone: Drone, description: str, predicate) -> None:
    while True:
        snapshot = await drone.get_telemetry()
        if predicate(snapshot):
            return
        alt = (
            f"{snapshot.position.relative_altitude_m:.1f} m"
            if snapshot.position
            else "?"
        )
        print(f"  ... waiting for {description} (alt {alt}, "
              f"mode {snapshot.flight_mode})")
        await asyncio.sleep(2.0)


async def main() -> None:
    guardrails = GuardrailConfig(
        readonly=False,          # explicit opt-in: this is a simulator
        max_altitude_m=50.0,
        geofence_radius_m=200.0,
    )
    drone = Drone(MavsdkAdapter(), guardrails=guardrails)

    print(f"Capabilities: {sorted(c.value for c in drone.capabilities())}")

    print("Connecting and reading telemetry ...")
    snapshot = await drone.get_telemetry()
    assert snapshot.position is not None, "no position — is SITL ready?"
    home_lat = snapshot.position.latitude_deg
    home_lon = snapshot.position.longitude_deg
    print(f"  home: ({home_lat:.6f}, {home_lon:.6f}), "
          f"battery {snapshot.battery.remaining_percent:.0f}%, "
          f"GPS {snapshot.gps.fix_type} ({snapshot.gps.num_satellites} sats)")

    print("Arming (operator-confirmed) ...")
    await drone.arm(confirm=True)

    print(f"Taking off to {TAKEOFF_ALTITUDE_M} m ...")
    await drone.takeoff(TAKEOFF_ALTITUDE_M, confirm=True)
    await wait_until(
        drone,
        "takeoff altitude",
        lambda s: s.position is not None
        and s.position.relative_altitude_m > TAKEOFF_ALTITUDE_M * 0.9,
    )

    target_lat = home_lat + TARGET_OFFSET_DEG[0]
    target_lon = home_lon + TARGET_OFFSET_DEG[1]
    print(f"Flying to waypoint ({target_lat:.6f}, {target_lon:.6f}) "
          f"at {CRUISE_ALTITUDE_M} m ...")
    await drone.goto(target_lat, target_lon, CRUISE_ALTITUDE_M)
    await asyncio.sleep(20.0)  # let it travel; watch the console/Gazebo

    print("Returning to launch and landing ...")
    await drone.return_to_launch()
    await wait_until(drone, "landing", lambda s: s.in_air is False)

    print("Done — vehicle is on the ground.")


if __name__ == "__main__":
    asyncio.run(main())
