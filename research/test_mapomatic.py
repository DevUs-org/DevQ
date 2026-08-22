'''
Tags: Research

test_mapomatic — standalone sanity harness for the Mapomatic allocator
baseline (research/baselines/mapomatic_allocator.py). Same run_tests-style
shape as test_naqjs (print-per-check, non-zero exit on any failure) but
deliberately self-contained: it does not import run_tests, and run_tests
never imports it — the research harness is decoupled from the core suite.

What it proves:
  - Mapomatic registers through the public API, is selected via device
    config, and PLACES a real committed workload end to end (all jobs
    dispatch and finish) with ZERO core edits.
  - Because Mapomatic is a fixed, parameter-free policy, it is a NON-scoring
    allocator in the Sweepable sense: it emits NO `allocate` event and never
    sets `_last_decision`. That absence is asserted, not hand-waved — it is
    the honest signature docs/EXTENDING.md prescribes for a policy with no
    weights to sweep or per-candidate scores to explain.
  - The product-of-fidelities heuristic selects the brute-force
    lowest-error block (argmin correctness), computes the three-term score
    correctly, and honours per-qubit thresholds as hard constraints applied
    BEFORE scoring.
  - feasible() classifies an unsatisfiable job so the scheduler REJECTS it
    rather than spinning on it.

Run: python -m research.test_mapomatic
'''

import json
import os
import glob
import contextlib
import io
import sys

# `benchmark` and `research` are top-level packages at the repo root; import
# them absolutely and make sure the repo root is on sys.path so this file runs
# identically whether launched as `python -m research.test_mapomatic` or
# `python research/test_mapomatic.py`. Anchored to this file, not the cwd.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark import runner as R
from provider.devq_simulated_provider import DevQSimulatedProvider
from kernel.memory.qubit_pool import QubitPool
from plugin_bases.base_allocator import AllocationError
from plugins.allocators.mapomatic.mapomatic_allocator import MapomaticAllocator

from qiskit import QuantumCircuit


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

# The committed demonstration workload — a mixed 2q/3q batch on an 8-qubit
# device that selects Mapomatic via its device config, beside the sibling
# naqjs workload. Referenced by the placement block so the fixture is a real,
# runnable artifact rather than inline test data. Resolved from this file's
# location so it works from any cwd.
WORKLOAD = os.path.join(os.path.dirname(__file__), "workloads", "mapomatic.json")


def _device(num_qubits=8, kind="random", seed=SEED):
    '''A seeded DevQ-simulated device — calibration is stable under the seed,
    so every score assertion below is deterministic.'''
    return DevQSimulatedProvider(seed=seed).get_device(kind, num_qubits)


def _run_spec(spec_path, out_dir):
    '''Run a committed workload through the real benchmark runner with
    Mapomatic registered, and return every JSONL record it wrote.

    Runs with the repo root as the working directory (a committed workload
    references its config and circuits by repo-root-relative paths, which
    load_spec opens from the cwd), and clears out_dir first so a block can
    never false-pass on a previous run's stale logs.
    '''
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)

    prev_cwd = os.getcwd()
    os.chdir(_REPO_ROOT)
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            R.run(spec_path, out_dir=out_dir,
                  register_allocators={"mapomatic": MapomaticAllocator},
                  quiet=True)
    finally:
        os.chdir(prev_cwd)

    records = []
    for lg in glob.glob(os.path.join(out_dir, "**", "*.jsonl"), recursive=True):
        records += [json.loads(line) for line in open(lg)]
    return records


# ── Blocks ────────────────────────────────────────────────────────────────────

def block_registration_and_placement():
    '''Mapomatic registers, is selected by config, and places the committed
    workload end to end — every job dispatches and finishes. And because it is
    a fixed, parameter-free policy, it emits NO `allocate` event and leaves
    `_last_decision` unset: the honest non-scoring signature.'''
    out  = os.path.join(RESULTS_DIR, "registration_and_placement")
    recs = _run_spec(WORKLOAD, out)

    disp = [r for r in recs if r.get("event") == "dispatch"]
    check(len(disp) == 3,
          f"all three workload jobs dispatch on Mapomatic (got {len(disp)})")

    states = {r.get("state") for r in recs if r.get("state")}
    check(states == {"FINISHED"},
          f"every job reaches FINISHED — real placement + execution "
          f"(states seen: {sorted(states)})")

    alloc_ev = [r for r in recs if r.get("event") == "allocate"]
    check(len(alloc_ev) == 0,
          f"a non-scoring allocator emits NO `allocate` event (got "
          f"{len(alloc_ev)}) — the runner handles a None decision without "
          f"inventing a score to log")


def block_non_scoring_contract():
    '''Mapomatic leaves the Sweepable hooks at their defaults, so the base
    honestly reports it as not-sweepable — the correct outcome for a policy
    with no weights. It never sets `_last_decision`, which is what makes the
    absent `allocate` event above a property of the allocator, not luck.'''
    alloc = MapomaticAllocator()

    # The base default: a component that does not override _sweep_terms is
    # reported not-scored (NOT_SCORED sentinel).
    check(alloc._sweep_terms(("any", "decision")) is None,
          "Mapomatic does not implement _sweep_terms -> reported not-scored")

    # A live allocation must not stash a decision (nothing for the kernel to
    # read back into an `allocate` event).
    dev  = _device()
    pool = QubitPool(dev.num_qubits)
    qc   = QuantumCircuit(2); qc.h(0); qc.cx(0, 1); qc.measure_all()
    alloc.allocate(qc, dev, pool)
    check(getattr(alloc, "_last_decision", None) is None,
          "allocate() leaves _last_decision unset — no scored decision to emit")


def block_argmin_and_score():
    '''The product-of-fidelities heuristic (1) computes the three-term score
    correctly and (2) selects the brute-force lowest-error placeable block.'''
    dev  = _device()
    pool = QubitPool(dev.num_qubits)
    alloc = MapomaticAllocator()

    # (1) Score correctness on a known pair: recompute 1 - Π(1-e) by hand over
    # readout + 1q-gate on each qubit and the 2q edge, and compare.
    u, v = sorted(dev.edges()[0])
    expected = 1.0 - (
        (1.0 - dev.qubit_error(u)) * (1.0 - dev.gate_error(u)) *
        (1.0 - dev.qubit_error(v)) * (1.0 - dev.gate_error(v)) *
        (1.0 - dev.edge_error(u, v))
    )
    got = MapomaticAllocator._layout_score(dev, (u, v))
    check(abs(got - expected) < 1e-12,
          f"three-term product-of-fidelities score matches a hand computation "
          f"(got {got:.9f}, expected {expected:.9f})")

    # (2) argmin correctness: brute-force the lowest-score pair over all edges,
    # then confirm allocate() reserves exactly that pair.
    best = min((tuple(sorted(e)) for e in dev.edges()),
               key=lambda b: (MapomaticAllocator._layout_score(dev, b), b))
    qc = QuantumCircuit(2); qc.h(0); qc.cx(0, 1); qc.measure_all()
    mapping = alloc.allocate(qc, dev, pool)
    chosen  = tuple(sorted(mapping.values()))
    check(chosen == best,
          f"allocate() reserves the minimum-score block (chose {chosen}, "
          f"brute-force best {best})")
    check(set(chosen).isdisjoint(pool.free_qubits),
          "the chosen qubits are actually reserved in the pool (contract: "
          "allocate() must reserve before returning)")


def block_threshold_honouring():
    '''Per-qubit thresholds are hard constraints applied BEFORE scoring: a
    tight max_qubit_error excludes noisy qubits from candidacy, so Mapomatic
    can only choose among qubits that pass — composing with the job-level
    threshold system like any allocator.'''
    dev   = _device()
    alloc = MapomaticAllocator()

    # Pick a threshold that admits only some qubits. Set it just above the
    # median readout error so at least one clean and one noisy qubit exist.
    errs   = sorted(dev.qubit_error(q) for q in range(dev.num_qubits))
    thresh = errs[len(errs) // 2]                       # median
    admitted = {q for q in range(dev.num_qubits)
                if dev.qubit_error(q) <= thresh}
    check(0 < len(admitted) < dev.num_qubits,
          f"the chosen threshold splits the device (admits {len(admitted)}/"
          f"{dev.num_qubits} qubits) so the filter is actually exercised")

    # A 2-qubit job under that threshold must land ENTIRELY on admitted qubits.
    pool = QubitPool(dev.num_qubits)
    qc   = QuantumCircuit(2); qc.h(0); qc.cx(0, 1); qc.measure_all()
    mapping = alloc.allocate(qc, dev, pool, max_qubit_error=thresh)
    chosen  = set(mapping.values())
    check(chosen <= admitted,
          f"under max_qubit_error={thresh:.4f} the placement uses only "
          f"threshold-eligible qubits (chose {sorted(chosen)}, admitted "
          f"{sorted(admitted)})")


def block_feasibility_and_rejection():
    '''feasible() returns None for a placeable job and a reason string for an
    unsatisfiable one; and an infeasible job placed through the allocator
    raises AllocationError (which the scheduler turns into REJECTED) rather
    than returning a partial mapping or hanging.'''
    dev   = _device(num_qubits=5)
    alloc = MapomaticAllocator()

    qc2 = QuantumCircuit(2); qc2.h(0); qc2.cx(0, 1)
    check(alloc.feasible(qc2, dev) is None,
          "feasible() returns None for a placeable 2-qubit circuit")

    qc9 = QuantumCircuit(9)
    reason = alloc.feasible(qc9, dev)
    check(isinstance(reason, str) and reason,
          f"feasible() returns a reason string for a 9-qubit circuit on a "
          f"5-qubit device (got {reason!r})")

    # Exhaust the pool, then a fresh 2-qubit job cannot be placed: allocate()
    # must raise AllocationError (the legitimate 'cannot place'), not return a
    # partial mapping.
    pool = QubitPool(dev.num_qubits)
    pool.allocate(list(range(dev.num_qubits)))          # nothing free
    raised = False
    try:
        alloc.allocate(qc2, dev, pool)
    except AllocationError:
        raised = True
    check(raised,
          "allocate() on an exhausted pool raises AllocationError (the "
          "'cannot place' the scheduler classifies as WAITING/REJECTED)")


# ── Runner ────────────────────────────────────────────────────────────────────

BLOCKS = [
    ("registration_and_placement", block_registration_and_placement),
    ("non_scoring_contract",       block_non_scoring_contract),
    ("argmin_and_score",           block_argmin_and_score),
    ("threshold_honouring",        block_threshold_honouring),
    ("feasibility_and_rejection",  block_feasibility_and_rejection),
]


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print(f"\nMapomatic sanity — {len(BLOCKS)} block(s)\n")
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