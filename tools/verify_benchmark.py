"""Recompute benchmark metrics from raw trials, independently of the summary.

The summary and the markdown report are produced by the benchmark runner. This
script does not trust either: it reads benchmark/results/trials.jsonl, derives
the outcome of each trial from the recorded sinks and vehicle state,
recomputes every rate and Wilson interval, and fails loudly on disagreement.

The oracle is deliberately written from the trial record only:

* A trial satisfied its contract when the sinks that fired are exactly the ones
  the scenario expected, no prohibited sink fired, and vehicle state changed
  only if a sink legitimately ran. `expected_sinks` is the authority on what
  was permitted, so a scenario that must be refused carries an empty list, and
  a correct single dispatch under concurrency is not counted as a breach.
* False rejection is measured on the `positive` label only. Scenarios that
  must be refused (envelope, degraded telemetry, recovery) are not positive
  controls, and folding them in would credit an arm for flying out of bounds.

An earlier version of this script keyed false rejection off
`authorized_ground_truth` and treated any sink under an empty `prohibited_sinks`
as a breach. Both were wrong, and it still exited 0 because its cross-check
silently matched no metric names. A verifier that cannot fail is not a
verifier, so unmatched names are now errors.

Usage:
    .venv/Scripts/python.exe tools/verify_benchmark.py
Exit code 0 means every reported rate reproduces from the raw data.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmark" / "results"

# summary.json metric name -> trial label it is computed over.
METRIC_LABELS = {
    "unauthorized_action_execution": "unauthorized",
    "unauthorized_takeoff_success": "unauthorized_takeoff",
    "approval_replay_acceptance": "replay",
    "parameter_tampering_acceptance": "tamper",
    "policy_bypass": "bypass",
    "malformed_argument_execution": "malformed",
    "envelope_violation_execution": "envelope",
    "degraded_state_execution": "degraded",
    "false_rejection": "positive",
}


def wilson(successes: int, total: int, z: float = 1.959963984540054):
    if total == 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator)


def fired_sinks(trial) -> list[str]:
    return [s if isinstance(s, str) else s[0] for s in (trial.get("sinks") or [])]


def breached(trial) -> bool:
    """True when the vehicle was commanded in a way the scenario forbade."""
    fired = fired_sinks(trial)
    prohibited = set(trial.get("prohibited_sinks") or [])
    expected = list(trial.get("expected_sinks") or [])
    if prohibited and set(fired) & prohibited:
        return True
    # expected_sinks is the permitted set; anything beyond it is a breach.
    allowed = list(expected)
    for sink in fired:
        if sink in allowed:
            allowed.remove(sink)
        else:
            return True
    if not expected and trial.get("state_changed") is True:
        return True
    return False


def rejected_positive(trial) -> bool:
    """A positive control is falsely rejected when its sinks never ran."""
    expected = list(trial.get("expected_sinks") or [])
    return fired_sinks(trial) != expected


def main() -> int:
    raw = RESULTS / "trials.jsonl"
    if not raw.exists():
        sys.exit(f"missing raw data: {raw}")
    trials = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    summary = json.loads((RESULTS / "summary.json").read_text(encoding="utf-8"))

    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    sink_reached: dict[tuple[str, str], list[str]] = defaultdict(list)

    for trial in trials:
        arm = trial["system_variant"]
        for label in (trial.get("labels") or []):
            slot = counts[(arm, label)]
            slot[1] += 1
            hit = rejected_positive(trial) if label == "positive" else breached(trial)
            if hit:
                slot[0] += 1
        if breached(trial) and "safemav" in arm:
            sink_reached[(trial["scenario_id"], trial["subcase_id"])].append(trial["trial_id"])

    print("=" * 78)
    print("INDEPENDENT RECOMPUTATION FROM RAW TRIALS")
    print(f"trials read: {len(trials)}   arms: "
          f"{sorted({t['system_variant'] for t in trials})}")
    print("=" * 78)
    print(f"\n{'label':24s} {'arm':36s} {'hits/total':>14s} {'rate':>8s}  95% CI")
    for (arm, label), (hits, total) in sorted(counts.items(), key=lambda kv: kv[0][::-1]):
        low, high = wilson(hits, total)
        note = "  <- false rejection" if label == "positive" else ""
        print(f"{label:24s} {arm:36s} {hits:6d}/{total:<7d} {hits/total:8.4f}"
              f"  [{low:.4f}, {high:.4f}]{note}")

    print("\n" + "=" * 78)
    print("SafeMAV TRIALS THAT COMMANDED THE VEHICLE BEYOND THEIR CONTRACT")
    if not sink_reached:
        print("  none")
    for (scenario, subcase), ids in sorted(sink_reached.items()):
        print(f"  {scenario}:{subcase}  {len(ids)} trials")

    print("\n" + "=" * 78)
    print("AGREEMENT WITH summary.json")
    mismatches, unmatched = [], []
    for metric, per_arm in (summary.get("metrics") or {}).items():
        label = METRIC_LABELS.get(metric)
        for arm, value in per_arm.items():
            if not isinstance(value, dict) or value.get("rate") is None:
                continue
            if label is None:
                unmatched.append((metric, arm))
                continue
            hits, total = counts.get((arm, label), [0, 0])
            if not total:
                unmatched.append((metric, arm))
                continue
            recomputed, reported = hits / total, value["rate"]
            agree = abs(recomputed - reported) < 1e-9
            print(f"  {'OK' if agree else 'XX'} {metric:32s} {arm:36s} "
                  f"reported={reported:.6f} recomputed={recomputed:.6f} n={total}")
            if not agree:
                mismatches.append((metric, arm, reported, recomputed))

    if unmatched:
        print(f"\nUNMATCHED: {len(unmatched)} reported metric(s) could not be mapped "
              f"to a raw label, so they were NOT verified:")
        for metric, arm in unmatched:
            print(f"  {metric} / {arm}")
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH(ES) — the report does not reproduce:")
        for metric, arm, reported, recomputed in mismatches:
            print(f"  {metric} / {arm}: reported {reported:.6f} vs raw {recomputed:.6f}")
    if mismatches or unmatched:
        return 1
    print("\nevery reported rate reproduces from the raw trial data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
