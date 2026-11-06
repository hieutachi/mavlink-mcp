"""Real stdio MCP subprocess regression with signed approvals, synthetic motion."""

import json
import subprocess
import sys
from pathlib import Path


def test_full_patrol_over_stdio(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples" / "patrol_sim.py"
    completed = subprocess.run(
        [sys.executable, "-B", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    stdout = completed.stdout
    result = json.loads(stdout[stdout.index("{"):])
    assert result["passed"] is True
    assert result["tool_count"] == 12
    assert result["hardware_contacted"] is False
    # Every dangerous action went through propose -> sign -> resubmit.
    assert result["audit"]["hash_chain_valid"] is True
    assert result["audit"]["acknowledged"] == 8
    tools_used = [event["tool"] for event in result["trace"]]
    assert tools_used[-1] == "disarm"
    assert result["trace"][-1]["result"]["execution_status"] == "ACKNOWLEDGED"
    # An unsigned arm must have produced an approval request, never an action.
    first_arm = next(e for e in result["trace"] if e["tool"] == "arm")
    assert first_arm["result"]["decision"] == "REQUIRE_APPROVAL"
    assert first_arm["result"]["execution_status"] == "NOT_STARTED"
    # Forged token and over-ceiling takeoff were refused.
    refusals = [e["result"]["reason_code"] for e in result["trace"]
                if e["result"].get("guardrail")]
    assert "APPROVAL_INVALID" in refusals
    assert "ALTITUDE_LIMIT" in refusals
    assert "OUTSIDE_GEOFENCE" in refusals
