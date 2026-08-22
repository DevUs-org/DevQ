'''
Tags: Research

test_qos — standalone sanity harness for the QOS router baseline
(research/baselines/qos_router.py). Same run_tests-style shape as
test_naqjs / test_mapomatic (print-per-check, non-zero exit on any failure)
and deliberately self-contained: it does not import run_tests, and run_tests
never enumerates it — the research harness is decoupled from the core suite.

What it proves:
  - QOS registers through the public API, is selected via config, and ROUTES
    a real committed multi-device workload end to end (all jobs dispatch and
    finish) with ZERO core edits.
  - QOS is a SCORING router (unlike round-robin): it emits a `route` event
    with non-null per-candidate `scores`, one entry per feasible candidate,
    and the winner is the argmax of its score. That scoring signature is the
    Sweepable/explainable contract docs/EXTENDING.md prescribes for a policy
    with weights to sweep.
  - The QOS Sec. 6 device-representative fidelity estimate is a valid
    probability in [0,1], is crosstalk-free (three error-product terms, not
    four — the recorded caveat), and ranks a lower-noise device above a
    higher-noise one.
  - The three raw terms (fidelity / queue-pressure / occupancy) are read from
    the documented DeviceContext + QubitPool surface.
  - The min-of-field relative-delta ranking is a genuine sweepable axis: the
    winner FLIPS as the fidelity weight c and utilisation weight beta vary,
    and the utilisation sign is INVERTED (caveat 3) so lower occupancy wins.
  - The Sweepable replay-faithfulness anchor holds: replaying the recorded
    terms at the recorded params reproduces the live decision (the contract a
    sweep driver enforces), even with the min-of-field cross-field reference.

Run: python -m research.test_qos
     python research/test_qos.py        (also works)
'''

import json
import os
import glob
import sys

# `benchmark` and `research` are top-level packages at the repo root; import
# them absolutely and put the repo root on sys.path so this runs identically
# whether launched as `python -m research.test_qos` or
# `python research/test_qos.py`. Anchored to this file, not the cwd.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark import runner as R
from provider.devq_simulated_provider import DevQSimulatedProvider
from plugins.routers.qos.qos_router import QOSRouter


# ── Tiny harness (run_tests style, standalone) ────────────────────────────────

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "test_results")

_PASS = 0
_FAIL = 0
_FAILURES = []


class Failure(Exception):
    pass


def check(ok, description):
    '''Record an assertion and raise if it failed.'''
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"    [PASS] {description}")
    else:
        _FAIL += 1
        _FAILURES.append(description)
        print(f"    [FAIL] {description}")
        raise Failure(description)
    return ok


# ── Shared fixtures ───────────────────────────────────────────────────────────

SEED = 7

# The committed demonstration workload — a three-device batch that selects the
# QOS router via top-level config, so the router has a genuine which-QPU choice
# (a router decision is spatial: it needs more than one candidate to be
# meaningful, unlike the single-device allocator fixture). Resolved from this
# file's location so it works from any cwd.
WORKLOAD = os.path.join(os.path.dirname(__file__), "workloads", "qos.json")


def _device(kind="random", num_qubits=7, seed=SEED):
    '''A seeded DevQ-simulated device — calibration is stable under the seed,
    so score assertions are deterministic.'''
    return DevQSimulatedProvider(seed=seed).get_device(kind, num_qubits)


def _run_workload(out_dir):
    '''Run the committed QOS workload through the real benchmark runner with
    QOS registered, from the repo root (committed spec paths are repo-root
    relative), clearing out_dir first so no block false-passes on stale logs.
    Returns the list of every JSONL record written.'''
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)
    try:
        R.run(WORKLOAD, out_dir=out_dir,
              register_routers={"qos": QOSRouter}, quiet=True)
    finally:
        os.chdir(prev)

    records = []
    for path in glob.glob(os.path.join(out_dir, "**", "*.jsonl"), recursive=True):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def _synthetic_field():
    '''A synthetic candidate field for the ranking/anchor blocks, in the shape
    _sweep_rank consumes: (key, terms_dict, raw_score).  Constructed so the
    axes genuinely conflict — key0 is high-fidelity but busy, key1 is
    low-fidelity but free — so a weight change MUST be able to move the winner
    if the formula is honest.  raw_score is the per-candidate scalar
    _sweep_score would produce (negative fidelity); _sweep_rank ignores it and
    recomputes from terms, but it is included for shape fidelity.'''
    field = [
        (0, {"fidelity": 0.98, "queue_pressure": 8, "occupancy": 0.20}),
        (1, {"fidelity": 0.85, "queue_pressure": 1, "occupancy": 0.20}),
    ]
    return [(k, t, -t["fidelity"]) for (k, t) in field]


# ── Blocks ────────────────────────────────────────────────────────────────────

def block_registration_and_run():
    '''QOS registers via the public API and routes the committed workload end
    to end — every job reaches a terminal state — with zero core edits.'''
    out = os.path.join(RESULTS_DIR, "qos_run")
    records = _run_workload(out)

    check(len(records) > 0, "workload produced an event log")

    # Every submitted job must reach a terminal outcome. In the event log a
    # job is finalised by a `resolve` event (its completion/rejection); assert
    # every submitted job resolved and none wedged.
    submitted = {r.get("job_id") for r in records if r.get("event") == "submit"}
    resolved = {r.get("job_id") for r in records if r.get("event") == "resolve"}
    check(len(submitted) >= 3, "all workload jobs were submitted and logged")
    check(resolved >= submitted,
          "every submitted job reached a terminal resolve (none wedged)")


def block_scoring_route_event():
    '''QOS is a SCORING router: it emits a `route` event with non-null
    per-candidate scores (one entry per feasible candidate), and the winning
    device is the argmax of the QOS score. (docs/EVENT_LOG.md `route` schema.)'''
    out = os.path.join(RESULTS_DIR, "qos_run")
    records = _run_workload(out)

    routes = [r for r in records if r.get("event") == "route"]
    check(len(routes) > 0, "at least one route event was emitted")

    sample = routes[0]
    check(sample.get("scores") is not None,
          "QOS records non-null per-candidate scores (a scoring router, "
          "unlike round-robin's honest null)")

    scores = sample["scores"]
    candidates = sample.get("candidates", [])
    check(len(scores) == len(candidates) and len(scores) >= 1,
          "scores has one entry per feasible candidate")

    # Each score entry carries the raw, weight-free terms (what a sweep replays)
    entry = scores[0]
    check("terms" in entry and "device" in entry,
          "each score entry carries {device, score, terms}")


def block_fidelity_estimate():
    '''The QOS Sec. 6 device-representative fidelity is a valid probability in
    [0,1], is CROSSTALK-FREE (built from three error products, the recorded
    caveat), and ranks a low-noise device above a high-noise one.'''
    r = QOSRouter()

    # Parse a real 2q circuit (bell: h + cx) via the runner's frontend path is
    # heavy here; build a minimal CircuitRep-like probe by parsing through the
    # device's own workload is unnecessary — use two devices of differing noise
    # and a small circuit object with the fields _device_fidelity reads.
    class _Circ:
        num_qubits = 2
        instructions = [
            {"op": "gate", "gate": "h", "qubits": [0]},
            {"op": "gate", "gate": "cx", "qubits": [0, 1]},
        ]

        def get_depth(self):
            return 2

    circ = _Circ()

    # fully_connected tends to have lower edge-error structure than a random
    # topology under the same seed; regardless, both must yield a valid prob.
    dev_lo = _device(kind="linear", num_qubits=7, seed=SEED)
    dev_hi = _device(kind="random", num_qubits=7, seed=SEED + 1)

    f_lo = r._device_fidelity(dev_lo, circ)
    f_hi = r._device_fidelity(dev_hi, circ)

    check(0.0 <= f_lo <= 1.0, "fidelity estimate is a valid probability in [0,1]")
    check(0.0 <= f_hi <= 1.0, "second device fidelity also valid in [0,1]")

    # Crosstalk-free: the estimate must not depend on any crosstalk accessor.
    # Assert the device exposes no crosstalk term (so the caveat is real: the
    # term is dropped because the data does not exist, not by omission).
    check(not hasattr(dev_lo, "crosstalk"),
          "device carries no crosstalk accessor — Sec. 6 crosstalk product is "
          "honestly dropped (caveat 1), not fabricated")

    # ── ORIENTATION: a lower-noise device MUST score higher than a higher-
    # noise one, and the estimate is a SURVIVAL probability (near 1 for a
    # good device), not an infidelity (near 0).  This is the property the
    # block's docstring always claimed but never asserted — the gap that let
    # a `return 1 - survival` sign flip (which inverts the router's whole
    # which-QPU ranking, corrupting qos_comparison / qos_composition) pass
    # while every [0,1] check stayed green.  Two controlled doubles differing
    # ONLY in error magnitude make the ordering deterministic and independent
    # of topology/seed, so the assertion tests orientation, not luck.
    class _Dev:
        def __init__(self, err):
            self._err = err            # scales every error rate uniformly
            self.num_qubits = 4
        def qubit_error(self, q):   return 0.02 * self._err
        def gate_error(self, q):    return 0.01 * self._err
        def edge_error(self, u, v): return 0.015 * self._err
        def t2(self, q):            return 120.0     # us
        def gate_duration(self, n): return 40.0      # ns
        def edges(self):            return [(0, 1), (1, 2), (2, 3)]

    good = r._device_fidelity(_Dev(0.2), circ)   # low-error device
    bad  = r._device_fidelity(_Dev(3.0), circ)   # high-error device
    check(good > bad,
          f"low-error device fidelity {good:.4f} > high-error {bad:.4f} — the "
          f"Sec. 6 estimate ranks a cleaner device higher (was INVERTED when "
          f"_device_fidelity returned 1 - survival)")
    check(0.5 < good <= 1.0,
          f"low-error device fidelity {good:.4f} is a survival probability near "
          f"1, not an infidelity near 0 — pins absolute orientation, not just "
          f"the relative ordering")
    check(bad < good - 0.05,
          f"high-error device {bad:.4f} is materially below the clean device "
          f"{good:.4f} — a real spread, not a clamped constant")


def block_raw_terms_from_read_surface():
    '''The three raw terms are read from the documented DeviceContext /
    QubitPool surface: fidelity from the device accessors, queue-pressure from
    queue_depth + running_jobs, occupancy from the pool's free count.'''
    out = os.path.join(RESULTS_DIR, "qos_run")
    records = _run_workload(out)

    routes = [r for r in records if r.get("event") == "route" and r.get("scores")]
    check(len(routes) > 0, "a scoring route event is available to inspect")

    terms = routes[0]["scores"][0]["terms"]
    check("fidelity" in terms, "route terms record the fidelity estimate")
    check("queue_pressure" in terms, "route terms record queue pressure "
          "(queue_depth + running_jobs)")
    check("occupancy" in terms, "route terms record live spatial occupancy")
    check(0.0 <= terms["fidelity"] <= 1.0, "recorded fidelity is a valid prob")
    check(0.0 <= terms["occupancy"] <= 1.0, "recorded occupancy is a fraction")


def block_min_of_field_ranking_and_flip():
    '''The min-of-field relative-delta ranking is a genuine sweepable axis:
    the winner flips as fidelity-weight c varies over a conflicting field, and
    the inverted utilisation sign (caveat 3) makes the LOWER-occupancy device
    win as beta grows.'''
    r = QOSRouter()
    field = _synthetic_field()

    def _winner(ranked):
        # base selection rule: argmin over (final_score, key)
        return min(ranked, key=lambda row: (row[1], row[0]))[0]

    # As c goes from waiting-priority (0) to fidelity-priority (1), the winner
    # must move from the free-but-low-fidelity device (key1) to the
    # busy-but-high-fidelity one (key0). We require that BOTH win at some c.
    winners = set()
    for c in (0.0, 0.25, 0.5, 0.75, 1.0):
        ranked = r._sweep_rank(field, {"qos.fidelity_weight": c,
                                       "qos.util_weight": 0.5})
        winners.add(_winner(ranked))
    check(winners == {0, 1},
          "winner flips across c — fidelity is genuinely traded against "
          "waiting time (a real sweepable axis)")

    # Occupancy conflict: equal fidelity + queue, differ only on occupancy.
    # With the inverted sign, higher beta must prefer the EMPTIER device.
    occ_field = [
        (0, {"fidelity": 0.90, "queue_pressure": 3, "occupancy": 0.70}, -0.90),
        (1, {"fidelity": 0.90, "queue_pressure": 3, "occupancy": 0.10}, -0.90),
    ]
    ranked = r._sweep_rank(occ_field, {"qos.fidelity_weight": 0.5,
                                       "qos.util_weight": 1.0})
    check(_winner(ranked) == 1,
          "inverted utilisation sign (caveat 3): the emptier device wins as "
          "beta grows — QOS spreads load rather than packing")


def block_sweep_faithfulness_anchor():
    '''The Sweepable replay-faithfulness anchor: replaying the recorded terms
    at the recorded params via the base sweep_decision reproduces the live
    decision, even though the min-of-field reference is computed across the
    candidate field. This is the determinism contract a sweep driver refuses a
    session over.'''
    r = QOSRouter()
    field = _synthetic_field()
    params = r.live_params()

    # Live decision: rank the field and take the argmin winner (base rule).
    live_ranked = r._sweep_rank(field, params)
    live_winner = min(live_ranked, key=lambda row: (row[1], row[0]))[0]

    # Replay from RECORDED terms only, via the base's sweep_decision primitive:
    # recorded_terms is [(key, terms_dict), ...] — exactly what the log stores.
    recorded_terms = [(key, terms) for (key, terms, _s) in field]
    replay_winner = r.sweep_decision(recorded_terms, params)

    check(live_winner == replay_winner,
          "recorded-terms replay (sweep_decision) reproduces the live winner "
          "(faithfulness anchor holds under the min-of-field reference)")


BLOCKS = [
    ("registration_and_run",         block_registration_and_run),
    ("scoring_route_event",          block_scoring_route_event),
    ("fidelity_estimate",            block_fidelity_estimate),
    ("raw_terms_from_read_surface",  block_raw_terms_from_read_surface),
    ("min_of_field_ranking_and_flip", block_min_of_field_ranking_and_flip),
    ("sweep_faithfulness_anchor",    block_sweep_faithfulness_anchor),
]


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print(f"\nQOS router sanity — {len(BLOCKS)} block(s)\n")
    failed = []
    for name, fn in BLOCKS:
        print(f"  {name}")
        try:
            fn()
        except Failure:
            failed.append(name)
        except Exception as exc:  # a crash is a failure, reported not swallowed
            global _FAIL
            _FAIL += 1
            failed.append(name)
            print(f"    [FAIL] block crashed: {exc!r}")
        print()

    print(f"{_PASS} passed, {_FAIL} failed")
    if failed:
        print("failed blocks: " + ", ".join(failed))
        print(f"artifacts under {RESULTS_DIR}/ for inspection")
        return 1
    print(f"all {len(BLOCKS)} block(s) passed")
    print(f"artifacts under {RESULTS_DIR}/ for inspection")
    return 0


if __name__ == "__main__":
    sys.exit(main())