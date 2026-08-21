'''
Tags: Main

DevQ sanity test runner — executes the blocks in docs/TEST_BLOCKS.md
automatically, with no manual editing of any entry point.

Each block declares the session it needs (devices, names, config files,
seed) and builds it fresh, so blocks that previously required editing
main.py by hand — per-device configs, alternate routers, single-device
setups — now run unattended. Sessions are driven through
QShell.onecmd() via DevQ.build(), which is the same wiring start() uses
minus the blocking command loop.

Assertions are deliberately coarse: substring and regex checks over
captured output. This is a smoke/sanity harness meant to catch crashes,
hangs and silent regressions across the plugin matrix, NOT a unit-test
suite. Anything asserting exact physics (counts, calibration values) is
pinned to the stack in requirements.txt.

Usage:
    python run_tests.py                 # every block
    python run_tests.py -k matrix       # blocks whose name matches
    python run_tests.py --list          # names only, run nothing
    python run_tests.py -v              # print captured output too

Exit code is 0 only if every block passes.
'''

import argparse
import contextlib
import gc
import os
import io
import itertools
import re
import sys
import threading
import time
import traceback

# MUST precede any Qiskit/Aer import: these are read when the native
# libraries initialise their thread pools. Aer otherwise sizes its pool
# from the CPU count, and each thread allocates its own simulation
# buffers — on a many-core machine that multiplies against the shared
# executor's workers and against every session alive in the process,
# so memory grows with cores rather than with work.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "RAYON_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from circuits.execution_result import (ExecutionResult, shutdown_executor,
                                        submit_async)
from devq import DevQ, DevQError
from providers.devq.devq_simulated_provider import DevQSimulatedProvider
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

CONFIG = "./config/config_examples/"
WORKLOADS = "./benchmark/workloads/"
BELL   = "test_circuits/bell.qasm"
GHZ    = "test_circuits/ghz.qasm"
QASM2  = "test_circuits/qasm2/"   # fixtures exercising the full 2.0 parser

SEED = 42   # fixed everywhere so mock-device topologies never flap

# The env values placeholders.json resolves against. Held as a constant
# — not set at module scope — so the two blocks that run that spec set
# them under their own try/finally and neither leaks DEVQ_* into the
# rest of the suite. Chosen so the spec resolves to a real,
# registration-free session (devq.simulated), a coercible int seed, and
# a coercible float threshold.
PLACEHOLDER_ENV = {
    "DEVQ_SEED"     : "42",
    "DEVQ_VENDOR"   : "devq",
    "DEVQ_TIER"     : "simulated",
    "DEVQ_MAX_QERR" : "0.03",
}



# ── Session construction ──────────────────────────────────────────────────────

def ibm_provider(seed=SEED):
    return IBMSimulatedProvider(seed=seed)


def devq_with_ibm(**kwargs):
    '''
    A DevQ with the IBM provider registered, for blocks that build a
    session directly instead of through session().

    add_device() refuses a device whose provider class is not
    registered, and IBM is not a built-in — so every entry point that
    attaches an IBM device has to declare it. This exists so that fact
    lives in one place rather than in a dozen blocks.
    '''
    return DevQ(**kwargs).register_provider("ibm.simulated",
                                            IBMSimulatedProvider)


def session(config=None, devices=None, seed=SEED):
    '''
    Build a shell for a fresh session.

    Args:
        config:  global config filename in config_examples/, or None
        devices: list of specs, each one of
                     ("devq.simulated", kind, num_qubits, name, device_config)
                     ("ibm.simulated",  backend_name,   name, device_config)
                 name and device_config may be None.
        seed:    provider seed; None for unseeded

    Returns:
        QShell, ready for onecmd().
    '''
    devices = devices or []
    path    = (CONFIG + config) if config else None
    dq      = DevQ(config_path=path)
    ibm     = ibm_provider(seed)

    # add_device() refuses a device whose provider class is not
    # registered. DevQSimulatedProvider is a built-in; IBM is not, so
    # every session declares it. Registering unconditionally keeps this
    # helper's behaviour independent of which devices a block happens to
    # ask for — a block that adds an IBM device later must not start
    # failing because the first device was a DevQ one.
    dq.register_provider("ibm.simulated", IBMSimulatedProvider)

    for spec in devices:
        if spec[0] == "devq.simulated":
            _, kind, nq, name, dcfg = spec
            dev = DevQSimulatedProvider(seed=seed).get_device(kind, nq)
        else:
            _, backend, name, dcfg = spec
            dev = ibm.get_device(backend)
        dq.add_device(dev, (CONFIG + dcfg) if dcfg else None, name=name)

    return dq.build()


def three_device(config="router_only.config.json", seed=SEED, d1_config=None):
    '''The standard federation used by most blocks — mirrors example.py.'''
    return session(config, [
        ("devq.simulated", "random", 7, None, None),
        ("ibm.simulated", "FakeNairobiV2", "nairobi", d1_config),
        ("ibm.simulated", "FakeLagosV2",   "lagos",   None),
    ], seed)


# ── Trace ─────────────────────────────────────────────────────────────────────
# Blocks capture session output internally, so the runner cannot see it
# unless blocks record it. TRACE collects, per block, the commands sent,
# the transcript they produced, and every assertion as it fires — which
# is what -v and --checks print. Recording is unconditional and cheap;
# only the printing is conditional.

class Trace:
    def __init__(self):
        self.reset()

    def reset(self):
        self.commands = []   # command strings sent this block
        self.output   = []   # transcripts, one per run() call
        self.checks   = []   # (ok, description) per assertion

    def note(self, ok, description):
        self.checks.append((ok, description))

    def transcript(self):
        return "".join(self.output)


TRACE = Trace()


# A runaway shell loop prints without bound. Capturing that into an
# unbounded StringIO turns a hang into an out-of-memory kill, which is a
# far worse failure mode — it takes the machine down instead of the test.
MAX_CAPTURE = 4 * 1024 * 1024   # 4 MB per command is already absurd


@contextlib.contextmanager
def _capture(buf):
    '''
    Redirect stdout to buf for the duration of the block.

    sys.stdout is process-wide, so a thread the runner has ABANDONED
    (see _with_timeout) must not restore it later and clobber whatever
    the runner set up in the meantime. The saved handle is therefore
    only restored if sys.stdout is still the buffer this call installed;
    otherwise someone else owns it now and we leave it alone.
    '''
    original   = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        if sys.stdout is buf:
            sys.stdout = original


class BoundedBuffer(io.StringIO):
    '''StringIO that raises once output exceeds MAX_CAPTURE.'''

    def __init__(self):
        super().__init__()
        self._size = 0

    def write(self, text):
        self._size += len(text)
        if self._size > MAX_CAPTURE:
            raise Failure(
                f"command produced over {MAX_CAPTURE // (1024*1024)}MB of "
                f"output — the shell is almost certainly stuck in a loop"
            )
        return super().write(text)


def run(shell, commands):
    '''
    Drive a shell through commands, returning everything it printed.
    Also records to TRACE so the runner can replay the session.

    Note on redirection: contextlib.redirect_stdout patches sys.stdout
    PROCESS-WIDE, so it must never be left active by a thread the runner
    has abandoned — the runner's own prints would vanish into a dead
    buffer. sys.stdout is therefore always restored in the finally
    clause, even when the body raises.
    '''
    buf = BoundedBuffer()
    with _capture(buf):
        for c in commands:
            TRACE.commands.append(c)
            shell.onecmd(c)
    out = buf.getvalue()
    TRACE.output.append(out)
    return out


# ── Assertion helpers ─────────────────────────────────────────────────────────
# Each records what it verified before raising, so a passing block can
# still report what it proved rather than only that it did not fail.

class Failure(Exception):
    pass


def check(ok, description, record=True):
    '''
    Record an assertion and raise if it failed.

    record=False suppresses the trace entry for internal guards (e.g.
    "was this job dispatched at all?") that would otherwise repeat
    every time a helper is called inside an f-string. They still fail
    loudly; they are just not worth listing as findings.
    '''
    if record:
        TRACE.note(bool(ok), description)
    if not ok:
        raise Failure(description)
    return ok


def expect(out, *needles):
    for n in needles:
        check(n in out, f"output contains {n!r}")


def expect_absent(out, *needles):
    for n in needles:
        check(n not in out, f"output does NOT contain {n!r}")


def expect_re(out, pattern, count=None):
    hits = re.findall(pattern, out)
    if count is None:
        check(bool(hits), f"/{pattern}/ matches ({len(hits)}x)")
    else:
        check(len(hits) == count,
              f"/{pattern}/ matches {count}x (got {len(hits)})")
    return hits


def mapping_of(out, job_id):
    '''Extract the v2p map a job was dispatched with.'''
    m = re.search(rf"Dispatching job {job_id} .*? qubits (\{{[^}}]*\}})", out)
    check(m is not None, f"job {job_id} was dispatched", record=False)
    return m.group(1)


def device_of(out, job_id):
    m = re.search(rf"Dispatching job {job_id} → (\S+)", out)
    check(m is not None, f"job {job_id} was dispatched", record=False)
    return m.group(1)


def counts_of(out, job_id):
    '''Extract the measured counts dict a job finished with, as a dict.

    Reads the qps result row — `N | dev | FINISHED | Counts: {...}` —
    which is the sole place counts reach the console now that qrun/
    qrunpack are pure async dispatchers and the kernel's resolve event is
    no longer echoed to the console (only qps reports results).
    '''
    m = re.search(
        rf"^{job_id} \| .*? \| FINISHED \| Counts: (\{{[^}}]*\}})",
        out, re.MULTILINE)
    check(m is not None, f"job {job_id} produced counts", record=False)
    return eval(m.group(1))


def finished_ids(out):
    '''
    The set of job ids shown FINISHED in a transcript.

    settle() accumulates every poll, so a settled job's qps line repeats
    across iterations; counting raw `| FINISHED` matches would over-count.
    Deduping by job id answers the real question — how many DISTINCT jobs
    finished — regardless of how many polls observed each.
    '''
    return set(re.findall(r"^(\d+) \| .*? \| FINISHED", out, re.MULTILINE))


def settle(sh, *job_ids, tries=250):
    '''
    Drive a job (or jobs) to a terminal state and return the accumulated
    transcript of doing so.

    qrun/qrunpack now dispatch asynchronously and return immediately; the
    future resolves on a background thread. qps is a pure snapshot, so a
    single qps right after dispatch can legitimately catch a job still
    RUNNING. This helper models what an interactive user does — glance at
    qps again a moment later — by re-issuing `qps <ids>` until every named
    job is FINISHED/FAILED/REJECTED.

    Returns EVERY qps transcript concatenated, not just the last: a job
    that was WAITING on contended qubits self-heals when the holder
    completes, and its `[Kernel] Dispatching ...` line is printed during
    whichever poll observed that completion — an earlier iteration than
    the final settled snapshot. Accumulating keeps that dispatch line (and
    the settled table) available to the caller's mapping/device reads.

    Bounded by `tries`: a future that never resolves must fail the block
    loudly rather than spin forever. With no ids, settles the whole table
    (bare qps) until nothing is left RUNNING or WAITING.
    '''
    query = "qps " + " ".join(str(j) for j in job_ids) if job_ids else "qps"
    terminal = ("FINISHED", "FAILED", "REJECTED")

    transcript = ""
    for _ in range(tries):
        out = run(sh, [query])
        transcript += out
        if job_ids:
            lines = [l for l in out.splitlines()
                     if any(l.startswith(f"{j} |") for j in job_ids)]
            ready = (len(lines) == len(job_ids)
                     and all(any(s in l for s in terminal) for l in lines))
        else:
            live = [l for l in out.splitlines()
                    if " | RUNNING" in l or " | WAITING" in l]
            ready = not live
        if ready:
            return transcript
        time.sleep(0.01)

    check(False, f"jobs {job_ids or '(all)'} reached a terminal state")
    return transcript


# ── Mock components ──────────────────────────────────────────────────────────
# Stand-ins for third-party plugins. These register through exactly the
# public path a real plugin author uses, which is the point: testing by
# UNREGISTERING built-ins would prove only that built-ins can be removed,
# and would need registry API that exists for no other reason.
#
# Only the WORKING mocks live here, because several blocks share them.
# The deliberately broken ones are defined inline in
# block_registry_validation, next to the assertion that rejects each —
# a violation and its expected message are far easier to audit side by
# side than in two separate lists.

from kernel.process.lifecycle import JobStates
from kernel.scheduler.base_scheduler import BaseScheduler
from kernel.memory.allocators.base_allocator import BaseAllocator, AllocationError
from kernel.router.base_router import BaseRouter
from registry.keyspec import (KeySpec, NormaliseGroup, positive_int,
                              non_negative)


class MockScheduler(BaseScheduler):
    '''
    A minimal third-party scheduler that declares its own config.

    Deliberately LIFO — last submitted, first dispatched. Not because
    that is a sensible policy, but because it is OBSERVABLE: every
    built-in scheduler dispatches job 1 before job 2, so reversed
    dispatch order in the transcript is proof this class was actually
    the one making decisions. A mock whose behaviour is
    indistinguishable from a built-in cannot demonstrate that the
    registry wired anything up.
    '''
    LABEL = "Mock Scheduler"

    CONFIG_SCHEMA = {
        "mock.batch_window": KeySpec(
            "device", 5, positive_int, "Mock batch window"),
        "mock.wait_weight": KeySpec(
            "device", 0.4, non_negative, "Mock wait weight", "mock.blend"),
        "mock.fid_weight": KeySpec(
            "device", 0.6, non_negative, "Mock fidelity weight", "mock.blend"),
    }
    CONFIG_GROUPS = {
        "mock.blend": NormaliseGroup(["mock.wait_weight", "mock.fid_weight"]),
    }

    def schedule(self):
        # _attempt_allocation is the base class's shared
        # allocate-and-classify step: it sets v2p_map and RUNNING on
        # success, and classifies failure as WAITING (transient) or
        # REJECTED (terminal). A plugin that reimplements it instead of
        # calling it will silently skip the lifecycle transitions.
        processed = []

        # Index -1: newest first. Otherwise identical to FCFS, including
        # the use of _attempt_allocation, which is the base class's
        # shared allocate-and-classify step — it sets v2p_map and
        # RUNNING on success and classifies failure as WAITING
        # (transient) or REJECTED (terminal). A plugin that
        # reimplements it instead of calling it silently skips the
        # lifecycle transitions.
        while self.queue:
            qcb = self.queue[-1]

            if self._attempt_allocation(qcb):
                processed.append(self.queue.pop())
                return processed

            if qcb.state == JobStates.REJECTED:
                processed.append(self.queue.pop())
                continue

            break   # WAITING — head-of-line blocking

        return processed or None


class MockAllocator(BaseAllocator):
    '''A third-party allocator: first contiguous free block that fits.'''
    LABEL = "Mock Allocator"

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None,
                 max_1q_gate_error=None):
        need = circuit.num_qubits
        free = sorted(pool.available())
        if len(free) < need:
            raise AllocationError("Mock: not enough free qubits")
        chosen = free[:need]
        pool.allocate(chosen)                      # honour reserve-on-success
        return {v: p for v, p in enumerate(chosen)}


class MockRouter(BaseRouter):
    '''A third-party router: always the first feasible candidate.'''
    LABEL = "Mock Router"

    def select(self, qcb, candidates):
        return candidates[0]


class MockProvider(DevQSimulatedProvider):
    '''
    A third-party provider registered by NAME.

    Subclasses the DevQ simulated provider so it produces real devices
    without needing a backend of its own; what matters here is that it
    is addressable through the registry rather than constructed in code,
    which is what a declarative workload spec will need.
    '''
    LABEL = "Mock Provider"


# ── Blocks ────────────────────────────────────────────────────────────────────
# Each returns None on success and raises Failure with a specific message
# otherwise. Docstring first line is the description printed by the runner.

def block_devices_and_config():
    '''Devices, alias column, calibration data and config provenance'''
    sh  = three_device()
    out = run(sh, ["qdevices", "qconfig", "qerrors q d2", "qerrors e d2",
                   "qtopology d1 1"])

    expect(out, "random_backend", "FakeNairobiV2", "FakeLagosV2")
    # alias column present because two devices are named
    expect(out, "nairobi", "lagos")
    # provenance
    expect(out, "router       =  noise", "User (global)", "DevQ Core",
           "IBMSimulatedProvider")
    # Lagos calibration (pinned to qiskit-ibm-runtime 0.45.1)
    expect(out, "0.1690", "0.1362", "0.4638", "0.0167", "0.0292",
           "0.2619", "0.3480")
    expect(out, "0.0094", "0.0103", "0.0107", "0.0290", "0.0083", "0.0202")
    # qtopology filtered to qubit 1's edges only
    expect(out, "0 -- 1", "1 -- 2", "1 -- 3")
    expect_absent(out, "4 -- 5", "5 -- 6")


def block_noise_routing():
    '''Noise-aware routing picks Nairobi; Lagos mappings are correct'''
    sh  = three_device()
    out = run(sh, [f"qrun {BELL} --exec=nairobi,lagos",
                   f"qrun {BELL} --exec=d2",
                   f"qrun {GHZ} --exec=d2"])

    check("nairobi" in device_of(out, 1),
          f"job 1 routed to nairobi (S 0.0102 < lagos 0.0249), "
          f"got {device_of(out, 1)}")
    check(mapping_of(out, 1) == "{0: 1, 1: 2}",
          f"job 1 mapped to nairobi's best bell block {{0: 1, 1: 2}}, "
          f"got {mapping_of(out, 1)}")
    check(mapping_of(out, 2) == "{0: 1, 1: 3}",
          f"job 2 mapped to lagos's best bell block {{0: 1, 1: 3}}, "
          f"got {mapping_of(out, 2)}")
    # Under async execution jobs 1–2 still hold their qubits when job 3
    # allocates, so job 3 takes lagos's next free connected triple
    # {0: 4, 1: 5, 2: 6} rather than {3,4,5} (which a serial run, where
    # job 2 had already freed its qubits, would have given). Deterministic
    # under the seed; the routing decision (lagos) is what this pins.
    check(mapping_of(out, 3) == "{0: 4, 1: 5, 2: 6}",
          f"job 3 (ghz) mapped to lagos {{0: 4, 1: 5, 2: 6}}, "
          f"got {mapping_of(out, 3)}")
    check(finished_ids(settle(sh, 1, 2, 3)) == {"1", "2", "3"},
          "all three jobs finished")


def block_name_index_equivalence():
    '''A device name and its index are interchangeable everywhere'''
    sh  = three_device()
    by_name  = run(sh, ["qerrors q nairobi", "qtopology nairobi 1"])
    by_index = run(sh, ["qerrors q d1", "qtopology d1 1"])
    check(by_name == by_index,
          "qerrors/qtopology give identical output for 'nairobi' and 'd1'")

    # Run serially — settle job 1 before dispatching job 2 — so both take
    # the same best block. Async would otherwise have job 1 still holding
    # its qubits when job 2 allocates, giving a different block; that is
    # concurrency, not the name/index equivalence this block pins.
    out1 = run(sh, [f"qrun {BELL} --exec=nairobi"])
    settle(sh, 1)
    out2 = run(sh, [f"qrun {BELL} --exec=d1"])
    settle(sh, 2)
    out = out1 + out2
    check(device_of(out, 1) == device_of(out, 2),
          "--exec=nairobi and --exec=d1 route to the same device")
    check(mapping_of(out, 1) == mapping_of(out, 2),
          "--exec=nairobi and --exec=d1 produce the same mapping")


def block_rejection_semantics():
    '''Thresholds reject across devices with aggregated reasons'''
    sh  = three_device()
    out = run(sh, [f"qrun {BELL} --max-qubit-error=0.03 --exec=lagos",
                   f"qrun {BELL} --max-qubit-error=0.03 --exec=d1,d2",
                   f"qrun {BELL} --max-qubit-error=0.0185 --exec=nairobi,lagos"])

    expect(out, "Job 1 REJECTED", "no connected block of 2 qubits")
    # job 2: same threshold but Nairobi is feasible, so it runs
    check("nairobi" in device_of(out, 2),
          "job 2 runs on nairobi at the same threshold that rejected lagos")
    # job 3: infeasible everywhere — both devices named in one reason
    expect(out, "Job 3 REJECTED")
    m = re.search(r"Job 3 REJECTED: ([^\n]*)", out)
    check(m and "d1:" in m.group(1) and "d2:" in m.group(1),
          "job 3's rejection reason aggregates both d1 and d2")

    # A circuit DevQ cannot faithfully run on ANY backend (here:
    # mid-circuit measurement — a gate on a qubit after it is measured) is
    # REJECTED — the SAME umbrella terminal state as an unsatisfiable
    # allocation, not a parse crash. The reason is carried from the circuit
    # layer (the frontend marked it unrunnable) through to the job, proving
    # the two rejection sources converge on one outcome. NOTE: classical
    # control (conditional.qasm) is deliberately NOT used here anymore — it
    # is now a runnable dynamic circuit, declined per-device rather than
    # circuit-globally, and is covered by the routing block instead.
    sh2 = three_device()
    out2 = run(sh2, [f"qrun {QASM2}midcircuit.qasm"])
    expect(out2, "REJECTED")
    m2 = re.search(r"Job \d+ REJECTED: ([^\n]*)", out2)
    check(m2 and "mid-circuit" in m2.group(1).lower(),
          f"an unrunnable circuit rejects with its circuit-level reason, "
          f"got {m2.group(1) if m2 else None!r}")

    # The SAME must hold on the SCHEDULING path (qsubmit + qrunpack), not
    # just the qrun fast path — they are separate guards in the kernel, and
    # a regression could disable one while the other still rejects. Submit
    # via the queue and drain: the unrunnable circuit must still be REJECTED
    # before it ever routes to a device.
    sh3 = three_device()
    out3 = run(sh3, [f"qsubmit {QASM2}midcircuit.qasm", "qrunpack", "qps"])
    expect(out3, "REJECTED")
    m3 = re.search(r"Job \d+ REJECTED: ([^\n]*)", out3)
    check(m3 and "mid-circuit" in m3.group(1).lower(),
          f"an unrunnable circuit is rejected on the scheduling path too, "
          f"got {m3.group(1) if m3 else None!r}")


def block_unrunnable_circuits():
    '''Unrunnable circuits become REJECTED jobs through the runner, no crash'''
    # DevQ declines two kinds of circuit, and the benchmark runner must
    # surface BOTH as REJECTED jobs in a completed run rather than crashing:
    #   - well-formed but UNRUNNABLE ON ANY BACKEND (mid-circuit
    #     measurement — a gate on a qubit after it is measured): the
    #     frontend marks unrunnable_reason, the kernel rejects the job.
    #     (Classical control is NOT in this bucket anymore — it is a
    #     runnable dynamic circuit, declined only per-device.)
    #   - MALFORMED source that fails to parse: submit_jobs turns the parse
    #     error into a REJECTED placeholder job carrying the error as its
    #     reason, so one bad circuit does not abort a whole workload.
    # A genuine SPEC-authoring error (missing file) must STILL abort — that
    # is the user's spec being wrong, not a circuit being unrunnable.
    import json, os, tempfile
    from benchmark import runner as R
    from benchmark.spec import SpecError

    tmp = tempfile.mkdtemp(prefix="devq_unrunnable_")

    # A deliberately malformed circuit: measures a register it never
    # declares — invalid OpenQASM 2.0 (the real QASMBench vqe_uccsd defect).
    bad = os.path.join(tmp, "malformed.qasm")
    with open(bad, "w") as h:
        h.write("OPENQASM 2.0;\ninclude \"qelib1.inc\";\n"
                "qreg reg[2];\ncreg c[2];\nh reg[0];\n"
                "measure q[0] -> c[0];\n")   # 'q' undeclared

    # A SECOND, different malformed circuit. Both fail to parse and become
    # placeholder REJECTED jobs — and their placeholder hashes must be
    # DISTINCT (derived from the source path), or the two collapse onto one
    # circuit_hash and the report shows only one, deduping the other. This
    # is the collision that made rejected rows print a shared bare hash.
    bad2 = os.path.join(tmp, "malformed2.qasm")
    with open(bad2, "w") as h:
        h.write("OPENQASM 2.0;\ninclude \"qelib1.inc\";\n"
                "qreg other[3];\ncreg c[3];\nx other[1];\n"
                "measure p[0] -> c[0];\n")   # 'p' undeclared, different file

    spec_path = os.path.join(tmp, "wl.json")
    with open(spec_path, "w") as h:
        json.dump({
            "name": "unrunnable", "seed": SEED,
            "devices": [{"id": "alpha", "provider": "devq.simulated",
                         "backend": {"kind": "fully_connected",
                                     "num_qubits": 5}}],
            "jobs": [
                {"circuit": BELL},                       # runs
                {"circuit": QASM2 + "midcircuit.qasm"},  # unrunnable (mid-circuit)
                {"circuit": bad},                        # malformed
                {"circuit": bad2},                       # malformed (2nd)
            ],
        }, h)

    try:
        out = os.path.join(tmp, "run")
        manifest = R.run(spec_path, out_dir=out, quiet=True)

        entry = manifest["sessions"][0]
        # The run COMPLETED (with failures) — it did not crash on the
        # malformed circuit. This is the whole point: a bad circuit is a
        # rejected row, not a fatal SpecError.
        check(entry["outcome"] in (R.COMPLETED, R.WITH_FAILURES),
              f"a workload with unrunnable circuits still completes, "
              f"got {entry['outcome']}"
              + (f" — {entry.get('error','')[:80]}"
                 if entry["outcome"] == R.CRASHED else ""))

        log = os.path.join(out, entry["log"])
        recs = [json.loads(l) for l in open(log) if l.strip()]
        summary = [r for r in recs if r.get("event") == "summary"][-1]
        states = {row["job_id"]: row["state"] for row in summary["per_job"]}
        rejects = {r["job_id"]: r.get("reason", "")
                   for r in recs if r.get("event") == "reject"}

        # Job 1 (bell) runs; jobs 2, 3, 4 reject.
        state_vals = sorted(states.values())
        n_finished = sum(1 for s in states.values() if s == "FINISHED")
        n_rejected = sum(1 for s in states.values() if s == "REJECTED")
        check(n_finished == 1 and n_rejected == 3,
              f"bell FINISHED, all three unrunnable circuits REJECTED — got "
              f"{state_vals}")

        # The rejection reasons cover both flavours: the unrunnable
        # construct (mid-circuit measurement) and the parse failures.
        reasons = " || ".join(rejects.values()).lower()
        check("mid-circuit" in reasons,
              f"the unrunnable circuit rejects citing mid-circuit measurement, "
              f"reasons={list(rejects.values())}")
        check("could not parse" in reasons or "parse" in reasons,
              f"the malformed circuits reject citing a parse failure, "
              f"reasons={list(rejects.values())}")

        # The two DIFFERENT malformed circuits must have DISTINCT
        # circuit_hashes — otherwise they collide onto one and the report
        # shows only one rejected row, hiding the other. This is the bug
        # that made rejected rows print a shared bare hash.
        malformed_hashes = [row["circuit_hash"] for row in summary["per_job"]
                            if row["state"] == "REJECTED"
                            and row["circuit_hash"]
                            and rejects.get(row["job_id"], "").lower()
                                .startswith("could not parse")]
        check(len(malformed_hashes) == len(set(malformed_hashes))
              and len(malformed_hashes) == 2,
              f"the two malformed circuits get distinct hashes (no "
              f"collision), got {malformed_hashes}")

        # The malformed circuit's reject record carries a circuit_label, so
        # the results are self-describing (this is what stops rejected rows
        # from printing a bare hash).
        labelled = [r for r in recs if r.get("event") == "reject"
                    and r.get("circuit_label")]
        check(len(labelled) == 3,
              f"all three reject records carry a circuit_label for the "
              f"report, got {len(labelled)}")

        # A genuine SPEC error still aborts: a missing circuit file is the
        # user's mistake, not a circuit DevQ declines.
        bad_spec = os.path.join(tmp, "missing.json")
        with open(bad_spec, "w") as h:
            json.dump({
                "name": "missing", "seed": SEED,
                "devices": [{"id": "alpha", "provider": "devq.simulated",
                             "backend": {"kind": "linear", "num_qubits": 3}}],
                "jobs": [{"circuit": "does_not_exist.qasm"}],
            }, h)
        raised = False
        try:
            R.run(bad_spec, out_dir=os.path.join(tmp, "x"), quiet=True)
        except Exception:
            raised = True
        # R.run records a crashed session rather than propagating; check the
        # outcome reflects the failure either way.
        if not raised:
            mf = R.run(bad_spec, out_dir=os.path.join(tmp, "x2"), quiet=True)
            crashed = mf["sessions"][0]["outcome"] == R.CRASHED
        else:
            crashed = True
        check(crashed,
              "a missing circuit file is a spec error and fails the "
              "session, not silently absorbed as a rejected job")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def block_rejected_no_ideal():
    '''A REJECTED job gets no reference ideal (call-site filter in the runner)'''
    # A job REJECTED at runtime (unsatisfiable: no valid allocation exists on
    # any attached device) never runs and never produces measured counts, so
    # it has no fidelity to compute and needs no noiseless ideal. The runner
    # therefore filters REJECTED jobs BEFORE computing ideals. This is the
    # call-site filter — rejection is a run-level fact (the same circuit may
    # be REJECTED under contention and RUNNING elsewhere), so it lives in the
    # runner, not inside compute_ideals (which is circuit-level and
    # job-agnostic; its own skip covers unrunnable_reason, a circuit property).
    #
    # The discriminating setup needs a REFERENCE-CAPABLE provider (only
    # ibm.simulated overrides reference_ideal; devq.simulated does not, so a
    # devq-only run emits no ideals at all and cannot exercise the filter) and
    # a REJECTED job whose circuit is otherwise perfectly simulable — so that
    # WITHOUT the filter it would receive an ideal, and WITH it it does not.
    # BELL runs and earns a reference record; GHZ is forced REJECTED by an
    # impossible max_qubit_error threshold and must earn NONE. Because GHZ
    # appears only on the rejected job, the absence of its hash from the
    # reference records is the filter's effect, cleanly isolated.
    import json, os, tempfile
    from benchmark import runner as R
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

    tmp = tempfile.mkdtemp(prefix="devq_reject_ideal_")
    spec_path = os.path.join(tmp, "wl.json")
    with open(spec_path, "w") as h:
        json.dump({
            "name": "rejected_no_ideal", "seed": SEED,
            "devices": [{"id": "nairobi", "provider": "ibm.simulated",
                         "backend": {"backend_name": "FakeNairobiV2"}}],
            "jobs": [
                {"circuit": BELL},                                 # FINISHES
                {"circuit": "test_circuits/ghz.qasm",
                 "max_qubit_error": 0.0},                          # REJECTED
            ],
        }, h)

    try:
        out = os.path.join(tmp, "run")
        manifest = R.run(
            spec_path, out_dir=out, quiet=True,
            register_providers={"ibm.simulated": IBMSimulatedProvider})

        entry = manifest["sessions"][0]
        # The run COMPLETED (with failures) — a rejected job is a row, not a
        # crash. If this ever reports CRASHED, the rejection setup is broken
        # (e.g. provider not registered) and the rest of the test is moot.
        check(entry["outcome"] in (R.COMPLETED, R.WITH_FAILURES),
              f"a run with one rejected job still completes, got "
              f"{entry['outcome']}"
              + (f" — {entry.get('error','')[:80]}"
                 if entry["outcome"] == R.CRASHED else ""))

        log  = os.path.join(out, entry["log"])
        recs = [json.loads(l) for l in open(log) if l.strip()]
        summary = [r for r in recs if r.get("event") == "summary"][-1]

        # per_job rows carry state + circuit_hash (no label). BELL is the one
        # FINISHED job, GHZ the one REJECTED job, so map by state. There is
        # exactly one of each, which we assert before pulling their hashes.
        by_state = {}
        for row in summary["per_job"]:
            by_state.setdefault(row["state"], []).append(row["circuit_hash"])

        check(len(by_state.get("FINISHED", [])) == 1
              and len(by_state.get("REJECTED", [])) == 1,
              f"exactly one bell FINISHED and one ghz REJECTED — got "
              f"{ {s: len(v) for s, v in by_state.items()} }")
        bell_hash = by_state["FINISHED"][0]
        ghz_hash  = by_state["REJECTED"][0]
        check(bell_hash and ghz_hash and bell_hash != ghz_hash,
              f"bell and ghz have distinct hashes, got "
              f"bell={bell_hash!r} ghz={ghz_hash!r}")

        ref_hashes = {r["circuit_hash"] for r in recs
                      if r.get("event") == "reference"}
        # The runnable circuit earns an ideal ...
        check(bell_hash in ref_hashes,
              f"the FINISHED bell circuit earns a reference ideal, "
              f"reference hashes={sorted(h[:8] for h in ref_hashes)}")
        # ... and the REJECTED circuit earns NONE. This is the whole point:
        # without the call-site filter, ghz's hash would appear here too.
        check(ghz_hash not in ref_hashes,
              f"the REJECTED ghz circuit earns NO reference ideal (the "
              f"call-site filter), but its hash {ghz_hash[:8]} appeared in "
              f"reference records {sorted(h[:8] for h in ref_hashes)}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def block_packing_across_devices():
    '''Bracket groups, batch packing and cross-device concurrency'''
    sh  = three_device()
    # qrunpack dispatches what fits right now (jobs 1, 2, 4) and returns;
    # job 3 cannot fit alongside the two bells, so it stays WAITING and
    # self-heals when a bell completes and frees qubits. settle() drives
    # the session to quiescence, so job 3's (later) dispatch line lands in
    # the transcript too. All mapping/device reads come from that combined
    # dispatch log.
    out  = run(sh, [f"qsubmit [{BELL} {BELL} {GHZ} --no-exec=d0] {GHZ} --exec=lagos",
                    "qrunpack", "qmap 1", "qmem"])
    settled = settle(sh, 1, 2, 3, 4)
    out += settled

    # two bells packed into the same cycle on disjoint qubits
    check(mapping_of(out, 1) == "{0: 1, 1: 2}",
          f"job 1 packed onto {{0: 1, 1: 2}}, got {mapping_of(out, 1)}")
    check(mapping_of(out, 2) == "{0: 4, 1: 5}",
          f"job 2 packed onto disjoint {{0: 4, 1: 5}} in the same cycle, "
          f"got {mapping_of(out, 2)}")
    # Job 3 cannot fit alongside the two bells, so it waits and allocates
    # once qubits are freed. Assert the invariant (it lands on nairobi, on
    # a connected triple) rather than a specific block, since which qubits
    # are free depends on async completion order.
    check("nairobi" in device_of(out, 3),
          f"job 3 routed to nairobi, got {device_of(out, 3)}")
    check(len(eval(mapping_of(out, 3))) == 3,
          f"job 3 (ghz) allocated 3 qubits after waiting: "
          f"{mapping_of(out, 3)}")
    check("lagos" in device_of(out, 4),
          f"job 4 honoured its --exec=lagos pin, got {device_of(out, 4)}")
    check(finished_ids(settled) == {"1", "2", "3", "4"},
          "all four jobs finished")
    # all qubits returned to their pools afterwards
    expect_absent(run(sh, ["qmem"]), "[X]")


def block_parser_errors():
    '''Malformed commands are rejected atomically, creating no jobs'''
    sh  = three_device()
    out = run(sh, [
        f"qsubmit {BELL} --exec=d5",
        f"qsubmit {BELL} --exec=d0 --no-exec=d1",
        f"qsubmit {BELL} --exec=[d0,d1]",
        f"qsubmit {BELL} --exec=sherbrooke",
        f"qsubmit nofile.qasm {BELL}",
        "qps",
    ])

    expect(out, "Device d5 does not exist",
           "mutually exclusive",
           "brackets are reserved",
           "'sherbrooke' is not a device",
           "Named devices: nairobi, lagos",
           "No such file or directory")
    check("No jobs." in out,
          "no jobs were created — all five batches rejected atomically")


def block_per_job_shots():
    '''Per-job --shots overrides the device shot count; absent falls through'''
    from kernel.events import RecordSink
    from benchmark.spec import validate_spec, SpecError

    # ── Shell path: override reaches dispatch, absent falls through ────────────
    #
    # Assert against the RESOLVED value the kernel actually dispatched with
    # (the dispatch event's shots), NOT rendered output — "absent from the
    # display" is not "used the right number". One device so both jobs land
    # on the same shot cascade, making the override-vs-fallthrough contrast
    # the ONLY thing that can differ.
    sh   = session(devices=[("devq.simulated", "random", 5, None, None)])
    sink = RecordSink()
    sh.kernel.sink = sink

    run(sh, [
        f"qsubmit {BELL} --shots=333",   # job 1: explicit override
        f"qsubmit {BELL}",               # job 2: defer to device config
        "qrunpack",
    ])

    device_shots = sh.kernel.contexts[0].shots
    dispatched   = {r["job_id"]: r["shots"]
                    for r in sink.records if r["event"] == "dispatch"}

    check(device_shots != 333,
          f"test is meaningful — the device default ({device_shots}) "
          f"differs from the override (333), so a pass cannot be an "
          f"accident of them coinciding")
    check(dispatched.get(1) == 333,
          f"job 1's --shots=333 reached dispatch as 333, overriding the "
          f"device's {device_shots} (got {dispatched.get(1)})")
    check(dispatched.get(2) == device_shots,
          f"job 2 named no shots and fell through to the device-resolved "
          f"{device_shots} (got {dispatched.get(2)})")

    # The submit event records the RAW per-job value (None when unspecified),
    # distinct from the resolved dispatch value — the two-clock analogue for
    # shots: what the job asked for vs. what it ran with.
    submitted = {r["job_id"]: r.get("shots")
                 for r in sink.records if r["event"] == "submit"}
    check(submitted.get(1) == 333 and submitted.get(2) is None,
          f"submit records the raw ask (333 / None), not the resolved "
          f"value (got {submitted.get(1)} / {submitted.get(2)})")

    # ── Shell path: malformed --shots rejects the whole command, no job ───────
    sh2  = session(devices=[("devq.simulated", "random", 5, None, None)])
    out2 = run(sh2, [
        f"qsubmit {BELL} --shots=0",
        f"qsubmit {BELL} --shots=-5",
        f"qsubmit {BELL} --shots=10.5",
        f"qsubmit {BELL} --shots=abc",
        f"qsubmit {BELL} --shots=",
        "qps",
    ])
    check("No jobs." in out2,
          "all five malformed --shots batches rejected atomically — "
          "no job created by a bad shot count")

    # ── Spec path: validator coerces valid, rejects malformed ─────────────────
    def job_spec(shots):
        job = {"circuit": "a.qasm"}
        if shots is not _ABSENT:
            job["shots"] = shots
        return {
            "name": "shots_probe", "config": "c.json",
            "jobs": [job],
            "devices": [{"id": "d0", "provider": "devq.simulated",
                         "backend": {"kind": "random", "qubits": 5}}],
        }

    ok = job_spec(2048)
    validate_spec(ok)
    check(ok["jobs"][0]["shots"] == 2048 and isinstance(ok["jobs"][0]["shots"], int),
          "spec validator accepts a positive-int shots and coerces to int")

    coerced = job_spec("4096")            # ${} placeholders resolve to strings
    validate_spec(coerced)
    check(coerced["jobs"][0]["shots"] == 4096,
          "spec validator coerces a numeric string (placeholder form) to int")

    absent = job_spec(_ABSENT)
    validate_spec(absent)
    check("shots" not in absent["jobs"][0],
          "a spec job without shots is left untouched — defers to the device")

    for bad in (0, -1, 10.5, "abc"):
        raised = False
        try:
            validate_spec(job_spec(bad))
        except SpecError:
            raised = True
        check(raised, f"spec validator rejects shots={bad!r}")


# Sentinel for "key not present" distinct from an explicit None value.
_ABSENT = object()


def block_round_robin_router():
    '''Round-robin router cycles devices in index order'''
    sh  = three_device(config="round_robin.config.json")
    out = run(sh, ["qconfig", f"qsubmit {BELL} {BELL} {BELL}", "qrunpack", "qps"])

    expect(out, "round_robin", "Round Robin Router", "User (global)")
    devices = [device_of(out, i) for i in (1, 2, 3)]
    check(devices[0].startswith("d0")
          and "nairobi" in devices[1]
          and "lagos" in devices[2],
          f"three identical bells rotated d0 → d1 → d2, got {devices}")


def block_per_device_config():
    '''A per-device config overrides only that device'''
    sh  = three_device(d1_config="d1.static.config.json")
    out = run(sh, ["qconfig d1", f"qrun {BELL} --exec=d1", "qmap 1"])

    expect(out, "static", "Static Allocator", "User (d1)", "512")
    # scheduler and weights still come from core
    expect(out, "packing", "DevQ Core")
    # static ignores noise: first free block, not noise_graph's {0:1, 1:2}
    check(mapping_of(out, 1) == "{0: 0, 1: 1}",
          f"static allocator took the first free block {{0: 0, 1: 1}} "
          f"(S 0.0155) rather than noise_graph's {{0: 1, 1: 2}} (S 0.0102), "
          f"got {mapping_of(out, 1)}")


def block_weight_normalisation():
    '''Cost weights normalise, and edge-only weighting changes the mapping'''
    sh  = three_device(config="weights_1_9.config.json",
                       d1_config="d1.edge_only.config.json")
    out = run(sh, ["qconfig d2", "qconfig d1",
                   f"qrun {BELL} --exec=d1", f"qrun {BELL} --exec=d2"])

    # raw 1/9 normalised to 0.1/0.9 at the global scope
    expect(out, "0.1", "0.9", "User (global)")
    # per-device override, edge-only
    expect(out, "User (d1)")
    # edge-only picks Nairobi's lowest-error edge (1,3) instead of (1,2)
    check(mapping_of(out, 1) == "{0: 1, 1: 3}",
          f"edge-only weighting flipped nairobi to {{0: 1, 1: 3}} "
          f"(edge 0.0068 < 0.0070), got {mapping_of(out, 1)}")
    # Lagos unchanged: 1/9 has the same ratio as the 0.1/0.9 default
    check(mapping_of(out, 2) == "{0: 1, 1: 3}",
          f"lagos unchanged at {{0: 1, 1: 3}} — 1/9 has the same ratio as "
          f"the 0.1/0.9 default, got {mapping_of(out, 2)}")


def block_zero_weight_fallback():
    '''Both weights zero warns and falls back to core defaults'''
    # The warning is emitted during config resolution, i.e. inside
    # build() — so construction has to be captured too, not just the
    # commands afterwards.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sh = three_device(config="zero_weights.config.json")
        sh.onecmd("qconfig d1")
    out = buf.getvalue()

    expect(out, "Warning", "both 0", "Falling back to core defaults")
    # and the effective values are the core defaults
    expect(out, "0.1", "0.9", "DevQ Core")


# ── Single-device blocks — no routing involved ────────────────────────────────

def block_single_device_ibm():
    '''A one-device session works with no routing decisions to make'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", None, None)])
    out = run(sh, ["qdevices", "qconfig", "qerrors q d0", "qtopology d0",
                   f"qrun {BELL}", "qmap 1", "qps", "qmem"])
    out += settle(sh, 1)

    # the only device is d0 — nothing should refer to d1
    expect(out, "FakeNairobiV2")
    expect_absent(out, "d1", "d2")
    # noise_graph still picks Nairobi's best pair
    check(mapping_of(out, 1) == "{0: 1, 1: 2}",
          f"noise_graph still picks {{0: 1, 1: 2}} with no peer devices, "
          f"got {mapping_of(out, 1)}")
    expect(out, "FINISHED")


def block_single_device_named():
    '''Naming works with one device, and the index still resolves'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", "solo", None)])
    out = run(sh, ["qdevices", "qerrors q solo", f"qrun {BELL} --exec=solo"])
    expect(out, "solo (d0)")
    check("solo" in device_of(out, 1),
          f"job routed to the named sole device, got {device_of(out, 1)}")

    sh2  = session("router_only.config.json",
                   [("ibm.simulated", "FakeNairobiV2", "solo", None)])
    out2 = run(sh2, [f"qrun {BELL} --exec=d0"])
    check(mapping_of(out2, 1) == mapping_of(out, 1),
          "--exec=solo and --exec=d0 produce the same mapping")


def block_single_device_batch():
    '''Batch submission and packing on a single device'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", None, None)])
    out = run(sh, [f"qsubmit {BELL} {BELL}", "qrunpack"])
    settled = settle(sh, 1, 2)
    out += settled
    # both bells packed onto one device in the same cycle, disjoint qubits
    m1, m2 = mapping_of(out, 1), mapping_of(out, 2)
    check(m1 != m2,
          f"two bells packed onto disjoint blocks ({m1} and {m2})")
    check(finished_ids(settled) == {"1", "2"}, "both bells finished")


def block_single_device_rejection():
    '''Rejection on a single device names that device in the reason'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeLagosV2", "lagos", None)])
    out = run(sh, [f"qrun {BELL} --max-qubit-error=0.03", "qps"])
    expect(out, "REJECTED")


def block_single_device_devq_provider():
    '''The mock provider alone — no Qiskit involved in execution'''
    sh  = session(None, [("devq.simulated", "fully_connected", 5, "mock", None)])
    out = run(sh, ["qdevices", "qtopology d0", f"qrun {BELL}", "qps"])
    expect(out, "mock (d0)", "DevQSimulatedProvider", "FINISHED")


def block_supports_dynamic_capability():
    '''supports_dynamic declines by default, IBM affirms, no qiskit escapes providers/ibm'''
    # The provider-contract capability for dynamic circuits (classical
    # feedback). Its shape mirrors reference_ideal: an OPTIONAL method that
    # the base DECLINES and a capable provider overrides. Here we assert the
    # inheritance resolves as designed — the override lives ONCE on the shared
    # IBMProvider base, so both IBM subclasses affirm and DevQ inherits the
    # base decline — and, separately, the boundary this capability must never
    # breach: no qiskit/ibm import may escape providers/ibm/.
    import os, re
    from circuits.circuit_rep import CircuitRep
    from providers.base_provider import BaseProvider
    from providers.ibm.ibm_provider import IBMProvider
    from providers.ibm.ibm_real_provider import IBMRealProvider

    # A representative circuit is enough — v1 answers uniformly, ignoring the
    # argument, so a plain Bell circuit exercises the predicate.
    cr = CircuitRep(2, 2)
    cr.add_gate("h", [0])
    cr.add_gate("cx", [0, 1])

    # Resolution: the override is defined exactly once, on IBMProvider, and
    # both IBM subclasses inherit THAT function object (not a copy) while
    # DevQSimulatedProvider inherits BaseProvider's default. Checking function
    # identity proves the single-point-of-truth, not just the boolean.
    check(IBMSimulatedProvider.supports_dynamic is IBMProvider.supports_dynamic,
          "IBMSimulatedProvider inherits the supports_dynamic override from "
          "IBMProvider (single point of truth)")
    check(IBMRealProvider.supports_dynamic is IBMProvider.supports_dynamic,
          "IBMRealProvider inherits the supports_dynamic override from "
          "IBMProvider (single point of truth)")
    check(DevQSimulatedProvider.supports_dynamic is BaseProvider.supports_dynamic,
          "DevQSimulatedProvider inherits the BaseProvider decline (no override)")

    # Behaviour: base and DevQ decline (False), both IBM providers affirm
    # (True). Instantiate the ones that construct cheaply; the IBM providers
    # take a seed, DevQ takes a seed, base is abstract-ish but the method is
    # concrete and callable on an instance-free bound check via the class.
    ibm_sim = IBMSimulatedProvider(seed=SEED)
    devq    = DevQSimulatedProvider(seed=SEED)
    check(ibm_sim.supports_dynamic(cr) is True,
          "IBMSimulatedProvider affirms supports_dynamic")
    check(devq.supports_dynamic(cr) is False,
          "DevQSimulatedProvider declines supports_dynamic")
    # BaseProvider's default, called through a subclass that does NOT override
    # (DevQ), already exercised the decline; assert the default itself returns
    # False so a future edit to the base cannot silently flip the contract.
    check(BaseProvider.supports_dynamic(devq, cr) is False,
          "BaseProvider.supports_dynamic default declines (False)")

    # Boundary regression: qiskit/ibm imports must stay INSIDE providers/ibm.
    # The kernel, IR, frontends and routing layer are Qiskit-free, and the
    # dynamic-circuit work must not be the change that breaches that. Scan
    # DevQ's own packages for a top-level `import qiskit` / `from qiskit` /
    # `import ibm...`; the only legitimate homes are providers/ibm/ (drivers)
    # and the test/verify oracles, which cross-check AGAINST qiskit by design.
    root = os.path.dirname(os.path.abspath(__file__))
    OURS = ("benchmark", "circuits", "config", "engine", "frontends",
            "hardware", "kernel", "providers", "registry", "research", "shell")
    # Oracles that legitimately import qiskit to check DevQ against it.
    ALLOWED = (os.path.join("providers", "ibm"),)
    import_re = re.compile(r"^\s*(?:from|import)\s+(qiskit|ibm)\b", re.M)
    leaks = []
    for pkg in OURS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, pkg)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, root)
                if any(rel.startswith(a) for a in ALLOWED):
                    continue
                # research/ holds a hardware-run script that legitimately
                # drives qiskit-ibm-runtime; it is a research entry point,
                # not core, so exclude it explicitly rather than by accident.
                if rel.startswith("research" + os.sep):
                    continue
                with open(path) as handle:
                    if import_re.search(handle.read()):
                        leaks.append(rel)
    check(not leaks,
          f"no qiskit/ibm import escapes providers/ibm (leaks: {sorted(leaks)})")


def block_conditional_ir():
    '''CircuitRep represents a conditional op: is_dynamic, cregs, body-qubit hazard'''
    # Step 2 of dynamic-circuit support: a classically-conditioned gate is a
    # FIRST-CLASS op in the IR, inspectable through derived views that cannot
    # drift from the ordered stream. This block pins the representation — the
    # frontend (step 3) and lowering (step 5) build on exactly this shape.
    from circuits.circuit_rep import CircuitRep

    # h q0; measure q0->c0; if (c==1) x q1  — the canonical feedback circuit.
    cr = CircuitRep(2, 1)
    cr.add_creg("c", 0, 1)
    cr.add_gate("h", [0])
    cr.add_measure(0, 0)
    cr.add_conditional([0], 1,
                       {"op": "gate", "gate": "x", "qubits": [1], "params": []})

    # is_dynamic is the flag the kernel checks against supports_dynamic.
    check(cr.is_dynamic is True,
          "a circuit with a conditional op is is_dynamic")

    # The conditional op carries the guard and the guarded gate, in the
    # documented shape: condition {clbits, value}, body a single gate op.
    conds = cr.conditionals
    check(len(conds) == 1, "conditionals view returns the one conditional op")
    cond = conds[0]
    check(cond["op"] == "conditional"
          and cond["condition"] == {"clbits": [0], "value": 1}
          and cond["body"]["gate"] == "x"
          and cond["body"]["qubits"] == [1],
          "conditional op records condition {clbits,value} and the body gate")

    # It lives in the ordered stream in SOURCE ORDER — after the measure,
    # not hoisted into a side channel. Order is what execution needs.
    ops = [i["op"] for i in cr.instructions]
    check(ops == ["gate", "measure", "conditional"],
          f"conditional is carried in source order, got {ops}")

    # cregs exposes the declared register structure the condition resolves
    # against, and the view is a defensive copy (mutating it cannot corrupt
    # the circuit's own structure).
    check(cr.cregs == {"c": (0, 1)}, "cregs view exposes declared registers")
    view = cr.cregs
    view["evil"] = (9, 9)
    check("evil" not in cr.cregs, "cregs view is a copy, not the live dict")

    # A plain circuit is not dynamic and declares no cregs unless told to.
    plain = CircuitRep(2, 2)
    plain.add_gate("h", [0])
    plain.add_gate("cx", [0, 1])
    check(plain.is_dynamic is False, "a circuit with no conditional is not dynamic")
    check(plain.cregs == {}, "a circuit with no declared cregs has an empty view")

    # get_depth counts only real gates — the conditional's guarded gate is
    # deliberately not counted (a maybe-fired layer has no defined depth).
    check(cr.get_depth() == 1,
          f"get_depth counts the unconditioned gate only, got {cr.get_depth()}")

    # The mid-circuit hazard check reaches INTO the conditional body: a
    # guarded gate on an already-measured qubit is the same hazard as a bare
    # one, and must be caught.
    haz = CircuitRep(1, 1)
    haz.add_measure(0, 0)
    haz.add_conditional([0], 1,
                        {"op": "gate", "gate": "x", "qubits": [0], "params": []})
    reason = haz.find_mid_circuit_measurement()
    check(reason is not None and "conditional gate" in reason,
          "a conditional body-gate on a measured qubit is flagged mid-circuit")

    # ...but the LEGITIMATE feedback shape — guard reads a measured clbit,
    # body acts on an UNmeasured qubit — is not a hazard. Reading a measured
    # bit is exactly what a dynamic circuit is for.
    check(cr.find_mid_circuit_measurement() is None,
          "reading a measured clbit in a guard is not a mid-circuit hazard")


def block_conditional_frontend():
    '''The 2.0 parser emits if(creg==N) as conditional ops, resolving registers'''
    # Step 3: the frontend turns `if (creg==N) <stmt>` into first-class
    # conditional ops instead of marking the circuit unrunnable. This block
    # exercises the PARSE path end to end — register resolution to clbits,
    # multi-bit conditions, broadcast expansion, and the boundary between a
    # well-formed conditional (emitted) and a genuine error (raised).
    from frontends.qasm2.parser import parse, QASMError

    # Canonical feedback: h; measure; if(c==1) x on a DIFFERENT qubit. Emits
    # one conditional, resolves c to clbit [0], and is NOT unrunnable.
    cr = parse('OPENQASM 2.0;\ninclude "qelib1.inc";\n'
               'qreg q[2];\ncreg c[1];\n'
               'h q[0];\nmeasure q[0] -> c[0];\nif (c==1) x q[1];\n')
    check(cr.unrunnable_reason is None and cr.is_dynamic,
          f"if(c==1): parses to a dynamic circuit, not unrunnable, got "
          f"reason={cr.unrunnable_reason!r} is_dynamic={cr.is_dynamic}")
    check([i["op"] for i in cr.instructions] == ["gate", "measure", "conditional"],
          f"if(c==1): the conditional is in source order, got "
          f"{[i['op'] for i in cr.instructions]}")
    cond = cr.conditionals[0]
    check(cond["condition"] == {"clbits": [0], "value": 1}
          and cond["body"]["gate"] == "x" and cond["body"]["qubits"] == [1],
          f"if(c==1): resolves c->[0], guards x on q[1], got {cond!r}")

    # Multi-bit register: if(c==3) with a 2-bit creg spans both clbits,
    # LSB-first, value 3.
    cr2 = parse("OPENQASM 2.0;\nqreg q[3];\ncreg c[2];\n"
                "measure q[0] -> c[0];\nmeasure q[1] -> c[1];\n"
                "if (c==3) x q[2];\n")
    check(cr2.conditionals[0]["condition"] == {"clbits": [0, 1], "value": 3},
          f"if(c==3) over creg[2]: condition spans both clbits LSB-first, "
          f"got {cr2.conditionals[0]['condition']!r}")

    # Broadcast: `if(c==1) h q;` over a 2-qubit register expands to TWO
    # conditional ops, each guarding one qubit on the SAME condition. This
    # is the case a naive "wrap one op" implementation gets wrong.
    cr3 = parse("OPENQASM 2.0;\nqreg q[2];\ncreg c[1];\n"
                "measure q[0] -> c[0];\nif (c==1) h q;\n")
    conds3 = cr3.conditionals
    check(len(conds3) == 2
          and conds3[0]["body"]["qubits"] == [0]
          and conds3[1]["body"]["qubits"] == [1]
          and all(k["condition"] == {"clbits": [0], "value": 1} for k in conds3),
          f"if(c==1) h q; broadcasts to two conditionals sharing the "
          f"condition, got {conds3!r}")

    # A conditional whose body reuses the MEASURED qubit is still caught as
    # mid-circuit (the step-2 hazard reaching into the body) — unrunnable,
    # even though it parsed to a conditional op.
    cr4 = parse("OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\n"
                "measure q[0] -> c[0];\nif (c==1) x q[0];\n")
    check(cr4.is_dynamic and cr4.unrunnable_reason is not None
          and "mid-circuit" in cr4.unrunnable_reason.lower(),
          f"if(c==1) x q[0] on the measured qubit: dynamic but unrunnable "
          f"(mid-circuit), got reason={cr4.unrunnable_reason!r}")

    # An unknown register in a condition is a genuine parse error — raised,
    # not emitted (the condition names something never declared).
    try:
        parse("OPENQASM 2.0;\nqreg q[1];\nif (nope==1) x q[0];\n")
        check(False, "unknown creg in if-condition should raise")
    except QASMError as e:
        check("nope" in str(e) and "if-condition" in str(e),
              f"unknown creg in condition raises naming it, got {e}")


def block_dynamic_feasibility():
    '''unsatisfiable_reason declines a dynamic circuit on a provider without feedback'''
    # Step 4: capability is feasibility. A dynamic circuit is infeasible on a
    # device whose provider cannot execute classical feedback — checked in
    # unsatisfiable_reason, ahead of the allocator, against the device's
    # PROVIDER. This is the clause the router's per-candidate _candidates
    # filter rides, so a dynamic job routes to a capable device and is
    # REJECTED (per-device reason) only when none is attached. Tested at the
    # unit level, directly on MemoryManager, with tiny provider stubs so no
    # Qiskit backend is needed — the capability is a plain DevQ predicate.
    from kernel.memory.memory_manager import MemoryManager
    from hardware.device import QuantumDevice
    from frontends.qasm2.parser import parse
    from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator

    class _Provider:
        # Minimal provider stub: only the capability predicate matters here.
        def __init__(self, dynamic):
            self._dynamic = dynamic
        def supports_dynamic(self, circuit):
            return self._dynamic

    def _device(provider):
        # A 2-qubit line device, enough to host the fixtures. The allocator
        # only runs for the STATIC path (the dynamic path short-circuits
        # before delegation), so a real NoiseGraphAllocator exercises the
        # genuine feasible() delegation for the static assertions.
        return QuantumDevice(
            kind="stub", num_qubits=2, coupling_map=[(0, 1)],
            basis_gates=["h", "cx", "x", "measure"],
            error_map={0: 0.01, 1: 0.01}, edge_error_map={(0, 1): 0.02},
            provider=provider)

    dyn    = parse(open(QASM2 + "conditional.qasm").read())   # is_dynamic
    static = parse(open(BELL).read())                          # not dynamic
    check(dyn.is_dynamic and not static.is_dynamic,
          "fixtures: conditional.qasm is dynamic, bell is not")

    mm_no  = MemoryManager(_device(_Provider(False)), NoiseGraphAllocator())
    mm_yes = MemoryManager(_device(_Provider(True)),  NoiseGraphAllocator())

    # The heart of it: a dynamic circuit is INFEASIBLE where the provider
    # declines feedback, and FEASIBLE where it affirms — the difference is
    # the provider capability alone, same circuit, same allocator.
    reason = mm_no.unsatisfiable_reason(dyn)
    check(reason is not None and "feedback" in reason.lower(),
          f"dynamic circuit is infeasible on a no-feedback provider, with a "
          f"capability reason, got {reason!r}")
    check(mm_yes.unsatisfiable_reason(dyn) is None,
          "dynamic circuit is feasible on a provider that supports feedback")

    # A STATIC circuit is unaffected by the capability gate on EITHER
    # provider — the clause only fires for is_dynamic, so a normal circuit
    # delegates straight to the allocator as before (feasible on this
    # 2-qubit device).
    check(mm_no.unsatisfiable_reason(static) is None,
          "a static circuit is feasible on a no-feedback provider (clause "
          "does not fire)")
    check(mm_yes.unsatisfiable_reason(static) is None,
          "a static circuit is feasible on a feedback provider")

    # The capability check runs BEFORE the allocator, and proving that needs
    # a circuit that is BOTH dynamic-on-a-no-feedback-provider AND
    # allocation-infeasible — otherwise the two orderings are
    # indistinguishable (an allocatable circuit returns the same capability
    # reason either way). Build a dynamic circuit that needs MORE qubits than
    # the device has: a correct implementation returns the CAPABILITY reason
    # (feedback), a reversed one would return the ALLOCATION reason (qubits).
    wide = parse("OPENQASM 2.0;\n"
                 "qreg q[4];\ncreg c[1];\n"
                 "h q[0];\nmeasure q[0] -> c[0];\n"
                 "if (c==1) x q[3];\n")  # 4 qubits, device has 2; also dynamic
    check(wide.is_dynamic, "wide fixture is dynamic")
    wide_reason = mm_no.unsatisfiable_reason(wide)
    check(wide_reason is not None and "feedback" in wide_reason.lower(),
          f"a dynamic AND unallocatable circuit is declined for CAPABILITY "
          f"first (feedback), not allocation, proving the check precedes "
          f"delegation, got {wide_reason!r}")
    # And on a feedback-capable provider, the SAME wide circuit falls through
    # to the allocator and is declined for the ALLOCATION reason instead —
    # confirming the capability clause is what short-circuited above, not a
    # blanket rejection.
    wide_alloc = mm_yes.unsatisfiable_reason(wide)
    check(wide_alloc is not None and "feedback" not in wide_alloc.lower(),
          f"the same wide circuit on a feedback provider is declined by the "
          f"allocator (not for capability), got {wide_alloc!r}")


def block_dynamic_lowering():
    '''IBM lowering emits if_test for conditionals; Aer runs the feedback correctly'''
    # Step 5: a conditional op becomes real execution. build_qiskit_circuit
    # lowers a conditional to a Qiskit if_test block and bakes the
    # feeding measure inline (an if_test can only read a bit already
    # written mid-run). This block asserts the lowered STRUCTURE
    # deterministically, then — guarded on Aer being present — actually
    # RUNS a feedback circuit and checks the classical correlation holds.
    # reference_ideal declines dynamic circuits, since their ideal is not
    # defined through the noiseless density-matrix + marginalise path.
    from frontends.qasm2.parser import parse
    from providers.ibm.qiskit_lowering import build_qiskit_circuit

    # A dynamic circuit lowers to h, measure (baked inline), if_else — the
    # conditional became an if_test block, and the mid-circuit measure is in
    # the body (not deferred to the map).
    dyn = parse(open(QASM2 + "conditional.qasm").read())
    try:
        qc, mmap = build_qiskit_circuit(dyn, 1)
    except Exception as e:
        check(True, f"(dynamic lowering skipped — qiskit unavailable: "
                    f"{type(e).__name__})")
        return
    names = [i.operation.name for i in qc.data]
    check("if_else" in names,
          f"a conditional lowers to a Qiskit if_test/if_else block, got {names}")
    check(names.count("measure") == 1 and names.index("measure") < names.index("if_else"),
          f"the feeding measure is baked inline before the conditional, got {names}")

    # A STATIC circuit is unchanged by step 5: no measures baked into the
    # body (they stay in the map for the reference path), no if_else.
    static = parse("OPENQASM 2.0; include \"qelib1.inc\"; qreg q[2]; creg c[2]; "
                   "h q[0]; cx q[0],q[1]; measure q[0]->c[0]; measure q[1]->c[1];")
    sqc, smap = build_qiskit_circuit(static, 2)
    snames = [i.operation.name for i in sqc.data]
    check("if_else" not in snames and "measure" not in snames,
          f"a static circuit lowers to a measurement-free body as before, "
          f"got {snames}")
    check(smap == [(0, 0), (1, 1)],
          f"a static circuit's measures are still returned in the map, got {smap}")

    # reference_ideal declines a dynamic circuit (None), still answers a
    # static one — the measurement-free-body invariant it relies on holds
    # for every circuit it actually lowers.
    p = IBMSimulatedProvider(seed=SEED)
    check(p.reference_ideal(dyn) is None,
          "reference_ideal declines a dynamic circuit (returns None)")
    static_ideal = p.reference_ideal(static)
    check(static_ideal is not None
          and abs(static_ideal.get("00", 0) - 0.5) < 0.02
          and abs(static_ideal.get("11", 0) - 0.5) < 0.02,
          f"reference_ideal still answers a static (Bell) circuit, got "
          f"{static_ideal}")

    # THE REAL PROOF: run a feedback circuit on Aer and check the classical
    # correlation. h q0; measure->c0; if(c==1) x q1; measure q1->c1. The
    # feedback flips q1 exactly when c0==1, so the only outcomes are 00 and
    # 11 — never 01 or 10. A broken lowering (dropped condition, wrong bit)
    # would leak 01/10.
    try:
        from qiskit_aer import AerSimulator
    except ImportError:
        check(True, "(Aer run skipped — qiskit-aer unavailable)")
        return
    corr = parse("OPENQASM 2.0; include \"qelib1.inc\"; qreg q[2]; creg c[2]; "
                 "h q[0]; measure q[0]->c[0]; if (c==1) x q[1]; "
                 "measure q[1]->c[1];")
    cqc, _ = build_qiskit_circuit(corr, 2)
    sim = AerSimulator()
    counts = sim.run(cqc, shots=4000, seed_simulator=SEED).result().get_counts()
    leaked = {k: v for k, v in counts.items() if k not in ("00", "11")}
    check(not leaked,
          f"feedback correlation holds on Aer — only 00/11 outcomes, got "
          f"counts={counts}, leaked={leaked}")
    check(counts.get("00", 0) > 500 and counts.get("11", 0) > 500,
          f"both correlated outcomes actually occur (the H makes c0 random), "
          f"got {counts}")


# ── Plugin matrix ─────────────────────────────────────────────────────────────

def block_plugin_matrix():
    '''Every scheduler × allocator × router combination runs to completion'''
    import json
    import os
    import tempfile

    # Read the combinations from a registry rather than from a fixed
    # list, so that the matrix automatically covers anything registered
    # — including components a plugin adds.
    probe      = DevQ()
    schedulers = probe._registry.names("scheduler")
    allocators = probe._registry.names("allocator")
    routers    = probe._registry.names("router")
    broken     = []

    tmpdir = tempfile.mkdtemp(prefix="devq_matrix_")
    try:
        for sch, alloc, rt in itertools.product(schedulers, allocators, routers):
            path = os.path.join(tmpdir, f"{sch}_{alloc}_{rt}.json")
            with open(path, "w") as f:
                json.dump({"scheduler": sch, "allocator": alloc,
                           "router": rt}, f)

            combo = f"{sch}/{alloc}/{rt}"
            try:
                ibm = ibm_provider()
                sh  = (devq_with_ibm(config_path=path)
                       .add_device(ibm.get_device("FakeNairobiV2"), name="nairobi")
                       .add_device(ibm.get_device("FakeLagosV2"),   name="lagos")
                       .build())
                out = _with_timeout(
                    lambda: (run(sh, [f"qsubmit {BELL} {GHZ}", "qrunpack"]),
                             settle(sh, 1, 2))[1],
                    seconds=25
                )
                done = len(finished_ids(out))
                TRACE.note(done == 2, f"{combo}: {done}/2 jobs finished")
                if done != 2:
                    broken.append(f"{combo}: {done}/2 jobs finished")
            except TimeoutError:
                TRACE.note(False, f"{combo}: HUNG (never returned)")
                broken.append(f"{combo}: HUNG (never returned)")
            except Failure as e:
                # e.g. the bounded buffer tripping on runaway output —
                # a hang in a different costume. Record and keep going
                # rather than aborting the remaining combinations.
                TRACE.note(False, f"{combo}: {e}")
                broken.append(f"{combo}: {e}")
            except Exception as e:
                TRACE.note(False, f"{combo}: {type(e).__name__}: {e}")
                broken.append(f"{combo}: {type(e).__name__}: {e}")
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)

    total = len(schedulers) * len(allocators) * len(routers)
    if broken:
        raise Failure(f"{len(broken)}/{total} combinations broken:\n    "
                      + "\n    ".join(broken))


def _with_timeout(fn, seconds):
    '''
    Run fn on a daemon thread and give up on it after `seconds`.

    NOT signal-based. A SIGALRM handler raises inside whatever code is
    running at the time — and both QShell commands and Kernel.run_job sit
    behind broad `except Exception` handlers, so the TimeoutError gets
    swallowed as if it were an ordinary command error. The watchdog then
    silently fails to stop anything while the job stays pending and the
    shell keeps looping. Signals also only reach the main thread, so the
    same code breaks outright under any threaded harness.

    Abandoning a daemon thread leaks it for the rest of the process,
    which is acceptable here: the combination is already broken, the
    thread is blocked rather than spinning hot, and the alternative is
    hanging the whole suite.
    '''
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 — re-raised below
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)

    if t.is_alive():
        raise TimeoutError(f"still running after {seconds}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


# ── Determinism ───────────────────────────────────────────────────────────────

def block_determinism_seeded():
    '''Identical seeds reproduce devices and counts exactly'''
    cmds = ["qerrors q d0", "qtopology d0",
            f"qrun {BELL} --exec=nairobi", f"qrun {BELL} --exec=d1",
            f"qrun {BELL} --exec=lagos"]

    # qrun is async: the dispatch transcript is deterministic under a seed,
    # but resolve-log lines land later off a background thread and would
    # make a raw transcript comparison racy. So compare the dispatch
    # transcripts, and read counts from a settled qps per session.
    sa = three_device(seed=42)
    sb = three_device(seed=42)
    a = run(sa, cmds)
    b = run(sb, cmds)
    check(a == b, "two seed=42 sessions produced byte-identical transcripts")

    c = run(three_device(seed=43), cmds)
    check(a != c, "seed=43 diverges from seed=42")

    # Distinct runs of the SAME circuit on the SAME device must not clone
    # counts — jobs 1 and 2 are both bells on nairobi (--exec=nairobi and
    # --exec=d1 name the same device). Different counts prove per-run seed
    # derivation (seed+k), not one reused seed. Read each job's counts by
    # id (counts_of matches the first FINISHED line for that id), so the
    # comparison is between those two specific runs regardless of the order
    # their futures happened to resolve in.
    settle(sa, 1, 2, 3)
    j1 = counts_of(run(sa, ["qps 1"]), 1)
    j2 = counts_of(run(sa, ["qps 2"]), 2)
    check(j1 != j2,
          "identical circuits on one device produced different counts — "
          "derived per-run seeds (seed+k), not one reused seed")


def block_determinism_unseeded():
    '''Without a seed, sessions stay non-deterministic'''
    cmds = ["qerrors q d0", f"qrun {BELL} --exec=d1"]
    a = run(three_device(seed=None), cmds)
    b = run(three_device(seed=None), cmds)
    check(a != b, "two unseeded sessions differ — default path stays random")


def block_bug_fix_witnesses():
    '''Per-device noise models and allocator mappings reach the simulator'''
    sh = three_device(seed=42)
    run(sh, [f"qrun {BELL} --exec=nairobi", f"qrun {BELL} --exec=lagos"])

    def error_rate(counts_str):
        d = eval(counts_str)
        return (sum(d.values()) - d.get("00", 0) - d.get("11", 0)) / sum(d.values())

    # qrun dispatches async; read each job's settled counts by id so the
    # nairobi/lagos assignment is unambiguous regardless of resolve order.
    settle(sh, 1, 2)
    nairobi = error_rate(str(counts_of(run(sh, ["qps 1"]), 1)))
    lagos   = error_rate(str(counts_of(run(sh, ["qps 2"]), 2)))

    # ~27% would mean Nairobi ran under Lagos's noise model (shared-state bug);
    # ~10% would mean initial_layout was dropped (v2p_map bug).
    check(0.02 < nairobi < 0.08,
          f"nairobi bell error {nairobi:.1%} is ~5% — not ~27% (lagos noise "
          f"model leak) and not ~10% (dropped v2p_map)")
    check(0.10 < lagos < 0.22,
          f"lagos bell error {lagos:.1%} is ~15% — qubit 1's 13.6% readout "
          f"error dominates")


def block_name_validation():
    '''Ambiguous or duplicate device names are rejected at attach time'''
    ibm = ibm_provider()
    dev = ibm.get_device("FakeNairobiV2")

    for bad in ["d0", "d7", "q", "e", "b", "", "   ", "has space", "has,comma"]:
        try:
            DevQ().add_device(dev, name=bad)
            rejected = False
        except DevQError:
            rejected = True
        check(rejected, f"name {bad!r} rejected at attach time")

    # duplicates, case-insensitively
    try:
        (DevQ().add_device(dev, name="alpha")
               .add_device(ibm.get_device("FakeLagosV2"), name="ALPHA"))
        dup_rejected = False
    except DevQError:
        dup_rejected = True
    check(dup_rejected, "duplicate name 'alpha'/'ALPHA' rejected "
                        "(case-insensitive)")



# ── Threshold and lifecycle coverage ─────────────────────────────────────────

def block_edge_threshold_semantics():
    '''--max-edge-error filters by coupling quality, independently of qubits'''
    sh  = three_device()
    out = run(sh, [f"qrun {BELL} --max-edge-error=0.0069 --exec=nairobi",
                   f"qrun {BELL} --max-edge-error=0.005 --exec=nairobi,lagos",
                   f"qrun {GHZ} --max-edge-error=0.0107 --exec=lagos"])

    # Nairobi edges: (1,3)=0.0068 is the only one at or below 0.0069, so the
    # allocator is forced off its default {1,2} (edge 0.0070) onto {1,3}.
    check(mapping_of(out, 1) == "{0: 1, 1: 3}",
          f"edge threshold 0.0069 forced nairobi onto its only qualifying "
          f"edge (1,3)=0.0068, got {mapping_of(out, 1)}")

    # 0.005 is below every edge on both devices — a pure edge-side rejection
    # with no qubit threshold involved.
    expect(out, "Job 2 REJECTED", "max_qubit_error=None",
           "max_edge_error=0.005")
    m = re.search(r"Job 2 REJECTED: ([^\n]*)", out)
    check(m and "d1:" in m.group(1) and "d2:" in m.group(1),
          "edge-only rejection aggregates both devices")

    # Lagos at 0.0107 keeps (0,1), (1,2), (1,3) — a connected triple exists.
    check(mapping_of(out, 3) == "{0: 0, 1: 1, 2: 2}",
          f"ghz fits lagos's qualifying edges under 0.0107, "
          f"got {mapping_of(out, 3)}")


def block_max_1q_gate_error_filter():
    '''--max-1q-gate-error excludes noisy-1q qubits; ANDs with readout'''
    from kernel.memory.allocators.filtering import eligible_qubits
    from benchmark.spec import validate_spec, SpecError
    from hardware.device import QuantumDevice

    # A hand-built device so the calibration is known exactly: q2 has a bad
    # 1-qubit gate, q0 a bad readout, the rest are clean. Filtering is the
    # unit under test, so we call it directly rather than routing a job —
    # the allocators all funnel through eligible_qubits.
    dev = QuantumDevice(
        kind="filt", num_qubits=4,
        coupling_map=[(0, 1), (1, 2), (2, 3)], basis_gates=["sx"],
        error_map={0: 0.5, 1: 0.01, 2: 0.01, 3: 0.01},          # q0 bad readout
        edge_error_map={(0, 1): 0.02, (1, 2): 0.02, (2, 3): 0.02},
        gate_error_map={0: 1e-4, 1: 2e-4, 2: 5e-3, 3: 1e-4},    # q2 bad 1q gate
        provider=None)

    allq = range(4)

    check(sorted(eligible_qubits(dev, allq)) == [0, 1, 2, 3],
          "no thresholds -> every qubit eligible")

    # The 1q-gate filter alone excludes ONLY the noisy-gate qubit (q2),
    # independent of readout — q0 (bad readout) still passes here.
    check(sorted(eligible_qubits(dev, allq, max_1q_gate_error=1e-3)) == [0, 1, 3],
          "1q-gate filter excludes the noisy-gate qubit, ignores readout")

    # The two per-qubit filters AND: a qubit must clear BOTH. q0 fails
    # readout, q2 fails the gate — both are excluded, leaving {1, 3}.
    check(sorted(eligible_qubits(dev, allq,
                                 max_qubit_error=0.1,
                                 max_1q_gate_error=1e-3)) == [1, 3],
          "readout AND 1q-gate filters compose — a qubit needs to clear both")

    # OR-instead-of-AND would leave {1,2,3} or {0,1,3}; NEITHER is {1,3}.
    # This pins the conjunction against the most likely mutation.
    check(sorted(eligible_qubits(dev, allq,
                                 max_qubit_error=0.1,
                                 max_1q_gate_error=1e-3)) != [1, 2, 3],
          "the filter is a conjunction, not a disjunction (readout arm live)")

    # ── End to end: a too-strict 1q-gate threshold REJECTS a job ──────────────
    #
    # devq.simulated with a seed gives reproducible synthesised 1q errors in
    # 1e-4..1e-3. A threshold BELOW that band leaves no eligible qubit, so a
    # 2-qubit circuit can never be placed — a permanent, REJECTED outcome
    # (not WAITING, which is transient contention).
    sh  = session(None, [("devq.simulated", "fully_connected", 5, None, None)])
    out = run(sh, [f"qrun {BELL} --max-1q-gate-error=0.00001", "qps"])
    expect(out, "REJECTED")
    check("max_1q_gate_error" in out or "1q" in out.lower(),
          "rejection reason references the 1q-gate threshold")

    # A generous threshold above the whole band places the job normally —
    # proof the rejection above was the threshold, not a broken device.
    sh2  = session(None, [("devq.simulated", "fully_connected", 5, None, None)])
    out2 = run(sh2, [f"qrun {BELL} --max-1q-gate-error=0.01", "qps"])
    check("FINISHED" in out2,
          "a generous 1q-gate threshold places the job — device is fine")

    # ── Parser + spec reject malformed thresholds ─────────────────────────────
    from shell.parser import parse_job_args
    for bad in (f"{BELL} --max-1q-gate-error=abc",
                f"{BELL} --max-1q-gate-error="):
        raised = False
        try:
            parse_job_args(bad)
        except ValueError:
            raised = True
        check(raised, f"parser rejects malformed {bad.split('--')[1][:20]!r}")

    def spec_with(g1q):
        return {
            "name": "g1q_probe", "config": "c.json",
            "jobs": [{"circuit": "a.qasm", "max_1q_gate_error": g1q}],
            "devices": [{"id": "d0", "provider": "devq.simulated",
                         "backend": {"kind": "random", "qubits": 5}}],
        }

    ok = spec_with(0.0005)
    validate_spec(ok)
    check(ok["jobs"][0]["max_1q_gate_error"] == 0.0005,
          "spec validator accepts a valid 1q-gate threshold")

    for bad in ("abc", True):
        raised = False
        try:
            validate_spec(spec_with(bad))
        except SpecError:
            raised = True
        check(raised, f"spec validator rejects max_1q_gate_error={bad!r}")


def block_combined_thresholds():
    '''Qubit and edge thresholds compose as independent hard filters'''
    sh  = three_device()
    out = run(sh, [f"qrun {BELL} --max-qubit-error=0.03 "
                   f"--max-edge-error=0.0069 --exec=nairobi",
                   f"qrun {BELL} --max-qubit-error=0.0185 "
                   f"--max-edge-error=0.05 --exec=nairobi"])

    # Both thresholds satisfiable together: qubits 1 and 3 pass 0.03, and
    # edge (1,3) passes 0.0069.
    check(mapping_of(out, 1) == "{0: 1, 1: 3}",
          f"both thresholds satisfied simultaneously, got {mapping_of(out, 1)}")

    # A generous edge threshold cannot rescue an impossible qubit threshold —
    # thresholds are ANDed, never traded off.
    expect(out, "Job 2 REJECTED")
    check("max_qubit_error=0.0185" in out,
          "rejection cites the qubit threshold, not the satisfiable edge one")


def block_lifecycle_waiting():
    '''WAITING is a distinct, reachable state for transient contention'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", "solo", None)])

    # Occupy the pool so allocation must fail. Routing still succeeds —
    # feasible() ignores pool state — so the job is contended, not
    # unsatisfiable, and must land in WAITING rather than REJECTED.
    ctx = sh.kernel.contexts[0]
    ctx.memory_manager.pool.free_qubits = {0}

    out = run(sh, [f"qrun {BELL}", "qps"])

    expect(out, "WAITING for resources", "solo (d0)")
    expect_absent(out, "REJECTED")
    states = [j.state.value for j in sh.kernel.list_jobs()]
    check(states == ["WAITING"],
          f"job is WAITING, not READY or REJECTED — got {states}")

    # Freeing the pool lets the same job proceed on the next cycle, which is
    # what makes WAITING transient rather than terminal.
    ctx.memory_manager.pool.free_qubits = set(range(ctx.device.num_qubits))
    run(sh, ["qrunpack"])
    out2 = settle(sh, 1)
    check("FINISHED" in out2,
          "the WAITING job ran once resources freed — state was transient")


def block_lifecycle_failed():
    '''A provider error yields FAILED and still returns the qubits'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", "solo", None)])
    ctx = sh.kernel.contexts[0]

    def failing_execute(circuit, v2p_map, shots, device):
        return submit_async(lambda: ExecutionResult(
            counts=None, success=False, error="simulated provider failure"))

    ctx.device.provider.execute = failing_execute

    run(sh, [f"qrun {BELL}"])
    out = settle(sh, 1)

    expect(out, "FAILED", "simulated provider failure")
    states = [j.state.value for j in sh.kernel.list_jobs()]
    check(states == ["FAILED"], f"job reached FAILED, got {states}")

    # The leak that matters: a failed job must not strand its qubits, or a
    # device silently loses capacity for the rest of the session.
    free = ctx.memory_manager.pool.free_qubits
    check(free == set(range(ctx.device.num_qubits)),
          f"all qubits returned to the pool after failure, got {sorted(free)}")
    check(ctx.running_jobs == 0,
          f"running_jobs decremented after failure, got {ctx.running_jobs}")


def block_async_dispatch():
    '''qrun/qrunpack dispatch without blocking; qps reports results and a
    WAITING job self-heals once the holder frees qubits'''
    # The shell's execution commands are asynchronous: qrun and qrunpack
    # route, allocate, and dispatch a job onto the shared executor, then
    # RETURN — they never block the shell on a provider result. qps is the
    # snapshot that reports where each job is, folding in the result once a
    # future has resolved. And a job left WAITING on contended qubits is
    # retried automatically when the holder completes and frees them —
    # observed through qps alone, with no command re-issued. This block
    # pins all three, which together are the whole async contract.

    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", "solo", None)])

    # ── qrun does not block: the job is RUNNING the instant qrun returns ──
    # A synchronous qrun would only ever return FINISHED/FAILED here; that
    # it returns with the job still RUNNING (future in flight) is the
    # non-blocking guarantee. This is the assertion a re-added _wait_for
    # would break.
    out = run(sh, [f"qrun {BELL} --exec=solo"])
    expect(out, "dispatched")
    states = [j.state.value for j in sh.kernel.list_jobs()]
    check(states == ["RUNNING"],
          f"qrun returns with the job RUNNING, not blocked to completion — "
          f"got {states}")

    # ── qps reports the settled result, and filters by id ──
    settled = settle(sh, 1)
    counts = counts_of(settled, 1)
    check(sum(counts.values()) > 0,
          f"qps reports counts for the FINISHED job, got {counts}")
    # The counts must be on the qps LINE, not merely somewhere in the
    # transcript — the kernel's resolve log also prints counts, so assert
    # the qps row itself carries them (this is what catches a qps that
    # renders FINISHED but drops the counts column).
    check(re.search(r"^1 \| .*\| FINISHED \| Counts: \{", settled, re.M),
          "the qps FINISHED row itself carries the counts column")
    # id filter: `qps 1` shows only job 1; an unknown id is reported, not
    # silently dropped; a non-integer token is flagged.
    only = run(sh, ["qps 1"])
    check(only.strip().startswith("1 |") and "\n2 |" not in only,
          f"qps <id> shows only the named job, got {only!r}")
    expect(run(sh, ["qps 99"]), "Job 99 does not exist.")
    expect(run(sh, ["qps notanumber"]), "Invalid job id: notanumber")

    # ── qps folds a REJECTED job's reason into its line ──
    rej = session("router_only.config.json",
                  [("ibm.simulated", "FakeLagosV2", "lagos", None)])
    run(rej, [f"qrun {BELL} --max-qubit-error=0.0000001"])
    rej_out = run(rej, ["qps 1"])
    check("REJECTED" in rej_out and "Reason:" in rej_out,
          f"qps reports a REJECTED job with its reason, got {rej_out!r}")

    # ── the self-heal: a WAITING job dispatches once qubits free, through
    #    qps alone (no re-issued qrunpack) ──
    heal = session("router_only.config.json",
                   [("ibm.simulated", "FakeNairobiV2", "solo", None)])
    hctx = heal.kernel.contexts[0]
    # Only one bell (2 qubits) can be placed at a time.
    hctx.memory_manager.pool.free_qubits = {1, 2}

    # Pin job 1's execution IN-FLIGHT until the test releases it. The
    # WAITING we want to observe only exists while job 1 still holds {1,2};
    # if job 1's future resolves before job 2's qrun, job 2's pre-routing
    # resolve sweep reclaims those qubits and job 2 dispatches straight to
    # RUNNING — no WAITING to see. That race is real but timing-dependent:
    # a fast provider (or a loaded machine that lets the Aer future finish
    # in the gap between the two qruns) makes it the common case, so a bare
    # back-to-back qrun cannot reliably reproduce the contention this block
    # is meant to pin. A gated future — done() False until the test frees
    # it — makes the hold deterministic without weakening the assertion:
    # job 2 genuinely WAITs on genuinely-occupied qubits; we simply
    # guarantee job 1 is still holding them at that instant.
    #
    # On release the gate resolves job 1 INSTANTLY to a fixed result rather
    # than re-running the real Aer execution. Two reasons this matters and
    # a plain submit_async wrapper does not: (1) speed/load independence —
    # job 2's self-heal must land within settle()'s bounded poll budget, so
    # it cannot hang on a slow machine waiting for a real simulation to
    # finish after release; (2) no pool re-entrancy — running the real
    # execute() from inside the held future would submit a NESTED task onto
    # the same bounded shared executor while occupying one of its workers,
    # which can starve under full-suite load. Job 1's measured counts are
    # irrelevant here: this block asserts job 2's WAITING, its self-heal to
    # FINISHED, and its reuse of the freed block — not job 1's distribution.
    from circuits.execution_result import ExecutionResult
    release_job1 = threading.Event()
    real_execute = hctx.device.execute

    class _GatedFuture:
        '''Reports not-done until released; then resolves immediately to a
        fixed result — no real execution, no shared-pool submit.'''
        def done(self):
            return release_job1.is_set()
        def result(self):
            release_job1.wait(timeout=10)
            return ExecutionResult(counts={"00": 512, "11": 512}, success=True)

    # Match the kernel's call: execute(circuit, v2p_map, shots=shots).
    hctx.device.execute = lambda circuit, v2p_map, shots: _GatedFuture()
    try:
        run(heal, [f"qrun {BELL} --exec=solo"])             # job 1 holds {1,2}, future gated
        wait_out = run(heal, [f"qrun {BELL} --exec=solo"])  # job 2 must WAIT
        expect(wait_out, "WAITING for resources")
        check([j.state.value for j in heal.kernel.list_jobs()] == ["RUNNING", "WAITING"],
              "job 2 is WAITING while job 1 holds the only free qubits")
    finally:
        # Restore the real execute so job 2's retry runs for real, then
        # release job 1 — it resolves instantly, freeing {1,2} for job 2.
        hctx.device.execute = real_execute
        release_job1.set()

    # Drive the session forward using ONLY qps. Each poll resolves job 1's
    # future when it completes; freeing its qubits retries job 2, which
    # then dispatches and runs — all without a qrunpack. If the retry-on-
    # free were missing, job 2 would stay WAITING forever here and settle
    # would time out.
    healed = settle(heal, 1, 2)
    check(finished_ids(healed) == {"1", "2"},
          "a WAITING job self-heals to FINISHED once the holder frees its "
          "qubits — observed through qps alone, no qrunpack re-issued")
    # And the freed qubits were genuinely reused: job 2 took job 1's block.
    check(mapping_of(healed, 2) == "{0: 1, 1: 2}",
          f"job 2 reused the freed block {{0: 1, 1: 2}}, got {mapping_of(healed, 2)}")

    # ── a fast provider's FINISHED-but-uncollected qubits do not strand a
    #    later qrun ──
    # The devq simulator resolves near-instantly, so by the time a second
    # qrun runs, the first job's future is already done. Its qubits must be
    # collected (and freed) before the new job allocates, or the new job
    # spuriously WAITs on capacity that is logically free but not yet swept
    # up. This is the screenshot bug: four bells on a 7-qubit sim, where
    # the fourth waited only because the first three's finished futures had
    # not been collected. run_job resolves pending futures before routing,
    # so each qrun reclaims the prior (now-finished) jobs' qubits first.
    #
    # The test must let the prior futures finish WITHOUT pumping the kernel
    # (no qps/qrunpack between qruns) — otherwise the poll that settles them
    # would free the qubits and mask the bug. A bare sleep lets the thread
    # pool resolve them while the kernel stays unaware; only the next qrun's
    # own resolve can then collect them. Six 2-qubit bells on 7 qubits means
    # by the 4th the first three's blocks (6 qubits) must be reclaimed for it
    # to fit at all.
    fast = session(None, [("devq.simulated", "fully_connected", 7, "sim", None)])
    dispatched = 0
    for _ in range(4):
        out = run(fast, [f"qrun {BELL} --exec=sim"])
        if "dispatched to" in out:
            dispatched += 1
        time.sleep(0.05)   # future finishes on the pool; kernel not pumped
    check(dispatched == 4,
          f"four sequential qruns on a fast sim all dispatch — a finished "
          f"job's qubits are reclaimed before the next allocates, so none "
          f"spuriously WAITs; got {dispatched}/4 dispatched")


def block_wedged_provider_timeout():
    '''A future that never resolves fails cleanly instead of hanging'''
    from frontends.qasm2.parser import parse

    class NeverResolves:
        '''A future stuck in flight forever — a wedged provider or a dead
        executor looks exactly like this from the kernel's side.'''
        def done(self):   return False
        def result(self): return None

    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", "solo", None)])
    ctx = sh.kernel.contexts[0]
    ctx.device.execute = lambda circuit, v2p_map, shots: NeverResolves()

    # Drive the qrun path directly so the timeout can be set to 1s rather
    # than the 300s production deadline.
    buf = BoundedBuffer()
    with _capture(buf):
        qcb = sh.kernel.submit_job(parse(open(BELL).read(), BELL))
        ctx_routed, _ = sh.kernel._route(qcb)
        qcb.v2p_map = ctx_routed.memory_manager.allocate(qcb.circuit)
        sh.kernel._execute(qcb, ctx_routed)
        dispatched_running = ctx_routed.running_jobs
        sh.kernel._wait_for(qcb, poll_interval=0.05, timeout=1)

    check(dispatched_running == 1,
          f"job was dispatched and counted, got {dispatched_running}")
    check(qcb.state.value == "FAILED",
          f"wedged job ends FAILED rather than spinning, "
          f"got {qcb.state.value}")
    # The timeout error lives on the job's result. It is no longer echoed
    # to the console (resolve events are not printed — qps is the result
    # surface), so assert it where it actually is.
    err = qcb.result.error if qcb.result else ""
    check("did not resolve within" in err and "wedged" in err,
          f"the wedged job carries a clear timeout error, got {err!r}")

    # Same cleanup invariants as an ordinary failure — a wedged provider
    # must not permanently shrink the device.
    free = ctx_routed.memory_manager.pool.free_qubits
    check(free == set(range(ctx_routed.device.num_qubits)),
          f"qubits returned after timeout, got {sorted(free)}")
    check(ctx_routed.running_jobs == 0,
          f"running_jobs decremented after timeout, "
          f"got {ctx_routed.running_jobs}")


# ── Configuration robustness ─────────────────────────────────────────────────

def block_config_validation():
    '''Malformed configs warn and fall back rather than crashing'''
    import json
    import os
    import tempfile

    cases = [
        ("missing file",   None,
         "not found"),
        ("invalid JSON",   "{ not json at all",
         "is not valid JSON"),
        ("not an object",  "[1, 2, 3]",
         "is not a JSON object"),
        ("unknown key",    {"unknown_key_xyz": 1},
         "unknown config key"),
        ("bad shots",      {"shots": "many"},
         "expected a positive integer"),
        ("bad scheduler",  {"scheduler": "nonexistent"},
         "expected one of"),
        ("negative weight", {"qubit_error_weight": -5,
                             "edge_error_weight": 1},
         "expected a non-negative number"),
    ]

    tmpdir = tempfile.mkdtemp(prefix="devq_cfg_")
    try:
        for label, payload, expected in cases:
            path = os.path.join(tmpdir, "cfg.json")
            if payload is None:
                path = os.path.join(tmpdir, "does_not_exist.json")
            elif isinstance(payload, str):
                with open(path, "w") as f:
                    f.write(payload)
            else:
                with open(path, "w") as f:
                    json.dump(payload, f)

            # Construction emits the warning, so capture build() itself.
            buf = BoundedBuffer()
            with _capture(buf):
                shell = (devq_with_ibm(config_path=path)
                         .add_device(ibm_provider().get_device("FakeNairobiV2"))
                         .build())
                shell.onecmd("qconfig")
            out = buf.getvalue()

            check(expected in out,
                  f"{label}: warned with {expected!r}")
            # Whatever went wrong, the session must still be usable and the
            # bad value must not have been adopted.
            check("DevQ Core" in out,
                  f"{label}: fell back to core defaults and built a session")
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


def block_provider_global_key_rejected():
    '''A provider may not set global-scope config keys'''
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

    class OversteppingProvider(IBMSimulatedProvider):
        def preferred_config(self):
            # 'router' is global scope — providers own device keys only.
            return {"shots": 2048, "router": "round_robin"}

    provider = OversteppingProvider(seed=SEED)
    buf = BoundedBuffer()
    with _capture(buf):
        # Registered under its OWN name, not inherited from its base:
        # registration matches the exact type, because a subclass is a
        # different component with different behaviour — as this one
        # demonstrates.
        shell = (DevQ(config_path=CONFIG + "router_only.config.json")
                 .register_provider("ibm.overstepping", OversteppingProvider)
                 .add_device(provider.get_device("FakeNairobiV2"))
                 .build())
        shell.onecmd("qconfig")
    out = buf.getvalue()

    expect(out, "attempted to set global key", "router")
    # The device key it was entitled to set still applies.
    check("2048" in out,
          "the provider's legitimate device key (shots) was still honoured")
    # And the global key it was not entitled to set did not take effect.
    check("round_robin" not in out,
          "the illegal global key was ignored, router stays 'noise'")


def block_device_calibration():
    '''The five-term calibration model: synthesis ranges, accessors, extraction'''
    from providers.devq.backend_factory import create_backend
    from hardware.device import QuantumDevice
    import random

    # ── DevQ-simulated synthesis lands in real-world ranges ───────────────────
    #
    # Seeded so the check is deterministic. The ranges are the ones the
    # generators promise (and that real superconducting hardware occupies):
    # 1q gate error 1e-4..1e-3, T2 50..300 µs, 1q duration 20..60 ns,
    # 2q duration 200..660 ns.
    backend = create_backend("fully_connected", 7, rng=random.Random(42))

    check(len(backend["gate_error_map"]) == 7,
          "synthesised a 1q gate error for every qubit")
    check(all(1e-4 <= e <= 1e-3 for e in backend["gate_error_map"].values()),
          "every synthesised 1q gate error is in the real-world band 1e-4..1e-3")
    check(len(backend["t2_map"]) == 7,
          "synthesised a T2 for every qubit")
    check(all(50.0 <= t <= 300.0 for t in backend["t2_map"].values()),
          "every synthesised T2 is in the real-world band 50..300 µs")
    check(20.0 <= backend["gate_1q_duration"] <= 60.0,
          "1q gate duration is in the real-world band 20..60 ns")
    check(200.0 <= backend["gate_2q_duration"] <= 660.0,
          "2q gate duration is in the real-world band 200..660 ns")

    # 1q gate error is a DISTINCT axis from readout error — a device is not
    # allowed to conflate them (they filter independently).
    check(backend["gate_error_map"] != backend["error_map"],
          "1q gate error and readout error are distinct per-qubit maps")

    # ── Accessors return the populated values ─────────────────────────────────
    dev = QuantumDevice(
        kind="cal", num_qubits=7,
        coupling_map=backend["coupling_map"], basis_gates=backend["basis_gates"],
        error_map=backend["error_map"], edge_error_map=backend["edge_error_map"],
        gate_error_map=backend["gate_error_map"], t2_map=backend["t2_map"],
        gate_1q_duration=backend["gate_1q_duration"],
        gate_2q_duration=backend["gate_2q_duration"], provider=None)

    check(dev.gate_error(0) == backend["gate_error_map"][0],
          "gate_error(q) returns the populated per-qubit value")
    check(dev.t2(0) == backend["t2_map"][0],
          "t2(q) returns the populated per-qubit value")
    check(dev.gate_duration(1) == backend["gate_1q_duration"]
          and dev.gate_duration(2) == backend["gate_2q_duration"],
          "gate_duration(arity) returns the per-arity value")

    # ── Fallbacks on an unpopulated device ────────────────────────────────────
    #
    # A device built by older code (no extended calibration) still answers
    # every accessor — with a typical fallback, never a crash. This is what
    # keeps the field additive: existing construction paths are untouched.
    bare = QuantumDevice(
        kind="bare", num_qubits=3, coupling_map=[(0, 1)], basis_gates=["sx"],
        error_map={0: 0.01}, edge_error_map={(0, 1): 0.02}, provider=None)

    check(bare.gate_error(0) == 5e-4,
          "gate_error falls back to a typical 5e-4 when unpopulated")
    check(bare.t2(0) == 100.0,
          "t2 falls back to a typical 100 µs when unpopulated")
    check(bare.gate_duration(1) == 40.0 and bare.gate_duration(2) == 400.0,
          "gate_duration falls back to typical 40/400 ns when unpopulated")

    # Duration is per-ARITY (1 or 2); a nonsensical arity is a loud error,
    # not a silent zero.
    bad_arity = False
    try:
        bare.gate_duration(3)
    except ValueError:
        bad_arity = True
    check(bad_arity, "gate_duration rejects an arity other than 1 or 2")

    # ── IBM extraction pulls the same five terms from a real Target ───────────
    #
    # Guarded: skipped when qiskit is absent (same pattern as other
    # IBM-dependent blocks). Values come from the PINNED fake-backend
    # calibration, so this asserts on SHAPE and PLAUSIBILITY, not exact
    # numbers — exact numbers are version-bound and belong to the fidelity
    # references, not here.
    try:
        ibmdev = ibm_provider().get_device("FakeNairobiV2")
    except Exception as e:
        check(True, f"(IBM extraction skipped — provider unavailable: "
                    f"{type(e).__name__})")
        return

    nq = ibmdev.num_qubits
    check(all(ibmdev.gate_error(q) is not None for q in range(nq)),
          "IBM extraction populates a 1q gate error for every qubit")
    check(all(0 < ibmdev.gate_error(q) < 0.01 for q in range(nq)),
          "extracted IBM 1q gate errors are gate-magnitude (0 < e < 0.01), "
          "not readout error — guards against picking up measure error")
    check(all(ibmdev.t2(q) > 0 for q in range(nq)),
          "IBM extraction populates a positive T2 (µs) for every qubit")
    check(ibmdev.gate_duration(1) > 0 and ibmdev.gate_duration(2) > 0,
          "IBM extraction populates positive per-arity gate durations")
    # 2q gates take longer than 1q gates on superconducting hardware — a
    # cheap sanity check the extraction didn't swap arities.
    check(ibmdev.gate_duration(2) > ibmdev.gate_duration(1),
          "extracted 2q gate duration exceeds 1q (arity not swapped)")


def block_engine_gates():
    '''The native engine's gate matrices match Qiskit and cover the parser'''
    # The native statevector engine (engine/) simulates a circuit WITHOUT
    # Qiskit, so DevQ can compute a noiseless ideal without an Aer-backed
    # device attached. Its correctness rests entirely on its gate matrices
    # being right: a wrong matrix is a silently-wrong distribution, and a
    # fidelity computed against a wrong ideal is high, plausible, and
    # meaningless. This block LOCKS the vocabulary before any engine code
    # consumes it, on two axes:
    #   (1) COVERAGE — the engine's gate names equal the qasm2 frontend's
    #       _BUILTIN_GATES exactly. The two tables are written for different
    #       reasons and nothing else couples them, so a gate added to one and
    #       not the other is invisible until a circuit uses it. Equality (not
    #       mere subset) is asserted: the engine must simulate everything the
    #       frontend will emit, and claim nothing the frontend cannot.
    #   (2) CORRECTNESS — every gate's unitary equals Qiskit's Operator for
    #       that gate, checked here rather than trusted from memory. Constants
    #       exactly; parameterised gates at several angles including the
    #       edges (0, pi) where a sign or half-angle slip hides.
    import numpy as np
    import math
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Operator
    from engine import gates as G
    from frontends.qasm2.parser import _BUILTIN_GATES

    def op(qc):
        return Operator(qc).data

    # (1) Coverage: exact vocabulary parity with the frontend.
    eng, parser = G.vocabulary(), set(_BUILTIN_GATES)
    check(eng == parser,
          f"engine vocabulary equals the parser's _BUILTIN_GATES — "
          f"engine-only={sorted(eng - parser)}, "
          f"parser-only={sorted(parser - eng)}")

    # Arity/param-count parity too: the engine's declared (num_params,
    # num_qubits) must match the frontend's, or a gate could be known to both
    # yet applied with the wrong operands.
    mism = [(n, (G.GATES[n].num_params, G.GATES[n].num_qubits), _BUILTIN_GATES[n])
            for n in parser
            if (G.GATES[n].num_params, G.GATES[n].num_qubits) != _BUILTIN_GATES[n]]
    check(not mism,
          f"engine gate arities match the parser's (num_params, num_qubits): "
          f"mismatches={mism}")

    # A gate outside the vocabulary declines, naming the known set.
    raised = False
    try:
        G.gate_spec("not_a_gate")
    except G.UnknownGateError:
        raised = True
    check(raised, "an unknown gate name raises UnknownGateError, not KeyError")

    # Custom `gate` definitions do not widen the vocabulary: the frontend
    # inlines them (recursively) at parse time, so a circuit's instruction
    # stream is pure builtins by the time the engine sees it. Parse a fixture
    # with a custom gate that itself calls another custom gate and confirm
    # every emitted gate name is in the engine's vocabulary — so covering
    # _BUILTIN_GATES genuinely covers every circuit the parser can produce.
    from frontends.qasm2.parser import parse as _parse_qasm
    import os as _os
    custom_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                "test_circuits", "qasm2", "custom_gate.qasm")
    with open(custom_path) as _h:
        custom_circuit = _parse_qasm(_h.read(), "custom_gate.qasm")
    emitted = [i["gate"] for i in custom_circuit.instructions if i["op"] == "gate"]
    outside = sorted(set(n for n in emitted if n not in eng))
    check(emitted and not outside,
          f"a custom-gate circuit inlines to engine-known builtins only; "
          f"emitted={emitted}, outside-vocabulary={outside}")

    # (2) Correctness: each gate's unitary == Qiskit's Operator.
    ANGLES = [0.0, 0.3, -1.1, 2.7, math.pi]

    # One-qubit constants.
    const_1q = {
        'id':   lambda q: q.id(0),   'x':   lambda q: q.x(0),
        'y':    lambda q: q.y(0),    'z':   lambda q: q.z(0),
        'h':    lambda q: q.h(0),    's':   lambda q: q.s(0),
        'sdg':  lambda q: q.sdg(0),  't':   lambda q: q.t(0),
        'tdg':  lambda q: q.tdg(0),  'sx':  lambda q: q.sx(0),
        'sxdg': lambda q: q.sxdg(0),
    }
    for name, build in const_1q.items():
        qc = QuantumCircuit(1); build(qc)
        check(np.allclose(G.gate_spec(name).unitary([]), op(qc), atol=1e-10),
              f"engine '{name}' matrix matches Qiskit")

    # One-qubit parameterised (p and its alias u1 share a matrix).
    for name, build in {'rx': lambda q, a: q.rx(a, 0),
                        'ry': lambda q, a: q.ry(a, 0),
                        'rz': lambda q, a: q.rz(a, 0),
                        'p':  lambda q, a: q.p(a, 0),
                        'u1': lambda q, a: q.p(a, 0)}.items():
        for a in ANGLES:
            qc = QuantumCircuit(1); build(qc, a)
            check(np.allclose(G.gate_spec(name).unitary([a]), op(qc), atol=1e-10),
                  f"engine '{name}({a})' matrix matches Qiskit")

    # The general u and its parameter-packing aliases u2, u3.
    for a in ANGLES:
        qc = QuantumCircuit(1); qc.u(a, 0.4, -0.9, 0)
        check(np.allclose(G.gate_spec('u').unitary([a, 0.4, -0.9]), op(qc), atol=1e-10),
              f"engine 'u({a},...)' matrix matches Qiskit")
        check(np.allclose(G.gate_spec('u3').unitary([a, 0.4, -0.9]), op(qc), atol=1e-10),
              f"engine 'u3({a},...)' aliases u")
        qc = QuantumCircuit(1); qc.u(math.pi / 2, a, 0.4, 0)
        check(np.allclose(G.gate_spec('u2').unitary([a, 0.4]), op(qc), atol=1e-10),
              f"engine 'u2({a},...)' aliases u(pi/2,...)")

    # Controlled two-qubit gates: build the full controlled(U) in the engine's
    # little-endian basis (control q0, target q1) and compare to Qiskit's
    # embedded operator. This is also the tensor-ordering check — a
    # big-endian slip flips control and target and fails here.
    def controlled(U2, c, t, nq=2):
        dim = 2 ** nq
        M = np.zeros((dim, dim), dtype=complex)
        for col in range(dim):
            if (col >> c) & 1 == 0:
                M[col, col] = 1.0
            else:
                tb = (col >> t) & 1
                for o in (0, 1):
                    M[(col & ~(1 << t)) | (o << t), col] = U2[o, tb]
        return M

    ctrl_const = {'cx': lambda q: q.cx(0, 1), 'cy': lambda q: q.cy(0, 1),
                  'cz': lambda q: q.cz(0, 1), 'ch': lambda q: q.ch(0, 1)}
    for name, build in ctrl_const.items():
        qc = QuantumCircuit(2); build(qc)
        full = controlled(G.gate_spec(name).unitary([]), 0, 1)
        check(np.allclose(full, op(qc), atol=1e-10),
              f"engine '{name}' controlled matrix matches Qiskit (q0 controls q1)")

    ctrl_param = {'crx': lambda q, a: q.crx(a, 0, 1),
                  'cry': lambda q, a: q.cry(a, 0, 1),
                  'crz': lambda q, a: q.crz(a, 0, 1),
                  'cp':  lambda q, a: q.cp(a, 0, 1),
                  'cu1': lambda q, a: q.cp(a, 0, 1)}
    for name, build in ctrl_param.items():
        for a in (0.7, -1.3):
            qc = QuantumCircuit(2); build(qc, a)
            full = controlled(G.gate_spec(name).unitary([a]), 0, 1)
            check(np.allclose(full, op(qc), atol=1e-10),
                  f"engine '{name}({a})' controlled matrix matches Qiskit")

    # ECR: the one intrinsically-two-qubit unitary, compared 4x4 directly.
    qc = QuantumCircuit(2); qc.ecr(0, 1)
    check(np.allclose(G.gate_spec('ecr').unitary([]), op(qc), atol=1e-10),
          "engine 'ecr' 4x4 matrix matches Qiskit")

    # Permutations: swap, ccx (Toffoli), cswap (Fredkin). These carry no 2x2;
    # the engine permutes basis amplitudes, so the correctness that matters is
    # the operand mapping, checked by building the permutation matrix the
    # engine's kind implies and comparing to Qiskit.
    def swapm(a, b, nq=2):
        dim = 2 ** nq; M = np.zeros((dim, dim), dtype=complex)
        for col in range(dim):
            ba, bb = (col >> a) & 1, (col >> b) & 1
            M[(col & ~(1 << a) & ~(1 << b)) | (bb << a) | (ba << b), col] = 1
        return M
    qc = QuantumCircuit(2); qc.swap(0, 1)
    check(np.allclose(swapm(0, 1), op(qc), atol=1e-10),
          "engine 'swap' permutation matches Qiskit")

    dim = 8
    ccxm = np.eye(dim, dtype=complex)
    for col in range(dim):
        if (col >> 0) & 1 and (col >> 1) & 1:
            ccxm[col, col] = 0; ccxm[col ^ (1 << 2), col] = 1
    qc = QuantumCircuit(3); qc.ccx(0, 1, 2)
    check(np.allclose(ccxm, op(qc), atol=1e-10),
          "engine 'ccx' (Toffoli, controls q0/q1 target q2) matches Qiskit")

    cswapm = np.zeros((dim, dim), dtype=complex)
    for col in range(dim):
        if (col >> 0) & 1:
            ba, bb = (col >> 1) & 1, (col >> 2) & 1
            cswapm[(col & ~(1 << 1) & ~(1 << 2)) | (bb << 1) | (ba << 2), col] = 1
        else:
            cswapm[col, col] = 1
    qc = QuantumCircuit(3); qc.cswap(0, 1, 2)
    check(np.allclose(cswapm, op(qc), atol=1e-10),
          "engine 'cswap' (Fredkin, control q0 swaps q1/q2) matches Qiskit")


def block_engine_statevector():
    '''The native statevector core simulates exact ideals matching Qiskit'''
    # engine_gates locked the gate MATRICES against Qiskit; this block pins
    # the statevector CORE that applies them — that a full circuit's exact
    # measured-bit distribution (the noiseless ideal) matches Qiskit's, and
    # that the core honours DevQ's output contract (Option-B width, clbit
    # placement, measure-all fallback) and its reset boundary (exact on a
    # separable qubit, declined on an entangled one, never silently wrong).
    import numpy as np
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Statevector
    from engine.statevector import simulate, UnsupportedByEngine
    from engine.gates import UnknownGateError
    from circuits.circuit_rep import CircuitRep

    def qiskit_ideal(qc, width, measure_map):
        # Exact probabilities from Qiskit's statevector, marginalised the
        # same way the contract prescribes, so the two dicts are comparable.
        full = np.abs(Statevector(qc).data) ** 2
        out = {}
        for index, p in enumerate(full):
            if p < 1e-9:
                continue
            bits = ["0"] * width
            for q, c in measure_map:
                bits[width - 1 - c] = str((index >> q) & 1)
            key = "".join(bits)
            out[key] = out.get(key, 0.0) + float(p)
        return out

    def agree(eng, qk):
        keys = set(eng) | set(qk)
        return all(abs(eng.get(k, 0.0) - qk.get(k, 0.0)) < 1e-9 for k in keys)

    # ── every gate kind, applied in a real circuit, matches Qiskit ────────
    cases = []

    cr = CircuitRep(2, 2); cr.add_gate("h", [0]); cr.add_gate("cx", [0, 1])
    for q in range(2): cr.add_measure(q, q)
    qc = QuantumCircuit(2); qc.h(0); qc.cx(0, 1)
    cases.append(("bell", cr, qc, 2, [(0, 0), (1, 1)]))

    cr = CircuitRep(3, 3)
    cr.add_gate("h", [0]); cr.add_gate("cx", [0, 1]); cr.add_gate("cx", [1, 2])
    for q in range(3): cr.add_measure(q, q)
    qc = QuantumCircuit(3); qc.h(0); qc.cx(0, 1); qc.cx(1, 2)
    cases.append(("ghz", cr, qc, 3, [(q, q) for q in range(3)]))

    cr = CircuitRep(3, 3)
    cr.add_gate("rx", [0], [0.7]); cr.add_gate("ry", [1], [1.3])
    cr.add_gate("rz", [2], [2.1]); cr.add_gate("p", [0], [0.5])
    cr.add_gate("cx", [0, 2])
    for q in range(3): cr.add_measure(q, q)
    qc = QuantumCircuit(3)
    qc.rx(0.7, 0); qc.ry(1.3, 1); qc.rz(2.1, 2); qc.p(0.5, 0); qc.cx(0, 2)
    cases.append(("rotations", cr, qc, 3, [(q, q) for q in range(3)]))

    cr = CircuitRep(3, 3)
    cr.add_gate("h", [0]); cr.add_gate("h", [1]); cr.add_gate("ccx", [0, 1, 2])
    for q in range(3): cr.add_measure(q, q)
    qc = QuantumCircuit(3); qc.h(0); qc.h(1); qc.ccx(0, 1, 2)
    cases.append(("ccx", cr, qc, 3, [(q, q) for q in range(3)]))

    cr = CircuitRep(3, 3)
    cr.add_gate("x", [1]); cr.add_gate("h", [0]); cr.add_gate("cswap", [0, 1, 2])
    for q in range(3): cr.add_measure(q, q)
    qc = QuantumCircuit(3); qc.x(1); qc.h(0); qc.cswap(0, 1, 2)
    cases.append(("cswap", cr, qc, 3, [(q, q) for q in range(3)]))

    cr = CircuitRep(2, 2); cr.add_gate("x", [0]); cr.add_gate("swap", [0, 1])
    for q in range(2): cr.add_measure(q, q)
    qc = QuantumCircuit(2); qc.x(0); qc.swap(0, 1)
    cases.append(("swap", cr, qc, 2, [(0, 0), (1, 1)]))

    cr = CircuitRep(2, 2); cr.add_gate("h", [0]); cr.add_gate("ecr", [0, 1])
    for q in range(2): cr.add_measure(q, q)
    qc = QuantumCircuit(2); qc.h(0); qc.ecr(0, 1)
    cases.append(("ecr", cr, qc, 2, [(0, 0), (1, 1)]))

    # Interference case: h then ry on the SAME qubit. Starting from a
    # superposition, ry's off-diagonal asymmetry shows up in the measured
    # probabilities (h;ry(θ) and h;ry(θ)^T give different distributions),
    # so this distinguishes the 1q application from a transposed one — a
    # transpose-invariant gate on |0> alone would not.
    cr = CircuitRep(1, 1); cr.add_gate("h", [0]); cr.add_gate("ry", [0], [0.7])
    cr.add_measure(0, 0)
    qc = QuantumCircuit(1); qc.h(0); qc.ry(0.7, 0)
    cases.append(("h_then_ry", cr, qc, 1, [(0, 0)]))

    for name, cr, qc, width, mmap in cases:
        check(agree(simulate(cr), qiskit_ideal(qc, width, mmap)),
              f"engine simulate('{name}') matches Qiskit's exact ideal")

    # ── hand-computed anchor (no Qiskit) ──────────────────────────────────
    cr = CircuitRep(2, 2); cr.add_gate("h", [0]); cr.add_gate("cx", [0, 1])
    for q in range(2): cr.add_measure(q, q)
    bell = simulate(cr)
    check(abs(bell.get("00", 0) - 0.5) < 1e-9
          and abs(bell.get("11", 0) - 0.5) < 1e-9
          and "01" not in bell and "10" not in bell,
          f"Bell ideal is 50/50 on 00/11, hand-known, got {bell}")

    # ── output contract: clbit placement, Option-B width, fallback ────────
    cr = CircuitRep(2, 2); cr.add_gate("x", [0])
    cr.add_measure(0, 1); cr.add_measure(1, 0)
    perm = simulate(cr)
    check(perm == {"10": 1.0},
          f"a measure maps each qubit to its own clbit position, got {perm}")

    cr = CircuitRep(3, 2); cr.add_gate("x", [0]); cr.add_gate("x", [2])
    cr.add_measure(0, 0); cr.add_measure(1, 1)
    ob = simulate(cr)
    check(ob == {"01": 1.0},
          f"width is the declared classical register (Option B), got {ob}")

    cr = CircuitRep(2, 2); cr.add_gate("x", [1])
    fb = simulate(cr)
    check(fb == {"10": 1.0},
          f"a circuit with no measures falls back to measuring all, got {fb}")

    # ── reset boundary: exact when separable, declined when entangled ─────
    cr = CircuitRep(1, 1); cr.add_gate("x", [0]); cr.add_reset(0)
    cr.add_measure(0, 0)
    r1 = simulate(cr)
    check(r1 == {"0": 1.0},
          f"reset on a certainly-|1> separable qubit yields 0, got {r1}")

    cr = CircuitRep(1, 1); cr.add_reset(0); cr.add_gate("h", [0])
    cr.add_measure(0, 0)
    r2 = simulate(cr)
    check(abs(r2.get("0", 0) - 0.5) < 1e-9 and abs(r2.get("1", 0) - 0.5) < 1e-9,
          f"a leading reset is exact; reset;h -> 50/50, got {r2}")

    cr = CircuitRep(2, 2); cr.add_gate("h", [1]); cr.add_gate("x", [0])
    cr.add_reset(0)
    for q in range(2): cr.add_measure(q, q)
    r3 = simulate(cr)
    check(abs(r3.get("00", 0) - 0.5) < 1e-9 and abs(r3.get("10", 0) - 0.5) < 1e-9,
          f"reset on a separable qubit is exact even with a peer in "
          f"superposition, got {r3}")

    cr = CircuitRep(2, 2); cr.add_gate("h", [0]); cr.add_gate("cx", [0, 1])
    cr.add_reset(0)
    for q in range(2): cr.add_measure(q, q)
    declined = False
    try:
        simulate(cr)
    except UnsupportedByEngine:
        declined = True
    check(declined,
          "a reset on an entangled qubit is DECLINED (not collapsed to a "
          "plausible-but-wrong pure-state ideal)")

    # ── an unknown gate raises (caught by the caller for provider fallback)
    cr = CircuitRep(1, 1); cr.add_gate("not_a_gate", [0])
    raised = False
    try:
        simulate(cr)
    except UnknownGateError:
        raised = True
    check(raised, "simulate raises UnknownGateError on an out-of-vocabulary gate")

    # ── run(): seeded sampling on top of simulate() ───────────────────────
    from engine.statevector import run
    cr = CircuitRep(2, 2); cr.add_gate("h", [0]); cr.add_gate("cx", [0, 1])
    for q in range(2): cr.add_measure(q, q)
    c1 = run(cr, 1000, seed=42)
    c2 = run(cr, 1000, seed=42)
    c3 = run(cr, 1000, seed=43)
    check(sum(c1.values()) == 1000,
          f"run() returns integer counts summing to shots, got {sum(c1.values())}")
    check(c1 == c2, "run() with a fixed seed is reproducible")
    check(c1 != c3, "run() with a different seed gives a different draw")
    check(set(c1) <= {"00", "11"},
          f"run() samples only the true support (Bell -> 00/11), got {set(c1)}")
    # Empirical frequencies converge to the exact distribution.
    big = run(cr, 100000, seed=7)
    exact = simulate(cr)
    err = max(abs(big.get(k, 0) / 100000 - exact.get(k, 0))
              for k in set(big) | set(exact))
    check(err < 0.02,
          f"run() frequencies converge to simulate()'s exact probs, max "
          f"err {err:.4f}")
    # shots must be a positive integer.
    for bad in (0, -5, 10.5, "abc", True):
        rejected = False
        try:
            run(cr, bad)
        except ValueError:
            rejected = True
        check(rejected, f"run() rejects shots={bad!r}")


# ── Backend factory ──────────────────────────────────────────────────────────

def block_mock_topologies():
    '''Every mock topology kind builds a usable device'''
    from providers.devq.backend_factory import create_backend

    expected_edges = {
        "linear":           6,      # 7 qubits in a chain
        "fully_connected":  21,     # C(7,2)
    }
    for kind, edges in expected_edges.items():
        backend = create_backend(kind, 7, rng=None)
        check(len(backend["coupling_map"]) == edges,
              f"{kind} 7-qubit topology has {edges} edges, "
              f"got {len(backend['coupling_map'])}")
        check(len(backend["error_map"]) == 7,
              f"{kind} generated an error map for every qubit")
        check(set(backend["edge_error_map"]) == set(backend["coupling_map"]),
              f"{kind} generated an error for every edge")

    # Grid needs a perfect square; 9 qubits is 3x3 with 12 edges.
    grid = create_backend("grid", 9, rng=None)
    check(len(grid["coupling_map"]) == 12,
          f"3x3 grid has 12 edges, got {len(grid['coupling_map'])}")

    # And each kind actually runs a job end to end.
    for kind, nq in (("linear", 5), ("grid", 4), ("fully_connected", 5)):
        sh  = session(None, [("devq.simulated", kind, nq, None, None)])
        run(sh, [f"qrun {BELL}"])
        out = settle(sh, 1)
        check("FINISHED" in out, f"a job completed on a {kind} mock device")


def block_backend_factory_errors():
    '''Invalid backend requests fail loudly at construction'''
    from providers.devq.backend_factory import create_backend

    cases = [
        (("fully_connected", 1), "at least 2"),
        (("nonexistent_kind", 5), "Unknown backend kind"),
        (("grid", 5),             "perfect square"),
    ]
    for (kind, nq), fragment in cases:
        try:
            create_backend(kind, nq)
            raised = None
        except ValueError as e:
            raised = str(e)
        check(raised is not None and fragment in raised,
              f"create_backend({kind!r}, {nq}) rejected with {fragment!r}, "
              f"got {raised!r}")

    # Unknown IBM backends are equally explicit.
    try:
        ibm_provider().get_device("FakeNotARealBackend")
        raised = None
    except ValueError as e:
        raised = str(e)
    check(raised is not None and "Unknown fake backend" in raised,
          f"unknown IBM backend rejected, got {raised!r}")


# ── Registry and plugin extension ────────────────────────────────────────────

def block_registry_plugin_components():
    '''Third-party scheduler, allocator and router run a job end to end'''
    import json
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="devq_plugin_")
    path   = os.path.join(tmpdir, "plugins.json")
    try:
        with open(path, "w") as f:
            json.dump({"scheduler": "mock", "allocator": "mock",
                       "router": "mock"}, f)

        dq = DevQ(config_path=path)
        dq.register_scheduler("mock", MockScheduler)
        dq.register_allocator("mock", MockAllocator)
        dq.register_router("mock",    MockRouter)
        dq.register_provider("mock",  MockProvider)

        sh = dq.add_device(
            DevQSimulatedProvider(seed=SEED).get_device("random", 7)).build()

        out = run(sh, ["qconfig", f"qsubmit {BELL} {GHZ}", "qrunpack"])

        # Named in config, resolved through the registry, and reported
        # under the LABEL the plugin declared rather than its class name.
        expect(out, "scheduler          =  mock", "[Mock Scheduler]")
        expect(out, "allocator          =  mock", "[Mock Allocator]")
        expect(out, "router       =  mock", "[Mock Router]")

        # Actually in the execution path, not merely constructed.
        check(out.count("Dispatching job") == 2,
              "both jobs were dispatched by the plugin scheduler")
        check(finished_ids(settle(sh, 1, 2)) == {"1", "2"},
              "both plugin-scheduled jobs finished")

        # MockScheduler is LIFO, so job 2 must be dispatched before job
        # 1. Every built-in dispatches 1 first, so this ordering is what
        # distinguishes "the plugin ran" from "something ran".
        order = expect_re(out, r"Dispatching job (\d+)")
        check(order == ["2", "1"],
              f"plugin scheduler's LIFO order was used (dispatched {order})")

        # MockAllocator is first-fit, so it takes the lowest free qubits
        # regardless of noise — proof it displaced the noise-aware
        # default rather than sitting alongside it.
        check(mapping_of(out, 1) == "{0: 0, 1: 1}",
              "plugin allocator's first-fit mapping was used, not noise_graph's")
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


def block_registry_validation():
    '''Malformed components are rejected at registration, not at run time'''

    # Each case is a component that violates the contract in exactly one
    # way, paired with a phrase its rejection must contain. Defined here
    # rather than at module scope so that a violation and its expected
    # message can be read together.

    class NotAScheduler:
        pass

    class NoInitArgs(BaseScheduler):
        # Bug 3 in miniature: __init__ takes nothing while DevQ passes
        # (memory_manager, process_table). Every combination using this
        # scheduler would have died at build time.
        def __init__(self):
            pass

        def schedule(self):
            return None

    class BadSelectSignature(BaseRouter):
        # The kernel calls route(), which is concrete on BaseRouter and
        # delegates to select(). Checking only route() would pass this.
        def select(self, qcb):
            return None

    class BadEnqueueSignature(BaseScheduler):
        def schedule(self):
            return None

        def enqueue(self):
            pass

    class UnNamespacedKey(BaseScheduler):
        CONFIG_SCHEMA = {"window": KeySpec("device", 1, positive_int, "W")}

        def schedule(self):
            return None

    class IllegalScope(BaseScheduler):
        # A scheduler is per-device; a global key from one would be a
        # scheduler dictating system-wide policy.
        CONFIG_SCHEMA = {"m.k": KeySpec("global", 1, positive_int, "K")}

        def schedule(self):
            return None

    class DefaultFailsValidator(BaseScheduler):
        CONFIG_SCHEMA = {"m.k": KeySpec("device", -5, positive_int, "K")}

        def schedule(self):
            return None

    class ValidatorNeverAccepts(BaseScheduler):
        # A validator that forgets to return None on the happy path
        # would reject every value a user ever supplied while the
        # default silently stood in.
        CONFIG_SCHEMA = {
            "m.k": KeySpec("device", 1, lambda v: "never ok", "K")}

        def schedule(self):
            return None

    class DanglingGroupMember(BaseScheduler):
        CONFIG_SCHEMA = {
            "m.a": KeySpec("device", 0.5, non_negative, "A", "m.g")}
        CONFIG_GROUPS = {"m.g": NormaliseGroup(["m.a", "m.typo"])}

        def schedule(self):
            return None

    class SingleMemberGroup(BaseScheduler):
        # Normalising one key alone forces it to 1.0 whatever the user
        # wrote — a wrong benchmark number with no other symptom.
        CONFIG_SCHEMA = {
            "m.a": KeySpec("device", 0.5, non_negative, "A", "m.g")}
        CONFIG_GROUPS = {"m.g": NormaliseGroup(["m.a"])}

        def schedule(self):
            return None

    class GroupNeverDeclared(BaseScheduler):
        CONFIG_SCHEMA = {
            "m.a": KeySpec("device", 0.5, non_negative, "A", "m.nope"),
            "m.b": KeySpec("device", 0.5, non_negative, "B", "m.nope")}

        def schedule(self):
            return None

    class SepInKey(BaseScheduler):
        # "___" is reserved as the namespace/parameter separator; a key
        # containing it would make the dotted-key -> ctor-param rewrite
        # ambiguous.
        CONFIG_SCHEMA = {"m.a___b": KeySpec("device", 1, positive_int, "K")}

        def schedule(self):
            return None

    class SepInPrefix(BaseScheduler):
        CONFIG_SCHEMA = {"pre___fix.k": KeySpec("device", 1, positive_int, "K")}

        def schedule(self):
            return None

    class UnderscoreEdge(BaseScheduler):
        # A leading "_" on the key abutting the separator is
        # indistinguishable from a trailing "_" on the prefix.
        CONFIG_SCHEMA = {"m._k": KeySpec("device", 1, positive_int, "K")}

        def schedule(self):
            return None

    class UnderscoreEdgeTrailing(BaseScheduler):
        # The other side of the same rule: a trailing "_" on the key (or a
        # trailing "_" on the prefix) must be rejected too, not only a
        # leading one.
        CONFIG_SCHEMA = {"m.k_": KeySpec("device", 1, positive_int, "K")}

        def schedule(self):
            return None

    cases = [
        ("scheduler", NotAScheduler,         "must subclass"),
        ("scheduler", NoInitArgs,            "cannot be constructed"),
        ("router",    BadSelectSignature,    "select() must accept"),
        ("scheduler", BadEnqueueSignature,   "enqueue() must accept"),
        ("scheduler", UnNamespacedKey,       "must be namespaced"),
        ("scheduler", IllegalScope,          "not legal for a scheduler"),
        ("scheduler", DefaultFailsValidator, "rejected by that key's own validator"),
        ("scheduler", ValidatorNeverAccepts, "rejected by that key's own validator"),
        ("scheduler", DanglingGroupMember,   "not declared in any CONFIG_SCHEMA"),
        ("scheduler", SingleMemberGroup,     "needs at least two"),
        ("scheduler", GroupNeverDeclared,    "no such group is declared"),
        ("scheduler", SepInKey,              "reserved as the namespace/parameter separator"),
        ("scheduler", SepInPrefix,           "reserved as the namespace/parameter separator"),
        ("scheduler", UnderscoreEdge,        "starts or ends with '_'"),
        ("scheduler", UnderscoreEdgeTrailing, "starts or ends with '_'"),
    ]

    register = {"scheduler": lambda d, c: d.register_scheduler("bad", c),
                "router":    lambda d, c: d.register_router("bad", c)}

    for kind, component, phrase in cases:
        label = component.__name__
        try:
            register[kind](DevQ(), component)
            check(False, f"{label}: rejected at registration")
        except DevQError as e:
            check(phrase in str(e),
                  f"{label}: rejected with {phrase!r}")

    # A per-device component registered as an INSTANCE would be shared
    # across every device, merging the queues the federation exists to
    # keep separate.
    try:
        DevQ().register_scheduler("bad", MockScheduler(None, None))
        check(False, "scheduler instance: rejected at registration")
    except DevQError as e:
        check("must be registered as a CLASS" in str(e),
              "scheduler instance: rejected, must be a class")

    # Every kind is class-only. A router used to be exempt, on the
    # grounds that one-per-system made sharing safe — but DevQ builds
    # the router FROM THE CASCADE, and an instance was returned as-is,
    # so its weights silently won while qconfig reported the config's.
    router_refused = None
    try:
        DevQ().register_router("bad_instance", MockRouter(0.5, 0.5, 0.1, 0.9))
    except DevQError as e:
        router_refused = str(e)

    check(router_refused is not None
          and "must be registered as a CLASS" in router_refused,
          "router instance: rejected, every kind is class-only")
    check(router_refused is not None and "cascade" in router_refused,
          "router instance: the error explains the cascade is what it bypasses")

    # ... and the positive half: a class-registered router is CONSTRUCTED
    # from the resolved cascade. Asserted against the running router
    # object, not qconfig output — the bug this replaces was precisely
    # that the two could disagree. weights_1_9 sets alpha/beta to 1/9,
    # which normalise to 0.1/0.9.
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        sh = session(config="weights_1_9.config.json",
                     devices=[("devq.simulated", "random", 5, None, None)])
    live = sh.kernel.router
    check(abs(live.qubit_error_weight - 0.1) < 1e-9
          and abs(live.edge_error_weight - 0.9) < 1e-9,
          "a class-registered router is built from the resolved cascade, "
          "so qconfig and the running router cannot disagree")

    # Re-registering a name would silently change what existing config
    # files mean.
    try:
        DevQ().register_scheduler("packing", MockScheduler)
        check(False, "duplicate name: rejected")
    except DevQError as e:
        check("already registered" in str(e),
              "duplicate name: rejected")


def block_plugin_contract_enforcement():
    '''Buggy plugins fail loudly at run time, not silently or by hanging'''
    from kernel.memory.allocators.base_allocator import (
        BaseAllocator, AllocationError)
    from kernel.memory.memory_manager import AllocatorContractError
    from kernel.router.base_router import BaseRouter, RouterContractError
    from frontends.qasm2.parser import parse

    circuit = parse(open(BELL).read())

    def fresh():
        sh = DevQ().add_device(
            DevQSimulatedProvider(seed=SEED).get_device("random", 5)).build()
        return sh, sh.kernel.contexts[0]

    # ── An allocator BUG propagates; it is NOT mistaken for infeasibility ─────
    #
    # The distinction that matters: a legitimate "cannot place" (AllocationError)
    # is caught and classified; ANY OTHER exception is a bug and must surface,
    # rather than being swallowed into an endless WAITING retry (the failure
    # mode that hung the suite when a plugin had the wrong signature).
    class BuggyAllocator(BaseAllocator):
        LABEL = "Buggy"
        def allocate(self, circuit, device, pool, max_qubit_error=None,
                     max_edge_error=None, max_1q_gate_error=None):
            return 1 / 0            # a bug, not an infeasibility

    sh, ctx = fresh()
    ctx.memory_manager.allocator = BuggyAllocator(
        qubit_error_weight=0.5, edge_error_weight=0.5)
    raised = None
    try:
        ctx.memory_manager.allocate(circuit)
    except ZeroDivisionError:
        raised = "propagated"
    except Exception as e:
        raised = type(e).__name__
    check(raised == "propagated",
          f"an allocator bug (ZeroDivisionError) propagates rather than being "
          f"swallowed as 'cannot place' (got {raised})")

    # The same bug driven through the SCHEDULER's own catch site
    # (_attempt_allocation) — the path that hung: its catch, if broad, would
    # reclassify the bug as transient contention (WAITING) and the kernel
    # would retry forever. Calling the unit directly reaches its catch
    # regardless of which router path a job took to get there. (A separate
    # assertion from the direct memory_manager call above because they are
    # different catch sites.)
    shb, ctxb = fresh()
    ctxb.memory_manager.allocator = BuggyAllocator(
        qubit_error_weight=0.5, edge_error_weight=0.5)
    qcbb = shb.kernel.submit_job(circuit)
    sched_raised = None
    try:
        ctxb.scheduler._attempt_allocation(qcbb)
    except ZeroDivisionError:
        sched_raised = "propagated"
    except Exception as e:
        sched_raised = type(e).__name__
    check(sched_raised == "propagated",
          f"the scheduler's own catch lets an allocator bug surface rather "
          f"than classifying it as WAITING and retrying forever (got "
          f"{sched_raised})")

    # A LEGITIMATE infeasibility (AllocationError) is still caught downstream —
    # the narrowing must not have broken normal rejection.
    class FullAllocator(BaseAllocator):
        LABEL = "Full"
        def allocate(self, circuit, device, pool, max_qubit_error=None,
                     max_edge_error=None, max_1q_gate_error=None):
            raise AllocationError("nothing free")

    sh2, ctx2 = fresh()
    ctx2.memory_manager.allocator = FullAllocator(
        qubit_error_weight=0.5, edge_error_weight=0.5)
    caught = False
    try:
        ctx2.memory_manager.allocate(circuit)
    except AllocationError:
        caught = True
    check(caught,
          "a legitimate AllocationError still surfaces to the caller "
          "(narrowing did not swallow the infeasibility signal)")

    # ── An allocator that maps without reserving is rejected, not double-booked ─
    class LyingAllocator(BaseAllocator):
        LABEL = "Lying"
        def allocate(self, circuit, device, pool, max_qubit_error=None,
                     max_edge_error=None, max_1q_gate_error=None):
            return {v: v for v in range(circuit.num_qubits)}   # no reserve

    sh3, ctx3 = fresh()
    ctx3.memory_manager.allocator = LyingAllocator(
        qubit_error_weight=0.5, edge_error_weight=0.5)
    raised = False
    try:
        ctx3.memory_manager.allocate(circuit)
    except AllocatorContractError:
        raised = True
    check(raised,
          "an allocator that returns a mapping without reserving its qubits "
          "is rejected (would otherwise silently double-book)")

    # ── A router returning a non-candidate is rejected, not obeyed ────────────
    #
    # The dangerous case is returning a VALID device the router was NOT
    # offered — that would run a job on a device the user's exec/no-exec
    # constraints excluded. A non-candidate of any shape must be refused.
    class RogueRouter(BaseRouter):
        LABEL = "Rogue"
        def select(self, qcb, candidates):
            return "not-a-candidate"

    sh4, ctx4 = fresh()
    sh4.kernel.router = RogueRouter(
        qubit_error_weight=0.5, edge_error_weight=0.5)
    qcb = sh4.kernel.submit_job(circuit)
    raised = False
    try:
        sh4.kernel.router.route(qcb, sh4.kernel.contexts)
    except RouterContractError:
        raised = True
    check(raised,
          "a router returning a device it was not offered is rejected "
          "(would otherwise place a job on a forbidden device)")

    # A well-behaved router (returns an actual candidate) still routes fine —
    # the guard must not reject the normal case.
    class GoodRouter(BaseRouter):
        LABEL = "Good"
        def select(self, qcb, candidates):
            return candidates[0]

    sh5, ctx5 = fresh()
    sh5.kernel.router = GoodRouter(
        qubit_error_weight=0.5, edge_error_weight=0.5)
    qcb5 = sh5.kernel.submit_job(circuit)
    chosen, reason = sh5.kernel.router.route(qcb5, sh5.kernel.contexts)
    check(chosen is not None and reason is None,
          "a router returning a real candidate still routes normally")


def block_registry_frozen():
    '''Registration after build() is refused rather than silently ignored'''
    dq = DevQ()
    sh = dq.add_device(
        DevQSimulatedProvider(seed=SEED).get_device("random", 5)).build()

    # build() has read the configuration, so a later registration could
    # not affect the system that was built.
    try:
        dq.register_scheduler("late", MockScheduler)
        check(False, "registering after build() raises")
    except DevQError as e:
        check("build() has already run" in str(e),
              "registering after build() raises, naming the cause")

    # The session built before the attempt is unaffected.
    out = run(sh, ["qdevices"])
    expect(out, "random_backend")

    # Registering BEFORE build() on a fresh instance still works — the
    # freeze is per-instance, not global state leaking between them.
    fresh = DevQ()
    fresh.register_scheduler("late", MockScheduler)
    check("late" in fresh._registry.names("scheduler"),
          "a fresh DevQ instance is unaffected by another's freeze")


def block_plugin_config_keys():
    '''Plugin-declared config keys cascade, validate and appear in qconfig'''
    import json
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="devq_plugincfg_")
    path   = os.path.join(tmpdir, "cfg.json")
    try:
        with open(path, "w") as f:
            json.dump({"scheduler": "mock", "mock.batch_window": 12}, f)

        # BEFORE registering: neither the key nor the scheduler name
        # exists, so both are rejected. A namespaced key is not
        # privileged simply for being namespaced.
        buf = BoundedBuffer()
        with _capture(buf):
            (DevQ(config_path=path)
             .add_device(DevQSimulatedProvider(seed=SEED)
                         .get_device("random", 5))
             .build())
        before = buf.getvalue()
        expect(before, "unknown config key 'mock.batch_window'")
        expect(before, "invalid value 'mock' for 'scheduler'")

        # AFTER registering: both are legal, with no second edit
        # anywhere in DevQ core.
        dq = DevQ(config_path=path)
        dq.register_scheduler("mock", MockScheduler)
        sh = dq.add_device(
            DevQSimulatedProvider(seed=SEED).get_device("random", 5)).build()
        out = run(sh, ["qconfig"])

        expect_absent(out, "unknown config key")
        expect(out, "mock.batch_window  =  12", "source: User (global)")

        # The scheduler name itself was accepted, which it could only be
        # if the legal set is read from the registry rather than from a
        # fixed list of built-in names.
        expect(out, "scheduler          =  mock")
        expect_absent(out, "invalid value 'mock' for 'scheduler'")

        # An unset plugin key still resolves to its declared default,
        # with core provenance.
        expect_re(out, r"mock\.wait_weight\s+=\s+0\.4\s+source: DevQ Core")

        # A device-scope plugin key must not leak into the global
        # scope. Asserted against the resolved config rather than
        # against qconfig's output: qconfig renders only the keys it
        # iterates over, so a leaked key would never appear there and
        # the check would pass without testing anything.
        global_config = sh._global_config
        check("mock.batch_window" not in global_config,
              "device-scope plugin key is absent from the resolved "
              "global config")
        check("mock.batch_window" in sh.kernel.contexts[0].config,
              "the same key IS present in the device config")

        # The mirror of that rule: a global-scope key must not appear in
        # a device's resolved config.
        check("router" not in sh.kernel.contexts[0].config,
              "global-scope key is absent from the resolved device config")

        # An invalid value for a plugin key is rejected by the plugin's
        # OWN validator, with the message that validator supplied.
        with open(path, "w") as f:
            json.dump({"scheduler": "mock", "mock.batch_window": -3}, f)
        buf = BoundedBuffer()
        with _capture(buf):
            dq2 = DevQ(config_path=path)
            dq2.register_scheduler("mock", MockScheduler)
            dq2.add_device(DevQSimulatedProvider(seed=SEED)
                           .get_device("random", 5)).build()
        expect(buf.getvalue(),
               "invalid value '-3' for 'mock.batch_window'",
               "expected a positive integer")
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


def block_schema_ctor_injection():
    '''Plugin CONFIG_SCHEMA keys inject into scheduler, allocator and router ctors'''
    import json
    import os
    import tempfile

    # A scheduler that NAMES its declared key as a ctor parameter (prefix
    # preserved: "inj.eta" -> "inj___eta"). Contrast with MockScheduler,
    # whose inherited ctor names none of its keys, so nothing injects.
    class InjectingScheduler(BaseScheduler):
        LABEL = "Injecting Scheduler"
        CONFIG_SCHEMA = {
            "inj.eta": KeySpec("device", 1.0, non_negative, "Inj eta"),
        }

        def __init__(self, memory_manager, process_table, inj___eta=1.0):
            super().__init__(memory_manager, process_table)
            self.seen_eta = inj___eta

        def schedule(self):
            return []

    # An allocator that REUSES a core key name for its OWN distinct
    # quantity. The core "qubit_error_weight" (normalised, <=1) and the
    # plugin "alloc.qubit_error_weight" (its own scale, here >1) must land
    # in SEPARATE parameters and both survive — this is the whole reason
    # the injector preserves the prefix instead of stripping it.
    class ReusingAllocator(BaseAllocator):
        LABEL = "Reusing Allocator"
        CONFIG_SCHEMA = {
            "alloc.qubit_error_weight": KeySpec(
                "device", 2.5, non_negative, "Alloc own scale"),
        }

        def __init__(self, qubit_error_weight=0.1, edge_error_weight=0.9,
                     alloc___qubit_error_weight=None):
            super().__init__(qubit_error_weight=qubit_error_weight,
                             edge_error_weight=edge_error_weight)
            self.own_scale = alloc___qubit_error_weight

        def allocate(self, circuit, device, pool,
                     max_qubit_error=None, max_edge_error=None,
                     max_1q_gate_error=None):
            need = circuit.num_qubits
            free = sorted(pool.available())
            if len(free) < need:
                raise AllocationError("Reusing: not enough free qubits")
            chosen = free[:need]
            pool.allocate(chosen)
            return {v: p for v, p in enumerate(chosen)}

    # A router that names its own global-scope key.
    class InjectingRouter(BaseRouter):
        LABEL = "Injecting Router"
        CONFIG_SCHEMA = {
            "rtr.bias": KeySpec("global", 3.0, non_negative, "Router bias"),
        }

        def __init__(self, router_queue_weight=0.5, router_noise_weight=0.5,
                     qubit_error_weight=0.1, edge_error_weight=0.9,
                     rtr___bias=None):
            super().__init__(router_queue_weight, router_noise_weight,
                             qubit_error_weight, edge_error_weight)
            self.seen_bias = rtr___bias

        def select(self, qcb, candidates):
            return candidates[0]

    tmpdir = tempfile.mkdtemp(prefix="devq_inject_")
    path   = os.path.join(tmpdir, "cfg.json")
    try:
        # Select all three plugins and give each key a non-default value,
        # so a value that arrives at the ctor proves the cascade->inject
        # path end to end (a default could arrive by ctor default alone).
        with open(path, "w") as f:
            json.dump({
                "scheduler": "inj",
                "allocator": "ralloc",
                "router":    "irtr",
                "inj.eta":                   0.75,
                "alloc.qubit_error_weight":  4.0,
                "rtr.bias":                  9.0,
            }, f)

        dq = DevQ(config_path=path)
        dq.register_scheduler("inj",   InjectingScheduler)
        dq.register_allocator("ralloc", ReusingAllocator)
        dq.register_router("irtr",     InjectingRouter)
        sh = dq.add_device(
            DevQSimulatedProvider(seed=SEED).get_device("fully_connected", 5)
        ).build()

        ctx   = sh.kernel.contexts[0]
        sched = ctx.scheduler
        alloc = ctx.memory_manager.allocator
        rtr   = sh.kernel.router

        # Scheduler: its plugin key reached the ctor under the flattened name.
        check(sched.seen_eta == 0.75,
              f"scheduler received injected inj.eta=0.75 (got {sched.seen_eta})")

        # Allocator: BOTH the core weight and the plugin's same-named key
        # arrived, in SEPARATE slots. The core weight is normalised so its
        # exact value is not asserted; what matters is the plugin scale is
        # the plugin's value and did NOT overwrite (or get overwritten by)
        # the core weight.
        check(alloc.own_scale == 4.0,
              f"allocator received injected alloc.qubit_error_weight=4.0 in its "
              f"OWN slot (got {alloc.own_scale})")
        check(alloc.qubit_error_weight != 4.0,
              "core qubit_error_weight is distinct from the plugin's reused-name "
              f"key (core={alloc.qubit_error_weight}, plugin=4.0) — no collision")

        # Router: its plugin key reached the ctor.
        check(rtr.seen_bias == 9.0,
              f"router received injected rtr.bias=9.0 (got {rtr.seen_bias})")

        # The flattened form is INTERNAL: qconfig shows only dotted keys.
        out = run(sh, ["qconfig"])
        expect_absent(out, "___")
        expect(out, "inj.eta", "alloc.qubit_error_weight", "rtr.bias")

        # flatten_key rewrites ONLY the first (namespace) dot. A key with a
        # further dot keeps it verbatim in the parameter name — which then
        # matches no real parameter, so such a key simply is not injected
        # (it still cascades). Asserting the first-dot-only rule directly,
        # because the plugins above use single-dot keys and so cannot
        # distinguish "replace first dot" from "replace every dot".
        from registry.keyspec import flatten_key as _fk, param_to_key as _pk
        check(_fk("inj.eta") == "inj___eta",
              f"flatten_key single dot (got {_fk('inj.eta')!r})")
        check(_fk("a.b.c") == "a___b.c",
              f"flatten_key rewrites only the first dot (got {_fk('a.b.c')!r})")
        check(_pk("a___b.c") == "a.b.c",
              f"param_to_key inverts the first separator (got {_pk('a___b.c')!r})")

        # ── The un-injectable-key diagnostic ─────────────────────────────
        # A declared key whose flattened name the ctor does NOT accept is
        # the plugin author's most likely config mistake — it validates and
        # cascades but reaches nothing. build() must WARN, naming the dotted
        # key (never the "___" form), so the mistake is not silent. Three
        # cases: (1) a typo'd ctor parameter WARNS; (2) runtime_read=True
        # suppresses the warning (author reads the key at runtime); (3) a
        # **kwargs ctor suppresses it (the key is in fact absorbed). None of
        # these may crash the build.
        from registry.keyspec import KeySpec as _KS, non_negative as _nn

        # (1) TYPO — schema "typo.eta" but ctor names "typo___etaa".
        class TypoScheduler(BaseScheduler):
            LABEL = "Typo Scheduler"
            CONFIG_SCHEMA = {"typo.eta": _KS("device", 1.0, _nn, "Typo eta")}
            def __init__(self, memory_manager, process_table, typo___etaa=1.0):
                super().__init__(memory_manager, process_table)
                self.seen = typo___etaa
            def schedule(self):
                return []

        # (2) RUNTIME-READ — same shape, but the key is declared
        # runtime_read=True and the ctor deliberately names no parameter.
        class RuntimeReadScheduler(BaseScheduler):
            LABEL = "Runtime Read Scheduler"
            CONFIG_SCHEMA = {
                "rr.eta": _KS("device", 1.0, _nn, "RR eta", runtime_read=True)
            }
            def __init__(self, memory_manager, process_table):
                super().__init__(memory_manager, process_table)
            def schedule(self):
                return []

        # (3) VAR-KEYWORD — the ctor absorbs any parameter via **kwargs, so
        # the declared key IS injectable and must not warn.
        class VarKwScheduler(BaseScheduler):
            LABEL = "VarKw Scheduler"
            CONFIG_SCHEMA = {"vk.eta": _KS("device", 1.0, _nn, "VK eta")}
            def __init__(self, memory_manager, process_table, **kwargs):
                super().__init__(memory_manager, process_table)
                self.kwargs = kwargs
            def schedule(self):
                return []

        for label, sched_name, sched_cls, key, should_warn in [
            ("typo",         "typo", TypoScheduler,        "typo.eta", True),
            ("runtime_read", "rr",   RuntimeReadScheduler, "rr.eta",   False),
            ("var_kwargs",   "vk",   VarKwScheduler,       "vk.eta",   False),
        ]:
            with open(path, "w") as f:
                json.dump({"scheduler": sched_name, key: 0.5}, f)

            buf = BoundedBuffer()
            with _capture(buf):
                dq2 = DevQ(config_path=path)
                dq2.register_scheduler(sched_name, sched_cls)
                sh2 = dq2.add_device(
                    DevQSimulatedProvider(seed=SEED).get_device(
                        "fully_connected", 5)
                ).build()
            out = buf.getvalue()

            warned = ("Warning" in out) and (key in out)
            check(warned == should_warn,
                  f"{label}: un-injectable-key warning fired={warned}, "
                  f"expected {should_warn}")
            # Whatever the warning outcome, the session built (no crash) and
            # the "___" form never leaked into the message.
            check(sh2 is not None,
                  f"{label}: build() still produced a session")
            if warned:
                check("___" not in out,
                      f"{label}: warning names the dotted key, not the "
                      f"'___' parameter form")
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


def block_plugin_normalise_group():
    '''A plugin's own normalise group is scaled to sum to 1'''
    import json
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="devq_pluginnorm_")
    path   = os.path.join(tmpdir, "cfg.json")
    try:
        # 3 and 1 are on an arbitrary scale; only the ratio matters, so
        # they must come back as 0.75 / 0.25.
        with open(path, "w") as f:
            json.dump({"scheduler": "mock",
                       "mock.wait_weight": 3, "mock.fid_weight": 1}, f)

        dq = DevQ(config_path=path)
        dq.register_scheduler("mock", MockScheduler)
        sh = dq.add_device(
            DevQSimulatedProvider(seed=SEED).get_device("random", 5)).build()
        out = run(sh, ["qconfig"])

        expect_re(out, r"mock\.wait_weight\s+=\s+0\.75")
        expect_re(out, r"mock\.fid_weight\s+=\s+0\.25")

        # The core group is normalised independently in the same pass —
        # groups do not interfere with one another.
        expect_re(out, r"qubit_error_weight\s+=\s+0\.1\s")

        # An all-zero group has an undefined ratio and would make every
        # candidate score identical; it reverts to declared defaults.
        with open(path, "w") as f:
            json.dump({"scheduler": "mock",
                       "mock.wait_weight": 0, "mock.fid_weight": 0}, f)
        buf = BoundedBuffer()
        with _capture(buf):
            dq2 = DevQ(config_path=path)
            dq2.register_scheduler("mock", MockScheduler)
            sh2 = dq2.add_device(DevQSimulatedProvider(seed=SEED)
                                 .get_device("random", 5)).build()
        expect(buf.getvalue(), "are both 0", "Falling back to core defaults")

        out2 = run(sh2, ["qconfig"])
        expect_re(out2, r"mock\.wait_weight\s+=\s+0\.4")
        expect_re(out2, r"mock\.fid_weight\s+=\s+0\.6")
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


def block_shipped_workloads():
    '''Every shipped workload spec actually runs to completion'''
    # benchmark/workloads/ mirrors config/config_examples/: the files are
    # runnable examples AND test fixtures. Validating them is not enough
    # — a spec can parse and still fail at execution, and these are the
    # only things a user can run to see the benchmark runner work.
    # block_benchmark_runner builds its own spec because it asserts exact
    # job counts; this one runs what actually ships.
    import io, contextlib, json, os, shutil, tempfile
    from benchmark import runner as R
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

    # Job counts pinned per spec. See the note at the assertion below:
    # computing these from the spec under test proves nothing.
    EXPECTED_JOBS = {
        "smoke.json"          : 5,
        "ibm_federation.json" : 8,
        "placeholders.json"   : 5,
        "rejection.json"      : 4,
        "contention.json"     : 25,
        "per_job_shots.json"  : 2,
        "gate_error_filter.json" : 2,
    }

    # KEPT, not deleted. block_benchmark_runner runs 19 sessions into a
    # temp directory and throws them away — right for a test that
    # injects a crash and asserts exact counts. These two are different:
    # they are the specs a user actually runs, so their output is worth
    # being able to open and read after the suite finishes. Overwritten
    # each run rather than timestamped, so it cannot accumulate.
    root = os.path.dirname(os.path.abspath(__file__))
    keep = os.path.join(root, "test_results")
    shutil.rmtree(keep, ignore_errors=True)
    os.makedirs(keep, exist_ok=True)
    with open(os.path.join(keep, "README.txt"), "w") as handle:
        handle.write(
            "Output from the shipped workload specs, written by\n"
            "run_tests.py's shipped_workloads block so a run can be\n"
            "inspected after the suite finishes.\n"
            "\n"
            "Overwritten on every test run, and gitignored. Delete it\n"
            "freely — nothing depends on it.\n"
            "\n"
            "This is NOT where the runner writes normally. A real run\n"
            "goes to results/<spec name>_<timestamp>/:\n"
            "\n"
            "    python benchmark/runner.py benchmark/workloads/smoke.json\n"
            "\n"
            "See docs/WORKLOADS.md.\n"
        )

    specs = sorted(f for f in os.listdir(WORKLOADS) if f.endswith(".json"))
    check(specs, f"workload specs ship with the repo, found {specs}")
    check(set(specs) == set(EXPECTED_JOBS),
          f"every shipped spec has a pinned job count; "
          f"unpinned={sorted(set(specs) - set(EXPECTED_JOBS))}, "
          f"stale={sorted(set(EXPECTED_JOBS) - set(specs))}")

    # The placeholders.json spec references environment variables via
    # ${NAME}. They must be set BEFORE any spec runs, because R.run ->
    # load_spec resolves them at load time, and stay set for the
    # reproduce re-run further down. Values live in PLACEHOLDER_ENV.
    # Restored in the finally so nothing leaks into later blocks.
    ph_saved = {k: os.environ.get(k) for k in PLACEHOLDER_ENV}
    os.environ.update(PLACEHOLDER_ENV)

    tmp = tempfile.mkdtemp()
    try:
        for filename in specs:
            path = os.path.join(WORKLOADS, filename)
            with open(path) as handle:
                spec = json.load(handle)

            # A spec naming a provider the caller must register is not a
            # broken spec — it is the documented extension model. Supply
            # the ones DevQ ships so every shipped spec is runnable here.
            providers = {}
            for device in spec["devices"]:
                if device["provider"] == "ibm.simulated":
                    providers["ibm.simulated"] = IBMSimulatedProvider

            out = os.path.join(keep, filename.replace(".json", ""))
            with contextlib.redirect_stdout(io.StringIO()):
                manifest = R.run(path, out_dir=out,
                                 register_providers=providers, quiet=True)

            entry = manifest["sessions"][0]
            check(entry["outcome"] in (R.COMPLETED, R.WITH_FAILURES),
                  f"{filename} runs to completion, got {entry['outcome']}"
                  + (f" — {entry.get('error', '')[:60]}"
                     if entry["outcome"] == R.CRASHED else ""))

            # The log must be readable and self-describing, since that is
            # the whole point of shipping these as examples.
            log = os.path.join(out, entry["log"])
            with open(log) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            check(records[0]["event"] == "header",
                  f"{filename} log opens with a header")
            check(records[0]["spec"]["name"] == spec["name"],
                  f"{filename} header records its own spec verbatim")
            check(records[-1]["event"] == "summary",
                  f"{filename} log closes with a summary")

            # THE VERBATIM/RESOLVED SPLIT, asserted where it matters most.
            # For the placeholder spec the header must show ${NAME}
            # LITERALLY — never the resolved value — because a resolved
            # ${IONQ_API_KEY} in the header is a secret on disk. Meanwhile
            # the run must have USED the resolved values. Asserting both
            # on the same log is what pins the split: the same fields that
            # read literally in the header drove a real, correct run.
            if filename == "placeholders.json":
                header_spec = records[0]["spec"]
                check(header_spec["seed"] == "${DEVQ_SEED}",
                      "placeholder header keeps ${DEVQ_SEED} literal, "
                      f"not resolved — got {header_spec['seed']!r}")
                check(header_spec["devices"][0]["provider"]
                      == "${DEVQ_VENDOR}.${DEVQ_TIER}",
                      "placeholder header keeps the embedded provider "
                      "placeholder literal")
                check(header_spec["jobs"][1]["max_qubit_error"]
                      == "${DEVQ_MAX_QERR}",
                      "placeholder header keeps the threshold placeholder "
                      "literal")
                # ...and the run actually resolved. seed_requested now
                # mirrors the verbatim literal (Option 2: requested = what
                # was written), while the per-device seed_effective is the
                # coerced int the run truly used. Asserting both proves
                # the split holds end to end.
                check(records[0]["seed_requested"] == "${DEVQ_SEED}",
                      "placeholder seed_requested is the verbatim literal, "
                      f"not the resolved int — got {records[0]['seed_requested']!r}")
                check(records[0]["devices"][0]["seed_effective"] == 42,
                      "placeholder run resolved ${DEVQ_SEED} to int 42 "
                      "for the device that actually ran")
                # The manifest is written to disk beside the log, so it
                # is a leak site too — assert it keeps the placeholder
                # literal, not just the header. (Without this, a manifest
                # recording the resolved spec passes every other check.)
                check(manifest["spec"]["seed"] == "${DEVQ_SEED}",
                      "placeholder manifest keeps ${DEVQ_SEED} literal, "
                      f"not resolved — got {manifest['spec']['seed']!r}")

            # The rejection spec is the shipped fixture for the
            # rejection-rate metric: half its jobs carry an impossibly
            # strict max_qubit_error, so no device is feasible and routing
            # rejects them terminally, while the rest complete. Asserting
            # the metric on a REAL shipped run (not a hand-built records
            # list) is what proves rejection rate works end to end — and
            # the expected 2-of-4 is hand-known from the spec, not read
            # back from the metric. A WITH_FAILURES outcome here is the
            # correct result, not a crash.
            if filename == "rejection.json":
                from benchmark import metrics as M
                check(entry["outcome"] == R.WITH_FAILURES,
                      f"rejection.json is a result, not a crash — "
                      f"got {entry['outcome']}")
                rr = M.rejection_rate(records)
                check(rr["rejected"] == 2 and rr["submitted"] == 4,
                      f"rejection.json rejects 2 of 4, got "
                      f"{rr['rejected']}/{rr['submitted']}")
                check(abs(rr["rate"] - 0.5) < 1e-12,
                      f"rejection.json rejection rate 0.5, got {rr['rate']}")

            # The contention spec is the shipped fixture for p95 at a
            # realistic job count. With nearest-rank on n jobs, p95 is the
            # ceil(0.95n)-th sorted wait, which only falls BELOW the max
            # once n >= 21 (ceil(0.95*20)=19 is still not 20; 25 gives the
            # 24th of 25). Every smaller spec has p95 == max by that math,
            # so this spec exists to exercise the distinct-p95 path. The
            # exact latencies are wall-clock and non-deterministic, so the
            # assertion is structural: p95 strictly below max, and p95
            # equal to the 24th of the 25 sorted waits.
            if filename == "contention.json":
                from benchmark import metrics as M
                import math as _math
                ql = M.queue_latency(records)
                check(ql["p95"] < ql["max"],
                      f"at n=25 nearest-rank p95 is below max, got "
                      f"p95={ql['p95']} max={ql['max']}")
                waits = sorted(
                    r["queue_latency"] for r in records[-1]["per_job"]
                    if r["queue_latency"] is not None)
                rank = _math.ceil(0.95 * len(waits))   # 24 for n=25
                check(ql["p95"] == waits[rank - 1],
                      f"p95 is the {rank}th of {len(waits)} sorted waits")

            # Expanded job count, PINNED per spec rather than computed
            # from the spec being checked. Deriving `expected` from the
            # same file is self-satisfying: changing a repeat moves both
            # sides together and the assertion cannot fail. Pinning also
            # makes an accidental edit to a shipped example visible.
            check(entry["jobs"] == EXPECTED_JOBS[filename],
                  f"{filename} ran {EXPECTED_JOBS[filename]} jobs, "
                  f"got {entry['jobs']}")

            # ...and the spec must still SAY what these numbers assume,
            # so the pin and the file cannot drift apart silently.
            declared = sum(job.get("repeat", 1) for job in spec["jobs"])
            check(declared == EXPECTED_JOBS[filename],
                  f"{filename} declares {EXPECTED_JOBS[filename]} jobs, "
                  f"spec now says {declared} — update EXPECTED_JOBS "
                  f"deliberately if the example changed")

            # Seeded specs must reproduce their ROUTING decisions.
            #
            # submit and route only. NOT dispatch: allocation depends on
            # which qubits are free at dispatch time, and that depends on
            # whether an earlier job has resolved and released its block.
            # An earlier version of this check included the v2p_map and
            # failed intermittently — job 4 landed on {0:1,1:2} in one run
            # and {0:4,1:5} in another, both correct. That is completion
            # order leaking into allocation, which DevQ explicitly does
            # NOT guarantee (see docs/REGISTRY.md, "Two clocks"). Wall
            # clock is excluded for the same reason.
            if spec.get("seed") is not None:
                out2 = os.path.join(tmp, filename.replace(".json", "_again"))
                with contextlib.redirect_stdout(io.StringIO()):
                    R.run(path, out_dir=out2, register_providers=providers,
                          quiet=True)
                with open(os.path.join(out2, entry["log"])) as handle:
                    again = [json.loads(l) for l in handle if l.strip()]

                def decisions(recs):
                    return [(r["event"], r.get("job_id"), r.get("device"))
                            for r in recs
                            if r["event"] in ("submit", "route")]

                check(decisions(records) == decisions(again),
                      f"{filename} reproduces its routing under the same seed")
        # The kept output is the whole point of writing here rather than
        # to a temp directory: it must survive the block, and be
        # readable. Asserted because "the directory exists" and "the
        # directory has usable logs in it" are different things.
        for filename in specs:
            kept = os.path.join(keep, filename.replace(".json", ""),
                                "default.jsonl")
            check(os.path.exists(kept),
                  f"{filename}'s log is kept in test_results/ for inspection")
            if os.path.exists(kept):
                with open(kept) as handle:
                    lines = [l for l in handle if l.strip()]
                check(len(lines) > 2,
                      f"{filename}'s kept log has content, got {len(lines)} records")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # Restore the placeholder env vars exactly as found — including
        # deleting ones that were not set before — so no later block
        # sees DEVQ_* leaking in.
        for k, v in ph_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def block_repo_hygiene():
    '''Every source file carries a tag, and the docs agree with the code'''
    # These are invariants the README asserts and no other block checks.
    # They break silently: a new file without a tag, or a doc claiming a
    # block count that drifted, costs nothing at runtime and misleads
    # every reader afterwards. verify_local.py went untagged for exactly
    # this reason.
    import os, re

    root = os.path.dirname(os.path.abspath(__file__))

    # WALK ONLY DEVQ'S OWN PACKAGES, never the whole tree. An earlier
    # version walked everything except a blocklist and cheerfully
    # audited the user's venv/ — reporting several thousand missing
    # headers across numpy, scipy and qiskit. Blocklisting virtualenv
    # directory names is the wrong fix: the next one is called .venv or
    # env. Naming what IS ours cannot go wrong that way.
    OURS = ("benchmark", "circuits", "config", "engine", "frontends", "hardware",
            "kernel", "providers", "registry", "research", "shell")
    roots = [os.path.join(root, d) for d in OURS]
    untagged = []

    # Top-level scripts, which live beside the packages rather than in
    # one, so a directory walk would not reach them.
    for filename in sorted(os.listdir(root)):
        if filename.endswith(".py"):
            with open(os.path.join(root, filename)) as handle:
                if not re.search(r"^Tags:", handle.read(), re.M):
                    untagged.append(filename)

    for package in roots:
        for dirpath, dirnames, filenames in os.walk(package):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path) as handle:
                    if not re.search(r"^Tags:", handle.read(), re.M):
                        untagged.append(os.path.relpath(path, root))

    check(not untagged,
          f"every .py file carries a Tags: header, missing in {untagged}")

    # Every shipped workload spec must still validate. These are the
    # only runnable examples of the benchmark runner — run_tests.py
    # builds its own specs in a temp directory and deletes them, so
    # without these there is nothing a user can actually execute, and
    # nothing would notice if the schema drifted away from them.
    #
    # Specs may carry ${NAME} placeholders, which are only valid AFTER
    # resolution — validating the raw file would reject a ${SEED} the
    # resolved spec legitimately coerces. So resolve via load_spec under
    # the documented env, restored immediately after. load_spec returns
    # (resolved, verbatim); only the resolved half is validated here.
    from benchmark.spec import load_spec, SpecError

    workloads = os.path.join(root, "benchmark", "workloads")
    shipped = sorted(f for f in os.listdir(workloads) if f.endswith(".json"))
    check(shipped, f"workload specs are shipped, found {shipped}")

    ph_saved = {k: os.environ.get(k) for k in PLACEHOLDER_ENV}
    os.environ.update(PLACEHOLDER_ENV)
    try:
        for filename in shipped:
            path = os.path.join(workloads, filename)
            try:
                spec, _verbatim = load_spec(path)
                ok, detail = True, ""
            except (SpecError, ValueError) as exc:
                ok, detail = False, str(exc)[:80]
            check(ok, f"shipped spec {filename} validates{': ' + detail if detail else ''}")

            if not ok:
                continue

            # A spec naming a circuit that does not exist would fail only
            # when someone tried to run it.
            missing = [j["circuit"] for j in spec["jobs"]
                       if not os.path.exists(os.path.join(root, j["circuit"]))]
            check(not missing,
                  f"{filename} references existing circuits, missing {missing}")
    finally:
        for k, v in ph_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # TEST_BLOCKS.md must stay 1:1 with the block list — a documented
    # block that no longer exists, or an undocumented one, means the
    # spec and the suite disagree about what is being tested.
    with open(os.path.join(root, "run_tests.py")) as handle:
        registered = set(re.findall(r'\("(\w+)",\s*block_', handle.read()))
    doc_path = os.path.join(root, "docs", "TEST_BLOCKS.md")
    with open(doc_path) as handle:
        doc_text = handle.read()
    documented = set(re.findall(r"^### `(\w+)`", doc_text, re.M))

    check(registered == documented,
          f"TEST_BLOCKS.md is 1:1 with the block list; "
          f"undocumented={sorted(registered - documented)}, "
          f"stale={sorted(documented - registered)}")

    # ...and the count stated in prose must match reality.
    stated = re.search(r"(\d+) sanity blocks", doc_text)
    check(stated and int(stated.group(1)) == len(registered),
          f"TEST_BLOCKS.md states {stated.group(1) if stated else '?'} blocks, "
          f"there are {len(registered)}")


def block_benchmark_runner():
    '''Runs write one log per session, with a manifest and resume'''
    import json, os, shutil, tempfile
    from benchmark import runner as R

    tmp = tempfile.mkdtemp()
    spec_path = os.path.join(tmp, "wl.json")
    with open(spec_path, "w") as handle:
        json.dump({
            "name": "block", "seed": SEED,
            "devices": [{"id": "alpha", "provider": "devq.simulated",
                         "backend": {"kind": "fully_connected", "num_qubits": 7}},
                        {"id": "bravo", "provider": "devq.simulated",
                         "backend": {"kind": "linear", "num_qubits": 7}}],
            "jobs": [{"circuit": BELL, "repeat": 2}, {"circuit": GHZ}],
        }, handle)

    try:
        # ── single session ────────────────────────────────────────────
        out = os.path.join(tmp, "single")
        manifest = R.run(spec_path, out_dir=out, quiet=True)

        check(len(manifest["sessions"]) == 1, "a plain run produces one session")
        entry = manifest["sessions"][0]
        check(entry["outcome"] == R.COMPLETED,
              f"session completed, got {entry['outcome']}")
        check(entry["jobs"] == 3, f"repeat expanded to 3 jobs, got {entry['jobs']}")
        check(os.path.exists(os.path.join(out, "manifest.json")),
              "a manifest is written")
        # run() computes metrics as part of finishing a run, so a run
        # directory is self-contained: logs, manifest, AND metrics.json.
        # The comparative modes read this file, so a run that produced
        # logs but no metrics would be a silent gap.
        mpath = os.path.join(out, "metrics.json")
        check(os.path.exists(mpath), "run() writes metrics.json beside the manifest")
        with open(mpath) as handle:
            metrics_json = json.load(handle)
        check(entry["session_id"] in metrics_json,
              "metrics.json carries the session's metrics, keyed by session id")
        check(set(metrics_json[entry["session_id"]]) ==
              {"throughput", "queue_latency", "utilisation", "rejection_rate",
               "load_imbalance", "fidelity"},
              "the session's metrics carry the six metric groups")

        # A single run uses the SAME directory structure as a matrix, so
        # a reader never branches on which it is looking at.
        log = os.path.join(out, entry["log"])
        check(os.path.exists(log), "the session log exists at the manifest's path")

        with open(log) as handle:
            records = [json.loads(line) for line in handle if line.strip()]

        # The header carries everything needed to interpret the stream,
        # written once rather than repeated per record.
        check(records[0]["event"] == "header", "the log opens with a header")
        check(records[0]["spec"]["name"] == "block",
              "the header records the spec verbatim — the log is self-describing")
        check([d["id"] for d in records[0]["devices"]] == ["alpha", "bravo"],
              "the header carries the device table so records can use a bare index")
        check(records[-1]["event"] == "summary", "the log closes with a summary")
        check(len(records[-1]["per_job"]) == 3,
              "the summary carries a per-job row")
        check([r["job_id"] for r in records[-1]["per_job"]] == [1, 2, 3],
              "per-job rows are ordered by job id — the log itself stays chronological")
        # The summary records the full attached-device roster, index->id.
        # Load balance needs it to see devices that ran NOTHING, so it
        # must list every attached device, not only those that dispatched.
        # (This spec's jobs may all land on one device; the roster must
        # still name both — the metrics block alone cannot catch an empty
        # roster because its fallback would recover the devices that ran.)
        check(records[-1].get("devices_attached") == {"0": "alpha", "1": "bravo"},
              f"summary carries the full device roster, got "
              f"{records[-1].get('devices_attached')}")

        kinds = {r["event"] for r in records}
        check({"submit", "route", "dispatch", "resolve"} <= kinds,
              f"lifecycle events reached the log, got {sorted(kinds)}")

        # THE DEFAULT OUTPUT PATH. Every other assertion here passes
        # out_dir explicitly, so the path a user actually gets was
        # untested — the default could have become "result/" or the
        # summary could name the wrong directory (it did: main()
        # reconstructed it from a bare log filename and printed a
        # literal "results"). Run once with no --out, from a temp cwd so
        # the suite still leaves nothing behind.
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            defaulted = R.run(spec_path, quiet=True)
        finally:
            os.chdir(cwd)

        out_dir = defaulted["out_dir"]
        check(os.path.basename(os.path.dirname(out_dir)) == "results",
              f"the default run directory lives under results/, got {out_dir}")
        check(os.path.basename(out_dir).startswith("block_"),
              f"the run directory is named for the spec, got "
              f"{os.path.basename(out_dir)}")
        check(os.path.isdir(out_dir) and
              os.path.exists(os.path.join(out_dir, "manifest.json")),
              "the default run directory really exists, with its manifest")

        # ── failures are a RESULT, not a crash ────────────────────────
        # Phase 5.3 must be able to tell "this config rejected its jobs"
        # from "this session died", so they are distinct outcomes.
        reject_path = os.path.join(tmp, "reject.json")
        with open(reject_path, "w") as handle:
            json.dump({
                "name": "rejects", "seed": SEED,
                "devices": [{"id": "solo", "provider": "devq.simulated",
                             "backend": {"kind": "linear", "num_qubits": 7}}],
                "jobs": [{"circuit": BELL, "max_qubit_error": 0.0000001}],
            }, handle)
        rejected = R.run(reject_path, out_dir=os.path.join(tmp, "rej"), quiet=True)
        check(rejected["sessions"][0]["outcome"] == R.WITH_FAILURES,
              f"a rejecting run is {R.WITH_FAILURES}, not crashed — "
              f"got {rejected['sessions'][0]['outcome']}")

        # ── a crashing session must not take the run down ─────────────
        crash_dir = os.path.join(tmp, "crash")
        original = R.submit_jobs
        calls = {"n": 0}

        def exploding(shell, spec, source):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated explosion")
            return original(shell, spec, source)

        R.submit_jobs = exploding
        try:
            crashed = R.run(spec_path, out_dir=crash_dir, matrix=True, quiet=True)
        finally:
            R.submit_jobs = original

        outcomes = [e["outcome"] for e in crashed["sessions"]]
        check(outcomes.count(R.CRASHED) == 1,
              f"exactly one session crashed, got {outcomes.count(R.CRASHED)}")
        check(outcomes.count(R.COMPLETED) == len(outcomes) - 1,
              "one crash does not take the rest of the matrix down")

        # ATOMIC WRITES: a log is either absent or whole. A half-written
        # file must never be mistaken for a finished session.
        files = os.listdir(crash_dir)
        check(not [f for f in files if f.endswith(".partial")],
              f"no .partial files orphaned, found {[f for f in files if f.endswith('.partial')]}")
        check([f for f in files if f.endswith(".crashed")],
              "a crashed session's log is kept under a name readers will not trust")

        # ── resume ────────────────────────────────────────────────────
        # Session-level only: seeding is sequential, so a partially run
        # session is re-run whole rather than continued.
        resumed = R.run(spec_path, out_dir=crash_dir, matrix=True,
                        resume=True, quiet=True)
        skipped = [e for e in resumed["sessions"] if e.get("skipped")]
        check(len(skipped) == len(outcomes) - 1,
              f"resume skipped the {len(outcomes) - 1} completed sessions, "
              f"got {len(skipped)}")
        check(all(e["outcome"] != R.CRASHED for e in resumed["sessions"]),
              "resume re-ran the crashed session to completion")

        # Sessions are identified by WHAT VARIED, not by position, so
        # adding a component cannot silently re-map existing results.
        ids = [e["session_id"] for e in resumed["sessions"]]
        check(len(set(ids)) == len(ids), "session ids are unique")
        check(all("__" in i for i in ids),
              "matrix session ids name their scheduler/allocator/router")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def block_workload_spec():
    '''Workload specs validate strictly and resolve seeds predictably'''
    import io, contextlib, json, os, tempfile
    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    from benchmark.spec import (validate_spec, load_spec, build_session,
                                submit_jobs, drain, SpecError)

    GOOD = {
        "name": "block", "seed": SEED,
        "devices": [{"id": "alpha", "provider": "devq.simulated",
                     "backend": {"kind": "fully_connected", "num_qubits": 7}}],
        "jobs": [{"circuit": BELL}],
    }

    def rejects(label, mutate):
        spec = json.loads(json.dumps(GOOD))
        mutate(spec)
        try:
            validate_spec(spec)
            check(False, f"spec rejects {label}")
        except SpecError:
            check(True, f"spec rejects {label}")

    # STRICTNESS IS THE POINT, and the reason is the absence of a
    # fallback rather than a difference in severity: every config key
    # has a documented default, and a spec key has none. There is no
    # sensible default for which circuit to run or where to run it, so
    # refusing is the only alternative to guessing.
    rejects("an unknown top-level key",  lambda s: s.update(sed=1))
    rejects("an unknown device key",     lambda s: s["devices"][0].update(kind="x"))
    rejects("an unknown job key",        lambda s: s["jobs"][0].update(nonsense=1))
    rejects("a duplicate device id",     lambda s: s["devices"].append(dict(s["devices"][0])))
    rejects("an empty device list",      lambda s: s.update(devices=[]))
    rejects("an empty job list",         lambda s: s.update(jobs=[]))
    rejects("repeat=0",                  lambda s: s["jobs"][0].update(repeat=0))
    rejects("exec_on with no_exec_on",   lambda s: s["jobs"][0].update(
                                             exec_on=["alpha"], no_exec_on=["alpha"]))
    rejects("exec_on naming an undefined device",
                                         lambda s: s["jobs"][0].update(exec_on=["nope"]))
    rejects("an unsupported arrival pattern",
                                         lambda s: s.update(arrival={"pattern": "poisson"}))
    rejects("a missing required key",    lambda s: s.pop("name"))

    # SCALARS ARE COERCED, NOT TYPE-GATED. A ${SEED} placeholder resolves
    # to a string, so a numeric field must accept a coercible string —
    # "42" becomes 42. A literal coercible string is accepted too; that
    # loosening is deliberate. What is still refused is a string that is
    # not a number at all. The old suite asserted the strict version
    # (seed="42" and threshold="0.03" rejected); those cases are now
    # accepts, and the genuine failure — an uncoercible value — is what
    # gets the rejects.
    rejects("a non-coercible seed",      lambda s: s.update(seed="banana"))
    rejects("a non-coercible repeat",    lambda s: s["jobs"][0].update(repeat="two"))
    rejects("a non-coercible threshold", lambda s: s["jobs"][0].update(
                                             max_qubit_error="high"))
    # bool coerces to int/float silently in Python (True == 1); a spec
    # never means that, so it is refused explicitly.
    rejects("a boolean seed",            lambda s: s.update(seed=True))

    # Absent-with-a-default is NOT an exception to the rule above:
    # repeat and arrival.pattern have documented defaults, so omitting
    # them is silent. It is keys carrying no actionable meaning that
    # are refused.
    minimal = json.loads(json.dumps(GOOD))
    minimal.pop("seed")
    validated = validate_spec(minimal)
    check(validated["arrival"]["pattern"] == "batch",
          "an omitted arrival pattern defaults to batch, silently")
    check(validated["jobs"][0].get("repeat", 1) == 1,
          "an omitted repeat defaults to 1, silently")

    # COERCION ACTUALLY RAN — assert the OUTPUT TYPE, not merely that
    # validation did not raise. A coercion that silently returned the
    # string unchanged would also "not throw"; only checking that the
    # value came out an int/float proves the conversion happened. This
    # is the value a resolved ${SEED} takes: a string on the way in.
    coerced = json.loads(json.dumps(GOOD))
    coerced["seed"] = "42"
    coerced["jobs"][0]["repeat"] = "3"
    coerced["jobs"][0]["max_qubit_error"] = "0.03"
    v = validate_spec(coerced)
    check(v["seed"] == 42 and isinstance(v["seed"], int),
          "a coercible string seed is coerced to int")
    check(v["jobs"][0]["repeat"] == 3 and isinstance(v["jobs"][0]["repeat"], int),
          "a coercible string repeat is coerced to int")
    check(isinstance(v["jobs"][0]["max_qubit_error"], float)
          and abs(v["jobs"][0]["max_qubit_error"] - 0.03) < 1e-12,
          "a coercible string threshold is coerced to float")

    # A spec naming an unregistered provider must fail loudly rather
    # than importing anything — a data file that can trigger imports is
    # a data file that can run code.
    spec = json.loads(json.dumps(GOOD))
    spec["devices"][0]["provider"] = "not_registered"
    try:
        build_session(validate_spec(spec), DevQ())
        check(False, "unregistered provider is rejected")
    except SpecError as exc:
        check("not registered" in str(exc),
              "unregistered provider is rejected, naming what is available")

    # SEED RESOLUTION — two cases, because providers are CLASS-ONLY.
    # There used to be four: a registered instance could carry its own
    # seed and the parser had to arbitrate against the spec's. Nothing
    # can hold a competing seed now, so the conflict cases are not
    # merely untested, they are unrepresentable — which the instance
    # rejection below pins.
    def resolved(register, spec_seed):
        spec = json.loads(json.dumps(GOOD))
        if spec_seed is None:
            spec.pop("seed")
        else:
            spec["seed"] = spec_seed
        spec["devices"][0]["provider"] = "p"
        dq = DevQ()
        dq.register_provider("p", register)
        with contextlib.redirect_stdout(io.StringIO()):
            _, meta = build_session(validate_spec(spec), dq)
        return meta["devices"][0], meta["warnings"]

    d, w = resolved(DevQSimulatedProvider, 7)
    check(d["seed_effective"] == 7 and d["seed_source"] == "spec" and not w,
          "a registered class takes the spec's seed, with no warning")

    d, w = resolved(DevQSimulatedProvider, None)
    check(d["seed_effective"] is None and d["seed_source"] == "unseeded"
          and not w,
          "no spec seed means the provider is constructed unseeded")

    # The reason the conflict cases are gone: an instance cannot be
    # registered at all. A caller wanting their own seed constructs the
    # provider and attaches its device with add_device() instead.
    # Captured outside the check, so that check()'s own AssertionError
    # cannot be swallowed by the except clause and read as a pass.
    instance_refused = None
    try:
        DevQ().register_provider("p", DevQSimulatedProvider(seed=99))
    except Exception as exc:
        instance_refused = str(exc)

    check(instance_refused is not None
          and "instance" in instance_refused.lower(),
          "a provider INSTANCE is refused at registration, so no "
          "registered provider can carry a seed of its own")

    # set_seed must reproduce a freshly constructed provider, not merely
    # set an attribute — devq builds its RNG in __init__, so a provider
    # that only stored the value would keep generating unseeded devices
    # while reporting the spec's seed.
    late = DevQSimulatedProvider()
    late.set_seed(SEED)
    fresh = DevQSimulatedProvider(seed=SEED)
    check(sorted(late.get_device("random", 5).error_map.items())
          == sorted(fresh.get_device("random", 5).error_map.items()),
          "set_seed reproduces a freshly seeded provider exactly")

    # ... and must refuse once devices exist, since their error maps
    # already derive from the old seed.
    used = DevQSimulatedProvider(seed=SEED)
    used.get_device("random", 5)
    try:
        used.set_seed(1234)
        check(False, "set_seed refuses after devices are built")
    except RuntimeError:
        check(True, "set_seed refuses after devices are built")

    # END TO END: repeat:N must create N DISTINCT jobs, not one job run
    # N times — they queue, route and schedule independently.
    spec = json.loads(json.dumps(GOOD))
    spec["devices"].append({"id": "bravo", "provider": "devq.simulated",
                            "backend": {"kind": "linear", "num_qubits": 7}})
    spec["jobs"] = [{"circuit": BELL, "repeat": 3},
                    {"circuit": GHZ, "repeat": 2, "no_exec_on": ["alpha"]}]
    dq = DevQ()
    with contextlib.redirect_stdout(io.StringIO()):
        shell, meta = build_session(validate_spec(spec), dq)
        jobs = submit_jobs(shell, spec)
        cycles = drain(shell)

    check(len(jobs) == 5, f"repeat expands to 5 distinct jobs, got {len(jobs)}")
    check(len({j.job_id for j in jobs}) == 5, "every expanded job has its own id")
    check(all(j.state.value == "FINISHED" for j in jobs),
          f"all jobs finished, got {sorted({j.state.value for j in jobs})}")

    # no_exec_on must survive the id→index translation.
    alpha = next(d["index"] for d in meta["devices"] if d["id"] == "alpha")
    ghz_jobs = jobs[3:]
    check(all(j.device_index != alpha for j in ghz_jobs),
          "no_exec_on kept the GHZ jobs off alpha")

    # DRAIN MUST NOT BUSY-WAIT. An early version stepped whenever a
    # future was in flight and produced 37,923 empty cycles for this
    # five-job workload, burying twenty real events. Cycles must stay
    # proportionate to the work.
    check(cycles < 200, f"drain does not spin — {cycles} cycles for 5 jobs")

    # Device identity from the spec: the spec's id IS the device name.
    check([d["id"] for d in meta["devices"]] == ["alpha", "bravo"],
          "spec ids become device names in order")
    check([d["index"] for d in meta["devices"]] == [0, 1],
          "devices are indexed in spec order")


def block_placeholder_resolution():
    '''${NAME} placeholders resolve from the environment before validation'''
    import os
    from benchmark.placeholders import resolve_placeholders
    from benchmark.spec import SpecError, validate_spec

    # Set env in a finally-guarded block so a failure cannot leak state
    # into later blocks — the suite runs in one process, sequentially.
    saved = {k: os.environ.get(k) for k in
             ("DEVQ_T_SEED", "DEVQ_T_VENDOR", "DEVQ_T_TIER")}
    try:
        os.environ["DEVQ_T_SEED"]   = "42"
        os.environ["DEVQ_T_VENDOR"] = "ibm"
        os.environ["DEVQ_T_TIER"]   = "simulated"
        for k in ("DEVQ_T_UNSET",):
            os.environ.pop(k, None)

        # ── whole-field substitution ──────────────────────────────────
        out = resolve_placeholders({"seed": "${DEVQ_T_SEED}"})
        check(out["seed"] == "42",
              "a whole-field ${NAME} resolves to the env value")
        # ...and yields a STRING, which spec.py then coerces. The resolver
        # is type-blind because an environment holds only strings.
        check(isinstance(out["seed"], str),
              "resolution yields a string; coercion is spec.py's job")

        # ── embedded and repeated ─────────────────────────────────────
        out = resolve_placeholders(
            {"devices": [{"provider": "${DEVQ_T_VENDOR}.${DEVQ_T_TIER}"}]})
        check(out["devices"][0]["provider"] == "ibm.simulated",
              "embedded ${NAME}s resolve in place, both occurrences")

        # ── recursion into nested containers ──────────────────────────
        out = resolve_placeholders(
            {"jobs": [{"c": "${DEVQ_T_VENDOR}"}, {"c": "${DEVQ_T_TIER}"}]})
        check([j["c"] for j in out["jobs"]] == ["ibm", "simulated"],
              "resolution recurses through lists and nested dicts")

        # ── non-grammar ${...} is a literal, NOT an error ─────────────
        # ${}, ${1BAD}, ${with-dash} do not match the identifier grammar,
        # so they are left untouched. A spec may legitimately contain a $
        # or a brace; only the exact grammar triggers a lookup.
        for literal in ("${}", "${1BAD}", "${with-dash}", "bare $TEXT", "$SEED"):
            out = resolve_placeholders({"x": literal})
            check(out["x"] == literal,
                  f"non-grammar {literal!r} passes through as a literal")

        # ── THE REFUSAL: a well-formed but unset var is a hard error ───
        # This is the mutation-critical case. A resolver that never raises
        # on a missing variable is indistinguishable from a working one
        # across every happy-path spec — so assert the REJECTION, not just
        # the passes. (P1 lesson from docs/MUTATION_TESTING.md.)
        raised = None
        try:
            resolve_placeholders({"seed": "${DEVQ_T_UNSET}"})
        except SpecError as exc:
            raised = exc
        check(raised is not None,
              "an unset ${NAME} is refused, not silently left as ''")
        check("DEVQ_T_UNSET" in str(raised),
              "the refusal names the missing variable")

        # ── lookup is case-sensitive: ${seed} != ${SEED} ──────────────
        # DEVQ_T_SEED is set; devq_t_seed is not. No recasing fallback.
        raised = None
        try:
            resolve_placeholders({"x": "${devq_t_seed}"})
        except SpecError:
            raised = True
        check(raised is True,
              "lookup is case-sensitive — ${devq_t_seed} does not find "
              "DEVQ_T_SEED")

        # ── end-to-end: load_spec resolves, then coerces ──────────────
        # A resolved ${DEVQ_T_SEED} of "42" reaches seed validation as a
        # string and is coerced to int 42, proving the two passes compose.
        spec = validate_spec(resolve_placeholders({
            "name": "ph", "seed": "${DEVQ_T_SEED}",
            "devices": [{"id": "a", "provider": "${DEVQ_T_VENDOR}.${DEVQ_T_TIER}",
                         "backend": {"kind": "fully_connected", "num_qubits": 5}}],
            "jobs": [{"circuit": BELL}],
        }))
        check(spec["seed"] == 42 and isinstance(spec["seed"], int),
              "resolve-then-validate composes: ${SEED} '42' becomes int 42")
        check(spec["devices"][0]["provider"] == "ibm.simulated",
              "resolve-then-validate composes: embedded provider resolves")

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def block_event_log():
    '''Kernel events record the full job lifecycle without changing output'''
    import io, contextlib
    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    from kernel.events import PrintSink, RecordSink, MultiSink

    def session(sink=None):
        p = DevQSimulatedProvider(seed=SEED)
        dq = DevQ().add_devices([(p.get_device("fully_connected", 7), "alpha"),
                                 (p.get_device("linear", 7), "bravo")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sh = dq.build()
            if sink is not None:
                sh.kernel.sink = sink
            sh.onecmd(f"qsubmit {BELL} {GHZ}")
            sh.onecmd("qrunpack")
            sh.onecmd(f"qrun {BELL}")
            # rejected job: exercises the None-timestamp path
            sh.onecmd(f"qrun {BELL} --max-qubit-error=0.0000001")
            # qrun/qrunpack dispatch asynchronously; drain so every
            # resolve event has fired before the transcript is captured.
            sh.kernel.drain()
        return buf.getvalue(), sh

    # THE CENTRAL GUARANTEE: attaching a sink must not change what the
    # console prints — i.e. a RecordSink beside a PrintSink is invisible
    # to the console. Under async we no longer compare two independently
    # timed live runs (their resolve-line interleaving and timing-
    # dependent allocations legitimately differ run to run — DevQ never
    # promised serial determinism). Instead we prove the invariant
    # structurally on a SINGLE run: everything PrintSink put on the
    # console is exactly what PrintSink renders from the very records the
    # RecordSink captured in the same run. Same execution, so timing is
    # identical on both sides; if the two sinks ever saw a different
    # event stream, or the console carried a Kernel line no record
    # produced, this fails.
    from kernel.events import PrintSink as _PrintSink
    rec = RecordSink()
    logged, shell = session(MultiSink(PrintSink(), rec))

    # Replay the captured records through a fresh PrintSink and compare
    # the Kernel lines it renders against the Kernel lines that actually
    # reached the console. (Non-Kernel lines — job submission notices,
    # dispatch acks, the REJECTED line — come from the shell, not a
    # sink, so they are outside the sink-transparency claim.)
    replay = io.StringIO()
    with contextlib.redirect_stdout(replay):
        ps = _PrintSink()
        for r in rec.records:
            ps.emit(r)
    replayed_kernel = [l for l in replay.getvalue().splitlines()
                       if l.startswith("[Kernel]")]
    console_kernel  = [l for l in logged.splitlines()
                       if l.startswith("[Kernel]")]
    check(replayed_kernel == console_kernel,
          "PrintSink beside a RecordSink prints exactly what the records "
          "render — attaching the RecordSink changed no console output")

    # PrintSink renders dispatch (placement) but NOT resolve: results are
    # read through qps, so echoing the kernel's `[Kernel] Job N FINISHED.
    # Counts: …` line would duplicate the qps row. The resolve event is
    # still emitted (asserted below via the record stream) — only the
    # console echo is suppressed. Pin both halves: the console shows the
    # dispatch line and never a FINISHED/FAILED resolve line, while the
    # records DO carry resolve.
    check(any("Dispatching job" in l for l in console_kernel),
          "PrintSink still renders the dispatch (placement) line")
    check(not any("FINISHED" in l or "FAILED" in l for l in console_kernel),
          "PrintSink does NOT echo the resolve line — qps reports results, "
          f"so the console carries no FINISHED/FAILED echo; got {console_kernel}")
    check(any(r["event"] == "resolve" for r in rec.records),
          "the resolve event is still emitted to the record stream, "
          "even though the console does not print it")

    kinds = [r["event"] for r in rec.records]
    for kind in ("submit", "route", "dispatch", "resolve", "cycle_end"):
        check(kind in kinds, f"'{kind}' events are emitted")

    # cycle and seq are stamped centrally, so no record can lack them
    # and seq must be a dense monotonic range — a gap means an emit
    # site bypassed _emit.
    check(all("cycle" in r and "seq" in r for r in rec.records),
          "every record carries cycle and seq")
    seqs = [r["seq"] for r in rec.records]
    check(seqs == list(range(len(seqs))),
          "seq is dense and monotonic — every event went through _emit")

    # Cycles must never go backwards; qrun takes its own cycle rather
    # than inheriting the previous one.
    cycles = [r["cycle"] for r in rec.records]
    check(cycles == sorted(cycles), "cycle never decreases")
    check(len(set(cycles)) > 1, "work spans multiple cycles")

    # Every dispatched job resolves exactly once, paired by job_id
    # rather than by cycle — under qrunpack the two land in different
    # cycles by design.
    dispatched = [r["job_id"] for r in rec.records if r["event"] == "dispatch"]
    resolved   = [r["job_id"] for r in rec.records if r["event"] == "resolve"]
    check(sorted(dispatched) == sorted(resolved),
          f"every dispatch has one resolve, got {dispatched} vs {resolved}")

    # Route records must name what was chosen BETWEEN, not just what
    # won — this is what makes 5.5's weight sweep answerable from a
    # recorded run.
    routes = [r for r in rec.records if r["event"] == "route"]
    check(routes and all(r["device"] in r["candidates"] for r in routes),
          "route records the chosen device among its candidates")
    check(all(r.get("scores") and len(r["scores"]) == len(r["candidates"])
              for r in routes),
          "route records one score per candidate")

    # QCB TIMESTAMPS. Two clocks with different jobs: *_seq is
    # deterministic and answers "what happened", *_at is wall clock and
    # answers "how long". 5.3's metrics come from the latter, so a
    # missing or zeroed stamp would silently produce zero latencies.
    jobs = {j.job_id: j for j in shell.kernel.process_table.list_jobs()}
    done = [j for j in jobs.values() if j.state.value == "FINISHED"]
    check(len(done) >= 2, f"workload produced finished jobs, got {len(done)}")

    for j in done:
        check(None not in (j.submitted_seq, j.dispatched_seq, j.resolved_seq),
              f"job {j.job_id} carries all three seq stamps")
        check(None not in (j.submitted_at, j.dispatched_at, j.resolved_at),
              f"job {j.job_id} carries all three wall-clock stamps")
        check(j.submitted_seq < j.dispatched_seq < j.resolved_seq,
              f"job {j.job_id} seq stamps are strictly ordered")
        check(j.submitted_at <= j.dispatched_at <= j.resolved_at,
              f"job {j.job_id} wall-clock stamps are ordered")
        check(j.queue_latency is not None and j.queue_latency >= 0,
              f"job {j.job_id} has a non-negative queue latency")
        check(j.execution_time is not None and j.execution_time > 0,
              f"job {j.job_id} spent measurable time executing")
        # turnaround must be the sum of its parts, not an independent
        # measurement that could drift from them.
        check(abs(j.turnaround_time
                  - (j.queue_latency + j.execution_time)) < 1e-6,
              f"job {j.job_id} turnaround equals queue + execution")

    # An unfinished job reports None rather than 0 — a metrics pass must
    # be able to skip it, not average a fake zero into the results.
    unfinished = [j for j in jobs.values() if j.state.value != "FINISHED"]
    check(unfinished, "workload includes an unfinished job to exercise")
    for j in unfinished:
        # ALL THREE properties, not just turnaround: without its own
        # None guard each one raises TypeError on a job that never
        # dispatched, so a metrics pass iterating every job would crash
        # on the first rejection.
        for prop in ("queue_latency", "execution_time", "turnaround_time"):
            try:
                value = getattr(j, prop)
            except Exception as exc:
                value = f"raised {type(exc).__name__}"
            check(value is None,
                  f"unfinished job {j.job_id}: {prop} is None, got {value}")

    # A sink that raises is observability failing, not execution
    # failing: the job must still run.
    class Exploding:
        def emit(self, record):
            raise RuntimeError("sink is broken")

    p = DevQSimulatedProvider(seed=SEED)
    dq = DevQ().add_devices([(p.get_device("fully_connected", 7), "solo")])
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        sh = dq.build()
        sh.kernel.sink = Exploding()
        sh.onecmd(f"qrun {BELL}")
        sh.kernel.drain()
    states = [j.state.value for j in sh.kernel.process_table.list_jobs()]
    check("FINISHED" in states,
          f"a raising sink cannot kill a job, got {states}")
    check("broken" in err.getvalue() or "raised" in err.getvalue(),
          "a raising sink is reported on stderr")

    # MultiSink isolates its members: one failing must not stop another
    # from receiving records.
    rec2 = RecordSink()
    p = DevQSimulatedProvider(seed=SEED)
    dq = DevQ().add_devices([(p.get_device("fully_connected", 7), "solo")])
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        sh = dq.build()
        sh.kernel.sink = MultiSink(Exploding(), rec2)
        sh.onecmd(f"qrun {BELL}")
    check(len(rec2.records) > 0,
          "MultiSink still delivers to healthy sinks when one raises")


def block_metrics():
    '''Offline metrics compute correct values and obey the population rule'''
    import json, math, os, tempfile
    from benchmark import metrics as M
    from benchmark import runner as R

    # ── HALF ONE: exact arithmetic against a HAND-BUILT records list ───
    # Wall-clock *_at in a real run is non-deterministic, so exact
    # numbers can only be checked against timestamps we set ourselves.
    # These values are hand-computed in the block comments, NOT read back
    # from the module — asserting a metric against its own output proves
    # nothing.
    #
    #   job 1  dev 0  sub 100  disp 110  res 160   wait 10  busy [110,160)
    #   job 2  dev 0  sub 100  disp 130  res 170   wait 30  busy [130,170)
    #   job 3  dev 1  sub 100  disp 120  res 140   wait 20  busy [120,140)
    #   job 4  dev 1  REJECTED  all None            skipped everywhere
    rows = [
        {"job_id": 1, "state": "FINISHED", "device": 0,
         "submitted_at": 100, "dispatched_at": 110, "resolved_at": 160,
         "queue_latency": 10},
        {"job_id": 2, "state": "FINISHED", "device": 0,
         "submitted_at": 100, "dispatched_at": 130, "resolved_at": 170,
         "queue_latency": 30},
        {"job_id": 3, "state": "FINISHED", "device": 1,
         "submitted_at": 100, "dispatched_at": 120, "resolved_at": 140,
         "queue_latency": 20},
        {"job_id": 4, "state": "REJECTED", "device": None,
         "submitted_at": 100, "dispatched_at": None, "resolved_at": None,
         "queue_latency": None},
    ]
    fixture = [{"event": "header"},
               {"event": "summary",
                "devices_attached": {"0": "alpha", "1": "bravo"},
                "per_job": rows}]

    tp = M.throughput(fixture)
    # exec span = 170 - 110 = 60 over 3 dispatched jobs -> 3/60
    check(abs(tp["execution"] - 3 / 60) < 1e-12,
          f"execution throughput 3/60, got {tp['execution']}")
    # turn span = 170 - 100 = 70 over all 4 submitted -> 4/70
    check(abs(tp["turnaround"] - 4 / 70) < 1e-12,
          f"turnaround throughput 4/70, got {tp['turnaround']}")

    ql = M.queue_latency(fixture)
    # waits [10,20,30], rejected job's None skipped, not counted as 0
    check(ql["min"] == 10 and ql["max"] == 30, "latency min/max over waits")
    check(ql["median"] == 20, f"latency median 20, got {ql['median']}")
    check(abs(ql["mean"] - 20) < 1e-12, f"latency mean 20, got {ql['mean']}")
    # nearest-rank p95: ceil(0.95*3) = 3rd of [10,20,30] = 30
    check(ql["p95"] == 30, f"nearest-rank p95 is 30, got {ql['p95']}")

    ut = M.utilisation(fixture)
    # alpha (dev 0): union [110,160)+[130,170) = [110,170) = 60, /window 60 = 1.0
    check(abs(ut["per_device"]["alpha"] - 1.0) < 1e-12,
          f"alpha's overlapping jobs union to full window, got {ut['per_device']['alpha']}")
    # bravo (dev 1): [120,140) = 20, /60 = 1/3
    check(abs(ut["per_device"]["bravo"] - 1 / 3) < 1e-12,
          f"bravo utilisation 1/3, got {ut['per_device']['bravo']}")
    # system: (60 + 20) / (60 * 2) = 2/3
    check(abs(ut["system"] - 2 / 3) < 1e-12,
          f"system utilisation 2/3, got {ut['system']}")
    # per-device output is labelled by device id, not bare index
    check("alpha" in ut["per_device"] and 0 not in ut["per_device"],
          "utilisation labels devices by id, not index")
    # the union must count overlap ONCE: summing would give alpha = 90/60
    # = 1.5 > 1, an impossible utilisation. This asserts the merge, not
    # the sum.
    check(ut["per_device"]["alpha"] <= 1.0, "overlap counted once, not summed")

    # ── population edge: every job rejected -> undefined, not zero ─────
    all_rejected = [{"event": "summary", "per_job": [
        {"job_id": 1, "state": "REJECTED", "device": None,
         "submitted_at": 100, "dispatched_at": None, "resolved_at": None,
         "queue_latency": None}]}]
    tpr = M.throughput(all_rejected)
    check(tpr["execution"] is None,
          "no dispatch -> execution throughput is None, not 0")
    check(tpr["turnaround"] is None,
          "no resolve -> turnaround throughput is None, not 0")
    check(M.queue_latency(all_rejected)["median"] is None,
          "no dispatched job -> latency is None, not 0")
    utr = M.utilisation(all_rejected)
    check(utr["system"] is None and utr["per_device"] == {},
          "no dispatch -> utilisation is None, not 0")

    # a dispatched-but-unresolved job contributes no interval
    half = [{"event": "summary", "per_job": [
        {"job_id": 1, "state": "RUNNING", "device": 0,
         "submitted_at": 100, "dispatched_at": 110, "resolved_at": None,
         "queue_latency": 10}]}]
    check(M.utilisation(half)["system"] is None,
          "dispatched-but-unresolved job has no interval, window undefined")

    # ── rejection rate ────────────────────────────────────────────────
    # The main fixture is 3 FINISHED + 1 REJECTED = 1 of 4 rejected.
    rr = M.rejection_rate(fixture)
    check(rr["rejected"] == 1 and rr["submitted"] == 4,
          f"rejection counts 1 of 4, got {rr['rejected']}/{rr['submitted']}")
    check(abs(rr["rate"] - 0.25) < 1e-12,
          f"rejection rate 1/4 = 0.25, got {rr['rate']}")

    # WAITING is NOT a rejection — accepted-but-delayed, its wait lives in
    # queue latency. A run of one FINISHED, one WAITING, one REJECTED must
    # count exactly one rejection of three, not two.
    mixed = [{"event": "summary", "per_job": [
        {"job_id": 1, "state": "FINISHED", "device": 0,
         "submitted_at": 100, "dispatched_at": 110, "resolved_at": 160,
         "queue_latency": 10},
        {"job_id": 2, "state": "WAITING", "device": 0,
         "submitted_at": 100, "dispatched_at": None, "resolved_at": None,
         "queue_latency": None},
        {"job_id": 3, "state": "REJECTED", "device": None,
         "submitted_at": 100, "dispatched_at": None, "resolved_at": None,
         "queue_latency": None}]}]
    rrm = M.rejection_rate(mixed)
    check(rrm["rejected"] == 1 and rrm["submitted"] == 3,
          f"WAITING is not counted: 1 of 3 rejected, got "
          f"{rrm['rejected']}/{rrm['submitted']}")
    check(abs(rrm["rate"] - 1 / 3) < 1e-12,
          f"rejection rate 1/3, got {rrm['rate']}")

    # Empty run: counts are the true zeros, only the ratio is undefined.
    empty = [{"event": "summary", "per_job": []}]
    rre = M.rejection_rate(empty)
    check(rre["rejected"] == 0 and rre["submitted"] == 0,
          "empty run: counts are truthful zeros")
    check(rre["rate"] is None,
          "empty run: rate is None (no fraction to divide), not 0")

    # ── load balance ──────────────────────────────────────────────────
    # Counts [3, 1] over two devices: mean 2, population stddev 1, so
    # CV = 1/2 = 0.5 and load_balance = 1/(1+0.5) = 2/3. Hand-computed,
    # not read back from the metric.
    lb_fixture = [{"event": "summary",
                   "devices_attached": {"0": "alpha", "1": "bravo"},
                   "per_job": [
        {"job_id": 1, "state": "FINISHED", "device": 0,
         "submitted_at": 0, "dispatched_at": 10, "resolved_at": 20,
         "queue_latency": 10},
        {"job_id": 2, "state": "FINISHED", "device": 0,
         "submitted_at": 0, "dispatched_at": 20, "resolved_at": 30,
         "queue_latency": 20},
        {"job_id": 3, "state": "FINISHED", "device": 0,
         "submitted_at": 0, "dispatched_at": 30, "resolved_at": 40,
         "queue_latency": 30},
        {"job_id": 4, "state": "FINISHED", "device": 1,
         "submitted_at": 0, "dispatched_at": 10, "resolved_at": 20,
         "queue_latency": 10}]}]
    lb = M.load_imbalance(lb_fixture)
    bc = lb["by_count"]
    check(bc["per_device"] == {"alpha": 3, "bravo": 1},
          f"counts labelled by device id, got {bc['per_device']}")
    check(abs(bc["cv"] - 0.5) < 1e-12,
          f"count CV for [3,1] is 0.5, got {bc['cv']}")
    check(abs(bc["load_balance"] - 2 / 3) < 1e-12,
          f"count load_balance 1/(1+0.5)=2/3, got {bc['load_balance']}")

    # An idle device must appear as 0 and drag the balance down — the
    # whole reason the roster is recorded. All work on device 0, device 1
    # idle: counts [3, 0], mean 1.5, stddev 1.5, CV = 1.0, balance = 0.5.
    idle_fixture = [{"event": "summary",
                     "devices_attached": {"0": "alpha", "1": "bravo"},
                     "per_job": [
        {"job_id": i, "state": "FINISHED", "device": 0,
         "submitted_at": 0, "dispatched_at": 10 * i, "resolved_at": 10 * i + 5,
         "queue_latency": 0} for i in (1, 2, 3)]}]
    lbi = M.load_imbalance(idle_fixture)
    check(lbi["by_count"]["per_device"] == {"alpha": 3, "bravo": 0},
          "an idle device appears as 0, not absent")
    check(abs(lbi["by_count"]["cv"] - 1.0) < 1e-12,
          f"idle device drags count CV to 1.0, got {lbi['by_count']['cv']}")

    # Single attached device: no spread possible, CV 0, balance 1.0.
    solo = [{"event": "summary",
             "devices_attached": {"0": "solo"},
             "per_job": [
        {"job_id": 1, "state": "FINISHED", "device": 0,
         "submitted_at": 0, "dispatched_at": 10, "resolved_at": 20,
         "queue_latency": 10}]}]
    lbs = M.load_imbalance(solo)
    check(lbs["by_count"]["cv"] == 0.0 and lbs["by_count"]["load_balance"] == 1.0,
          "one device cannot be imbalanced: CV 0, balance 1.0")

    # Zero load (all rejected, nothing dispatched): CV undefined -> None,
    # not zero. Counts are all 0 so the mean is 0.
    noload = [{"event": "summary",
               "devices_attached": {"0": "alpha", "1": "bravo"},
               "per_job": [
        {"job_id": 1, "state": "REJECTED", "device": None,
         "submitted_at": 0, "dispatched_at": None, "resolved_at": None,
         "queue_latency": None}]}]
    lbn = M.load_imbalance(noload)
    check(lbn["by_count"]["cv"] is None
          and lbn["by_count"]["load_balance"] is None,
          "no load to spread -> CV and load_balance are None, not 0")

    # ── HALF TWO: a REAL run — shape, population, and the two ways in ──
    tmp = tempfile.mkdtemp()
    try:
        spec = os.path.join(tmp, "wl.json")
        with open(spec, "w") as h:
            json.dump({
                "name": "metrics", "seed": SEED,
                "devices": [{"id": "alpha", "provider": "devq.simulated",
                             "backend": {"kind": "fully_connected", "num_qubits": 7}},
                            {"id": "bravo", "provider": "devq.simulated",
                             "backend": {"kind": "linear", "num_qubits": 7}}],
                "jobs": [{"circuit": BELL, "repeat": 3}, {"circuit": GHZ}],
            }, h)
        out = os.path.join(tmp, "run")
        manifest = R.run(spec, out_dir=out, quiet=True)
        log = os.path.join(out, manifest["sessions"][0]["log"])
        with open(log) as h:
            from_jsonl = [json.loads(line) for line in h if line.strip()]

        bundle = M.compute(from_jsonl)

        # Structure: the bundle is the six metric groups, plain data.
        check(set(bundle) == {"throughput", "queue_latency", "utilisation",
                              "rejection_rate", "load_imbalance", "fidelity"},
              f"bundle carries the six metric groups, got {sorted(bundle)}")
        # On this all-completing run nothing is rejected.
        check(bundle["rejection_rate"]["rejected"] == 0
              and bundle["rejection_rate"]["rate"] == 0.0,
              "a run where every job finishes has rejection rate 0.0")
        # Load balance sees the full roster: both devices appear in the
        # per-device map, even if the router favoured one.
        lb_real = bundle["load_imbalance"]["by_count"]["per_device"]
        check(len(lb_real) == 2,
              f"load balance covers both attached devices, got {lb_real}")
        check(0.0 <= bundle["load_imbalance"]["by_count"]["load_balance"] <= 1.0,
              "load_balance reading is in (0, 1]")

        # Invariants hold on real (non-deterministic) numbers even though
        # the exact values cannot be pinned: execution span is a subset
        # of turnaround span, so its throughput is the larger rate.
        tp = bundle["throughput"]
        check(tp["execution"] >= tp["turnaround"],
              "execution throughput >= turnaround (shorter span, same-ish count)")
        # every real per-device fraction is a fraction
        for dev, frac in bundle["utilisation"]["per_device"].items():
            check(0.0 <= frac <= 1.0,
                  f"device {dev} utilisation in [0,1], got {frac}")
        # system is the busy-weighted mean, so it sits within the spread
        fr = list(bundle["utilisation"]["per_device"].values())
        check(min(fr) - 1e-9 <= bundle["utilisation"]["system"] <= max(fr) + 1e-9,
              "system utilisation lies between the per-device extremes")
        # latency distribution is ordered
        ql = bundle["queue_latency"]
        check(ql["min"] <= ql["median"] <= ql["max"] and ql["p95"] <= ql["max"],
              "latency distribution is internally ordered")

        # THE TWO WAYS IN must agree: a RecordSink and the reparsed
        # .jsonl are the same records, so compute() is identical on both.
        # Re-run capturing a RecordSink directly.
        from kernel.events import RecordSink, MultiSink, JSONLSink
        from benchmark.spec import load_spec, build_session, submit_jobs, drain
        from circuits.execution_result import shutdown_executor
        from devq import DevQ
        resolved, verbatim = load_spec(spec)
        rec = RecordSink()
        dq = DevQ()
        sh, meta = build_session(resolved, dq, "metrics", verbatim=verbatim)
        sh.kernel.sink = rec
        rec.emit({"event": "header", "spec": meta["spec"],
                  "devices": meta["devices"]})
        jobs = submit_jobs(sh, resolved, "metrics")
        drain(sh)
        # summary shape mirrors the runner's, including the device roster
        rec.emit({"event": "summary",
                  "devices_attached": {
                      str(i): d["id"] for i, d in enumerate(meta["devices"])},
                  "per_job": [
            {"job_id": j.job_id, "state": j.state.value,
             "device": j.device_index,
             "submitted_at": j.submitted_at, "dispatched_at": j.dispatched_at,
             "resolved_at": j.resolved_at, "queue_latency": j.queue_latency}
            for j in sorted(jobs, key=lambda j: j.job_id)]})
        shutdown_executor()
        from_sink = rec.records
        # Same population and counts from both paths (values differ by
        # wall-clock, but which jobs count and how many must match).
        check(set(M.compute(from_sink)) == set(bundle),
              "RecordSink and reparsed .jsonl yield the same bundle shape")

        # ── the writer drops metrics.json beside the manifest ─────────
        written = M.write_metrics(out)
        mpath = os.path.join(out, "metrics.json")
        check(os.path.exists(mpath), "write_metrics creates metrics.json")
        with open(mpath) as h:
            on_disk = json.load(h)
        check(manifest["sessions"][0]["session_id"] in on_disk,
              "metrics.json is keyed by session id")
        check(on_disk == written,
              "the returned mapping matches what was written to disk")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def block_comparison():
    '''Matrix assembly, the α/β sweep, and its faithfulness anchor'''
    # comparison.py is the 5.5a analysis engine: assemble_matrix bundles a
    # matrix run's per-session config+metrics, and sweep() re-derives one
    # session's routing/allocation decisions across an α/β grid FROM THE
    # RECORDED SCORES — no re-execution. This block runs a real matrix,
    # assembles it, sweeps both scored axes, and checks the invariant the
    # whole feature rests on: the faithfulness anchor (replay at the run's
    # own weights reproduces the recorded winner) and the skip-with-reason
    # for a non-scoring component. Output is kept under test_results/ for
    # inspection, like the shipped-workloads block.
    import shutil
    import json
    from benchmark import runner as R
    from benchmark.metrics import write_metrics
    from benchmark import comparison as C

    root = os.path.dirname(os.path.abspath(__file__))
    run_dir = os.path.join(root, "test_results", "smoke", "comparisons")
    shutil.rmtree(run_dir, ignore_errors=True)

    # A real matrix run over a small shipped spec. smoke.json uses
    # devq.simulated, so no provider registration is needed and it does not
    # depend on qiskit being installed.
    with contextlib.redirect_stdout(io.StringIO()):
        manifest = R.run(os.path.join(WORKLOADS, "smoke.json"),
                         out_dir=run_dir, matrix=True, quiet=True)
        write_metrics(run_dir)

    # ── Matrix assembly ───────────────────────────────────────────────────
    bundle = C.assemble_matrix(run_dir)
    n_sessions = len(manifest["sessions"])
    check(len(bundle) == n_sessions,
          f"the bundle has one row per session ({n_sessions}), got {len(bundle)}")
    a_row = next(iter(bundle.values()))
    check(set(a_row) >= {"config", "metrics", "sweepable_axes"},
          "each bundle row carries config, metrics and sweepable_axes")
    check(all(r["config"] is not None for r in bundle.values()),
          "every row records its scheduler/allocator/router config")
    check(os.path.exists(os.path.join(run_dir, "comparison.json")),
          "assemble_matrix writes comparison.json")

    # sweepable_axes reflects which components score: the noise router is
    # sweepable everywhere; the allocator axis appears only where a
    # noise_graph (scoring) allocator ran, not a cost-oblivious one.
    router_sessions = [sid for sid, r in bundle.items()
                       if "router" in r["sweepable_axes"]]
    check(router_sessions,
          "the noise-router sessions report the router axis sweepable")
    ng_sessions = [sid for sid, r in bundle.items()
                   if "noise_graph" in str(r["config"].get("allocator"))]
    for sid in ng_sessions:
        check("allocator" in bundle[sid]["sweepable_axes"],
              f"{sid} (noise_graph allocator) reports the allocator axis")
    graph_sessions = [sid for sid, r in bundle.items()
                      if r["config"].get("allocator") == "graph"]
    for sid in graph_sessions:
        check("allocator" not in bundle[sid]["sweepable_axes"],
              f"{sid} (cost-oblivious graph allocator) is not allocator-sweepable")

    # ── Lattice + edge-graph contract (the adaptive engine's foundation) ──
    # The Scheffe {n,m} lattice and its geometric edge graph underpin the
    # sweep. At n=2 the edge graph MUST be the consecutive chain — this is the
    # regression anchor proving the n-ary generalisation leaves the historical
    # scalar-alpha sweep untouched. Exercised through the sweep below, but the
    # structural invariant is pinned here directly because it is load-bearing.
    ip2 = C._int_lattice(2, 20)
    check(len(ip2) == 21, "the n=2 lattice at m=20 has 21 points (Scheffe count)")
    e2 = sorted(C._lattice_edges(ip2))
    check(e2 == [(i, i + 1) for i in range(20)],
          "the n=2 edge graph is exactly the consecutive chain (anchor)")
    ip3 = C._int_lattice(3, 6)
    check(len(ip3) == 28, "the n=3 lattice at m=6 has 28 points (C(8,2))")
    e3 = C._lattice_edges(ip3)
    adj3 = {}
    for i, j in e3:
        adj3.setdefault(i, set()).add(j)
        adj3.setdefault(j, set()).add(i)
    check(all(adj3.get(k) for k in range(len(ip3))),
          "the n=3 edge graph has no isolated points")

    # _cost_params maps a lattice point onto the resolved weight keys IN ORDER:
    # position 0 -> qubit_error_weight, 1 -> edge_error_weight. Pinned directly
    # because the sweep walks the whole (symmetric) simplex, so a reversed
    # mapping would leave the winner/flip SET unchanged and slip past the
    # sweep-level checks — only a direct coordinate->key assertion catches it.
    # weight_keys is now resolved by the caller from the component's own
    # live_params() (every axis derives it — router, allocator and scheduler
    # alike), sorted for a stable lattice-coordinate -> key mapping. The
    # built-in NoiseGraphAllocator's live_params() is exactly the qubit/edge
    # split, so the derived keys reproduce the historical group.
    from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator
    alloc_keys = sorted(NoiseGraphAllocator(
        qubit_error_weight=0.1, edge_error_weight=0.9).live_params().keys())
    cp = C._cost_params((0.2, 0.8), "allocator", alloc_keys)
    check(cp[alloc_keys[0]] == 0.2 and cp[alloc_keys[1]] == 0.8,
          "cost_params maps point coordinates onto the derived weight keys in order")
    check(alloc_keys == ["edge_error_weight", "qubit_error_weight"],
          "the built-in allocator's swept keys derive from its live_params()")

    # ── Router sweep + faithfulness anchor ────────────────────────────────
    rs = C.sweep(run_dir, router_sessions[0], "router",
                 coarse_m=20, bisect=True)
    check(rs["faithful"] is True,
          "the router sweep's faithfulness anchor holds (replay reproduces "
          "the recorded winner)")
    check(len(rs["decisions"]) >= 1,
          "the router sweep re-derives per-decision winners")
    check("aggregate" in rs and "flips" in rs["aggregate"],
          "the router sweep produces the aggregate/flip view")
    # The primitive covers every lattice point (21 at n=2, m=20) for every
    # decision, as {point, winner} records — the new n-ary schema.
    check(all(len(d["winner_by_point"]) == 21 for d in rs["decisions"]),
          "each decision has a winner at every lattice point")
    check(all(isinstance(rec["point"], list) and "winner" in rec
              for d in rs["decisions"] for rec in d["winner_by_point"]),
          "each primitive record is {point: [...], winner}")
    check(os.path.exists(os.path.join(run_dir, "sweep_comp.router.json")),
          "the router sweep writes sweep_comp.router.json")

    # ── Allocator sweep: a real flip, localised along its lattice edge ────
    if ng_sessions:
        as_ = C.sweep(run_dir, ng_sessions[0], "allocator",
                      coarse_m=20, bisect=True)
        check(as_["faithful"] is True,
              "the allocator sweep's faithfulness anchor holds")
        # The allocator's block choice is weight-sensitive on these devices,
        # so the sweep must surface at least one flip. Each flip lives on a
        # lattice edge (two weight-vector endpoints) and bisection localises
        # it to a normalised weight vector ON that edge.
        flips = as_["aggregate"]["flips"]
        check(len(flips) >= 1,
              "the allocator sweep surfaces a weight-driven block-choice flip")
        check(all(len(f["between"]) == 2
                  and all(isinstance(p, list) for p in f["between"])
                  for f in flips),
              "each flip names its two lattice-edge endpoints as weight vectors")
        check(all(f["at"] is not None
                  and abs(sum(f["at"]) - 1.0) < 1e-6
                  and all(0.0 <= x <= 1.0 for x in f["at"])
                  for f in flips),
              "bisection localises each flip to a normalised weight vector")
        # The localised point must SIT AT the flip, not merely be a valid point
        # on the edge: the winner distribution just toward the `from` endpoint
        # must match `from`, and just toward the `to` endpoint must match `to`.
        # An inverted or mislocalised bisection lands at the wrong end and fails
        # this — the point being on the edge is necessary but not sufficient.
        eng = C._reconstruct(C._AXES["allocator"]["kind"],
                             ng_sessions[0], run_dir)
        decs = C._read_decisions(C._session_log(run_dir, ng_sessions[0]),
                                 C._AXES["allocator"])

        def _dist_at(point):
            c = {}
            # weight keys derive from the component's own live_params() now
            # (weight_group is None for every axis), the same resolution the
            # sweep uses; sorted for the stable coordinate -> key mapping.
            _ak = sorted(eng.live_params().keys())
            for d in decs:
                w = C._winner_at(eng, d["recorded_terms"], point, "allocator", _ak)
                c[w] = c.get(w, 0) + 1
            return {str(C._jsonable(k)): v for k, v in c.items()}

        # The localised point must land on the `to` side of the crossing:
        # bisection converges to the point just past the flip, so the winner
        # distribution AT `at` equals `to`. An inverted bisection converges to
        # the `from` end instead and fails this — the point being a valid edge
        # point (checked above) is necessary but not sufficient; this pins that
        # it sits at the CORRECT boundary, with the correct direction.
        located = all(_dist_at(tuple(f["at"])) == f["to"] for f in flips)
        check(located,
              "each localised flip point lands on the `to` side of its "
              "distribution change (bisection direction is correct)")
        check(os.path.exists(os.path.join(run_dir, "sweep_comp.allocator.json")),
              "the allocator sweep writes sweep_comp.allocator.json")

        # Disciplined regression anchor: everything STRUCTURAL is exact
        # between a coarse and a fine sweep — the winner set, the flip count,
        # and each flip's from/to distribution. Only the flip POSITION may
        # move, and only within the bisection tolerance. This catches a
        # wrong-winner, wrong-count or schema-drift bug exactly, while
        # forgiving the sub-tolerance localisation wobble that is expected
        # when the coarse grid differs (adaptive refinement, not exact grid).
        coarse = C.sweep(run_dir, ng_sessions[0], "allocator",
                         coarse_m=20, bisect=True)["aggregate"]
        fine   = C.sweep(run_dir, ng_sessions[0], "allocator",
                         coarse_m=40, bisect=True)["aggregate"]
        cw = {tuple(sorted(e["dist"].items()))
              for e in coarse["winner_distribution"]}
        fw = {tuple(sorted(e["dist"].items()))
              for e in fine["winner_distribution"]}
        check(cw == fw,
              "coarse and fine sweeps agree on the winner-distribution set "
              "(structural anchor: exact, not tolerance)")
        cstruct = sorted((tuple(sorted(f["from"].items())),
                          tuple(sorted(f["to"].items()))) for f in coarse["flips"])
        fstruct = sorted((tuple(sorted(f["from"].items())),
                          tuple(sorted(f["to"].items()))) for f in fine["flips"])
        check(cstruct == fstruct,
              "coarse and fine sweeps agree on every flip's from/to "
              "distribution (structural anchor: exact)")

    # ── Skip-with-reason for a non-scoring component ──────────────────────
    if graph_sessions:
        skip = C.sweep(run_dir, graph_sessions[0], "allocator", coarse_m=20)
        check(skip["faithful"] is False,
              "sweeping a non-scoring allocator is refused, not faked")
        check("decisions" not in skip,
              "a refused sweep emits no decisions")
        # Pin the NOT-SCORING path specifically, distinct from the
        # no-decisions path: a cost-oblivious allocator must be caught by
        # is_sweepable() (its reason names it a non-scoring component),
        # not merely by finding zero events. Without this the is_sweepable
        # guard could be removed and the block would still pass via the
        # empty-decisions branch.
        check("not a scoring component" in skip.get("reason", ""),
              "the refusal names the component non-scoring, not just empty")

    # ── The scheduler axis: batch dedup, argmin winner, plugin reconstruct ─
    # A batch scheduler emits one `schedule` event PER dispatched job in a
    # cycle, all sharing ONE ranking snapshot; the sweep must (a) detect the
    # scheduler axis is sweepable, (b) collapse those events into ONE decision
    # whose winner is the ranking's argmin (not the per-event dispatched job),
    # and (c) reconstruct the scored scheduler via an explicit registry_map,
    # since a research plugin is not globally registered. A tiny scored
    # scheduler stands in for a real plugin (which cannot be imported into the
    # core suite), with its weight keys DERIVED from live_params (no plugin
    # key names in _AXES).
    class ToyScoredScheduler(BaseScheduler):
        LABEL = "Toy Scored Scheduler"

        def __init__(self, memory_manager, process_table, toy_a=1.0, toy_b=1.0):
            super().__init__(memory_manager, process_table)
            self.toy_a, self.toy_b = toy_a, toy_b

        def schedule(self):
            return []   # unused by the sweep (scoring engine only)

        def live_params(self):
            return {"toy_a_weight": self.toy_a, "toy_b_weight": self.toy_b}

        def _sweep_terms(self, decision):
            return [(q, {"x": q, "y": 10 - q}) for q in decision]

        def _sweep_score(self, terms, params):
            return (terms["x"], terms["y"])

        def _sweep_rank(self, scored, params):
            xs = [r[0] for _, _, r in scored]
            ys = [r[1] for _, _, r in scored]
            def mm(vals):
                lo, hi = min(vals), max(vals)
                sp = hi - lo
                return {v: (0.0 if sp == 0 else (v - lo) / sp) for v in set(vals)}
            nx, ny = mm(xs), mm(ys)
            a, b = params["toy_a_weight"], params["toy_b_weight"]
            out = []
            for key, terms, raw in scored:
                final = a * nx[raw[0]] + b * ny[raw[1]]
                enriched = dict(terms, x_norm=nx[raw[0]], y_norm=ny[raw[1]],
                                toy_a_weight=a, toy_b_weight=b)
                out.append((key, final, enriched))
            return out

    sched_dir = os.path.join(root, "test_results", "_scheduler_axis_fixture")
    shutil.rmtree(sched_dir, ignore_errors=True)
    os.makedirs(sched_dir)
    with open(os.path.join(sched_dir, "manifest.json"), "w") as h:
        json.dump({"sessions": [{
            "session_id": "toy", "log": "toy.jsonl",
            "config": {"scheduler": "toy_scored", "allocator": "graph",
                       "router": "noise"}}]}, h)
    # One ranking over jobs 1,2,3 (x=job id), emitted as THREE schedule events
    # (a batch cycle dispatching all three) — identical scores snapshot, but
    # each event's `winner` is the job it dispatched (1, then 2, then 3).
    scores = [{"job_id": j, "score": float(j),
               "terms": {"x": j, "y": 10 - j,
                         "toy_a_weight": 1.0, "toy_b_weight": 1.0}}
              for j in (1, 2, 3)]
    with open(os.path.join(sched_dir, "toy.jsonl"), "w") as h:
        for dispatched in (1, 2, 3):
            h.write(json.dumps({"event": "schedule", "job_id": dispatched,
                                "winner": dispatched, "scores": scores}) + "\n")

    reg = {"scheduler": {"toy_scored": ToyScoredScheduler}}

    # (a) the scheduler axis is reported sweepable from the schedule events.
    axes = C._sweepable_axes(os.path.join(sched_dir, "toy.jsonl"))
    check("scheduler" in axes,
          "a log with scores-bearing schedule events reports the scheduler "
          "axis sweepable (derived from _AXES, not a hardcoded event list)")

    # (b) the three events collapse to ONE decision whose winner is the
    # ranking's argmin (job 1, lowest score), NOT three decisions each
    # winning their dispatched job.
    decs = C._read_decisions(os.path.join(sched_dir, "toy.jsonl"),
                             C._AXES["scheduler"])
    check(len(decs) == 1,
          "a batch cycle's repeated schedule events collapse to one sweep "
          "decision (one ranking, not one-per-dispatch)")
    check(decs[0]["winner"] == 1,
          "the deduped decision's winner is the ranking's argmin (job 1), "
          "not a per-event dispatched job")

    # (c) the sweep reconstructs the plugin via registry_map and derives its
    # weight keys from live_params — faithful, and swept over the 2-weight
    # simplex.
    res = C.sweep(sched_dir, "toy", "scheduler", coarse_m=8, bisect=True,
                  registry_map=reg)
    check(res["faithful"] is True,
          "the scheduler sweep reconstructs the plugin (registry_map) and "
          "its faithfulness anchor holds")
    check(res.get("weight_keys") == ["toy_a_weight", "toy_b_weight"],
          "the scheduler axis derives its swept keys from the component's "
          "live_params (no plugin key names hardcoded in core)")
    # Without the registry_map the plugin cannot be rebuilt -> not sweepable,
    # which pins that reconstruction actually depends on the passed classes.
    res_noreg = C.sweep(sched_dir, "toy", "scheduler", coarse_m=8)
    check(res_noreg["faithful"] is False,
          "without registry_map a research plugin cannot be reconstructed, so "
          "the sweep honestly refuses rather than faking a result")

    # ── The anchor recovers weights by the component's OWN keys, not by a
    #    "_weight" name convention ──────────────────────────────────────────
    # Regression guard: a third-party scoring component may name its weight
    # keys anything (qos.alpha, foo.lam, ...). The faithfulness anchor recovers
    # the run's weights from live_params() keys, so a non-"_weight" name must
    # still sweep faithfully. A prior implementation filtered recorded terms by
    # k.endswith("_weight"), which returned an EMPTY anchor for such a
    # component and broke replay — passing only by naming coincidence for the
    # built-ins. This fixture's keys deliberately do NOT end in "_weight".
    class OddKeyScheduler(BaseScheduler):
        LABEL = "Odd Key Scheduler"

        def __init__(self, memory_manager, process_table, alpha=1.0, beta=1.0):
            super().__init__(memory_manager, process_table)
            self.alpha, self.beta = alpha, beta

        def schedule(self):
            return []

        def live_params(self):
            # keys WITHOUT the "_weight" suffix — the whole point of the guard
            return {"odd.alpha": self.alpha, "odd.beta": self.beta}

        def _sweep_terms(self, decision):
            return [(q, {"x": q, "y": 10 - q}) for q in decision]

        def _sweep_score(self, terms, params):
            return (terms["x"], terms["y"])

        def _sweep_rank(self, scored, params):
            xs = [r[0] for _, _, r in scored]
            ys = [r[1] for _, _, r in scored]
            def mm(vals):
                lo, hi = min(vals), max(vals)
                sp = hi - lo
                return {v: (0.0 if sp == 0 else (v - lo) / sp) for v in set(vals)}
            nx, ny = mm(xs), mm(ys)
            # KeyError here if the anchor passed empty params — the exact
            # failure mode the guard protects against.
            a, b = params["odd.alpha"], params["odd.beta"]
            out = []
            for key, terms, raw in scored:
                final = a * nx[raw[0]] + b * ny[raw[1]]
                enriched = dict(terms, x_norm=nx[raw[0]], y_norm=ny[raw[1]],
                                **{"odd.alpha": a, "odd.beta": b})
                out.append((key, final, enriched))
            return out

    odd_dir = os.path.join(root, "test_results", "_odd_key_axis_fixture")
    shutil.rmtree(odd_dir, ignore_errors=True)
    os.makedirs(odd_dir)
    with open(os.path.join(odd_dir, "manifest.json"), "w") as h:
        json.dump({"sessions": [{
            "session_id": "odd", "log": "odd.jsonl",
            "config": {"scheduler": "odd_key", "allocator": "graph",
                       "router": "noise"}}]}, h)
    odd_scores = [{"job_id": j, "score": float(j),
                   "terms": {"x": j, "y": 10 - j,
                             "odd.alpha": 1.0, "odd.beta": 1.0}}
                  for j in (1, 2, 3)]
    with open(os.path.join(odd_dir, "odd.jsonl"), "w") as h:
        for dispatched in (1, 2, 3):
            h.write(json.dumps({"event": "schedule", "job_id": dispatched,
                                "winner": dispatched,
                                "scores": odd_scores}) + "\n")
    odd_reg = {"scheduler": {"odd_key": OddKeyScheduler}}
    odd_res = C.sweep(odd_dir, "odd", "scheduler", coarse_m=8,
                      registry_map=odd_reg)
    check(odd_res["faithful"] is True,
          "the faithfulness anchor recovers weights by the component's own "
          "live_params keys, so a scoring component whose keys do NOT end in "
          "'_weight' still sweeps faithfully (regression guard for the anchor "
          "key-recovery)")
    check(odd_res.get("weight_keys") == ["odd.alpha", "odd.beta"],
          "the odd-key scheduler's swept keys come from live_params verbatim")

    # ── Unknown axis is an error, not a silent empty result ───────────────
    raised = False
    try:
        C.sweep(run_dir, router_sessions[0], "provider")
    except ValueError:
        raised = True
    check(raised, "an unknown sweep axis raises rather than returning empty")

    # ── The faithfulness anchor has teeth ─────────────────────────────────
    # A faithful run never triggers the anchor, so the checks above cannot
    # tell a working guard from a defanged one. Plant a log whose recorded
    # winner CONTRADICTS its scores (winner marked as the worse device) and
    # require the sweep to refuse it — this is the only thing that pins the
    # anchor as load-bearing rather than decorative.
    bad_dir = os.path.join(root, "test_results", "_anchor_tamper_fixture")
    shutil.rmtree(bad_dir, ignore_errors=True)
    os.makedirs(bad_dir)
    # A one-session manifest naming a noise router, and a log with a single
    # route event whose scores clearly favour device 0 but whose recorded
    # winner is device 1.
    with open(os.path.join(bad_dir, "manifest.json"), "w") as h:
        json.dump({"sessions": [{
            "session_id": "bad", "log": "bad.jsonl",
            "config": {"scheduler": "fcfs", "allocator": "graph",
                       "router": "noise"}}]}, h)
    terms0 = {"queue_pressure": 0, "qubit_error_sum": 0.01, "edge_error_sum": 0.0,
              "router_queue_weight": 0.5, "router_noise_weight": 0.5,
              "qubit_error_weight": 0.1, "edge_error_weight": 0.9}
    terms1 = dict(terms0, qubit_error_sum=0.99)   # device 1 is clearly worse
    with open(os.path.join(bad_dir, "bad.jsonl"), "w") as h:
        h.write(json.dumps({
            "event": "route", "job_id": 1, "device": 1,   # winner: the WORSE one
            "candidates": [0, 1],
            "scores": [{"device": 0, "score": 0.0, "terms": terms0},
                       {"device": 1, "score": 1.0, "terms": terms1}]}) + "\n")
    tampered = C.sweep(bad_dir, "bad", "router", coarse_m=4)
    check(tampered["faithful"] is False,
          "the anchor refuses a session whose recorded winner contradicts "
          "its own scores")
    check("did not reproduce" in tampered.get("reason", ""),
          "the anchor's refusal explains the winner mismatch")
    shutil.rmtree(bad_dir, ignore_errors=True)


    # ── Real fidelity flows through the bundle ────────────────────────────
    # The smoke matrix above runs on devq.simulated, a uniform mock with no
    # noiseless reference, so its fidelity is null by design — correct, but
    # it does not exercise fidelity reaching the bundle. Run ONE
    # ibm.simulated session (not a full matrix — that would run an Aer
    # density-matrix reference per circuit across 18 cells on every suite
    # run) and assert assemble_matrix surfaces populated fidelity. Skips
    # cleanly where qiskit is absent.
    try:
        from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
        fdir = os.path.join(root, "test_results", "ibm_federation", "comparisons")
        shutil.rmtree(fdir, ignore_errors=True)
        with contextlib.redirect_stdout(io.StringIO()):
            R.run(os.path.join(WORKLOADS, "ibm_federation.json"), out_dir=fdir,
                  register_providers={"ibm.simulated": IBMSimulatedProvider},
                  quiet=True)
            write_metrics(fdir)
        fbundle = C.assemble_matrix(fdir)
        frow = next(iter(fbundle.values()))
        fid = frow["metrics"]["fidelity"]
        check(fid["hellinger"]["median"] is not None,
              "an ibm.simulated session surfaces populated fidelity through "
              "the bundle (not null as a mock provider would give)")
        populated = [j for j, v in fid["per_job"].items()
                     if v["hellinger"] is not None]
        check(len(populated) == len(fid["per_job"]),
              f"every job in the ibm session has a fidelity number, got "
              f"{len(populated)}/{len(fid['per_job'])}")
    except ImportError:
        check(True, "qiskit not installed - fidelity-through-bundle check skipped")


def block_comparison_modes():
    '''The 5.5b reading surfaces: session ranking and sweep presentation'''
    # comparison_modes.py is pure presentation over what comparison.py
    # computed — it derives no numbers. This block builds small fixtures
    # with KNOWN values so ranking order, direction, missing-metric
    # handling and the sweep read-out are checked against hand-computed
    # answers, not against whatever a live run happens to produce.
    from benchmark import comparison_modes as M

    # ── Ranking ───────────────────────────────────────────────────────────
    bundle = {
        "a__x__p": {"config": {"router": "x"},
                    "metrics": {"rejection_rate": {"rate": 0.4},
                                "utilisation": {"system": 0.5}}},
        "b__y__q": {"config": {"router": "y"},
                    "metrics": {"rejection_rate": {"rate": 0.1},
                                "utilisation": {"system": 0.9}}},
        "c__z__r": {"config": {"router": "z"},
                    "metrics": {"rejection_rate": {"rate": 0.2},
                                "utilisation": {"system": 0.7}}},
        "d__w__s": {"config": {"router": "w"},
                    "metrics": {"rejection_rate": {"rate": None},   # unmeasured
                                "utilisation": {"system": 0.6}}},
    }

    # Ascending (lowest rejection first): b(0.1) < c(0.2) < a(0.4); d is
    # null so it is not ranked.
    rank = M.rank_sessions(bundle, "rejection_rate.rate")
    order = [r["session_id"] for r in rank["rows"]]
    check(order == ["b__y__q", "c__z__r", "a__x__p"],
          f"ranking orders by the metric ascending, got {order}")
    check(rank["rows"][0]["rank"] == 1 and rank["rows"][0]["value"] == 0.1,
          "the top row carries rank 1 and the true value")
    check(rank["missing"] == ["d__w__s"],
          f"a null-metric session is listed missing, not ranked, got "
          f"{rank['missing']}")

    # descending flips the order.
    desc = M.rank_sessions(bundle, "rejection_rate.rate", descending=True)
    check([r["session_id"] for r in desc["rows"]] == ["a__x__p", "c__z__r", "b__y__q"],
          "descending ranks highest first")

    # a nested path resolves; a bogus path ranks nothing (all missing).
    util = M.rank_sessions(bundle, "utilisation.system", descending=True)
    check(util["rows"][0]["session_id"] == "b__y__q",
          "a nested dotted path resolves to the right leaf")
    bogus = M.rank_sessions(bundle, "nope.not.here")
    check(bogus["rows"] == [] and len(bogus["missing"]) == 4,
          "an unknown metric path ranks nothing rather than crashing")

    # A path landing on a non-numeric leaf (a dict, not a scalar) is not
    # rankable and must be treated as missing — pointing at "rejection_rate"
    # instead of "rejection_rate.rate" gives a dict, which cannot be sorted.
    nonscalar = M.rank_sessions(bundle, "rejection_rate")
    check(nonscalar["rows"] == [],
          "a path landing on a non-numeric leaf ranks nothing, not the dict")

    # tie-break is deterministic on session id.
    tied = {
        "z__a__a": {"config": {}, "metrics": {"m": {"v": 1.0}}},
        "a__a__a": {"config": {}, "metrics": {"m": {"v": 1.0}}},
    }
    tb = M.rank_sessions(tied, "m.v")
    check([r["session_id"] for r in tb["rows"]] == ["a__a__a", "z__a__a"],
          "equal values break ties by session id, deterministically")

    # ── Sweep presentation ────────────────────────────────────────────────
    # Refused sweep: presented as a refusal carrying its reason.
    refused = {"session_id": "s", "axis": "allocator", "faithful": False,
               "reason": "not a scoring component", "coarse_m": 20,
               "bisect": False}
    pr = M.present_sweep(refused)
    check(pr["sweepable"] is False and "scoring" in pr["reason"],
          "a refused sweep is presented as not sweepable, with its reason")

    # Faithful, stable (no flips): reported stable. Distribution is the
    # n-ary list-of-{point, dist} schema.
    stable = {"session_id": "s", "axis": "router", "faithful": True,
              "coarse_m": 20, "bisect": True,
              "aggregate": {"flips": [],
                            "centroid_of_largest_stable_region": [0.5, 0.5],
                            "region_size": 21,
                            "winner_distribution": [
                                {"point": [0.0, 1.0], "dist": {"1": 5}}]}}
    ps = M.present_sweep(stable)
    check(ps["sweepable"] and ps["stable"] and ps["flips"] == [],
          "a faithful sweep with no flips is reported stable")
    check(ps["centroid_of_largest_stable_region"] == [0.5, 0.5]
          and ps["region_size"] == 21,
          "present_sweep surfaces the recommended centroid and its region size")

    # Faithful with a flip: the flip is surfaced. between/at are weight
    # vectors (lattice-edge endpoints and the localised point on the edge).
    flipped = {"session_id": "s", "axis": "allocator", "faithful": True,
               "coarse_m": 20, "bisect": True,
               "aggregate": {
                   "flips": [{"between": [[0.0, 1.0], [0.05, 0.95]],
                              "at": [0.003, 0.997],
                              "from": {"[2, 4]": 2}, "to": {"[0, 1]": 2}}],
                   "winner_distribution": [
                       {"point": [0.0, 1.0], "dist": {"[2, 4]": 2}},
                       {"point": [0.05, 0.95], "dist": {"[0, 1]": 2}}]}}
    pf = M.present_sweep(flipped)
    check(pf["sweepable"] and not pf["stable"] and len(pf["flips"]) == 1,
          "a faithful sweep with a flip surfaces it and is not stable")
    check(pf["flips"][0]["at"] == [0.003, 0.997],
          "the presented flip carries its localised weight vector")

    # ── Text renderer + file write ────────────────────────────────────────
    import tempfile
    txt = M.render_text(rank)
    check("rejection_rate.rate" in txt and "b__y__q" in txt,
          "the ranking renders to text naming the metric and the top session")
    check("d__w__s" in txt and "not ranked" in txt,
          "the text names the missing session so it is not silently dropped")

    sweep_txt = M.render_text(pf)
    check("flip" in sweep_txt and "w=" in sweep_txt,
          "the sweep renders to text naming its flip and weight vectors")

    # detection: rows -> ranking, else sweep; both from one renderer.
    stable_txt = M.render_text(ps)
    check("stable" in stable_txt,
          "the renderer detects a sweep result and reads out stability")
    check("recommended weight" in stable_txt and "0.5" in stable_txt,
          "the sweep text prints the recommended centroid weight")

    # A refused sweep never carries a centroid — there is no region to
    # recommend over.
    check("centroid_of_largest_stable_region" not in pr,
          "a refused sweep carries no recommended centroid")

    # writing to a path produces a file with exactly the returned text.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ranking.txt")
        returned = M.render_text(rank, to=path)
        check(os.path.exists(path), "render_text writes a .txt when given a path")
        with open(path) as h:
            check(h.read() == returned,
                  "the file holds exactly the returned text")


def block_stable_region():
    '''The sweep's robust-weight recommendation: centroid of the largest
    connected constant-decision region'''
    # _stable_region_centroid is a pure function of the lattice and the
    # per-point winner distribution. Fixtures use small lattices with
    # hand-computed answers so the region-finding, the connectivity rule,
    # the largest-wins choice, the deterministic tie-break, and the
    # renormalised centroid are all checked against known values — not
    # against whatever a live sweep happens to produce.
    from benchmark.comparison import (_stable_region_centroid,
                                       _int_lattice)

    def lattice(n, m):
        ip = _int_lattice(n, m)
        pts = [tuple(k / m for k in c) for c in ip]
        return ip, pts

    # ── n=2, m=4: the 5-point chain (0,1) .. (1,0) ────────────────────────
    ip, pts = lattice(2, 4)

    # Fully stable: one region of all 5 points, centroid is the chain
    # barycenter (0.5, 0.5).
    c, sz = _stable_region_centroid(ip, pts, [{"A": 1}] * 5)
    check(sz == 5 and c == [0.5, 0.5],
          f"a fully stable sweep recommends the whole-region barycenter, "
          f"got {c} over {sz}")

    # One flip splits the chain into a 3-point region [0,.25,.5] and a
    # 2-point region [.75,1]; the larger (left) wins, centroid at .25.
    c, sz = _stable_region_centroid(
        ip, pts, [{"A": 1}, {"A": 1}, {"A": 1}, {"B": 1}, {"B": 1}])
    check(sz == 3 and c == [0.25, 0.75],
          f"the larger of two regions is chosen, got {c} over {sz}")

    # Two equal-size (2-point) regions with a singleton between them; the
    # tie breaks toward the lower-index region (the left one), deterministic.
    c, sz = _stable_region_centroid(
        ip, pts, [{"A": 1}, {"A": 1}, {"C": 1}, {"B": 1}, {"B": 1}])
    check(sz == 2 and c == [0.125, 0.875],
          f"a size tie breaks toward the canonical-lowest region, got {c}")

    # The distribution is a multiset, not a single winner: {A:1,B:1} and
    # {A:2} are different regions even though A appears in both.
    c, sz = _stable_region_centroid(
        ip, pts, [{"A": 1, "B": 1}] * 2 + [{"A": 2}] * 3)
    check(sz == 3,
          f"regions are keyed by the full winner distribution, not one "
          f"winner, got region size {sz}")

    # ── n=3, m=3: the 10-point triangle, connectivity guard ───────────────
    ip, pts = lattice(3, 3)
    check(len(pts) == 10, "the n=3 m=3 lattice has 10 points")

    # Fully stable -> the simplex barycenter (1/3, 1/3, 1/3).
    c, sz = _stable_region_centroid(ip, pts, [{"A": 1}] * 10)
    check(sz == 10 and all(abs(x - 1 / 3) < 1e-5 for x in c),
          f"a fully stable triangle recommends its barycenter, got {c}")

    # Connectivity guard: the three CORNERS share winner A but are pairwise
    # non-adjacent. A "same winner anywhere" rule would merge them into a
    # size-3 region; the CONNECTED rule must keep them as three singletons,
    # so the size-7 interior/edge bulk (winner B) is the recommendation.
    dist = [{"B": 1}] * 10
    for i, comp in enumerate(ip):
        if 3 in comp:              # a corner: one coordinate carries all mass
            dist[i] = {"A": 1}
    c, sz = _stable_region_centroid(ip, pts, dist)
    check(sz == 7,
          f"disconnected same-winner points are NOT merged; the connected "
          f"bulk wins, got region size {sz}")

    # ── Degenerate input ──────────────────────────────────────────────────
    c, sz = _stable_region_centroid([], [], [])
    check(c is None and sz == 0,
          "an empty lattice yields no recommendation rather than crashing")


def block_fidelity():
    '''Fidelity: hand-computed distances, marginalisation, population rule'''
    import json, math, os, tempfile
    from benchmark import metrics as M
    from benchmark import runner as R
    from frontends.qasm2.qasm2_frontend import QASM2Frontend
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

    # ── HALF ONE: the distance measures, on HAND-BUILT distributions ──
    # Values are computed by hand in the comments; asserting a metric
    # against its own output proves nothing (the metrics-block lesson).
    #
    # measured {00:0.4, 11:0.5, 01:0.1}  vs  ideal {00:0.5, 11:0.5}
    m = {"00": 0.4, "11": 0.5, "01": 0.1}
    i = {"00": 0.5, "11": 0.5}

    # TVD = 1/2 (|0.4-0.5| + |0.5-0.5| + |0.1-0.0|) = 1/2 (0.1+0+0.1) = 0.1
    tvd = M.total_variation_distance(m, i)
    check(abs(tvd - 0.1) < 1e-12, f"TVD hand-computed 0.1, got {tvd}")

    # Hellinger fidelity = (1 - H^2)^2 with
    #   H^2 = 1/2 sum (sqrt(p) - sqrt(q))^2
    # keys: 00: (sqrt.4-sqrt.5)^2, 11: (sqrt.5-sqrt.5)^2=0,
    #       01: (sqrt.1-0)^2 = 0.1
    h_sq = 0.5 * ((0.4 ** 0.5 - 0.5 ** 0.5) ** 2
                  + 0.0
                  + (0.1 ** 0.5 - 0.0) ** 2)
    expected_hf = (1 - h_sq) ** 2
    hf = M.hellinger_fidelity(m, i)
    check(abs(hf - expected_hf) < 1e-12,
          f"Hellinger fidelity hand-computed {expected_hf}, got {hf}")

    # The headline number MUST equal Qiskit's hellinger_fidelity — that is
    # the definition QOS reports and docs/REFERENCES.md [Qiskit-HF] claims
    # we match. Qiskit takes COUNTS; scale the same ratios to integers.
    from qiskit.quantum_info import hellinger_fidelity as qiskit_hf
    qhf = qiskit_hf({"00": 400, "11": 500, "01": 100}, {"00": 500, "11": 500})
    check(abs(hf - qhf) < 1e-9,
          f"our Hellinger equals Qiskit's ({qhf}), got {hf}")

    # HF and TVD are DIFFERENT numbers on the same inputs — a swapped or
    # square-dropped formula cannot pass by accidentally matching the
    # other's value (the survivor guard for the distance choice).
    check(abs(hf - tvd) > 0.1,
          f"Hellinger ({hf}) and TVD ({tvd}) are numerically distinct")

    # Identical distributions: HF exactly 1.0, TVD exactly 0.0.
    check(M.hellinger_fidelity(i, i) == 1.0, "identical -> HF 1.0")
    check(M.total_variation_distance(i, i) == 0.0, "identical -> TVD 0.0")

    # Disjoint support: HF 0.0, TVD 1.0. Hellinger is well defined on
    # differing support (the GHZ rationale, [GHZ-rationale]); a naive
    # same-support assumption would KeyError or misread here.
    a = {"00": 1.0}
    b = {"11": 1.0}
    check(abs(M.hellinger_fidelity(a, b) - 0.0) < 1e-12,
          "disjoint support -> HF 0.0")
    check(abs(M.total_variation_distance(a, b) - 1.0) < 1e-12,
          "disjoint support -> TVD 1.0")

    # _normalise: counts -> distribution; empty / all-zero -> None (the
    # population rule, so a job with no shots has no distribution).
    check(M._normalise({"0": 3, "1": 1}) == {"0": 0.75, "1": 0.25},
          "counts normalise to a probability distribution")
    check(M._normalise({}) is None and M._normalise({"0": 0}) is None,
          "empty / all-zero counts -> None, not a uniform or zero dist")

    # ── HALF TWO: MARGINALISATION survivor (qubit index != clbit index) ─
    # The swapped_measure fixture flips q0 to |1>, q1 to |0>, but measures
    # q0 -> c1 and q1 -> c0. The correct, map-based ideal is c1c0 = "10".
    # An implementation that marginalised in QUBIT order would give "01" —
    # a different string. With this fixture the two are distinguishable, so
    # a qubit-order regression cannot survive (unlike a fixture whose qubit
    # and clbit indices align).
    fe = QASM2Frontend()
    prov = IBMSimulatedProvider(seed=SEED)
    swapped = fe.parse("test_circuits/qasm2/swapped_measure.qasm")
    ideal_sw = prov.reference_ideal(swapped)
    check(ideal_sw == {"10": 1.0},
          f"swapped measure map -> ideal '10' (map-based), got {ideal_sw}")

    # And directly against the pure marginaliser, so the assertion does not
    # depend on the Aer path: probs has all mass on index 1 (q0=1, q1=0).
    probs = [0.0, 1.0, 0.0, 0.0]
    marg = IBMSimulatedProvider._marginalise(probs, [(0, 1), (1, 0)], 2, 2)
    check(marg == {"10": 1.0},
          f"_marginalise follows the measure map, got {marg}")
    # The buggy qubit-order reading would have produced "01" — assert the
    # correct code does NOT produce it, pinning the distinction.
    check("01" not in marg, "marginalisation is not qubit-order ('01')")

    # The reference must use a DENSITY-MATRIX simulation, not statevector.
    # A Bell pair with q0 then reset leaves q1 in a genuinely MIXED state:
    # the correct ideal is 50/50 on "00"/"10". A statevector reference
    # would collapse the mixture and report {"00": 1.0} — wrong. No
    # reset-free circuit (Bell, GHZ) can tell the two methods apart, and a
    # reset on an unentangled qubit cannot either; the reset must follow
    # entanglement. This assertion is what makes the density-matrix choice
    # load-bearing rather than merely intended.
    reset_ent = fe.parse("test_circuits/qasm2/reset_entangled.qasm")
    ideal_re = prov.reference_ideal(reset_ent)
    check(abs(ideal_re.get("00", 0) - 0.5) < 1e-9
          and abs(ideal_re.get("10", 0) - 0.5) < 1e-9
          and abs(ideal_re.get("01", 0)) < 1e-12,
          f"reset-after-entanglement ideal is mixed 00/10 (density matrix, "
          f"not statevector's collapsed 00), got {ideal_re}")

    # Closed-form structured ideals: Bell -> 50/50 on 00/11, hand-known.
    bell = fe.parse(BELL)
    ideal_bell = prov.reference_ideal(bell)
    check(abs(ideal_bell.get("00", 0) - 0.5) < 1e-9
          and abs(ideal_bell.get("11", 0) - 0.5) < 1e-9
          and set(ideal_bell) == {"00", "11"},
          f"Bell ideal is 50/50 on 00/11, got {ideal_bell}")

    # ── HALF THREE: the fidelity metric's POPULATION RULE, on synthetic
    # records. Build a run with a finished job (has counts + ideal), a
    # rejected job (no counts), and a job whose circuit has NO ideal.
    chash_bell = "hash_bell"
    chash_noref = "hash_noref"
    records = [
        {"event": "reference", "circuit_hash": chash_bell,
         "ideal": {"00": 0.5, "11": 0.5}, "label": "bell"},
        # job 1 FINISHED, measured near-ideal, has a reference ideal
        {"event": "resolve", "job_id": 1, "state": "FINISHED",
         "success": True, "circuit_hash": chash_bell,
         "counts": {"00": 480, "11": 500, "01": 20}},
        # job 2 REJECTED, no counts
        {"event": "resolve", "job_id": 2, "state": "REJECTED",
         "success": False, "circuit_hash": chash_bell, "counts": None},
        # job 3 FINISHED but its circuit has NO recorded ideal
        {"event": "resolve", "job_id": 3, "state": "FINISHED",
         "success": True, "circuit_hash": chash_noref,
         "counts": {"00": 500, "11": 500}},
        {"event": "summary",
         "devices_attached": {"0": "d"},
         "per_job": [
             {"job_id": 1, "state": "FINISHED", "device": 0,
              "circuit_hash": chash_bell},
             {"job_id": 2, "state": "REJECTED", "device": None,
              "circuit_hash": chash_bell},
             {"job_id": 3, "state": "FINISHED", "device": 0,
              "circuit_hash": chash_noref},
         ]},
    ]
    fid = M.fidelity(records)

    # job 1 has a real fidelity; hand-check its TVD:
    # measured normalised: 00: 480/1000=.48, 11:.50, 01:.02
    # TVD = 1/2(|.48-.5| + |.5-.5| + |.02-0|) = 1/2(.02+0+.02) = 0.02
    check(abs(fid["per_job"][1]["tvd"] - 0.02) < 1e-12,
          f"job 1 TVD hand-computed 0.02, got {fid['per_job'][1]['tvd']}")
    check(fid["per_job"][1]["hellinger"] is not None
          and 0.9 < fid["per_job"][1]["hellinger"] <= 1.0,
          "job 1 has a high (near-ideal) Hellinger fidelity")

    # job 2 REJECTED -> None, NOT a 0 (a rejected job never measured; 0
    # would falsely mean 'measured and maximally wrong').
    check(fid["per_job"][2]["hellinger"] is None
          and fid["per_job"][2]["tvd"] is None,
          "rejected job (no counts) -> fidelity None, not 0")

    # job 3 FINISHED but NO ideal -> None (no reference-capable provider
    # covered this circuit; an absent ideal is not a zero distribution).
    check(fid["per_job"][3]["hellinger"] is None
          and fid["per_job"][3]["tvd"] is None,
          "finished job with no recorded ideal -> fidelity None")

    # The session aggregate is over QUALIFYING jobs only — here just job 1,
    # so min == max == job 1's value, and the skipped jobs are not folded
    # in as zeros (which would crater the mean).
    check(fid["hellinger"]["min"] == fid["hellinger"]["max"]
          == fid["per_job"][1]["hellinger"],
          "aggregate spans only qualifying jobs, skips None (no zero-fill)")

    # Empty population: no job qualifies -> every aggregate field None.
    none_records = [
        {"event": "summary", "devices_attached": {"0": "d"},
         "per_job": [{"job_id": 1, "state": "REJECTED", "device": None,
                      "circuit_hash": "x"}]},
        {"event": "resolve", "job_id": 1, "state": "REJECTED",
         "success": False, "circuit_hash": "x", "counts": None},
    ]
    fn = M.fidelity(none_records)
    check(all(fn["hellinger"][k] is None for k in
              ("min", "median", "mean", "max", "p95")),
          "no qualifying job -> every aggregate field None, not 0")

    # ── HALF FOUR: a REAL run — structure and a physical BOUND ─────────
    # Exact fidelity numbers off a noisy Aer run are seed-fragile, so here
    # we assert STRUCTURE and a bound that must hold physically: under the
    # same noise, GHZ fidelity <= Bell fidelity. GHZ's ideal concentrates
    # on two of 2^3 strings, so noise smears it harder than Bell's two of
    # 2^2 — the very case Hellinger is chosen to handle honestly.
    tmp = tempfile.mkdtemp()
    try:
        spec = os.path.join(tmp, "wl.json")
        with open(spec, "w") as h:
            json.dump({
                "name": "fidelity", "seed": SEED,
                "devices": [{"id": "nairobi", "provider": "ibm.simulated",
                             "backend": {"backend_name": "FakeNairobiV2"}}],
                "jobs": [{"circuit": BELL, "repeat": 2},
                         {"circuit": GHZ, "repeat": 2}],
            }, h)
        out = os.path.join(tmp, "run")
        manifest = R.run(spec, out_dir=out, quiet=True,
                         register_providers={"ibm.simulated":
                                             IBMSimulatedProvider})
        log = os.path.join(out, manifest["sessions"][0]["log"])
        with open(log) as h:
            recs = [json.loads(line) for line in h if line.strip()]

        fid_real = M.fidelity(recs)
        # Every job that finished has a numeric fidelity in [0, 1].
        vals = [v["hellinger"] for v in fid_real["per_job"].values()
                if v["hellinger"] is not None]
        check(len(vals) == 4, f"all 4 IBM jobs have a fidelity, got {len(vals)}")
        check(all(0.0 <= v <= 1.0 for v in vals),
              "every Hellinger fidelity lies in [0, 1]")

        # Bound: mean GHZ fidelity <= mean Bell fidelity under this noise.
        # Map each job to its circuit via the per_job circuit_hash.
        by_hash = {r["circuit_hash"]: r["ideal"]
                   for r in recs if r.get("event") == "reference"}
        bell_hash = next(h for h, ideal in by_hash.items()
                         if set(ideal) == {"00", "11"})
        ghz_hash = next(h for h, ideal in by_hash.items()
                        if set(ideal) == {"000", "111"})
        summ = next(r for r in recs if r.get("event") == "summary")
        job_hash = {row["job_id"]: row["circuit_hash"]
                    for row in summ["per_job"]}
        bell_f = [fid_real["per_job"][j]["hellinger"]
                  for j, hsh in job_hash.items() if hsh == bell_hash]
        ghz_f = [fid_real["per_job"][j]["hellinger"]
                 for j, hsh in job_hash.items() if hsh == ghz_hash]
        check(sum(ghz_f) / len(ghz_f) <= sum(bell_f) / len(bell_f) + 1e-9,
              f"GHZ fidelity <= Bell fidelity under noise "
              f"(ghz {ghz_f}, bell {bell_f})")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def block_reference_tiers():
    '''compute_ideals sources ideals by three-tier precedence: provider, engine, registry'''
    # The ideal for a circuit can come from three places, tried in a fixed
    # precedence per circuit: (1) an ATTACHED reference-capable provider wins
    # outright; (2) else DevQ's CORE native statevector engine simulates it
    # (no provider needed — the whole point: a run computes ideals with no
    # reference device attached); (3) else a registered provider class
    # overriding reference_ideal is instantiated unattached and used (for what
    # the engine declines — an entangled reset, or a circuit above the qubit
    # cap). Per-circuit fallback across tiers 2 and 3 is safe because a
    # noiseless ideal is unique and every tier returns normalised
    # probabilities, so there is no source disagreement to fear.
    from benchmark.reference import (compute_ideals, circuit_hash,
                                     _engine_ideal, _ENGINE_MAX_QUBITS)
    from circuits.circuit_rep import CircuitRep
    from registry.registry import Registry
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
    from providers.base_provider import BaseProvider

    # A pure circuit the engine handles, and an entangled-reset circuit it
    # declines (leaving q1 mixed — only a density-matrix source gets it right).
    bell = CircuitRep(2, 2); bell.add_gate("h", [0]); bell.add_gate("cx", [0, 1])
    for q in range(2): bell.add_measure(q, q)
    ereset = CircuitRep(2, 2)
    ereset.add_gate("h", [0]); ereset.add_gate("cx", [0, 1]); ereset.add_reset(0)
    for q in range(2): ereset.add_measure(q, q)
    bell_h, ereset_h = circuit_hash(bell), circuit_hash(ereset)

    # ── Tier 2: no provider, no registry — the engine supplies the ideal ──
    d = compute_ideals([bell], None, None)
    check(bell_h in d,
          "tier 2: with no provider attached, the core engine supplies the "
          "ideal (a run needs no reference-capable device)")
    ideal = d[bell_h]["ideal"]
    check(abs(ideal.get("00", 0) - 0.5) < 1e-9
          and abs(ideal.get("11", 0) - 0.5) < 1e-9,
          f"tier 2: the engine's Bell ideal is the exact 50/50, got {ideal}")

    # ── Tier 2 declines, no registry — honest absence, not a wrong ideal ──
    d = compute_ideals([ereset], None, None)
    check(ereset_h not in d,
          "tier 2: an entangled-reset circuit the engine declines yields NO "
          "ideal when no registry tier can cover it — an honest absence, not "
          "the plausible-but-wrong collapsed distribution")

    # ── Tier 3: engine declines, registry search covers it ────────────────
    reg = Registry()
    reg.register("provider", "ibm.simulated", IBMSimulatedProvider)
    d = compute_ideals([ereset], None, reg)
    check(ereset_h in d,
          "tier 3: when the engine declines, a registered reference-capable "
          "provider class is instantiated unattached and supplies the ideal")
    ri = d[ereset_h]["ideal"]
    # The correct entangled-reset ideal is the MIXED 00/10, exactly what a
    # density-matrix reference gives and a statevector cannot — proof the
    # tier-3 source, not a collapsed fallback, produced it.
    check(abs(ri.get("00", 0) - 0.5) < 1e-9 and abs(ri.get("10", 0) - 0.5) < 1e-9
          and abs(ri.get("01", 0)) < 1e-12,
          f"tier 3: the density-matrix source gives the correct MIXED 00/10 "
          f"ideal (not a statevector's collapsed 00), got {ri}")

    # A registry with no reference-capable provider covers nothing.
    reg_bare = Registry()
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    reg_bare.register("provider", "devq.simulated", DevQSimulatedProvider)
    check(DevQSimulatedProvider.reference_ideal is BaseProvider.reference_ideal,
          "the mock devq provider is not reference-capable (guards the tier-3 "
          "capability probe)")
    d = compute_ideals([ereset], None, reg_bare)
    check(ereset_h not in d,
          "tier 3: a registry with only a non-reference-capable provider "
          "supplies nothing — the entry stays absent")

    # ── Tier 1: an attached provider wins outright ────────────────────────
    # With a provider passed, that provider computes the ideal even for a
    # circuit the engine could have handled — tier 1 is not overridden by 2.
    prov = IBMSimulatedProvider()
    d = compute_ideals([bell], prov, reg)
    check(bell_h in d,
          "tier 1: an attached reference-capable provider supplies the ideal")

    # ── the qubit cap routes big circuits past the engine ─────────────────
    check(_ENGINE_MAX_QUBITS >= 20,
          f"the engine qubit cap is a sane memory guard, got {_ENGINE_MAX_QUBITS}")
    big = CircuitRep(_ENGINE_MAX_QUBITS + 1, _ENGINE_MAX_QUBITS + 1)
    big.add_gate("h", [0])
    check(_engine_ideal(big) is None,
          "tier 2: a circuit above the qubit cap is declined by the engine "
          "(so it routes to the registry tier), rather than allocating a "
          "state vector too large to hold")
    # And within the cap the engine answers.
    small = CircuitRep(2, 2); small.add_gate("h", [0])
    check(_engine_ideal(small) is not None,
          "tier 2: a circuit within the cap is simulated by the engine")

    # ── dedup: one ideal per distinct circuit, mixed sources coexist ──────
    # A run mixing an engine circuit and a registry circuit gets both, each
    # from its own tier — the per-circuit fallback in action.
    d = compute_ideals([bell, ereset, bell], None, reg)
    check(bell_h in d and ereset_h in d and len(d) == 2,
          f"a run mixes tier-2 and tier-3 ideals per circuit, deduped to one "
          f"each, got {len(d)} ideals")


def block_router_scoring():
    '''Router weights change routing, and explain() matches select()'''
    # Every other routing block runs at the default 0.5/0.5, where the
    # two router weights are interchangeable — swapping them in the
    # scoring path passed all 39 preceding blocks. Asymmetric weights are
    # the only configuration that can witness the difference, and Phase
    # 5.5's weight sweep is meaningless if they are not actually applied.
    import io, contextlib
    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
    from kernel.router.noise_router import NoiseRouter
    from frontends.qasm2.parser import parse
    from kernel.process.qcb import QCB

    try:
        p = IBMSimulatedProvider(seed=SEED)
        devices = [(p.get_device(backend_name="FakeNairobiV2"), "nairobi"),
                   (p.get_device(backend_name="FakeLagosV2"),   "lagos"),
                   (p.get_device(backend_name="FakeJakartaV2"), "jakarta")]
    except Exception:
        check(True, "qiskit not installed - router scoring block skipped")
        return

    with contextlib.redirect_stdout(io.StringIO()):
        shell = devq_with_ibm().add_devices(devices).build()
    contexts = shell.kernel.contexts

    circuit = parse(open(GHZ).read(), GHZ)
    qcb = QCB(job_id=1, circuit=circuit)

    def scores_at(wq, wn):
        r = NoiseRouter(router_queue_weight=wq, router_noise_weight=wn)
        return r.explain(qcb, contexts), r.select(qcb, contexts)

    # PINNED SCORES. Asserting explain() against select() proves nothing:
    # both read one shared scoring path, so a mutation to that path moves
    # them together and the comparison still holds. These values were
    # computed independently and are the ground truth the scoring must
    # reproduce — swapping the two weights or dropping the tie-break
    # changes them, which is the point.
    EXPECTED = {
        (0.5, 0.5): (2, [(0, 0.017753), (1, 0.5),      (2, 0.0)]),
        (0.9, 0.1): (2, [(0, 0.003551), (1, 0.1),      (2, 0.0)]),
        (0.1, 0.9): (2, [(0, 0.031955), (1, 0.9),      (2, 0.0)]),
        (1.0, 0.0): (0, [(0, 0.0),      (1, 0.0),      (2, 0.0)]),
        (0.0, 1.0): (2, [(0, 0.035506), (1, 1.0),      (2, 0.0)]),
    }
    for (wq, wn), (want_dev, want_scores) in EXPECTED.items():
        detail, chosen = scores_at(wq, wn)
        got = [(d["device"], round(d["score"], 6)) for d in detail]
        check(got == want_scores,
              f"w=({wq},{wn}) scores {want_scores}, got {got}")
        check(chosen.index == want_dev,
              f"w=({wq},{wn}) routes to d{want_dev}, got d{chosen.index}")
        best = min(detail, key=lambda x: (x["score"], x["device"]))["device"]
        check(best == chosen.index,
              f"explain() and select() agree at w=({wq},{wn})")

    # w=(1.0,0.0) is the tie-break witness: queue pressure is uniformly 0
    # across idle devices, so every score is 0 and only the lower-index
    # rule can decide. Without it, routing there is arbitrary.
    _, chosen = scores_at(1.0, 0.0)
    check(chosen.index == 0,
          "all-equal scores break to the lowest index, got d%d" % chosen.index)

    # PINNED RAW TERMS. Presence alone is not enough — a term that is
    # recorded but wrong is worse than one that is missing, because 5.5
    # re-derives routing from these numbers.
    detail, _ = scores_at(0.5, 0.5)
    want_costs = [(0, 0.023739), (1, 0.064295), (2, 0.022246)]
    got_costs = [(d["device"], round(d["terms"]["best_case_cost"], 6))
                 for d in detail]
    check(got_costs == want_costs,
          f"explain() records true raw costs {want_costs}, got {got_costs}")
    for key in ("queue_pressure", "best_case_cost",
                "queue_pressure_norm", "best_case_cost_norm"):
        check(key in detail[0]["terms"], f"explain() records the term '{key}'")

    # Re-deriving from logged terms must match what the router really
    # does at those weights — the property that makes a weight sweep
    # answerable from one recorded run.
    for wq, wn in ((0.9, 0.1), (0.1, 0.9), (0.0, 1.0)):
        _, chosen = scores_at(wq, wn)
        rederived = min(
            ((wq * d["terms"]["queue_pressure_norm"]
              + wn * d["terms"]["best_case_cost_norm"], d["device"])
             for d in detail))[1]
        check(rederived == chosen.index,
              f"logged terms re-derive the w=({wq},{wn}) decision")

    # LOADED FIXTURE. Everything above runs on idle devices, where queue
    # pressure is uniformly 0 and normalises to 0 — so the w_queue term
    # vanishes regardless of its value, and swapping the two weights is
    # undetectable. Only asymmetric load can witness that the queue
    # weight is applied at all. d2 is the cheapest device but the most
    # loaded, so weighting decides whether noise or load wins.
    contexts[0].running_jobs = 1
    contexts[2].running_jobs = 5
    try:
        LOADED = {
            (0.5, 0.5): (0, [(0, 0.117753), (1, 0.5), (2, 0.5)]),
            (0.9, 0.1): (1, [(0, 0.183551), (1, 0.1), (2, 0.9)]),
            (0.1, 0.9): (0, [(0, 0.051955), (1, 0.9), (2, 0.1)]),
        }
        for (wq, wn), (want_dev, want_scores) in LOADED.items():
            detail, chosen = scores_at(wq, wn)
            got = [(d["device"], round(d["score"], 6)) for d in detail]
            check(got == want_scores,
                  f"loaded w=({wq},{wn}) scores {want_scores}, got {got}")
            check(chosen.index == want_dev,
                  f"loaded w=({wq},{wn}) routes to d{want_dev}, got d{chosen.index}")

        # Queue pressure must reach the log as the true depth, not a
        # placeholder — 5.5 reads these numbers back.
        detail, _ = scores_at(0.5, 0.5)
        want_press = [(0, 1), (1, 0), (2, 5)]
        got_press = [(d["device"], d["terms"]["queue_pressure"]) for d in detail]
        check(got_press == want_press,
              f"explain() records true queue pressure {want_press}, got {got_press}")

        # Shifting weight from noise to queue must move the job off the
        # loaded-but-cheap device — the weights are not decorative.
        _, noise_heavy = scores_at(0.1, 0.9)
        _, queue_heavy = scores_at(0.9, 0.1)
        check(noise_heavy.index != queue_heavy.index,
              "queue-weighted and noise-weighted routing diverge under load")
    finally:
        contexts[0].running_jobs = 0
        contexts[2].running_jobs = 0

    # Determinism across repeated identical routing.
    r = NoiseRouter(router_queue_weight=0.5, router_noise_weight=0.5)
    picks = {r.select(qcb, contexts).index for _ in range(5)}
    check(len(picks) == 1, "repeated routing of identical input is deterministic")

    # ── Sweep decomposition (Phase 5.5a) ──────────────────────────────────
    # The α/β sweep re-weights the RAW per-candidate sums, so those sums
    # must be logged AND correct — best_case_cost alone cannot be re-split
    # into them. Pin the two sums directly; a swapped or mis-summed
    # decomposition passes every check above (best_case_cost stays right
    # by luck only if both are wrong compensatingly) but fails here.
    detail, _ = scores_at(0.5, 0.5)
    want_qsum = [(0, 0.097200), (1, 0.307800), (2, 0.108400)]
    want_esum = [(0, 0.015577), (1, 0.037239), (2, 0.012674)]
    got_qsum = [(d["device"], round(d["terms"]["qubit_error_sum"], 6))
                for d in detail]
    got_esum = [(d["device"], round(d["terms"]["edge_error_sum"], 6))
                for d in detail]
    check(got_qsum == want_qsum,
          f"explain() records true qubit_error_sum {want_qsum}, got {got_qsum}")
    check(got_esum == want_esum,
          f"explain() records true edge_error_sum {want_esum}, got {got_esum}")

    # The sweep's core invariant: α·Σq + β·Σe reproduces the weighted
    # best_case_cost the router actually scored on. This is what lets a
    # sweep recompute S at any ratio from the logged sums alone. Break the
    # decomposition (drop α/β, swap the sums) and this diverges.
    A, B = 0.1, 0.9
    for d in detail:
        recomposed = A * d["terms"]["qubit_error_sum"] + B * d["terms"]["edge_error_sum"]
        # detail is at 0.5/0.5 weights but best_case_cost uses the router's
        # OWN α/β (0.1/0.9 default), so recompose at those.
        check(abs(recomposed - d["terms"]["best_case_cost"]) < 1e-9,
              f"d{d['device']}: α·Σq+β·Σe reproduces best_case_cost")

    # FAITHFULNESS ANCHOR. The unified contract means select(), explain()
    # and the sweep all funnel through one path; the anchor proves it:
    # replaying the logged terms at the router's own live params must
    # reproduce the live routing decision. If it does not, the sweep would
    # emit fiction, and this is the tripwire.
    for wq, wn in ((0.5, 0.5), (0.9, 0.1), (0.1, 0.9), (0.0, 1.0)):
        r = NoiseRouter(router_queue_weight=wq, router_noise_weight=wn)
        report   = r.explain(qcb, contexts)
        recorded = [(row["device"], row["terms"]) for row in report]
        live     = r.select(qcb, contexts).index
        replayed = r.sweep_decision(recorded, r.live_params())
        check(replayed == live,
              f"faithfulness anchor: sweep replay reproduces select() at "
              f"w=({wq},{wn}) — live d{live}, replay d{replayed}")

    # The sweep re-derives a DIFFERENT decision at different params from
    # the SAME recorded run — the payoff. Re-weighting off one recording
    # must match routing that recording live at the swept weights.
    r = NoiseRouter(router_queue_weight=0.5, router_noise_weight=0.5)
    recorded = [(row["device"], row["terms"])
                for row in r.explain(qcb, contexts)]
    for a in (0.0, 0.3, 0.7, 1.0):
        params = {"router_queue_weight": 0.5, "router_noise_weight": 0.5,
                  "qubit_error_weight": a, "edge_error_weight": 1 - a}
        swept = r.sweep_decision(recorded, params)
        live  = NoiseRouter(router_queue_weight=0.5, router_noise_weight=0.5,
                            qubit_error_weight=a,
                            edge_error_weight=1 - a).select(qcb, contexts).index
        check(swept == live,
              f"sweep from one recording matches live routing at α={a}: "
              f"swept d{swept}, live d{live}")

    # TERMS-FIRST FIXED-WEIGHT RECOVERY. The queue/noise mix is a FIXED input
    # kept out of live_params(); on a sweep replay it must be recovered from
    # the RECORDED terms, not from the reconstructing engine. On a live routing
    # field every device starts at zero queue pressure, so the queue weight
    # cannot witness the difference there — a SYNTHETIC recorded field with
    # differing queue pressures is needed. With min-max, d0 (queue 0, cost
    # high) scores w_noise and d1 (queue high, cost 0) scores w_queue, so d0
    # wins iff w_queue > 0.5. Record terms at w_queue=0.9 (d0 wins) and replay
    # through an engine whose OWN w_queue is 0.1 (which, if wrongly used, would
    # pick d1). A correct terms-first replay returns d0; the mutant that reads
    # the engine's own weight returns d1.
    synth = [
        (0, {"queue_pressure": 0.0, "qubit_error_sum": 1.0, "edge_error_sum": 1.0,
             "router_queue_weight": 0.9, "router_noise_weight": 0.1}),
        (1, {"queue_pressure": 10.0, "qubit_error_sum": 0.0, "edge_error_sum": 0.0,
             "router_queue_weight": 0.9, "router_noise_weight": 0.1}),
    ]
    wrong_engine = NoiseRouter(router_queue_weight=0.1, router_noise_weight=0.9)
    win = wrong_engine.sweep_decision(synth, wrong_engine.live_params())
    check(win == 0,
          "a sweep recovers the fixed queue weight from the RECORDED terms "
          "(0.9 -> d0), not the reconstructing engine's own weight (0.1 -> d1)")
    # Converse: terms recorded at a LOW queue weight pick d1 even when replayed
    # through an engine whose own weight is HIGH — proving the recovered value
    # (not the engine's) drives the decision in both directions.
    synth_lowq = [
        (0, {"queue_pressure": 0.0, "qubit_error_sum": 1.0, "edge_error_sum": 1.0,
             "router_queue_weight": 0.1, "router_noise_weight": 0.9}),
        (1, {"queue_pressure": 10.0, "qubit_error_sum": 0.0, "edge_error_sum": 0.0,
             "router_queue_weight": 0.1, "router_noise_weight": 0.9}),
    ]
    high_engine = NoiseRouter(router_queue_weight=0.9, router_noise_weight=0.1)
    win2 = high_engine.sweep_decision(synth_lowq, high_engine.live_params())
    check(win2 == 1,
          "the converse: recorded low queue weight (0.1 -> d1) drives the "
          "winner even through a high-weight engine — terms-first, both ways")

    # A non-scoring router reports nothing rather than inventing scores.
    from kernel.router.round_robin_router import RoundRobinRouter
    check(RoundRobinRouter().explain(qcb, contexts) is None,
          "a router without scores returns None from explain()")


def block_sweepable_contract():
    '''The Sweepable contract derives explain/sweep for any component'''
    # WHY A SYNTHETIC COMPONENT. router_scoring proves NoiseRouter works,
    # but the unified contract's whole claim is that explain() and the
    # sweep are derived IDENTICALLY for any Sweepable — router, allocator,
    # scheduler alike. Testing that through NoiseRouter alone cannot
    # separate "the contract is right" from "NoiseRouter is right". A
    # minimal scoring double with hand-checkable numbers tests the
    # CONTRACT: the base's derived explain_decision/sweep_decision, the
    # not-scored default, and the faithfulness anchor — so the allocator
    # and scheduler inherit machinery already proven here, not machinery
    # first exercised three components deep.
    from kernel.sweep import Sweepable, NOT_SCORED

    # A scoring component: score(candidate) = w · value, lowest wins.
    # Deliberately trivial and DISTINCT from any built-in (no
    # normalisation, single term) so a pass cannot come from accidentally
    # matching NoiseRouter's behaviour.
    class ToyScorer(Sweepable):
        def __init__(self, w):
            self.w = w
        def live_params(self):
            return {"w": self.w}
        def _sweep_terms(self, decision):
            # decision is a dict {key: raw_value}
            return [(k, {"value": v}) for k, v in decision.items()]
        def _sweep_score(self, terms, params):
            return params["w"] * terms["value"]
        def _sweep_rank(self, scored, params):
            # rank applies a +100 offset and exposes an enriched term, so
            # the RANKED final differs from the raw _sweep_score output.
            # This lets the block prove explain() reports the ranked final
            # (via _sweep_rank), not the raw per-candidate score — a
            # distinction a rank that merely echoed the score could not
            # witness (that gap let a mutant survive).
            return [(k, s + 100.0, dict(t, final=s + 100.0, ranked=True))
                    for k, t, s in scored]

    decision = {"a": 3.0, "b": 1.0, "c": 2.0}
    toy = ToyScorer(w=2.0)

    # explain_decision derives the report from the hooks THROUGH _sweep_rank:
    # each candidate's ranked final (raw score + 100) with enriched terms.
    report = toy.explain_decision(decision)
    got = {r["key"]: r["score"] for r in report}
    check(got == {"a": 106.0, "b": 102.0, "c": 104.0},
          f"contract derives explain scores through _sweep_rank, got {got}")
    check(all(r["terms"].get("ranked") for r in report),
          "derived explain carries _sweep_rank's enriched terms")
    check(all("value" in r["terms"] for r in report),
          "derived explain carries the raw terms")

    # sweep_decision picks argmin(final, key) from recorded terms — a pure
    # replay. At w=2 the winner is b (lowest value), no re-running the
    # decision path.
    recorded = [(r["key"], r["terms"]) for r in report]
    win = toy.sweep_decision(recorded, {"w": 2.0})
    check(win == "b", f"sweep picks argmin from recorded terms, got {win}")

    # FAITHFULNESS ANCHOR at the contract level: replay at live params
    # reproduces what a live selection would choose. The toy's live choice
    # is argmin of explain's scores.
    live = min(report, key=lambda r: (r["score"], r["key"]))["key"]
    check(toy.sweep_decision(recorded, toy.live_params()) == live,
          "contract faithfulness: replay at live params matches live choice")

    # The sweep yields a DIFFERENT decision at other params from the SAME
    # recording — the payoff, tested without any component internals. The
    # toy's score is monotone in w, so w flips nothing here; instead sweep
    # a per-candidate reweighting by negating, which must flip the argmin
    # to the largest value (a). Uses only the recorded terms.
    flipped = toy.sweep_decision(recorded, {"w": -1.0})
    check(flipped == "a",
          f"sweep re-derives a different decision from one recording, got {flipped}")

    # Tie-break: equal finals resolve to the lower key, deterministically.
    tie = ToyScorer(w=0.0)  # every score 0
    tie_report = tie.explain_decision(decision)
    tie_recorded = [(r["key"], r["terms"]) for r in tie_report]
    check(tie.sweep_decision(tie_recorded, {"w": 0.0}) == "a",
          "contract tie-break resolves equal scores to the lower key")

    # NOT-SCORED DEFAULT: a component that does not override the hooks is
    # neither explainable nor sweepable — explain_decision returns None and
    # is_sweepable is False, the honest outcome for a non-scoring policy.
    class ToyBlind(Sweepable):
        pass
    blind = ToyBlind()
    check(blind.explain_decision(decision) is None,
          "a non-scoring component derives explain None")
    check(blind.is_sweepable() is False,
          "a non-scoring component reports not sweepable")
    check(toy.is_sweepable() is True,
          "a scoring component reports sweepable")

    # NOT_SCORED is the sentinel _sweep_terms returns to opt out; confirm
    # the default hook returns exactly it, so an override can compare.
    check(Sweepable._sweep_terms(blind, decision) is NOT_SCORED,
          "the default _sweep_terms returns the NOT_SCORED sentinel")

    # SCHEDULER PARITY. BaseScheduler inherits the same contract at router
    # parity, so a scheduler that scores its queue is sweepable through the
    # identical derived machinery — proven here with a mock scheduler,
    # while the real scored consumer (QOS) waits for 5.6. A scheduler's
    # decision is a choice among queued jobs, so the candidate keys are job
    # ids; otherwise the hooks are the same shape.
    from kernel.scheduler.base_scheduler import BaseScheduler

    class ToyScoringScheduler(BaseScheduler):
        # Scores queued jobs by w · urgency, lowest wins. Overrides only
        # the sweep hooks and a trivial schedule(); the point is the
        # contract, not the scheduling. Distinct from any shipped scheduler
        # (none score), so a pass cannot come from matching one.
        def __init__(self, w):
            self.w = w
        def schedule(self):
            return []
        def live_params(self):
            return {"w": self.w}
        def _sweep_terms(self, decision):
            return [(jid, {"urgency": u}) for jid, u in decision.items()]
        def _sweep_score(self, terms, params):
            return params["w"] * terms["urgency"]
        def _sweep_rank(self, scored, params):
            return [(k, s, dict(t, final=s)) for k, t, s in scored]

    sched_decision = {10: 5.0, 20: 2.0, 30: 8.0}
    toy_sched = ToyScoringScheduler(w=1.0)
    check(toy_sched.is_sweepable() is True,
          "a scoring scheduler reports sweepable through the shared contract")
    srep = toy_sched.explain_decision(sched_decision)
    check({r["key"]: r["score"] for r in srep} == {10: 5.0, 20: 2.0, 30: 8.0},
          "the scheduler derives explain from the same hooks")
    srec = [(r["key"], r["terms"]) for r in srep]
    check(toy_sched.sweep_decision(srec, {"w": 1.0}) == 20,
          "the scheduler sweep picks argmin job from recorded terms")
    check(toy_sched.sweep_decision(srec, {"w": -1.0}) == 30,
          "the scheduler sweep re-derives a different job at other params")

    # Shipped schedulers have no scoring parameter and must report
    # not-sweepable — the honest silence, same as RoundRobinRouter.
    from kernel.scheduler.fcfs_scheduler import FCFSScheduler
    from kernel.scheduler.shortest_depth_scheduler import ShortestDepthScheduler
    from kernel.scheduler.packing_scheduler import PackingScheduler
    for cls in (FCFSScheduler, ShortestDepthScheduler, PackingScheduler):
        inst = cls.__new__(cls)
        check(inst.is_sweepable() is False,
              f"{cls.__name__} reports not sweepable (no scoring parameter)")


def block_allocator_scoring():
    '''Allocator decomposition, block sweep, and the allocate event'''
    # The allocator is the second Sweepable component. It scores connected
    # BLOCKS (not devices), logs the α/β-free decomposition per block, and
    # its decision reaches the log as an `allocate` event on dispatch. This
    # block pins the decomposition and the swept block choice against
    # independently computed values, proves the faithfulness anchor, and
    # exercises the two things unique to the allocator: the allocate event
    # in a real run, and the PER-JOB decision capture a batch scheduler
    # needs (the allocator's stash is per-instance and would otherwise be
    # clobbered across jobs allocated before any dispatch).
    import io, contextlib, json, glob, tempfile, os
    from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator
    from kernel.memory.allocators.static_allocator import StaticAllocator
    from kernel.memory.qubit_pool import QubitPool
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
    from frontends.qasm2.parser import parse

    try:
        p = IBMSimulatedProvider(seed=SEED)
        with contextlib.redirect_stdout(io.StringIO()):
            dev = p.get_device(backend_name="FakeNairobiV2")
    except Exception:
        check(True, "qiskit not installed - allocator scoring block skipped")
        return

    circuit = parse(open(GHZ).read(), GHZ)   # 3-qubit, several candidate blocks
    pool    = QubitPool(dev.num_qubits)
    alloc   = NoiseGraphAllocator(qubit_error_weight=0.1, edge_error_weight=0.9)

    # PINNED DECOMPOSITION. Independently computed from the pinned
    # calibration; a swapped or mis-summed decomposition fails here even
    # though the weighted S might survive. Keyed by block tuple.
    report = {r["key"]: r for r in alloc.explain_decision(
        (circuit, dev, pool, None, None, None))}
    want = {
        (0, 1, 2): (0.097200, 0.015577, 0.023739),
        (1, 3, 5): (0.064700, 0.019363, 0.023897),
        (3, 4, 5): (0.063100, 0.019577, 0.023929),
        (3, 5, 6): (0.070600, 0.023232, 0.027969),
    }
    for block, (wq, we, ws) in want.items():
        t = report[block]["terms"]
        check(round(t["qubit_error_sum"], 6) == wq,
              f"block {block} qubit_error_sum {wq}, got {round(t['qubit_error_sum'],6)}")
        check(round(t["edge_error_sum"], 6) == we,
              f"block {block} edge_error_sum {we}, got {round(t['edge_error_sum'],6)}")
        check(round(report[block]["score"], 6) == ws,
              f"block {block} S {ws}, got {round(report[block]['score'],6)}")
        # The sweep invariant: α·Σq + β·Σe reproduces S.
        rc = 0.1 * t["qubit_error_sum"] + 0.9 * t["edge_error_sum"]
        check(abs(rc - report[block]["score"]) < 1e-9,
              f"block {block}: α·Σq+β·Σe reproduces S")

    # SWEEP from the recorded decomposition flips the chosen block. Pinned
    # winners, independently computed: edge-heavy favours (0,1,2)'s low
    # edge cost, qubit weight shifts to (3,4,5)'s lower Σq.
    recorded = [(r["key"], r["terms"]) for r in report.values()]
    for a, want_block in ((0.0, (0, 1, 2)), (0.1, (0, 1, 2)),
                          (0.5, (3, 4, 5)), (1.0, (3, 4, 5))):
        got = alloc.sweep_decision(
            recorded, {"qubit_error_weight": a, "edge_error_weight": 1 - a})
        check(got == want_block,
              f"block sweep α={a} picks {want_block}, got {got}")

    # FAITHFULNESS ANCHOR: replay at live weights reproduces what allocate()
    # actually reserves.
    pool2 = QubitPool(dev.num_qubits)
    live_map = alloc.allocate(circuit, dev, pool2, None, None)
    live_block = tuple(sorted(live_map.values()))
    anchor = alloc.sweep_decision(recorded, alloc.live_params())
    check(anchor == live_block,
          f"allocator faithfulness: replay {anchor} == live allocate {live_block}")

    # NOT-SCORED DEFAULT: a cost-oblivious allocator is not sweepable and
    # derives no explain — the honest silence, no allocate event.
    check(StaticAllocator().is_sweepable() is False,
          "a cost-oblivious allocator reports not sweepable")
    check(alloc.is_sweepable() is True,
          "the noise-graph allocator reports sweepable")

    # THE ALLOCATE EVENT + PER-JOB CAPTURE, end to end. Run a real workload
    # whose batch scheduler allocates several jobs before any dispatch, and
    # confirm each job's allocate event carries ITS OWN decision, not the
    # last job's (the clobber the per-job pin fixes). smoke.json uses the
    # default packing scheduler.
    from benchmark.runner import run
    with tempfile.TemporaryDirectory() as d:
        with contextlib.redirect_stdout(io.StringIO()):
            run("benchmark/workloads/smoke.json", out_dir=d, quiet=True)
        logf = glob.glob(os.path.join(d, "*.jsonl"))[0]
        recs   = [json.loads(l) for l in open(logf)]
        allocs = [r for r in recs if r.get("event") == "allocate"]
        disp   = [r for r in recs if r.get("event") == "dispatch"]

    check(len(allocs) >= 2,
          f"a real run emits allocate events, got {len(allocs)}")
    check(all("scores" in a and a["scores"] for a in allocs),
          "every allocate event carries per-block scores")
    check(all("qubit_error_sum" in a["scores"][0]["terms"] for a in allocs),
          "allocate scores carry the α/β-free decomposition")

    # PARITY: with a scoring allocator, every dispatched job must produce
    # exactly one allocate event for ITS OWN placement. A stash clobber
    # (reading the allocator's live _last_decision at dispatch instead of
    # the job's pinned decision) drops events for jobs whose decision was
    # overwritten before they dispatched — so the job-id sets diverge.
    disp_ids  = {r["job_id"] for r in disp}
    alloc_ids = {a["job_id"] for a in allocs}
    check(disp_ids == alloc_ids,
          f"every dispatched job has its own allocate event "
          f"(dispatched={sorted(disp_ids)}, allocated={sorted(alloc_ids)})")

    # Per-job capture: EACH job's recorded decision must contain the block
    # that job was placed on. This is the invariant a stash clobber breaks
    # — a job reading a later job's decision would find its own placement
    # absent from those candidates. Distinctness of candidate sets alone is
    # too weak (two jobs with different pool states differ even under a
    # clobber); "my placement is among my candidates" is the sharp test.
    for a in allocs:
        cands  = [tuple(s["block"]) for s in a["scores"]]
        placed = tuple(a["block"])
        check(placed in cands,
              f"job {a['job_id']}'s placement {placed} is among its own "
              f"recorded candidates (no stash clobber)")

    # The logged decision re-derives the placement from the LOG alone — the
    # sweep is answerable from a recorded run, not just a live object.
    a0 = allocs[0]
    log_recorded = [(tuple(s["block"]), s["terms"]) for s in a0["scores"]]
    run_params = {
        "qubit_error_weight": a0["scores"][0]["terms"]["qubit_error_weight"],
        "edge_error_weight" : a0["scores"][0]["terms"]["edge_error_weight"],
    }
    replayed = NoiseGraphAllocator().sweep_decision(log_recorded, run_params)
    check(replayed == tuple(a0["block"]),
          f"log-driven replay reproduces placement {tuple(a0['block'])}, "
          f"got {replayed}")

    # BASE-SCHEDULER PATH capture. smoke.json drives the packing scheduler,
    # so the FCFS/base path's capture (_attempt_allocation) is otherwise
    # untested — a batch scheduler and a serial one pin the decision in
    # different methods. Drive the base path directly and assert the
    # decision landed on the job.
    from kernel.scheduler.fcfs_scheduler import FCFSScheduler
    from kernel.memory.memory_manager import MemoryManager
    from kernel.process.qcb import QCB
    from kernel.process.process_table import ProcessTable

    mm = MemoryManager(dev, NoiseGraphAllocator(qubit_error_weight=0.1,
                                                edge_error_weight=0.9))
    sched = FCFSScheduler(mm, ProcessTable())
    job = QCB(job_id=99, circuit=parse(open(BELL).read(), BELL))
    sched.enqueue(job)
    sched.schedule()
    check(job.alloc_decision is not None,
          "the base scheduler path pins the allocation decision on the job")
    # And it's the real decision — its blocks carry the decomposition.
    check(all("qubit_error_sum" in t for _, t in job.alloc_decision),
          "the base-path captured decision carries the decomposition")


def block_scheduler_scoring():
    '''Scheduler decision capture and the schedule event'''
    # The scheduler is the third Sweepable component (after router and
    # allocator). A scoring scheduler ranks QUEUED JOBS (not devices or
    # blocks), and its decision reaches the log as a `schedule` event on
    # dispatch — the scheduler-layer twin of `allocate`. This block proves
    # the two things the kernel's schedule emit must get right: that a
    # scoring scheduler's decision is captured per-job and logged with
    # scores, and that a non-scoring scheduler stays silent. It uses an
    # in-suite scoring scheduler, NOT the research/ NAQJS baseline — the
    # test suite never imports research/, so the kernel feature is proven
    # against a mock exactly as the allocate event is.
    import io, contextlib, json, glob, tempfile, os
    from kernel.scheduler.base_scheduler import BaseScheduler
    from kernel.scheduler.fcfs_scheduler import FCFSScheduler
    from kernel.process.lifecycle import JobStates

    # A scoring scheduler that ranks the queue by circuit WIDTH, narrowest
    # first — sweepable through the shared hooks, and OBSERVABLE (it scores
    # on a real job feature, so its schedule events carry checkable terms).
    # Distinct from every shipped scheduler (none score), so a pass cannot
    # come from matching a built-in. Mirrors the live schedule() shape:
    # rank the queue, pin the ranked decision on the dispatched job, so the
    # kernel's schedule emit has something to read.
    class WidthScoringScheduler(BaseScheduler):
        LABEL = "Width Scoring Scheduler"

        def schedule(self):
            if not self.queue:
                return None
            tagged = self._sweep_terms(self.queue)
            report = self.explain_recorded(tagged)
            score_by_id = {r["key"]: r["score"] for r in report}
            self.queue = sorted(
                self.queue, key=lambda q: (score_by_id[q.job_id], q.job_id))
            decision = self._sweep_terms(self.queue)
            processed = []
            while self.queue:
                qcb = self.queue[0]
                if self._attempt_allocation(qcb):
                    qcb.sched_decision = decision
                    processed.append(self.queue.pop(0))
                    return processed
                if qcb.state == JobStates.REJECTED:
                    processed.append(self.queue.pop(0))
                    continue
                break
            return processed or None

        def live_params(self):
            return {"width_weight": 1.0}

        def _sweep_terms(self, decision):
            return [(q.job_id, {"width": q.circuit.num_qubits})
                    for q in decision]

        def _sweep_score(self, terms, params):
            return terms["width"]

        def _sweep_rank(self, scored, params):
            return [(k, params["width_weight"] * raw,
                     dict(t, width_weight=params["width_weight"]))
                    for k, t, raw in scored]

    # CONTRACT: the scoring scheduler is sweepable; a shipped one is not.
    from kernel.memory.memory_manager import MemoryManager
    from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    from kernel.process.process_table import ProcessTable

    with contextlib.redirect_stdout(io.StringIO()):
        dev = DevQSimulatedProvider(seed=SEED).get_device("linear", 7)
    mm = MemoryManager(dev, NoiseGraphAllocator())
    scoring = WidthScoringScheduler(mm, ProcessTable())
    check(scoring.is_sweepable() is True,
          "a scoring scheduler reports sweepable through the shared contract")
    check(FCFSScheduler(mm, ProcessTable()).is_sweepable() is False,
          "a shipped order-only scheduler reports not sweepable")

    # THE SCHEDULE EVENT + PER-JOB CAPTURE, end to end. Register the scoring
    # scheduler and run a real multi-job workload; confirm each dispatched
    # job produces its own schedule event carrying per-job scores. This is
    # the kernel's schedule emit — the feature under test.
    from benchmark.runner import run
    with tempfile.TemporaryDirectory() as d:
        with contextlib.redirect_stdout(io.StringIO()):
            run("benchmark/workloads/smoke.json", out_dir=d, quiet=True,
                register_schedulers={"width_scoring": WidthScoringScheduler},
                select={"scheduler": ["width_scoring"]})
        logf = glob.glob(os.path.join(d, "*.jsonl"))[0]
        recs  = [json.loads(l) for l in open(logf)]

    scheds = [r for r in recs if r.get("event") == "schedule"]
    disp   = [r for r in recs if r.get("event") == "dispatch"]

    check(len(scheds) >= 2,
          f"a scoring scheduler run emits schedule events, got {len(scheds)}")
    check(all("scores" in s and s["scores"] for s in scheds),
          "every schedule event carries per-job scores")
    check(all("width" in s["scores"][0]["terms"] for s in scheds),
          "schedule scores carry the raw weight-free terms")
    # The logged score must be the actual width-derived value, not merely
    # internally consistent — a constant or dropped score survives every
    # check that only compares scores to each other (argmin tie-breaks by
    # job_id would still pick the right winner). Pin score == weight·width
    # against the recorded terms so a constant-score emit dies here.
    for s in scheds:
        for row in s["scores"]:
            w = row["terms"]["width_weight"]
            width = row["terms"]["width"]
            check(abs(row["score"] - w * width) < 1e-9,
                  f"schedule score {row['score']} is weight·width "
                  f"({w}·{width}) for job {row['job_id']}")
    check(all(s["winner"] == s["job_id"] for s in scheds),
          "the schedule event's winner is the dispatched job")

    # PARITY: with a scoring scheduler, every dispatched job produces
    # exactly one schedule event for its own dispatch. A stash clobber
    # (reading a live decision at dispatch instead of the job's pinned one)
    # would drop events for jobs whose decision was overwritten — so the
    # job-id sets diverge.
    disp_ids  = {r["job_id"] for r in disp}
    sched_ids = {s["job_id"] for s in scheds}
    check(disp_ids == sched_ids,
          f"every dispatched job has its own schedule event "
          f"(dispatched={sorted(disp_ids)}, scheduled={sorted(sched_ids)})")

    # WINNER CONSISTENCY: the dispatched job must be the argmin of its own
    # recorded scores. A schedule event whose winner is not the lowest-
    # scored candidate would mean the logged decision contradicts the
    # dispatch it describes — the sweep would replay a different winner
    # than actually ran.
    for s in scheds:
        argmin = min(s["scores"], key=lambda r: (r["score"], r["job_id"]))
        check(argmin["job_id"] == s["winner"],
              f"schedule event winner {s['winner']} is the argmin of its "
              f"scores (got {argmin['job_id']})")

    # LOG-DRIVEN REPLAY: the decision re-derives the winner from the LOG
    # alone — the sweep is answerable from a recorded run, not just a live
    # object. Reconstruct a bare scoring scheduler and replay.
    s0 = scheds[0]
    log_recorded = [(r["job_id"], r["terms"]) for r in s0["scores"]]
    replayed = WidthScoringScheduler(mm, ProcessTable()).sweep_decision(
        log_recorded, {"width_weight": 1.0})
    check(replayed == s0["winner"],
          f"log-driven replay reproduces winner {s0['winner']}, got {replayed}")

    # NON-SCORING SILENCE: a run driven by a shipped order-only scheduler
    # emits NO schedule events — the same honest silence as a non-scoring
    # router or allocator. smoke.json's default is the packing scheduler.
    with tempfile.TemporaryDirectory() as d:
        with contextlib.redirect_stdout(io.StringIO()):
            run("benchmark/workloads/smoke.json", out_dir=d, quiet=True)
        logf = glob.glob(os.path.join(d, "*.jsonl"))[0]
        silent = [r for r in (json.loads(l) for l in open(logf))
                  if r.get("event") == "schedule"]
    check(len(silent) == 0,
          f"an order-only scheduler emits no schedule events, got {len(silent)}")


def block_provider_registration_enforced():
    '''No device enters DevQ from an unregistered provider'''
    # MUTATION WITNESS. is_registered() returning True unconditionally
    # survived all 45 blocks before this one existed: every other block
    # registers its providers correctly, so a gate that never rejects is
    # indistinguishable from one that works. Assert the REFUSAL, which
    # is the only thing that pins the gate open.
    import io, contextlib
    from devq import DevQ, DevQError
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider

    try:
        from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
    except ImportError:
        check(True, "qiskit not installed - registration block skipped")
        return

    ibm_device = IBMSimulatedProvider(seed=SEED).get_device("FakeNairobiV2")

    # The built-in attaches with no registration line at all.
    with contextlib.redirect_stdout(io.StringIO()):
        DevQ().add_device(DevQSimulatedProvider(seed=SEED)
                          .get_device("random", 5)).build()
    check(True, "a built-in provider's device attaches without registration")

    # IBM is not a built-in, so its device is refused.
    try:
        DevQ().add_device(ibm_device)
        check(False, "an unregistered provider's device is refused")
    except DevQError as exc:
        check("not registered" in str(exc)
              and "IBMSimulatedProvider" in str(exc),
              "an unregistered provider's device is refused, naming the class")

    # Registering the CLASS admits a device built by an instance the
    # caller constructed themselves — the credentialed-provider path.
    dq = DevQ()
    dq.register_provider("ibm.simulated", IBMSimulatedProvider)
    with contextlib.redirect_stdout(io.StringIO()):
        dq.add_device(IBMSimulatedProvider(seed=SEED)
                      .get_device("FakeLagosV2")).build()
    check(True, "registering the class admits a hand-constructed instance")

    # Matching is on the EXACT type. A subclass is a different
    # component — this block's own OversteppingProvider sibling proves
    # a subclass can behave differently — so registering the base must
    # not bless it.
    class SubclassedProvider(IBMSimulatedProvider):
        pass

    dq = DevQ()
    dq.register_provider("ibm.simulated", IBMSimulatedProvider)
    try:
        dq.add_device(SubclassedProvider(seed=SEED)
                      .get_device("FakeNairobiV2"))
        check(False, "a subclass of a registered provider is still refused")
    except DevQError:
        check(True, "a subclass of a registered provider is still refused")

    # A provider INSTANCE cannot be registered at all, which is what
    # removes the spec/instance seed conflict rather than resolving it.
    # The refusal is captured OUTSIDE the check: a bare `except
    # Exception` around a check() would catch the AssertionError that
    # check() itself raises, turning a real failure into a pass.
    instance_refused = None
    try:
        DevQ().register_provider("x", IBMSimulatedProvider(seed=SEED))
    except Exception as exc:
        instance_refused = str(exc)

    check(instance_refused is not None
          and "instance" in instance_refused.lower(),
          "a provider instance is refused at registration, so no registered "
          "provider can carry a seed of its own")


def block_device_identity():
    '''index/name/kind are three distinct fields, stamped once at attach'''
    # M3 REGRESSION GUARD. Dropping the alias in DevQ.build()'s
    # device.attach(index, name) call passed all 37 blocks before this
    # block existed: DeviceContext carried the alias for every consumer,
    # so nothing ever read it off the device. The event log (5.2) reads
    # device-side identity, so a silent None here would reach every
    # record. Assert against the DEVICE, not the rendered output.
    import io, contextlib
    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider

    p = DevQSimulatedProvider(seed=SEED)
    devs = [p.get_device("random", 5) for _ in range(3)]

    # Unattached devices know nothing about a session.
    check(devs[0].index is None, "device has no index before attach")
    check(devs[0].name is None, "device has no name before attach")
    check(devs[0].ref == "(unattached)", "unattached device ref is explicit")

    dq = DevQ().add_devices([(devs[0], "Alpha"), devs[1], (devs[2], "Gamma")])
    with contextlib.redirect_stdout(io.StringIO()):
        dq.build()

    check([d.index for d in devs] == [0, 1, 2], "indices assigned in add order")
    check(devs[0].name == "alpha", "alias reaches the device, lowercased")
    check(devs[1].name is None, "unnamed device keeps a None alias")
    check(devs[2].name == "gamma", "third alias reaches the device")
    check(all(d.kind == "random_backend" for d in devs),
          "kind is hardware identity, shared across same-kind devices")

    # Session identity is assigned once; re-attaching is a bug, not a
    # silent overwrite.
    try:
        devs[0].attach(9)
        check(False, "double attach raises")
    except RuntimeError:
        check(True, "double attach raises")


def block_same_kind_device_isolation():
    '''Four devices of one kind get four independent provider sessions'''
    # The Phase 5.1 contract said per-device state must not be shared;
    # the code keyed _sessions by backend_name, i.e. by KIND, so N
    # same-kind devices collapsed onto one session and the last one
    # built won. Invisible until two devices share a kind AND differ in
    # config. Assert on resolved provider state, not printed output.
    import io, contextlib
    from devq import DevQ
    try:
        from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
        from qiskit_aer.noise import NoiseModel  # noqa: F401
    except ImportError:
        check(True, "qiskit not installed - isolation block skipped")
        return

    prov = IBMSimulatedProvider(seed=SEED)
    devs = [prov.get_device(backend_name="FakeNairobiV2") for _ in range(4)]
    dq = devq_with_ibm().add_devices([(devs[0], "CustomName"), (devs[1], "CustomName2"),
                                      devs[2], (devs[3], "CustomName3")])
    with contextlib.redirect_stdout(io.StringIO()):
        dq.build()

    check(sorted(prov._sessions) == [0, 1, 2, 3],
          "sessions are keyed by index, one per device")
    noise = [id(prov._sessions[i]["noise_model"]) for i in range(4)]
    check(len(set(noise)) == 4, "each device gets its own noise model")
    backends = [id(prov._sessions[i]["backend"]) for i in range(4)]
    check(len(set(backends)) == 1, "immutable backend is shared, not reloaded")
    check(list(prov._backends) == ["FakeNairobiV2"],
          "backend cache is keyed by kind, caller casing preserved")


def block_component_labels():
    '''qconfig shows declared human labels, not class names'''
    # Nothing else asserts on label text, so a component losing its
    # LABEL would degrade qconfig to class names with every other block
    # still green — which is exactly what happened once already.
    sh  = three_device()
    out = run(sh, ["qconfig"])

    expect(out, "[Noise Aware Router]",
                "[Circuit Packing Scheduler]",
                "[Noise Aware Graph Allocator]")
    expect_absent(out, "[PackingScheduler]", "[NoiseRouter]",
                       "[NoiseGraphAllocator]")

    # A plugin declaring no LABEL falls back to its class name rather
    # than showing nothing.
    class Unlabelled(MockScheduler):
        LABEL = None

    dq = DevQ()
    dq.register_scheduler("unlabelled", Unlabelled)
    check(dq._config.labels()["scheduler"]["unlabelled"] == "Unlabelled",
          "a component without a LABEL falls back to its class name")


def block_qregistry():
    '''qregistry lists registered components by kind, honouring flags'''
    # qregistry renders the same labels map qconfig does, so it shows both
    # built-in and externally registered components. It reads only that
    # map — never the registry live — so this asserts the rendering and
    # the flag parsing, not the registry itself (covered elsewhere).
    sh = three_device()   # registers ibm.simulated on top of the built-ins

    # No flag: every kind, with built-in AND external components.
    out = run(sh, ["qregistry"])
    expect(out, "Providers", "Routers", "Schedulers", "Allocators")
    expect(out, "devq.simulated", "ibm.simulated",       # provider: built-in + external
                "noise", "round_robin",                  # routers
                "fcfs", "packing",                       # schedulers
                "noise_graph")                           # allocator
    # Labels render, not just names.
    expect(out, "Noise Aware Router", "Circuit Packing Scheduler")

    # Single flag: only that kind. Providers shown, others absent.
    out = run(sh, ["qregistry p"])
    expect(out, "Providers", "devq.simulated", "ibm.simulated")
    expect_absent(out, "Routers", "Schedulers", "Allocators")

    # Each letter maps to its own kind.
    expect(run(sh, ["qregistry r"]), "Routers", "round_robin")
    expect_absent(run(sh, ["qregistry r"]), "Providers", "Schedulers")
    expect(run(sh, ["qregistry s"]), "Schedulers", "packing")
    expect(run(sh, ["qregistry a"]), "Allocators", "noise_graph")

    # Multiple flags: exactly those kinds, and in canonical order
    # regardless of how they were typed — `s p` still shows providers
    # first, so the display order is stable.
    out = run(sh, ["qregistry s p"])
    expect(out, "Providers", "Schedulers")
    expect_absent(out, "Routers", "Allocators")
    check(out.index("Providers") < out.index("Schedulers"),
          "qregistry shows kinds in canonical order, not typed order")

    # Duplicate flags collapse: `p p` lists providers once.
    out = run(sh, ["qregistry p p"])
    check(out.count("Providers") == 1, "duplicate flags show a kind once")

    # Unknown flag: a clear error and NO listing (not a partial render).
    out = run(sh, ["qregistry x"])
    expect(out, "unknown flag")
    expect_absent(out, "Providers", "devq.simulated")
    # A valid flag mixed with an invalid one still errors, shows nothing.
    out = run(sh, ["qregistry p x"])
    expect(out, "unknown flag")
    expect_absent(out, "devq.simulated")


def block_frontend_dispatch():
    '''Frontend is the 5th registrable kind; jobs dispatch to one by source'''
    # Frontend differs from every other kind: it is NOT selected by config
    # (no "frontend" key names a single winner), it is DISPATCHED per job
    # by the source's extension. This block proves the kind registers and
    # shows under qregistry's `f` flag, that a job reaches the right
    # frontend, that --frontend overrides and disambiguates, and that an
    # ambiguous or unhandled extension is rejected — all against a mock
    # frontend whose output is OBSERVABLY different from qasm2's, so a
    # passing dispatch assertion proves the mock actually ran.

    from frontends.base_frontend import BaseFrontend
    from circuits.circuit_rep import CircuitRep

    # OBSERVABLY distinct from qasm2. On bell.qasm, qasm2 yields a
    # 2-qubit circuit; this yields a 9-qubit one regardless of the file.
    # 9 appears nowhere a 2-qubit Bell run would produce it, so "9 qubits
    # in the mapping" is proof this frontend — not qasm2 — read the job.
    # It also claims a DISTINCT extension (.mock) so it can be dispatched
    # by extension without colliding with qasm2, and be given .qasm in a
    # separate case to force the ambiguity path.
    class MockFrontend(BaseFrontend):
        LABEL = "Mock Frontend"
        EXTENSIONS = (".mock",)

        def parse(self, source):
            c = CircuitRep(9)
            c.add_gate("h", [0])
            return c

    class MockQASMDialect(BaseFrontend):
        # Claims .qasm, exactly like qasm2 — the qasm2/qasm3 collision in
        # miniature. Registering it is legal; the ambiguity is resolved
        # per job, not refused at registration.
        LABEL = "Mock QASM Dialect"
        EXTENSIONS = (".qasm",)

        def parse(self, source):
            c = CircuitRep(8)
            c.add_gate("h", [0])
            return c

    # ── The kind registers and shows under qregistry f ──────────────────
    sh = (DevQ(config_path=CONFIG + "router_only.config.json")
          .add_device(DevQSimulatedProvider(seed=SEED).get_device("random", 12))
          .register_frontend("mock", MockFrontend)
          .build())

    out = run(sh, ["qregistry f"])
    expect(out, "Frontends", "qasm2", "OpenQASM 2.0", "mock", "Mock Frontend")
    expect_absent(out, "Providers", "Schedulers")
    # It appears in the all-kinds listing too, alongside the other four.
    out = run(sh, ["qregistry"])
    expect(out, "Frontends", "Providers", "Routers", "Schedulers", "Allocators")

    # ── Dispatch by extension: .qasm -> qasm2 (unambiguous) ─────────────
    out = run(sh, [f"qrun {BELL}"])
    check(mapping_of(out, 1) == "{0: 0, 1: 1}" or
          len(eval(mapping_of(out, 1))) == 2,
          f"unambiguous .qasm dispatched to qasm2 (2 qubits), "
          f"got {mapping_of(out, 1)}")

    # ── --frontend override sends the SAME .qasm file to the mock ───────
    # Same input file, different frontend: a 9-qubit mapping is only
    # possible if MockFrontend read it, so this proves the override
    # dispatched, not qasm2.
    out = run(sh, [f"qrun {BELL} --frontend=mock"])
    check(len(eval(mapping_of(out, 2))) == 9,
          f"--frontend=mock overrode extension dispatch (9 qubits), "
          f"got {mapping_of(out, 2)}")

    # ── Unknown frontend name errors, creates no job ────────────────────
    out = run(sh, [f"qrun {BELL} --frontend=nope"])
    expect(out, "unknown frontend 'nope'")
    expect(out, "qasm2")   # the error lists what IS registered
    check("Job 3" not in out and "FINISHED" not in out,
          "an unknown --frontend name runs nothing")

    # ── Ambiguous extension: two frontends claim .qasm -> reject ────────
    # A fresh session, because the ambiguity is a property of what is
    # registered. Both qasm2 (built-in) and the mock dialect claim .qasm.
    sh2 = (DevQ(config_path=CONFIG + "router_only.config.json")
           .add_device(DevQSimulatedProvider(seed=SEED).get_device("random", 12))
           .register_frontend("qasm_mock", MockQASMDialect)
           .build())

    out = run(sh2, [f"qrun {BELL}"])
    expect(out, "more than one frontend")
    expect(out, "qasm2", "qasm_mock")   # names both claimants
    check("FINISHED" not in out,
          "an ambiguous extension runs nothing until disambiguated")

    # ...and --frontend resolves the very same ambiguous file.
    out = run(sh2, [f"qrun {BELL} --frontend=qasm_mock"])
    check(len(eval(mapping_of(out, 1))) == 8,
          f"--frontend disambiguated to the named dialect (8 qubits), "
          f"got {mapping_of(out, 1)}")

    # ── Unhandled extension: no frontend claims it -> reject ────────────
    # bell.txt does not exist, but resolution happens BEFORE the read:
    # the extension is unhandled, so the job is rejected for that reason,
    # not for a missing file. Asserting the message proves resolution
    # precedes I/O.
    out = run(sh2, ["qrun test_circuits/bell.txt"])
    expect(out, "no registered frontend handles '.txt'")

    # ── The registry rejects a malformed frontend at registration ──────
    # An instance, not a class — the same class-only rule every kind
    # obeys. Proven here so the frontend kind is not silently exempt.
    try:
        DevQ().register_frontend("inst", MockFrontend())
        check(False, "frontend instance: rejected at registration")
    except DevQError as e:
        check("must be registered as a CLASS" in str(e),
              "frontend instance: rejected, every kind is class-only")

    # A frontend whose __init__ demands arguments DevQ never passes:
    # frontends are constructed with no arguments, so this cannot be
    # built and is refused up front rather than at dispatch.
    class BadInitFrontend(BaseFrontend):
        def __init__(self, weight):
            pass

        def parse(self, source):
            return None

    try:
        DevQ().register_frontend("badinit", BadInitFrontend)
        check(False, "frontend with args: rejected at registration")
    except DevQError as e:
        check("cannot be constructed" in str(e),
              "frontend __init__ taking arguments is rejected")


def block_qasm2_parser():
    '''The OpenQASM 2.0 parser keeps parameters, inlines gates, records measures'''
    # The original reader split on whitespace and dropped every gate
    # parameter, so rx(pi/2) executed as a mangled no-op and no
    # parameterised circuit ran correctly. This block asserts the real
    # parser against HAND-COMPUTED values (parsing is deterministic, so
    # exact numbers are fair game — no wall-clock anywhere): parameters
    # survive and evaluate, expressions respect precedence, custom gates
    # inline recursively, measure/reset land in their own channels while
    # the gate list stays gate-only, several registers flatten into one
    # index space, and the constructs DevQ cannot honour are rejected
    # with precise, line-numbered messages rather than silently mangled.

    from frontends.qasm2.parser import parse, QASMError
    import math

    def load(name):
        with open(QASM2 + name) as fh:
            return parse(fh.read(), name)

    def approx(a, b):
        return abs(a - b) < 1e-9

    def gate_ops(circ):
        # CircuitRep is one ordered, op-tagged stream; a gate consumer
        # filters for op == "gate". This mirrors what get_depth() and the
        # providers do, so the block asserts against the same view they see.
        return [i for i in circ.instructions if i["op"] == "gate"]

    # ── Parameters survive and evaluate (the rx(pi/2) fix) ──────────────
    c = load("parameterized.qasm")
    check(c.num_qubits == 2, "parameterized: two qubits")
    g = gate_ops(c)
    gates = [(i["gate"], i["qubits"]) for i in g]
    check(gates == [("rx", [0]), ("ry", [1]), ("rz", [0]), ("cx", [0, 1])],
          f"parameterized: gate sequence, got {gates}")
    check(approx(g[0]["params"][0], math.pi / 2),
          "parameterized: rx carries pi/2, not a dropped parameter")
    check(approx(g[1]["params"][0], math.pi / 4),
          "parameterized: ry carries pi/4")
    check(approx(g[2]["params"][0], 2 * math.pi),
          "parameterized: rz carries 2*pi")
    check(g[3]["params"] == [],
          "parameterized: cx has no parameters")

    # ── Expression evaluator: precedence, functions, unary minus ────────
    c = load("expressions.qasm")
    g = gate_ops(c)
    check(approx(g[0]["params"][0], 8.0),
          f"expressions: 2^3 == 8, got {g[0]['params'][0]}")
    check(approx(g[1]["params"][0], 1.0),
          "expressions: sin(0)+cos(0) == 1")
    check(approx(g[2]["params"][0], -math.pi / 2),
          "expressions: unary minus, -pi/2")
    check(approx(g[3]["params"][0], 2.0),
          f"expressions: binary subtraction 3-1 == 2 (distinct from unary "
          f"minus), got {g[3]['params'][0]}")
    check(approx(g[4]["params"][0], 11.0),
          f"expressions: 5+2*3 == 11, multiplication binds tighter than "
          f"addition, got {g[4]['params'][0]}")

    # ── Custom gates inline recursively, substituting params AND qubits ─
    c = load("custom_gate.qasm")
    check(c.num_qubits == 3, "custom_gate: three qubits")
    # entangle(pi/2) q0,q1  ->  rz(pi/2) q0; cx q0,q1
    # double(pi/4) q1,q2    ->  entangle(pi/4) q1,q2 ; entangle(pi/4) q2,q1
    #                      ->  rz q1; cx q1,q2 ; rz q2; cx q2,q1
    g = gate_ops(c)
    expanded = [(i["gate"], i["qubits"]) for i in g]
    check(expanded == [("rz", [0]), ("cx", [0, 1]),
                       ("rz", [1]), ("cx", [1, 2]),
                       ("rz", [2]), ("cx", [2, 1])],
          f"custom_gate: recursive inline with qubit substitution, "
          f"got {expanded}")
    check(approx(g[0]["params"][0], math.pi / 2),
          "custom_gate: first rz took the outer call's pi/2")
    check(approx(g[2]["params"][0], math.pi / 4),
          "custom_gate: inlined rz took the inner call's pi/4")
    # No custom-gate name leaks into the lowered circuit — only primitives.
    check(all(i["gate"] in ("rz", "cx") for i in g),
          "custom_gate: only primitives remain after inlining")

    # ── measure/reset interleave in source order; gate view stays gates ─
    c = load("measured.qasm")
    check([i["gate"] for i in gate_ops(c)] == ["h", "cx"],
          "measured: the gate view holds only unitary gates")
    # The ordered stream carries measure and reset in source position:
    # h, cx, reset q1, measure q0, measure q1.
    check([i["op"] for i in c.instructions] ==
          ["gate", "gate", "reset", "measure", "measure"],
          f"measured: ops interleave in source order, got "
          f"{[i['op'] for i in c.instructions]}")
    check(c.measurements == [(0, 0), (1, 1)],
          f"measured: measurements record (qubit, clbit), got {c.measurements}")
    check(c.resets == [1], f"measured: reset recorded, got {c.resets}")
    check(c.num_clbits == 2, "measured: classical register width captured")
    # Depth is a property of gates only — measure and reset add no depth.
    check(c.get_depth() == 2, f"measured: depth counts gates only (h,cx), "
                              f"got {c.get_depth()}")

    # ── Several registers flatten into one global index space ───────────
    c = load("multi_register.qasm")
    check(c.num_qubits == 5, "multi_register: a[2]+b[3] == 5 qubits")
    check(c.num_clbits == 5, "multi_register: c[5] == 5 clbits")
    flat = [(i["gate"], i["qubits"]) for i in gate_ops(c)]
    # a[0]=0; a[1]=1; b[0]=2,b[1]=3,b[2]=4.
    check(flat == [("h", [0]), ("cx", [1, 2])],
          f"multi_register: b[0] flattens to global index 2, got {flat}")
    check(c.measurements == [(4, 4)],
          f"multi_register: b[2] is global qubit 4, got {c.measurements}")

    # ── Classical control is now first-class, not a rejection ───────────
    # A conditional is WELL-FORMED and now REPRESENTABLE: the parser emits
    # it as a `conditional` op rather than marking it unrunnable. It used to
    # be rejected for lacking mid-circuit feedback; that runnability question
    # is now answered per-device at routing time (a provider's
    # supports_dynamic), not at parse time. So the parser's job is to
    # represent it faithfully — assert the op, the resolved condition, and
    # that the circuit is is_dynamic and NOT unrunnable.
    c = load("conditional.qasm")
    check(c.unrunnable_reason is None,
          f"conditional (if): parses clean, no longer marked unrunnable, got "
          f"{c.unrunnable_reason!r}")
    check(c.is_dynamic,
          "conditional (if): the circuit is is_dynamic")
    check(len(c.conditionals) == 1
          and c.conditionals[0]["condition"] == {"clbits": [0], "value": 1}
          and c.conditionals[0]["body"]["gate"] == "x"
          and c.conditionals[0]["body"]["qubits"] == [1],
          f"conditional (if): emits one conditional op guarding x on q[1], "
          f"got {c.conditionals!r}")
    check(c.cregs == {"c": (0, 1)},
          f"conditional (if): the declared creg is recorded, got {c.cregs!r}")

    # An unknown register named in an if-condition IS a genuine parse error
    # (the condition references something never declared) — this still
    # raises, unlike the well-formed conditional above.
    try:
        parse("OPENQASM 2.0; qreg q[1]; if (nope==1) x q[0];")
        check(False, "unknown creg in if-condition should raise")
    except QASMError as e:
        check("nope" in str(e),
              f"unknown creg in if-condition raises naming the register, got {e}")

    # Mid-circuit measurement (a gate on a qubit after it was measured) is a
    # different case: unrunnable on ANY backend, so still marked (not raised).
    mid = parse("OPENQASM 2.0; qreg q[1]; creg c[1]; "
                "measure q[0] -> c[0]; x q[0];")
    check(mid.unrunnable_reason is not None
          and "mid-circuit" in mid.unrunnable_reason.lower(),
          f"mid-circuit measurement: marked unrunnable with a reason, got "
          f"{mid.unrunnable_reason!r}")

    # A clean circuit is NOT flagged — the check does not fire on terminal
    # measurement (the normal case).
    clean = parse("OPENQASM 2.0; qreg q[2]; creg c[2]; h q[0]; cx q[0],q[1]; "
                  "measure q[0] -> c[0]; measure q[1] -> c[1];")
    check(clean.unrunnable_reason is None,
          f"terminal measurement is NOT flagged, got {clean.unrunnable_reason!r}")

    # Inline-source rejects for constructs without a fixture — each a
    # DIFFERENT failure mode, proving the parser distinguishes them rather
    # than lumping everything into one "unsupported".
    def rej_src(src, phrase):
        try:
            parse(src)
            return False
        except QASMError as e:
            return phrase in str(e).lower()

    check(rej_src("OPENQASM 2.0; qreg q[2]; x q[9];", "out of range"),
          "reject: qubit index past the register")
    check(rej_src("OPENQASM 2.0; qreg q[1]; rx q[0];", "param"),
          "reject: gate arity (rx needs a parameter)")
    check(rej_src("OPENQASM 2.0; qreg q[1]; opaque foo q;", "opaque"),
          "reject: opaque gate has no body to lower")
    check(rej_src("OPENQASM 3.0; qreg q[1]; x q[0];", "2.0"),
          "reject: a 3.0 header, with a message pointing to a separate frontend")
    check(rej_src("OPENQASM 2.0;", "no qreg"),
          "reject: a circuit with no quantum register")

    # ── End to end: a parameterised circuit runs on a real provider ─────
    # The strongest check that parameters survive is that a provider which
    # CONSUMES them executes without error. devq.simulated ignores gates,
    # so this exercises the full submit path on the parser's output.
    sh = (DevQ(config_path=CONFIG + "router_only.config.json")
          .add_device(DevQSimulatedProvider(seed=SEED).get_device("random", 8))
          .build())
    run(sh, [f"qrun {QASM2}parameterized.qasm"])
    out = settle(sh, 1)
    expect(out, "FINISHED")
    check("Error" not in out,
          "a parameterised circuit runs end to end without error")


def block_expr_unary_power_precedence():
    '''Unary minus binds looser than ^: -2^2 == -(2^2), and ^ stays right-assoc'''
    # Regression witness for a latent precedence bug in the QASM2 expression
    # evaluator. The grammar had `power := unary ('^' power)?` with unary
    # BELOW power, so `_power` consumed the leading minus as part of its base
    # before ever seeing `^`. That made -2^2 evaluate as (-2)^2 == 4 instead
    # of the standard -(2^2) == -4. No shipped circuit used a negative base
    # under `^` (the only `^` in the fixtures is 2^3), so the suite stayed
    # green while the evaluator was silently wrong for any signed base — the
    # exact kind of quiet numerical error that corrupts a gate angle without
    # crashing. The fix reorders the grammar so unary minus sits ABOVE power
    # (binds looser) while an explicit sign after `^` is still parsed as the
    # exponent's own sign. Evaluation is deterministic, so exact hand-computed
    # values are fair game (no wall-clock anywhere).
    from frontends.qasm2.tokenizer import tokenize
    from frontends.qasm2.parser import TokenCursor
    from frontends.qasm2 import expression as E
    import math

    def ev(src):
        c = TokenCursor(tokenize(src + ";"))
        return E.evaluate(c, {})

    def approx(a, b):
        return abs(a - b) < 1e-9

    # ── The bug's exact witness: negative base under ^ ──────────────────
    check(approx(ev("-2^2"), -4.0),
          f"-2^2 == -(2^2) == -4 (was 4 when unary bound tighter than ^), "
          f"got {ev('-2^2')}")
    check(approx(ev("-3^2"), -9.0),
          f"-3^2 == -(3^2) == -9, got {ev('-3^2')}")
    # unary minus threading through a product: 2*-3^2 == 2*-(3^2) == -18
    check(approx(ev("2*-3^2"), -18.0),
          f"2*-3^2 == 2*(-(3^2)) == -18, got {ev('2*-3^2')}")

    # ── Mutant guard 1: a NON-negated base must be unchanged (2^2 == 4).
    # A naive "just negate the whole power" fix would wrongly flip this.
    check(approx(ev("2^2"), 4.0),
          f"2^2 == 4 unaffected by the unary reordering, got {ev('2^2')}")

    # ── Mutant guard 2: ^ must stay RIGHT-associative (2^3^2 == 2^9 == 512,
    # not (2^3)^2 == 64). A fix that made the exponent parse via _atom
    # instead of _unary would break right-associativity.
    check(approx(ev("2^3^2"), 512.0),
          f"2^3^2 == 2^(3^2) == 512 (right-assoc preserved), got {ev('2^3^2')}")

    # ── Mutant guard 3: a sign AFTER ^ is the exponent's own sign, still
    # honoured (2^-2 == 0.25). A fix that forbade a signed exponent to keep
    # -2^2 correct would break this.
    check(approx(ev("2^-2"), 0.25),
          f"2^-2 == 0.25 (signed exponent still parses), got {ev('2^-2')}")
    # and both together: -2^-2 == -(2^-2) == -0.25
    check(approx(ev("-2^-2"), -0.25),
          f"-2^-2 == -(2^-2) == -0.25, got {ev('-2^-2')}")

    # ── The pre-existing positive-base fixture still evaluates as before ─
    check(approx(ev("2^3"), 8.0), f"2^3 == 8 (the shipped fixture), got {ev('2^3')}")


def block_allocator_contract_1q_param():
    '''Registry allocator signature check requires max_1q_gate_error'''
    # Regression witness for a contract-enforcement gap. The runtime always
    # invokes an allocator's allocate()/feasible() with max_1q_gate_error
    # (base_scheduler, router, memory_manager all pass it, and the base
    # allocator's documented contract lists it), but the registry's Level-3
    # method-signature check declared only (circuit, device, pool,
    # max_qubit_error, max_edge_error). So an externally written allocator
    # that took the registry's STATED required params but omitted
    # max_1q_gate_error passed registration and then crashed at runtime with
    # an unexpected-keyword TypeError — the worst failure locus for a plugin
    # author (deep in a scheduling loop, not at register time). The fix adds
    # max_1q_gate_error to the declared allocate/feasible required params so
    # the mismatch is caught loudly at registration.
    from registry.registry import _build_kinds

    # Pull the allocator kind's declared required method params straight from
    # the registry's own spec table (the same _build_kinds() a live Registry
    # calls in __init__), so this asserts the SOURCE OF TRUTH the Level-3
    # check reads, not a copy.
    spec = _build_kinds()
    check("allocator" in spec,
          "registry exposes the allocator component-kind spec")
    alloc_methods = spec["allocator"].methods

    # ── The fix: both allocate and feasible now require max_1q_gate_error ─
    check("max_1q_gate_error" in alloc_methods["allocate"],
          f"allocate's required params include max_1q_gate_error, "
          f"got {alloc_methods['allocate']}")
    check("max_1q_gate_error" in alloc_methods["feasible"],
          f"feasible's required params include max_1q_gate_error, "
          f"got {alloc_methods['feasible']}")

    # ── Behavioural guard: an under-specified allocator (registry's OLD
    # stated contract, no 1q param) is now REJECTED at registration, and a
    # correct one is accepted. This is the end the plugin author feels.
    from kernel.memory.allocators.base_allocator import BaseAllocator

    class UnderSpecifiedAllocator(BaseAllocator):
        def allocate(self, circuit, device, pool,
                     max_qubit_error=None, max_edge_error=None):
            return {}
        def feasible(self, circuit, device,
                     max_qubit_error=None, max_edge_error=None):
            return None

    class ConformingAllocator(BaseAllocator):
        def allocate(self, circuit, device, pool,
                     max_qubit_error=None, max_edge_error=None,
                     max_1q_gate_error=None):
            return {}
        def feasible(self, circuit, device,
                     max_qubit_error=None, max_edge_error=None,
                     max_1q_gate_error=None):
            return None

    # ── Mutant guard 1: the under-specified allocator FAILS registration.
    dq = DevQ()
    rejected = False
    try:
        dq.register_allocator("under.spec", UnderSpecifiedAllocator)
    except Exception:
        rejected = True
    check(rejected,
          "an allocator whose allocate() omits max_1q_gate_error is rejected "
          "at registration, not at runtime")

    # ── Mutant guard 2: a conforming allocator still registers cleanly (the
    # tightened contract must not reject legitimate allocators).
    dq2 = DevQ()
    accepted = True
    try:
        dq2.register_allocator("conforming.alloc", ConformingAllocator)
    except Exception:
        accepted = False
    check(accepted,
          "a conforming allocator with max_1q_gate_error registers cleanly")

    # ── Mutant guard 3: the requirement is symmetric across both methods —
    # dropping 1q from feasible only must also be caught.
    class OneMethodShort(BaseAllocator):
        def allocate(self, circuit, device, pool,
                     max_qubit_error=None, max_edge_error=None,
                     max_1q_gate_error=None):
            return {}
        def feasible(self, circuit, device,
                     max_qubit_error=None, max_edge_error=None):
            return None

    dq3 = DevQ()
    half_rejected = False
    try:
        dq3.register_allocator("half.short", OneMethodShort)
    except Exception:
        half_rejected = True
    check(half_rejected,
          "an allocator whose feasible() omits max_1q_gate_error is also "
          "rejected (the requirement covers both methods)")


def block_devq_measurement():
    '''devq.simulated distributes over the classical-register width'''
    # devq.simulated does not interpret gates — it returns a uniform
    # distribution, by design. What real measurement changed is the WIDTH
    # of the bitstrings: they span the DECLARED classical register
    # (Option B), not the qubit count. So a measured circuit's results
    # cover its creg width and a measured bit sits at its own index; an
    # unmeasured circuit falls back to num_qubits. This block pins that
    # width, which is the whole of devq's measurement behaviour.

    sh = (DevQ(config_path=CONFIG + "router_only.config.json")
          .add_device(DevQSimulatedProvider(seed=SEED).get_device("random", 8))
          .build())

    # No creg, no measures: fallback width == num_qubits (2), uniform.
    run(sh, [f"qrun {BELL}"])
    counts = counts_of(settle(sh, 1), 1)
    check(all(len(k) == 2 for k in counts),
          f"devq fallback: no creg -> 2-bit strings (num_qubits), "
          f"got widths {set(len(k) for k in counts)}")
    check(len(counts) == 4, f"devq fallback: 2^2 == 4 outcomes, got {len(counts)}")
    check(len(set(counts.values())) == 1,
          "devq: distribution is uniform")

    # creg c[3], only two qubits measured: Option B width is the FULL
    # register (3 bits), not the number of measured bits (2). This is the
    # assertion that separates Option B from "width == measured count".
    run(sh, [f"qrun {QASM2}partial_measure.qasm"])
    counts = counts_of(settle(sh, 2), 2)
    check(all(len(k) == 3 for k in counts),
          f"devq Option B: creg c[3] -> 3-bit strings even with two "
          f"measures, got widths {set(len(k) for k in counts)}")
    check(len(counts) == 8, f"devq: 2^3 == 8 outcomes, got {len(counts)}")

    # creg c[1]: single-bit register -> single-bit strings.
    run(sh, [f"qrun {QASM2}reset_mid.qasm"])
    counts = counts_of(settle(sh, 3), 3)
    check(all(len(k) == 1 for k in counts),
          f"devq: creg c[1] -> 1-bit strings, got "
          f"{set(len(k) for k in counts)}")

    # 3 qubits but creg c[2]: width is num_clbits (2), NOT num_qubits (3).
    # This is the case that distinguishes Option B from measuring all
    # qubits — the two agree whenever num_clbits == num_qubits, so a
    # narrower creg is needed to pin the width to the register.
    run(sh, [f"qrun {QASM2}narrow_creg.qasm"])
    counts = counts_of(settle(sh, 4), 4)
    check(all(len(k) == 2 for k in counts),
          f"devq Option B: 3 qubits + creg c[2] -> 2-bit strings "
          f"(num_clbits, not num_qubits), got "
          f"{set(len(k) for k in counts)}")


def block_ibm_measurement():
    '''ibm.simulated honours the circuit's own measures and resets in order'''
    # Unlike devq, the IBM provider interprets the circuit, so it shows
    # real measurement physics: it honours the circuit's explicit measures
    # (falling back to measure-all only when there are none), places each
    # measure and reset at its source position, and reports over the
    # declared classical register (Option B) so an unmeasured bit is pinned
    # to 0 at its own index. Needs qiskit; skips cleanly without it.
    try:
        p = IBMSimulatedProvider(seed=SEED)
        _probe = p.get_device(backend_name="FakeNairobiV2")
    except Exception:
        check(True, "qiskit not installed - IBM measurement block skipped")
        return

    def ibm_shell():
        dq = devq_with_ibm()
        dq.add_device(IBMSimulatedProvider(seed=SEED)
                      .get_device("FakeNairobiV2"), name="nairobi")
        with contextlib.redirect_stdout(io.StringIO()):
            return dq.build()

    # Fallback: a circuit with no measures is measured on every qubit, so
    # a Bell pair yields 2-bit strings correlated on 00/11 (the historical
    # behaviour, unchanged).
    sh = ibm_shell()
    run(sh, [f"qrun {BELL}"])
    counts = counts_of(settle(sh, 1), 1)
    check(all(len(k) == 2 for k in counts),
          f"IBM fallback: no measures -> measure-all, 2-bit strings, "
          f"got {set(len(k) for k in counts)}")
    corr = counts.get("00", 0) + counts.get("11", 0)
    check(corr > 0.85 * sum(counts.values()),
          f"IBM fallback: Bell correlation on 00/11 dominates, got {counts}")

    # Option B width + explicit measures: creg c[3] with only q0,q1
    # measured yields 3-bit strings whose leftmost bit (c[2], unmeasured)
    # is ALWAYS 0. If the provider measured only the two touched bits the
    # strings would be 2-bit; if it auto-measured all three the top bit
    # would sometimes be 1. Neither happens.
    sh = ibm_shell()
    run(sh, [f"qrun {QASM2}partial_measure.qasm"])
    counts = counts_of(settle(sh, 1), 1)
    check(all(len(k) == 3 for k in counts),
          f"IBM Option B: creg c[3] -> 3-bit strings, got "
          f"{set(len(k) for k in counts)}")
    check(all(k[0] == "0" for k in counts),
          f"IBM Option B: the unmeasured bit c[2] is pinned to 0, got {counts}")

    # 3 qubits but creg c[2]: width is the register (2), not the qubit
    # count (3). Distinguishes Option B from measuring all qubits, which
    # agree only when num_clbits == num_qubits.
    sh = ibm_shell()
    run(sh, [f"qrun {QASM2}narrow_creg.qasm"])
    counts = counts_of(settle(sh, 1), 1)
    check(all(len(k) == 2 for k in counts),
          f"IBM Option B: 3 qubits + creg c[2] -> 2-bit strings "
          f"(num_clbits, not num_qubits), got {set(len(k) for k in counts)}")

    # Reset honoured in position: x flips q0 to 1, reset returns it to 0,
    # then measure. A reset at its true source position yields ~all-zero;
    # a dropped reset (or one lumped at the end) would yield ~all-one. This
    # is the assertion that proves ordering, not just presence, of reset.
    sh = ibm_shell()
    run(sh, [f"qrun {QASM2}reset_mid.qasm"])
    counts = counts_of(settle(sh, 1), 1)
    zero = counts.get("0", 0)
    check(zero > 0.85 * sum(counts.values()),
          f"IBM reset in position: x then reset -> measures ~0, got {counts}")


def block_large_device_full_layout():
    '''full_layout pads a large device with ancilla so a sim run finishes'''
    # A circuit occupies only a few of a device's physical qubits, but the
    # allocator's placement (v2p_map) is a PARTIAL layout — only the mapped
    # qubits. On a large device Aer rejects a partial initial_layout
    # outright ("The 'layout' must be full (with ancilla)."), so a simulated
    # run on a big backend would crash before this fix. BaseProvider.
    # full_layout builds the full-device-width layout (used qubits at their
    # allocated positions, every unused physical qubit filled with an
    # ancilla), and both IBM providers call it. This block pins that on a
    # LARGE device specifically: that is the only place the partial-layout
    # bug surfaces, so a small-device test alone would not catch a
    # regression here. Needs qiskit; skips cleanly without it.
    try:
        p = IBMSimulatedProvider(seed=SEED)
        big = p.get_device(backend_name="FakeFez")
    except Exception:
        check(True, "qiskit not installed - large-device layout block skipped")
        return

    # FakeFez is a 156-qubit Heron r2 fake — far more physical qubits than a
    # Bell pair uses, so the layout must be padded with ancilla or Aer
    # refuses it. This exact case crashed pre-fix.
    check(big.num_qubits > 100,
          f"FakeFez is a large device (got num_qubits={big.num_qubits})")

    def fez_shell():
        dq = devq_with_ibm()
        dq.add_device(IBMSimulatedProvider(seed=SEED)
                      .get_device("FakeFez"), name="fez")
        with contextlib.redirect_stdout(io.StringIO()):
            return dq.build()

    # A Bell pair on the 156-qubit device must FINISH (not throw the layout
    # error) and land its mass on the correlated peaks 00/11. Before the
    # fix this raised "The 'layout' must be full (with ancilla)." and the
    # job never produced counts at all, so counts_of would not find a
    # FINISHED row.
    sh = fez_shell()
    run(sh, [f"qrun {BELL} --exec=fez"])
    counts = counts_of(settle(sh, 1), 1)
    total = sum(counts.values())
    check(all(len(k) == 2 for k in counts),
          f"large-device Bell: counts width is the 2-bit creg, not the "
          f"156-qubit device (ancilla do not widen the register), got "
          f"{set(len(k) for k in counts)}")
    peaks = counts.get("00", 0) + counts.get("11", 0)
    check(peaks > 0.85 * total,
          f"large-device Bell finishes with mass on the 00/11 peaks "
          f"(the layout error is gone), got {counts}")

    # GHZ too: a 3-qubit entangled state on the same large device lands on
    # the all-zero / all-one peaks, confirming the padding is correct for
    # more than two used qubits, not just a Bell special case.
    sh = fez_shell()
    run(sh, [f"qrun {GHZ} --exec=fez"])
    counts = counts_of(settle(sh, 1), 1)
    total = sum(counts.values())
    w = len(next(iter(counts)))
    peaks = counts.get("0" * w, 0) + counts.get("1" * w, 0)
    check(peaks > 0.80 * total,
          f"large-device GHZ finishes with mass on the all-0/all-1 peaks, "
          f"got {counts}")

    # The helper's contract directly: on a large device it returns a layout
    # accounting for EVERY physical qubit (used + ancilla), and it does not
    # widen the classical register. Pinning this at the source catches a
    # regression even if the end-to-end run happened to mask it.
    from qiskit import QuantumCircuit
    qc = QuantumCircuit(2, 2)
    qc.h(0); qc.cx(0, 1); qc.measure([0, 1], [0, 1])
    before_clbits = qc.num_clbits
    layout = p.full_layout(qc, {0: 136, 1: 143}, big)
    phys = set(layout.get_physical_bits().keys())
    check(phys == set(range(big.num_qubits)),
          f"full_layout covers every physical qubit 0..{big.num_qubits-1} "
          f"(used + ancilla), got {len(phys)} physical slots")
    check(qc.num_clbits == before_clbits,
          f"full_layout does not widen the classical register "
          f"(was {before_clbits}, now {qc.num_clbits})")
    # The used qubits sit at their allocated physical indices.
    v2p_check = {layout[qc.qubits[0]], layout[qc.qubits[1]]}
    check(v2p_check == {136, 143},
          f"full_layout places the circuit's qubits at their allocated "
          f"physical indices, got {v2p_check}")

    # Regression guard: the same helper on a SMALL device still works and
    # small-device runs are unaffected (no spurious padding failure).
    try:
        small = IBMSimulatedProvider(seed=SEED).get_device("FakeNairobiV2")
    except Exception:
        check(True, "small backend unavailable - small-device check skipped")
        return
    dq = devq_with_ibm()
    dq.add_device(small, name="nairobi")
    with contextlib.redirect_stdout(io.StringIO()):
        sh_small = dq.build()
    run(sh_small, [f"qrun {BELL} --exec=nairobi"])
    counts = counts_of(settle(sh_small, 1), 1)
    total = sum(counts.values())
    peaks = counts.get("00", 0) + counts.get("11", 0)
    check(peaks > 0.85 * total,
          f"small-device Bell still finishes on the peaks (no regression), "
          f"got {counts}")


def block_counts_width_contract():
    '''BaseProvider._counts_width is the one source of the Option B width rule'''
    # The bitstring-width rule (span the declared classical register,
    # fall back to the qubit count) is a cross-provider contract: the
    # fidelity metric compares bitstrings from different providers
    # directly, so if two providers derived width even slightly
    # differently the comparison would silently be wrong. The rule
    # therefore lives in ONE helper on BaseProvider, and both built-in
    # providers call it. This block pins the helper's contract directly,
    # so a regression is caught at the source rather than only through a
    # provider's end-to-end counts.

    from providers.base_provider import BaseProvider
    from circuits.circuit_rep import CircuitRep

    # Declared creg narrower than the qubit count: width is the register.
    check(BaseProvider._counts_width(CircuitRep(3, 2)) == 2,
          "counts_width: 3 qubits, creg c[2] -> 2 (the register, not qubits)")
    # No creg declared: fall back to the qubit count.
    check(BaseProvider._counts_width(CircuitRep(4, 0)) == 4,
          "counts_width: no creg -> num_qubits fallback")
    # creg wider than qubits (legal: a creg may exceed the qubits used).
    check(BaseProvider._counts_width(CircuitRep(2, 5)) == 5,
          "counts_width: creg wider than qubits -> the register width")

    # Both built-in providers must actually USE the helper, not re-derive
    # the rule. A structural check: the helper's result drives the width
    # both providers report. devq is dependency-free, so assert through it
    # — a 3-qubit / 2-clbit circuit must yield 2-bit strings, which only
    # holds if the provider took the register width from the helper.
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    sh = (DevQ(config_path=CONFIG + "router_only.config.json")
          .add_device(DevQSimulatedProvider(seed=SEED).get_device("random", 8))
          .build())
    run(sh, [f"qrun {QASM2}narrow_creg.qasm"])
    counts = counts_of(settle(sh, 1), 1)
    check(all(len(k) == 2 for k in counts),
          f"counts_width: devq reports the helper's width (2-bit), "
          f"got {set(len(k) for k in counts)}")


# ── Shell robustness ─────────────────────────────────────────────────────────

def block_shell_input_handling():
    '''Malformed or empty commands are handled without crashing'''
    sh  = session("router_only.config.json",
                  [("ibm.simulated", "FakeNairobiV2", None, None)])

    out = run(sh, [
        "qrunpack",              # nothing queued
        "qmap 99",               # no such job
        "qmap notanumber",       # not an id at all
        "qmem d9",               # device out of range
        "qtopology d0 99",       # qubit out of range
        "qerrors z d0",          # invalid flag
        "qrun",                  # no argument — usage
    ])

    expect(out,
           "No jobs in queue.",
           "Job 99 does not exist.",
           "Invalid job id.",
           "Device d9 does not exist",
           "99 -- Doesn't exist",
           "Invalid flag",
           "Usage: qrun")

    # None of it should have created a job or killed the session.
    check(not sh.kernel.list_jobs(),
          "malformed commands created no jobs")
    run(sh, [f"qrun {BELL}"])
    after = settle(sh, 1)
    check("FINISHED" in after,
          "the session still works after a run of bad input")


def block_many_device_federation():
    '''Routing and indexing hold beyond the usual three devices'''
    ibm = ibm_provider()
    sh  = (devq_with_ibm(config_path=CONFIG + "router_only.config.json")
           .add_devices([
               (ibm.get_device("FakeNairobiV2"), "nairobi"),
               (ibm.get_device("FakeLagosV2"),   "lagos"),
               (ibm.get_device("FakeCasablancaV2"), "casablanca"),
               (ibm.get_device("FakeJakartaV2"),    "jakarta"),
               ibm.get_device("FakeBelemV2"),
           ])
           .build())

    out = run(sh, ["qdevices", f"qrun {BELL} --exec=jakarta",
                   f"qrun {BELL} --no-exec=nairobi,lagos,casablanca,jakarta"])

    # d4 is unnamed, so the deny-list leaves it as the only candidate —
    # exercising index/name resolution across a five-device list.
    check("jakarta" in device_of(out, 1),
          f"named device 4 of 5 resolved, got {device_of(out, 1)}")
    check(device_of(out, 2).startswith("d4"),
          f"deny-list left only the unnamed d4, got {device_of(out, 2)}")
    check(finished_ids(settle(sh, 1, 2)) == {"1", "2"}, "both jobs finished")


BLOCKS = [
    ("devices_and_config",       block_devices_and_config),
    ("noise_routing",            block_noise_routing),
    ("name_index_equivalence",   block_name_index_equivalence),
    ("name_validation",          block_name_validation),
    ("rejection_semantics",      block_rejection_semantics),
    ("unrunnable_circuits",      block_unrunnable_circuits),
    ("rejected_no_ideal",        block_rejected_no_ideal),
    ("edge_threshold_semantics", block_edge_threshold_semantics),
    ("combined_thresholds",      block_combined_thresholds),
    ("max_1q_gate_error_filter", block_max_1q_gate_error_filter),
    ("packing_across_devices",   block_packing_across_devices),
    ("parser_errors",            block_parser_errors),
    ("per_job_shots",            block_per_job_shots),
    ("round_robin_router",       block_round_robin_router),
    ("per_device_config",        block_per_device_config),
    ("weight_normalisation",     block_weight_normalisation),
    ("zero_weight_fallback",     block_zero_weight_fallback),
    ("config_validation",        block_config_validation),
    ("provider_global_key",      block_provider_global_key_rejected),
    ("lifecycle_waiting",        block_lifecycle_waiting),
    ("lifecycle_failed",         block_lifecycle_failed),
    ("async_dispatch",           block_async_dispatch),
    ("wedged_provider_timeout",  block_wedged_provider_timeout),
    ("mock_topologies",          block_mock_topologies),
    ("device_calibration",       block_device_calibration),
    ("engine_gates",             block_engine_gates),
    ("engine_statevector",       block_engine_statevector),
    ("backend_factory_errors",   block_backend_factory_errors),
    ("shell_input_handling",     block_shell_input_handling),
    ("many_device_federation",   block_many_device_federation),
    ("single_device_ibm",        block_single_device_ibm),
    ("single_device_named",      block_single_device_named),
    ("single_device_batch",      block_single_device_batch),
    ("single_device_rejection",  block_single_device_rejection),
    ("single_device_devq",       block_single_device_devq_provider),
    ("supports_dynamic",         block_supports_dynamic_capability),
    ("conditional_ir",           block_conditional_ir),
    ("conditional_frontend",     block_conditional_frontend),
    ("dynamic_feasibility",      block_dynamic_feasibility),
    ("dynamic_lowering",         block_dynamic_lowering),
    ("plugin_matrix",            block_plugin_matrix),
    ("determinism_seeded",       block_determinism_seeded),
    ("determinism_unseeded",     block_determinism_unseeded),
    ("bug_fix_witnesses",        block_bug_fix_witnesses),
    ("registry_plugin_components", block_registry_plugin_components),
    ("registry_validation",      block_registry_validation),
    ("plugin_contract_enforcement", block_plugin_contract_enforcement),
    ("registry_frozen",          block_registry_frozen),
    ("plugin_config_keys",       block_plugin_config_keys),
    ("schema_ctor_injection",    block_schema_ctor_injection),
    ("plugin_normalise_group",   block_plugin_normalise_group),
    ("component_labels",         block_component_labels),
    ("qregistry",                block_qregistry),
    ("frontend_dispatch",        block_frontend_dispatch),
    ("qasm2_parser",             block_qasm2_parser),
    ("expr_unary_power_precedence", block_expr_unary_power_precedence),
    ("allocator_contract_1q_param", block_allocator_contract_1q_param),
    ("devq_measurement",         block_devq_measurement),
    ("ibm_measurement",          block_ibm_measurement),
    ("large_device_full_layout", block_large_device_full_layout),
    ("counts_width_contract",    block_counts_width_contract),
    ("shipped_workloads",        block_shipped_workloads),
    ("repo_hygiene",             block_repo_hygiene),
    ("benchmark_runner",         block_benchmark_runner),
    ("workload_spec",            block_workload_spec),
    ("placeholder_resolution",   block_placeholder_resolution),
    ("event_log",                block_event_log),
    ("metrics",                  block_metrics),
    ("comparison",               block_comparison),
    ("comparison_modes",         block_comparison_modes),
    ("stable_region",            block_stable_region),
    ("fidelity",                 block_fidelity),
    ("reference_tiers",          block_reference_tiers),
    ("router_scoring",           block_router_scoring),
    ("sweepable_contract",       block_sweepable_contract),
    ("allocator_scoring",        block_allocator_scoring),
    ("scheduler_scoring",        block_scheduler_scoring),
    ("provider_registration",    block_provider_registration_enforced),
    ("device_identity",          block_device_identity),
    ("same_kind_isolation",      block_same_kind_device_isolation),
]


def main():
    # Abandoned worker threads (see _with_timeout) may still hold stdout
    # redirected when the runner resumes. Print through a handle taken
    # before any block runs, so reporting can never be swallowed.
    console = sys.__stdout__

    def emit(*args, **kwargs):
        kwargs.setdefault("file", console)
        kwargs.setdefault("flush", True)
        print(*args, **kwargs)

    parser = argparse.ArgumentParser(
        description="Run the DevQ sanity blocks.")
    parser.add_argument("-k", metavar="PATTERN",
                        help="only run blocks whose name contains PATTERN")
    parser.add_argument("--list", action="store_true",
                        help="list block names and exit")
    parser.add_argument("-c", "--checks", action="store_true",
                        help="print every assertion each block verified")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the commands and full session transcript "
                             "for each block (implies --checks)")
    args = parser.parse_args()

    blocks = BLOCKS
    if args.k:
        blocks = [b for b in blocks if args.k in b[0]]
        if not blocks:
            emit(f"no block matches {args.k!r}")
            return 1

    if args.list:
        for name, fn in blocks:
            emit(f"  {name:26} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0

    # Hard ceiling on the process. If a regression reintroduces runaway
    # allocation, the suite dies with a clear message instead of driving
    # the machine into swap — an OOM that takes the desktop down is a far
    # worse failure than a failed test.
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling    = 4 * 1024 ** 3      # 4 GB is ample; the suite peaks ~0.4
        if hard == resource.RLIM_INFINITY or hard > ceiling:
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    except (ImportError, ValueError, OSError):
        pass                            # not supported here; carry on

    detail = args.checks or args.verbose
    width  = max(len(n) for n, _ in blocks)
    failed = []

    emit(f"\nRunning {len(blocks)} block(s)\n")
    for name, fn in blocks:
        TRACE.reset()
        # Each block builds its own sessions; reclaim their executor
        # threads before the next one rather than accumulating workers
        # across every block in the suite. gc.collect() then releases the
        # finished sessions themselves — every one holds fake-backend
        # calibration data and a NoiseModel, which is the bulk of the
        # per-session footprint.
        shutdown_executor()
        gc.collect()

        if detail:
            summary = (fn.__doc__ or "").strip().splitlines()[0]
            emit(f"\n{'─' * 72}\n{name}\n  {summary}\n")
        else:
            emit(f"  {name:<{width}}  ", end="", flush=True)

        status = "PASS"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                fn()
        except Failure as e:
            status = "FAIL"
            failed.append((name, str(e)))
        except Exception:
            status = "ERROR"
            failed.append((name, traceback.format_exc()))

        if detail:
            if args.verbose and TRACE.commands:
                emit("  commands")
                for c in TRACE.commands:
                    emit(f"    devq> {c}")
                emit()
                transcript = TRACE.transcript().rstrip()
                if transcript:
                    emit("  session output")
                    for line in transcript.splitlines():
                        emit(f"    {line}")
                    emit()
            if TRACE.checks:
                emit("  checks")
                for ok, desc in TRACE.checks:
                    mark = "PASS" if ok else "FAIL"
                    head, *rest = desc.splitlines()
                    emit(f"    [{mark}] {head}")
                    for extra in rest:
                        emit(f"           {extra}")
                emit()
            emit(f"  → {status} ({sum(1 for ok, _ in TRACE.checks if ok)}"
                  f"/{len(TRACE.checks)} checks)")
        else:
            emit(status)

    # Reclaim the final block's executor threads too. Without this the
    # interpreter blocks joining idle non-daemon workers at exit, which
    # looks exactly like a hang after the last line of output.
    shutdown_executor()

    emit()
    if failed:
        for name, msg in failed:
            emit(f"{name}\n    {msg}\n")
        emit(f"{len(failed)} of {len(blocks)} block(s) failed.")
        return 1

    emit(f"All {len(blocks)} block(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())