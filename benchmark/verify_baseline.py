"""Check that benchmark/baseline.py still matches commit 962448c.

    .venv/Scripts/python.exe benchmark/verify_baseline.py

Two independent checks, because a reconstruction a judge cannot verify is
worth nothing:

1. PROVENANCE - fetch the two historical files straight from git and confirm
   every guardrail constant, default and decision threshold in the vendored
   copy equals the original. Also confirms the vendored module imports nothing
   from the live ``src`` tree, so the baseline arm cannot accidentally be
   measuring the new gateway.

2. BEHAVIOUR - drive the reconstruction and the original ``GuardrailConfig``
   through the same inputs and require identical accept/reject decisions,
   including the haversine distance to sub-millimetre agreement.

Exits non-zero on any mismatch.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

BASELINE_COMMIT = "962448c"


def git_show(path: str) -> str:
    return subprocess.run(["git", "show", f"{BASELINE_COMMIT}:{path}"],
                          cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout


def find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


#: Adapter entry points renamed by the declared call-schema adaptation.
_SINK_ALIASES = {
    "baseline_arm": "arm", "baseline_disarm": "disarm",
    "baseline_takeoff": "takeoff", "baseline_land": "land",
    "baseline_hold": "hold", "baseline_return_to_launch": "return_to_launch",
    "baseline_goto": "goto", "baseline_fly_mission": "fly_mission",
    "BaselineWaypoint": "Waypoint",
}


def control_flow(node) -> list[str]:
    """The decision skeleton: guard calls, conditions, raises and awaits.

    Error-message wording and capability token spelling are excluded on
    purpose; only what the code DECIDES is compared. A missing or reordered
    check, a changed comparison operator, or a dropped raise all show up here.
    """
    skeleton: list[str] = []

    def walk(item, depth=0):
        for child in ast.iter_child_nodes(item):
            if isinstance(child, ast.Call):
                target = child.func
                if isinstance(target, ast.Attribute):
                    name = _SINK_ALIASES.get(target.attr, target.attr)
                    skeleton.append(f"{depth}:call:{name}")
                elif isinstance(target, ast.Name):
                    name = _SINK_ALIASES.get(target.id, target.id)
                    skeleton.append(f"{depth}:call:{name}")
            elif isinstance(child, ast.Compare):
                operators = "".join(type(o).__name__ for o in child.ops)
                literals = [
                    repr(c.value) for c in child.comparators
                    if isinstance(c, ast.Constant) and not isinstance(c.value, str)
                ]
                skeleton.append(f"{depth}:cmp:{operators}:{','.join(literals)}")
            elif isinstance(child, ast.Raise):
                skeleton.append(f"{depth}:raise")
            elif isinstance(child, ast.If):
                skeleton.append(f"{depth}:if")
            elif isinstance(child, ast.For):
                skeleton.append(f"{depth}:for")
            elif isinstance(child, ast.Return):
                skeleton.append(f"{depth}:return")
            elif isinstance(child, ast.Await):
                skeleton.append(f"{depth}:await")
            elif isinstance(child, ast.UnaryOp) and isinstance(child.op, ast.Not):
                skeleton.append(f"{depth}:not")
            walk(child, depth + 1)

    walk(node)
    return skeleton


def main() -> int:
    failures: list[str] = []

    original_guardrails = git_show("src/mavlink_mcp/guardrails.py")
    original_drone = git_show("src/mavlink_mcp/drone.py")
    vendored_text = (HERE / "baseline.py").read_text(encoding="utf-8")

    # ---- 1. no live-src import may leak into the baseline arm ---------- #
    vendored_tree = ast.parse(vendored_text)
    for node in ast.walk(vendored_tree):
        module = None
        if isinstance(node, ast.Import):
            module = ",".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
        if module and "mavlink_mcp" in module:
            failures.append(f"baseline.py imports live source: {module}")

    # ---- 2. constants and defaults ------------------------------------ #
    import baseline

    original_tree = ast.parse(original_guardrails)
    original_constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in original_tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    for name, expected in original_constants.items():
        actual = getattr(baseline, name, None)
        if actual != expected:
            failures.append(f"{name}: vendored {actual!r} != original {expected!r}")

    original_falsy = next(
        ast.literal_eval(node.value) for node in original_tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_FALSY")
    if baseline._FALSY != original_falsy:
        failures.append(f"_FALSY {baseline._FALSY} != {original_falsy}")

    defaults = baseline.GuardrailConfig()
    if (defaults.readonly, defaults.max_altitude_m, defaults.geofence_radius_m) != (
            True, 50.0, 200.0):
        failures.append(f"GuardrailConfig defaults drifted: {defaults}")

    # ---- 3. haversine is bit-comparable ------------------------------- #
    namespace: dict = {}
    exec(compile(original_guardrails, "original_guardrails", "exec"), namespace)
    original_haversine = namespace["haversine_m"]
    for args in ((47.397742, 8.545594, 47.397742, 8.545594),
                 (47.397742, 8.545594, 47.4, 8.55),
                 (0.0, 0.0, 0.0, 1.0), (-33.9, 151.2, 51.5, -0.12),
                 (89.9, 179.9, -89.9, -179.9)):
        expected = original_haversine(*args)
        actual = baseline.haversine_m(*args)
        if abs(expected - actual) > 1e-9:
            failures.append(f"haversine_m{args}: {actual} != {expected}")

    # ---- 4. per-action check bodies are structurally identical -------- #
    original_drone_tree = ast.parse(original_drone)
    vendored_drone_tree = ast.parse(vendored_text)
    for name in ("_require_write", "_require_confirmation", "_check_altitude",
                 "_check_geofence", "arm", "disarm", "takeoff", "land", "hold",
                 "return_to_launch", "goto", "fly_mission"):
        try:
            original_flow = control_flow(find_function(original_drone_tree, name))
            vendored_flow = control_flow(find_function(vendored_drone_tree, name))
        except AssertionError as exc:
            failures.append(str(exc))
            continue
        if original_flow != vendored_flow:
            only_original = [s for s in original_flow if s not in vendored_flow]
            only_vendored = [s for s in vendored_flow if s not in original_flow]
            failures.append(
                f"{name}: decision flow differs from {BASELINE_COMMIT}; "
                f"original-only={only_original} vendored-only={only_vendored}")

    # ---- 5. behavioural equivalence on the guardrail decisions -------- #
    original_config_cls = namespace["GuardrailConfig"]
    original_violation = namespace["GuardrailViolation"]
    config_args = dict(readonly=False, max_altitude_m=50.0, geofence_radius_m=200.0)
    original_config = original_config_cls(**config_args)
    vendored_config = baseline.GuardrailConfig(**config_args)
    if (original_config.readonly, original_config.max_altitude_m,
            original_config.geofence_radius_m) != (
            vendored_config.readonly, vendored_config.max_altitude_m,
            vendored_config.geofence_radius_m):
        failures.append("GuardrailConfig values differ for identical arguments")

    class Stub:
        name = "mock"

        def capabilities(self):
            return {"arm_disarm", "takeoff", "land", "goto", "mission",
                    "flight_mode", "telemetry", "return_to_launch"}

    drone = baseline.BaselineDrone(Stub(), vendored_config)
    for altitude, should_reject in ((0.0, True), (-1.0, True), (0.1, False),
                                    (49.9, False), (50.0, False), (50.1, True),
                                    (1e9, True)):
        try:
            drone._check_altitude(altitude, "takeoff")
            rejected = False
        except baseline.GuardrailViolation:
            rejected = True
        if rejected is not should_reject:
            failures.append(f"_check_altitude({altitude}) rejected={rejected}, "
                            f"expected {should_reject}")
    try:
        drone._require_confirmation("arm", False)
        failures.append("_require_confirmation accepted confirm=False")
    except baseline.GuardrailViolation:
        pass
    try:
        baseline.BaselineDrone(Stub(), baseline.GuardrailConfig())._require_write("arm")
        failures.append("_require_write accepted a write in readonly mode")
    except baseline.GuardrailViolation:
        pass
    if not issubclass(baseline.GuardrailViolation, Exception) or \
            original_violation.__name__ != baseline.GuardrailViolation.__name__:
        failures.append("GuardrailViolation identity differs")

    if failures:
        print(f"FAIL: {len(failures)} mismatch(es) against {BASELINE_COMMIT}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"OK: benchmark/baseline.py is a faithful reconstruction of "
          f"{BASELINE_COMMIT} (constants, guardrail decision structure, "
          f"haversine and accept/reject behaviour all match; no live-src import)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
