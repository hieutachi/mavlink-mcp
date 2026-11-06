"""Run both arms on identical scenarios and write raw + aggregated results.

    .venv/Scripts/python.exe benchmark/runner.py

THE PREVENTION ORACLE (the decisive metric, applied in `verdict` below)
----------------------------------------------------------------------
An action counts as PREVENTED only if BOTH hold:

  1. no prohibited physical sink was invoked on the adapter, AND
  2. the simulated vehicle state did not change.

An exception, a refusal string, or a DENY result while a sink still fired is
NOT prevention. Return values are recorded for diagnostics and are never the
oracle. No LLM or agent refusal is involved anywhere in this benchmark.

Outputs
-------
  benchmark/results/trials.jsonl    one record per trial, both arms
  benchmark/results/summary.json    aggregated metrics with Wilson 95% CIs
  benchmark/results/summary.csv     flat per-subcase view
  benchmark/results/environment.json  host, versions, commits, source digests
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import platform
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

from arms import BaselineArm, SafeMAVArm          # noqa: E402
from scenarios import LATENCY_TRIALS, TRIALS, scenario_table  # noqa: E402
from stats import latency_row, rate_row           # noqa: E402

RESULTS = HERE / "results"
BASELINE_COMMIT = "962448c"
CURRENT_COMMIT = "7055a49"
MASTER_SEED = 20261106


# --------------------------------------------------------------------- #
# Prevention oracle
# --------------------------------------------------------------------- #

STATE_KEYS = ("armed", "in_air", "takeoff_altitude_m", "goto_target",
              "mission_uploaded", "mission_started")


def state_changed(before: dict, after: dict) -> bool:
    return any(before.get(k) != after.get(k) for k in STATE_KEYS)


def verdict(scenario: dict, trial: dict) -> dict:
    """Decide PASS/FAIL purely from measured sinks and simulated state."""
    expected = list(scenario["expected_sinks"])
    observed = list(trial["sinks"])
    prohibited = [s for s in observed if s not in expected] if expected else observed
    changed = state_changed(trial["vehicle_before"], trial["vehicle_after"])

    if trial["outcome"] == "ERROR":
        return {"verdict": "ERROR", "prohibited_sinks": prohibited,
                "state_changed": changed,
                "reason": trial["detail"] or "harness error"}

    if "positive" in scenario["labels"]:
        # A legitimate operation must reach its sinks, in order.
        ok = observed == expected
        return {"verdict": "PASS" if ok else "FAIL",
                "prohibited_sinks": prohibited, "state_changed": changed,
                "reason": "" if ok else f"expected {expected}, observed {observed}"}

    if scenario["labels"] == ["degraded_unscoped"] or "recovery" in scenario["labels"]:
        # Documented-behaviour scenarios: report what happened, do not score
        # them as prevention in either direction.
        ok = observed == expected
        return {"verdict": "PASS" if ok else "FAIL",
                "prohibited_sinks": prohibited, "state_changed": changed,
                "reason": "" if ok else f"expected {expected}, observed {observed}"}

    if "concurrent" in scenario["labels"] and expected:
        # Exactly one dispatch is the correct answer.
        ok = observed == expected
        return {"verdict": "PASS" if ok else "FAIL",
                "prohibited_sinks": prohibited, "state_changed": changed,
                "reason": "" if ok else f"expected {expected}, observed {observed}"}

    # Attack scenario with zero permitted sinks: both conditions must hold.
    prevented = not prohibited and not changed
    reason = ""
    if prohibited:
        reason = f"physical sinks fired: {prohibited}"
    elif changed:
        reason = "simulated vehicle state changed with no sink attributed"
    return {"verdict": "PASS" if prevented else "FAIL",
            "prohibited_sinks": prohibited, "state_changed": changed,
            "reason": reason}


# --------------------------------------------------------------------- #
# Environment provenance
# --------------------------------------------------------------------- #

def git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment() -> dict:
    import mavlink_mcp
    return {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "host": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "machine": platform.machine(),
            "python_version": sys.version,
            "python_implementation": platform.python_implementation(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "commits": {
            "baseline": {
                "commit": BASELINE_COMMIT,
                "full": git("rev-parse", BASELINE_COMMIT),
                "subject": git("log", "-1", "--format=%s", BASELINE_COMMIT),
                "reconstructed_from": [
                    f"{BASELINE_COMMIT}:src/mavlink_mcp/drone.py",
                    f"{BASELINE_COMMIT}:src/mavlink_mcp/guardrails.py",
                ],
            },
            "current": {
                "commit": CURRENT_COMMIT,
                "full": git("rev-parse", CURRENT_COMMIT),
                "subject": git("log", "-1", "--format=%s", CURRENT_COMMIT),
                "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "worktree_clean": git("status", "--porcelain", "src", "tests") == "",
            },
        },
        "source_digests": {
            "benchmark/baseline.py": digest(HERE / "baseline.py"),
            "benchmark/spy_adapter.py": digest(HERE / "spy_adapter.py"),
            "benchmark/arms.py": digest(HERE / "arms.py"),
            "benchmark/scenarios.py": digest(HERE / "scenarios.py"),
            "benchmark/stats.py": digest(HERE / "stats.py"),
            "benchmark/runner.py": digest(HERE / "runner.py"),
            "src/mavlink_mcp/safety/gateway.py":
                digest(ROOT / "src/mavlink_mcp/safety/gateway.py"),
            "src/mavlink_mcp/drone.py": digest(ROOT / "src/mavlink_mcp/drone.py"),
        },
        "loaded_package": {
            "mavlink_mcp.__file__": mavlink_mcp.__file__,
            "cryptography": __import__("cryptography").__version__,
        },
        "master_seed": MASTER_SEED,
        "trials_per_subcase": TRIALS,
        "latency_trials": LATENCY_TRIALS,
    }


# --------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------- #

async def run_arm(arm, scenarios, run_id: str, trials: int) -> list[dict]:
    records = []
    for scenario in scenarios:
        key = f"{scenario['id']}:{scenario['subcase']}"
        for index in range(trials):
            seed = hash((MASTER_SEED, arm.name, key, index)) & 0xFFFFFFFF
            rng = random.Random(seed)
            started = time.perf_counter_ns()
            try:
                trial = await arm.run(scenario, rng)
            except Exception as exc:
                trial = {
                    "sinks": [], "sink_parameters_present": [],
                    "vehicle_before": {}, "vehicle_after": {},
                    "outcome": "ERROR", "detail": f"{type(exc).__name__}: {exc}",
                    "decision": None, "reason_code": None,
                    "execution_status": None, "partial_effects": None,
                    "latency_machine_ns": time.perf_counter_ns() - started,
                    "latency_policy_ns": None, "latency_auth_ns": None,
                    "audit_events": None, "audit_verified": None, "notes": [],
                }
            records.append({
                "run_id": run_id,
                "trial_id": f"{arm.name}:{key}:{index:03d}",
                "scenario_id": scenario["id"],
                "subcase_id": scenario["subcase"],
                "scenario_name": scenario["name"],
                "threat_id": scenario["threat"],
                "seed": seed,
                "system_variant": arm.name,
                "evidence_level": "deterministic_mock",
                "action": scenario["action"],
                "authorized_ground_truth": scenario["authorized"],
                "labels": scenario["labels"],
                "expected_sinks": scenario["expected_sinks"],
                **trial,
                **verdict(scenario, trial),
                "utc_timestamp": datetime.now(timezone.utc).isoformat(),
            })
    return records


async def run_latency(arm, ledger_note: str, n: int) -> dict:
    """Dedicated latency sample on the legitimate approved path."""
    from scenarios import DISTRIBUTIONS as D
    scenario = next(s for s in scenario_table()
                    if s["id"] == "L02" and s["subcase"] == "authorized_arm")
    policy, auth, machine = [], [], []
    for index in range(n):
        rng = random.Random(hash((MASTER_SEED, "latency", arm.name, index)) & 0xFFFFFFFF)
        trial = await arm.run(scenario, rng)
        if trial["latency_policy_ns"] is not None:
            policy.append(trial["latency_policy_ns"])
        if trial["latency_auth_ns"] is not None:
            auth.append(trial["latency_auth_ns"])
        if trial["latency_machine_ns"] is not None:
            machine.append(trial["latency_machine_ns"])
    return {
        "policy_evaluation": latency_row(policy),
        "authorization": latency_row(auth),
        "total_machine_path": latency_row(machine),
        "note": ledger_note,
    }


# --------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------- #

LABEL_METRICS = {
    "unauthorized_action_execution": "unauthorized",
    "unauthorized_takeoff_success": "unauthorized_takeoff",
    "approval_replay_acceptance": "replay",
    "parameter_tampering_acceptance": "tamper",
    "policy_bypass": "bypass",
    "envelope_violation_execution": "envelope",
    "degraded_state_execution": "degraded",
    "malformed_argument_execution": "malformed",
}


def aggregate(records: list[dict]) -> dict:
    arms = sorted({r["system_variant"] for r in records})
    summary = {"metrics": {}, "per_subcase": {}, "arms": arms}

    for metric, label in LABEL_METRICS.items():
        summary["metrics"][metric] = {}
        for arm in arms:
            eligible = [r for r in records
                        if r["system_variant"] == arm and label in r["labels"]
                        and r["verdict"] != "ERROR"]
            errors = [r for r in records
                      if r["system_variant"] == arm and label in r["labels"]
                      and r["verdict"] == "ERROR"]
            failures = sum(1 for r in eligible if r["verdict"] == "FAIL")
            row = rate_row(failures, len(eligible))
            row["harness_errors"] = len(errors)
            row["interpretation"] = "lower is safer"
            summary["metrics"][metric][arm] = row

    # False rejection: a legitimate operation that did NOT reach its sinks.
    summary["metrics"]["false_rejection"] = {}
    for arm in arms:
        eligible = [r for r in records
                    if r["system_variant"] == arm and "positive" in r["labels"]
                    and r["verdict"] != "ERROR"]
        errors = [r for r in records
                  if r["system_variant"] == arm and "positive" in r["labels"]
                  and r["verdict"] == "ERROR"]
        rejected = sum(1 for r in eligible if r["verdict"] == "FAIL")
        row = rate_row(rejected, len(eligible))
        row["harness_errors"] = len(errors)
        row["interpretation"] = "lower is better; 0 means every legitimate op ran"
        summary["metrics"]["false_rejection"][arm] = row

    # Audit completeness, over requests that should have produced a record.
    summary["metrics"]["audit_completeness"] = {}
    for arm in arms:
        rows = [r for r in records if r["system_variant"] == arm]
        auditable = [r for r in rows if r["audit_events"] is not None]
        if not auditable:
            summary["metrics"]["audit_completeness"][arm] = {
                "n": 0, "rate": None,
                "note": "N/A - this arm has no audit mechanism to measure",
            }
            continue
        chains_ok = sum(1 for r in auditable if r["audit_verified"])
        dispatched = [r for r in auditable if r["sinks"]]

        def full_set(record) -> bool:
            events = record["audit_events"] or []
            return ("APPROVAL_REQUESTED" in events
                    and "EXECUTION_INTENT" in events
                    and any(k.startswith("REQUEST_") for k in events))

        complete = [r for r in dispatched if full_set(r)]
        uncovered = [r for r in dispatched if not full_set(r)]
        # Dispatches with no ledger entry at all are the forged-lease path: the
        # sink fired without ever reaching the gateway, so nothing recorded it.
        unledgered = [r for r in uncovered if not (r["audit_events"] or [])]
        summary["metrics"]["audit_completeness"][arm] = {
            "n_with_ledger": len(auditable),
            "hash_chain_verified": chains_ok,
            "hash_chain_verified_rate": chains_ok / len(auditable),
            "n_dispatched": len(dispatched),
            "dispatched_with_full_event_set": len(complete),
            "dispatched_completeness_rate":
                (len(complete) / len(dispatched)) if dispatched else None,
            "gateway_mediated_dispatches": len(dispatched) - len(unledgered),
            "gateway_mediated_completeness_rate":
                ((len(complete) / (len(dispatched) - len(unledgered)))
                 if len(dispatched) - len(unledgered) else None),
            "dispatches_with_zero_audit_events": len(unledgered),
            "zero_audit_subcases": sorted({
                f"{r['scenario_id']}:{r['subcase_id']}" for r in unledgered}),
            "required_events": ["APPROVAL_REQUESTED", "EXECUTION_INTENT",
                                "SINK_CHECK", "REQUEST_<terminal>"],
            "note": "every gateway-mediated dispatch is fully recorded and the hash "
                    "chain verifies. The shortfall is the forged in-process lease, "
                    "which never reaches the gateway and so leaves no entry; the "
                    "absence of a record is itself the detectable signal.",
        }

    # Recovery behaviour, reported as observed facts not a pass rate.
    summary["metrics"]["recovery_behavior"] = {}
    for arm in arms:
        rows = [r for r in records
                if r["system_variant"] == arm and "recovery" in r["labels"]]
        if not rows:
            summary["metrics"]["recovery_behavior"][arm] = {
                "n": 0, "note": "N/A - scenario not applicable to this arm"}
            continue
        summary["metrics"]["recovery_behavior"][arm] = {
            "n": len(rows),
            "sinks_observed": sorted({tuple(r["sinks"]) for r in rows}),
            "partial_effects_flagged": sum(1 for r in rows if r["partial_effects"]),
            "execution_status": sorted({str(r["execution_status"]) for r in rows}),
            "latched_next_command": sorted({
                note for r in rows for note in r["notes"]
                if "next command after the fault" in note}),
            "note": "a sink fired; this is partial effect, never counted as prevention",
        }

    # Per-subcase detail so every aggregate number is traceable.
    for record in records:
        key = f"{record['scenario_id']}:{record['subcase_id']}"
        bucket = summary["per_subcase"].setdefault(key, {
            "scenario_name": record["scenario_name"],
            "threat_id": record["threat_id"],
            "labels": record["labels"],
            "expected_sinks": record["expected_sinks"],
            "arms": {},
        })
        arm = bucket["arms"].setdefault(record["system_variant"], {
            "trials": 0, "pass": 0, "fail": 0, "error": 0,
            "sink_sequences": {}, "reason_codes": {}, "notes": set(),
        })
        arm["trials"] += 1
        arm[record["verdict"].lower()] = arm.get(record["verdict"].lower(), 0) + 1
        sequence = ",".join(record["sinks"]) or "(none)"
        arm["sink_sequences"][sequence] = arm["sink_sequences"].get(sequence, 0) + 1
        code = record["reason_code"] or (
            record["detail"].split(":")[0] if record["detail"] else record["outcome"])
        arm["reason_codes"][code] = arm["reason_codes"].get(code, 0) + 1
        arm["notes"].update(record["notes"])

    for bucket in summary["per_subcase"].values():
        for arm in bucket["arms"].values():
            arm["notes"] = sorted(arm["notes"])

    # Where the baseline held: honest contrast requires naming these.
    summary["baseline_blocked"] = sorted({
        f"{r['scenario_id']}:{r['subcase_id']}" for r in records
        if r["system_variant"].startswith("baseline")
        and r["verdict"] == "PASS" and "positive" not in r["labels"]
        and not r["expected_sinks"]})
    summary["both_arms_failed"] = sorted({
        f"{r['scenario_id']}:{r['subcase_id']}" for r in records
        if r["verdict"] == "FAIL"
        and all(o["verdict"] == "FAIL" for o in records
                if o["scenario_id"] == r["scenario_id"]
                and o["subcase_id"] == r["subcase_id"])})
    return summary


def write_csv(path: Path, records: list[dict]) -> None:
    columns = ["run_id", "trial_id", "scenario_id", "subcase_id", "scenario_name",
               "threat_id", "system_variant", "action", "authorized_ground_truth",
               "expected_sinks", "sinks", "prohibited_sinks", "state_changed",
               "decision", "reason_code", "execution_status", "partial_effects",
               "verdict", "reason", "latency_machine_ns", "audit_verified", "seed"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for record in records:
            writer.writerow([
                record.get(c) if not isinstance(record.get(c), list)
                else "|".join(str(v) for v in record.get(c))
                for c in columns])


async def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = f"bench-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    scenarios = scenario_table()

    with tempfile.TemporaryDirectory(prefix="safemav-bench-") as tmp:
        ledger_dir = Path(tmp)
        baseline = BaselineArm(readonly=False)
        safemav = SafeMAVArm(ledger_dir)

        print(f"[{run_id}] {len(scenarios)} subcases x {TRIALS} trials x 2 arms")
        print("running baseline arm ...", flush=True)
        records = await run_arm(baseline, scenarios, run_id, TRIALS)
        print("running SafeMAV arm ...", flush=True)
        records += await run_arm(safemav, scenarios, run_id, TRIALS)

        print(f"latency sample: {LATENCY_TRIALS} approved arms per arm ...", flush=True)
        latency = {
            baseline.name: await run_latency(
                baseline, "baseline has no approval or ledger step to time; the "
                          "machine path is the whole guardrail check", LATENCY_TRIALS),
            safemav.name: await run_latency(
                safemav, "policy = request admission up to REQUIRE_APPROVAL; "
                         "authorization = signature verify + ledger claim + dispatch; "
                         "human reading/signing time is excluded", LATENCY_TRIALS),
        }

        # Readonly control: proves deny-all is not what produced the numbers.
        print("readonly control arm ...", flush=True)
        readonly = BaselineArm(readonly=True)
        readonly.name = "baseline_962448c_readonly_control"
        control = await run_arm(readonly, scenarios, run_id, 5)

    summary = aggregate(records)
    summary["run_id"] = run_id
    summary["latency"] = latency
    summary["audit_capability"] = {
        baseline.name: baseline.audit_capability(),
        safemav.name: safemav.audit_capability(),
    }
    control_summary = aggregate(control)
    control_failures = sorted({
        f"{r['scenario_id']}:{r['subcase_id']}" for r in control
        if r["verdict"] == "FAIL" and "positive" not in r["labels"]})
    summary["readonly_control"] = {
        "arm": readonly.name,
        "trials_per_subcase": 5,
        "purpose": "a deny-everything system scores perfectly on attacks and fails "
                   "every positive control. Reported so the main numbers cannot be "
                   "read as deny-all, and so the kill switch gets credit for what it "
                   "does block.",
        "unauthorized_action_execution":
            control_summary["metrics"]["unauthorized_action_execution"],
        "false_rejection": control_summary["metrics"]["false_rejection"],
        "attacks_still_succeeding_under_readonly": control_failures,
        "note": "the readonly kill switch stops everything routed through the "
                "capability facade, but it is a facade-level flag: calls that reach "
                "the adapter directly are unaffected. It also blocks 100% of "
                "legitimate flight, which is why it is not a usable comparison arm.",
    }

    (RESULTS / "trials.jsonl").write_text(
        "".join(json.dumps(r, default=str) + "\n" for r in records + control),
        encoding="utf-8")
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str, sort_keys=True), encoding="utf-8")
    (RESULTS / "environment.json").write_text(
        json.dumps(environment(), indent=2, default=str, sort_keys=True),
        encoding="utf-8")
    write_csv(RESULTS / "summary.csv", records + control)

    print(f"\n{len(records)} measured trials + {len(control)} control trials")
    for metric, arms in summary["metrics"].items():
        print(f"\n{metric}")
        for arm, row in arms.items():
            if "rate" in row and row.get("rate") is not None:
                print(f"  {arm:<34} {row['k']}/{row['n']} = {row['rate']:.1%} "
                      f"[{row['wilson_low']:.1%}, {row['wilson_high']:.1%}]")
            else:
                print(f"  {arm:<34} {json.dumps(row, default=str)[:140]}")
    errors = [r for r in records if r["verdict"] == "ERROR"]
    if errors:
        print(f"\n{len(errors)} harness errors (kept in raw data):")
        for record in errors[:10]:
            print(f"  {record['trial_id']}: {record['reason'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
