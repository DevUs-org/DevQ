'''
Tags: Research

test_naqjs — a self-contained sanity harness for the NAQJS scheduler
baseline (research/baselines/naqjs_scheduler.py).

Inspired by run_tests.py in STYLE (a `check` assertion, block functions,
a pass/fail summary) but deliberately standalone: it does not import
run_tests' helpers, because NAQJS is `Research`-tagged paper tooling that
uses DevQ but is not part of it, and coupling it to the core test harness
would break that separation. It writes run artifacts (event logs, metrics)
under research/test_results/ — gitignored scratch, kept for inspection.

Run:  python -m research.test_naqjs        (from the repo root)
Exit: 0 iff every block passes.

What it proves:
  - NAQJS registers through the documented plugin API and is selected via
    config, reporting sweepable/batch and its three weights.
  - The kernel's `schedule` event fires with NAQJS's full ranked-queue
    scores (the sched_decision seam) — the terms, the ordering, the winner.
  - The Sweepable hooks: correct min-max-normalised ranking, the
    faithfulness anchor (replay at live weights reproduces the recorded
    winner), scale-invariance (the ranking depends only on weight
    direction), and a genuine weight-driven flip.
  - The eta*N cap actually bounds cumulative dispatched width in a cycle.
'''

import json
import os
import glob
import contextlib
import io
import sys
import tempfile

# Both `benchmark` and `research` are top-level packages at the repo root, so
# they must be imported absolutely (a relative `..benchmark` would climb past
# the top-level package and fail). Ensure the repo root is on sys.path so this
# resolves however the file is launched — `python -m research.test_naqjs`
# already puts it there, and this guard makes a plain
# `python research/test_naqjs.py` behave identically rather than failing on the
# import. Anchored to this file, not the caller's cwd.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark import runner as R
from research.baselines.naqjs_scheduler import NAQJSScheduler


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

BELL = "test_circuits/bell.qasm"
GHZ  = "test_circuits/ghz.qasm"

# The committed demonstration workload — a mixed 3-job batch on an 8-qubit
# device that selects NAQJS via its device config. Lives in research/workloads/
# beside the sibling qasmbench_small workload; referenced by the seam block so
# the fixture is a real, runnable artifact rather than inline test data.
# Resolved from this file's location so it works from any cwd.
WORKLOAD = os.path.join(os.path.dirname(__file__), "workloads", "naqjs.json")


def _write_spec(tmp, jobs, num_qubits=8, eta=None, seed=7):
    '''Write a naqjs-selecting workload spec + its device config, return path.'''
    dev_cfg = {"scheduler": "naqjs"}
    if eta is not None:
        dev_cfg["naqjs.eta"] = eta
    dev_cfg_path = os.path.join(tmp, "dev.json")
    json.dump(dev_cfg, open(dev_cfg_path, "w"))

    spec = {
        "name": "naqjs_test",
        "seed": seed,
        "devices": [{
            "id": "sim",
            "provider": "devq.simulated",
            "backend": {"kind": "random", "num_qubits": num_qubits},
            "config": dev_cfg_path,
        }],
        "arrival": {"pattern": "batch"},
        "jobs": jobs,
    }
    spec_path = os.path.join(tmp, "spec.json")
    json.dump(spec, open(spec_path, "w"))
    return spec_path


def _run_spec(spec_path, out_dir):
    '''Drive the spec through the real runner with NAQJS registered; return
    the parsed event records from the single session log.

    Runs with the repo root as the working directory, because a committed
    workload references its device config and circuits by repo-root-relative
    paths (load_spec opens them from the cwd); this makes the test behave
    identically no matter where it is launched. Clears out_dir first so a
    block can never false-pass by reading a previous run's stale logs.
    '''
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)

    prev_cwd = os.getcwd()
    os.chdir(_REPO_ROOT)
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            R.run(spec_path, out_dir=out_dir,
                  register_schedulers={"naqjs": NAQJSScheduler}, quiet=True)
    finally:
        os.chdir(prev_cwd)

    logs = glob.glob(os.path.join(out_dir, "**", "*.jsonl"), recursive=True)
    records = []
    for lg in logs:
        records += [json.loads(line) for line in open(lg)]
    return records


# ── Blocks ────────────────────────────────────────────────────────────────────

def block_registration_and_seam():
    '''NAQJS registers, is selected, and the schedule seam fires with its
    full ranked-queue scores. Drives the committed research/workloads/naqjs.json
    workload, so the fixture is a real runnable spec, not inline test data.'''
    out = os.path.join(RESULTS_DIR, "registration_and_seam")
    recs = _run_spec(WORKLOAD, out)

    sched = [r for r in recs if r.get("event") == "schedule"]
    check(len(sched) >= 1,
          "the schedule seam fires — a scored scheduler emits `schedule` events")

    ev = sched[0]
    check("scores" in ev and len(ev["scores"]) == 3,
          "the schedule event carries the FULL ranked queue (all 3 jobs), "
          "not just the winner — required for the sweep to re-normalise")

    a_row = ev["scores"][0]
    check(set(a_row["terms"]) >= {"width", "shots", "seq",
                                  "width_norm", "shots_norm", "seq_norm"},
          "each score carries raw width/shots/seq terms plus normalised forms")

    # With equal weights the lowest combined normalised score wins.
    winner = ev["winner"]
    lowest = min(ev["scores"], key=lambda s: (s["score"], s["job_id"]))["job_id"]
    check(winner == lowest,
          "the dispatched winner is the lowest-scoring job (ascending, "
          "lowest-wins — no sign flip)")


def block_sweepable_hooks():
    '''The Sweepable hooks in isolation: ranking, faithfulness anchor,
    scale-invariance, and a weight-driven flip.'''
    s = NAQJSScheduler.__new__(NAQJSScheduler)
    s.naqjs_width_weight = s.naqjs_shots_weight = s.naqjs_seq_weight = 1.0
    s.naqjs_eta = 1.0
    s.naqjs_default_shots = None

    tagged = [(1, {"width": 2, "shots": 1024, "seq": 0}),
              (2, {"width": 3, "shots": 2048, "seq": 1}),
              (3, {"width": 2, "shots": 512,  "seq": 2})]

    report = s.explain_recorded(tagged)
    order  = [r["key"] for r in sorted(report, key=lambda x: (x["score"], x["key"]))]
    check(order == [1, 3, 2],
          f"equal-weight ranking is job 1 < 3 < 2 (got {order})")

    check(s.sweep_decision(tagged, s.live_params()) == 1,
          "faithfulness anchor: replay at live weights reproduces the "
          "recorded winner (job 1)")

    # The sweep's faithfulness anchor (in comparison.py) recovers a run's
    # weights from the ENRICHED terms _sweep_rank logs, matching them against
    # live_params() keys. So every live_params key must appear in the logged
    # terms carrying that key's live value — verify directly, since the
    # sweep_decision replay above reads from live_params and would pass even
    # if the self-logged copy were wrong.
    ranked_terms = s._sweep_rank(
        [(k, t, s._sweep_score(t, {})) for k, t in tagged], s.live_params())
    lp = s.live_params()
    _, _, enriched0 = ranked_terms[0]
    check(all(k in enriched0 and enriched0[k] == v for k, v in lp.items()),
          "each live_params weight is self-logged into the ranked terms "
          "with its live value (the sweep's weight-recovery anchor)")

    scaled = {"naqjs.width_weight": 5.0, "naqjs.shots_weight": 5.0,
              "naqjs.seq_weight": 5.0}
    check(s.sweep_decision(tagged, scaled) == 1,
          "scale-invariance: rescaling all weights leaves the winner "
          "unchanged (only weight direction matters)")

    # Shots-only weighting: job 3 has the fewest shots (norm 0.0) and wins.
    shots_only = {"naqjs.width_weight": 0.0, "naqjs.shots_weight": 1.0,
                  "naqjs.seq_weight": 0.0}
    check(s.sweep_decision(tagged, shots_only) == 3,
          "a different weighting flips the winner (shots-only -> job 3), "
          "proving the weights genuinely drive the decision")


def block_eta_cap():
    '''The eta*N cap bounds cumulative dispatched width in one cycle.'''
    # Use an 8-qubit device with eta=0.25 -> cap = 2 qubits, so the eta cap
    # (2) is STRICTLY BELOW the pool bound (8). Two 2-qubit circuits both fit
    # the pool, so if the eta break were removed BOTH would dispatch in
    # cycle 1; the cap is what holds cycle-1 dispatch to <=1. (A 4-qubit
    # device would let the pool alone explain a low count, masking a broken
    # cap — the mutant that deletes the eta break must be observable here.)
    tmp = tempfile.mkdtemp(prefix="naqjs_eta_")

    out_capped = os.path.join(RESULTS_DIR, "eta_capped")
    spec_capped = _write_spec(tmp, num_qubits=8, eta=0.25, jobs=[
        {"circuit": BELL, "shots": 1024},
        {"circuit": BELL, "shots": 1024},
    ])
    recs_capped = _run_spec(spec_capped, out_capped)
    # Count how many jobs were dispatched in the first schedule cycle.
    cyc1 = [r for r in recs_capped
            if r.get("event") == "dispatch" and r.get("cycle") == 1]
    check(len(cyc1) <= 1,
          f"eta=0.25 on an 8-qubit device (cap=2 < pool=8) caps cycle-1 "
          f"dispatch to <=1 two-qubit job (got {len(cyc1)})")

    # Same 8-qubit device, eta=1.0 (cap = 8 = pool): the cap is a no-op, so
    # BOTH 2-qubit jobs dispatch in cycle 1. The contrast with the capped
    # case above (same device, same jobs, only eta differs) is what proves
    # the eta break — not the pool — produced the difference.
    tmp2 = tempfile.mkdtemp(prefix="naqjs_uncapped_")
    out_uncapped = os.path.join(RESULTS_DIR, "eta_uncapped")
    spec_uncapped = _write_spec(tmp2, num_qubits=8, eta=1.0, jobs=[
        {"circuit": BELL, "shots": 1024},
        {"circuit": BELL, "shots": 1024},
    ])
    recs_uncapped = _run_spec(spec_uncapped, out_uncapped)
    cyc1_u = [r for r in recs_uncapped
              if r.get("event") == "dispatch" and r.get("cycle") == 1]
    check(len(cyc1_u) == 2,
          f"eta=1.0 (no-op cap) on the same 8-qubit device dispatches BOTH "
          f"jobs in cycle 1 (got {len(cyc1_u)}) — the capped case's <=1 is "
          f"the eta break, not the pool")


def block_shots_resolution():
    '''The three-tier shots feature: per-job shots win; else the plugin
    naqjs.default_shots; else a neutral constant so unspecified jobs tie.
    This is what lets NAQJS rank a workload whose jobs omit shots (e.g.
    QASMBench) instead of feeding None into the min-max and crashing.'''
    import types

    def _qcb(job_id, shots):
        # Minimal stand-in: _resolve_shots/_sweep_terms read only .shots,
        # .circuit.num_qubits, .job_id, .submitted_seq.
        return types.SimpleNamespace(
            job_id=job_id, shots=shots, submitted_seq=job_id,
            circuit=types.SimpleNamespace(num_qubits=2))

    # Tier 1: a job's own shots always win, even when a plugin default is set.
    s = NAQJSScheduler.__new__(NAQJSScheduler)
    s.naqjs_default_shots = 4096
    check(s._resolve_shots(_qcb(1, 1024)) == 1024,
          "per-job shots take precedence over naqjs.default_shots")

    # Tier 2: a job with no shots falls back to the plugin default.
    check(s._resolve_shots(_qcb(2, None)) == 4096,
          "a job that omits shots uses naqjs.default_shots when set")

    # Tier 3: no per-job shots and no plugin default -> neutral constant,
    # and every such job takes the SAME value so they tie after min-max.
    s2 = NAQJSScheduler.__new__(NAQJSScheduler)
    s2.naqjs_default_shots = None
    r = s2._resolve_shots(_qcb(3, None))
    check(r == NAQJSScheduler._NEUTRAL_SHOTS,
          f"unspecified shots with no default -> neutral constant (got {r})")

    # The crash regression: a full queue of shots=None jobs must rank without
    # raising (previously '<' not supported between NoneType).
    s2.naqjs_width_weight = s2.naqjs_shots_weight = s2.naqjs_seq_weight = 1.0
    tagged = s2._sweep_terms([_qcb(10, None), _qcb(11, None), _qcb(12, None)])
    shots_vals = {t[1]["shots"] for t in tagged}
    check(shots_vals == {NAQJSScheduler._NEUTRAL_SHOTS},
          "all-unspecified queue resolves every shots term to the neutral "
          f"constant (got {shots_vals})")
    report = s2.explain_recorded(tagged)   # must not raise
    check(len(report) == 3,
          "an all-unspecified-shots queue ranks without error")


# ── Runner ────────────────────────────────────────────────────────────────────

BLOCKS = [
    ("registration_and_seam", block_registration_and_seam),
    ("sweepable_hooks",       block_sweepable_hooks),
    ("shots_resolution",      block_shots_resolution),
    ("eta_cap",               block_eta_cap),
]


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print(f"\nNAQJS sanity — {len(BLOCKS)} block(s)\n")
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