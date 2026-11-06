#!/bin/bash
# Start headless PX4 SITL (built-in SIH dynamics, no Gazebo) detached, then
# report what MAVLink endpoints it actually bound. Read-only diagnostics.
set -u
PX4_DIR="$HOME/PX4-Autopilot"
LOG=/tmp/px4_sitl.log

pkill -9 -f 'px4_sitl_default/bin/px4' 2>/dev/null
pkill -9 -f 'bin/px4' 2>/dev/null
sleep 2

cd "$PX4_DIR" || exit 1
mkdir -p build/px4_sitl_default/rootfs
cd build/px4_sitl_default/rootfs || exit 1
# PX4 persists parameters and the simulated vehicle state between runs. Each
# evidence run must start from a disarmed, grounded vehicle.
rm -rf eeprom fs/microsd/*.bson 2>/dev/null

PX4_SYS_AUTOSTART=10040 \
PX4_SIM_MODEL=sihsim_quadx \
  setsid nohup ../bin/px4 -d \
    -s "$PX4_DIR/ROMFS/px4fmu_common/init.d-posix/rcS" \
    "$PX4_DIR/ROMFS/px4fmu_common" \
    > "$LOG" 2>&1 &

for _ in $(seq 1 40); do
  sleep 1
  grep -q 'Ready for takeoff' "$LOG" 2>/dev/null && break
done

echo "=== process ==="
pgrep -a -f 'bin/px4' | head -3
echo "=== bound UDP ==="
ss -ulnp 2>/dev/null | grep -E '145[0-9][0-9]|1855[0-9]' | head -8
echo "=== log tail ==="
tail -n 18 "$LOG"
