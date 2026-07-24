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

SEED = 42   # fixed everywhere so mock-device topologies never flap



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
from kernel.memory.allocators.base_allocator import BaseAllocator
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
                 max_qubit_error=None, max_edge_error=None):
        need = circuit.num_qubits
        free = sorted(pool.available())
        if len(free) < need:
            return None
        return {v: p for v, p in enumerate(free[:need])}


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
    check(mapping_of(out, 3) == "{0: 3, 1: 4, 2: 5}",
          f"job 3 (ghz) mapped to lagos {{0: 3, 1: 4, 2: 5}}, "
          f"got {mapping_of(out, 3)}")
    expect_re(out, r"\[Kernel\] Job \d+ FINISHED", 3)


def block_name_index_equivalence():
    '''A device name and its index are interchangeable everywhere'''
    sh  = three_device()
    by_name  = run(sh, ["qerrors q nairobi", "qtopology nairobi 1"])
    by_index = run(sh, ["qerrors q d1", "qtopology d1 1"])
    check(by_name == by_index,
          "qerrors/qtopology give identical output for 'nairobi' and 'd1'")

    out = run(sh, [f"qrun {BELL} --exec=nairobi", f"qrun {BELL} --exec=d1"])
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


def block_packing_across_devices():
    '''Bracket groups, batch packing and cross-device concurrency'''
    sh  = three_device()
    out = run(sh, [f"qsubmit [{BELL} {BELL} {GHZ} --no-exec=d0] {GHZ} --exec=lagos",
                   "qrunpack", "qps", "qmap 1", "qmem"])

    # two bells packed into the same cycle on disjoint qubits
    check(mapping_of(out, 1) == "{0: 1, 1: 2}",
          f"job 1 packed onto {{0: 1, 1: 2}}, got {mapping_of(out, 1)}")
    check(mapping_of(out, 2) == "{0: 4, 1: 5}",
          f"job 2 packed onto disjoint {{0: 4, 1: 5}} in the same cycle, "
          f"got {mapping_of(out, 2)}")
    # Job 3 cannot fit alongside the two bells, so it waits a cycle and
    # allocates once qubits are freed. Assert the invariant (it lands on
    # nairobi, on a connected triple) rather than a specific block, since
    # which qubits are free depends on async completion order.
    check("nairobi" in device_of(out, 3),
          f"job 3 routed to nairobi, got {device_of(out, 3)}")
    check(len(eval(mapping_of(out, 3))) == 3,
          f"job 3 (ghz) allocated 3 qubits after waiting a cycle: "
          f"{mapping_of(out, 3)}")
    check("lagos" in device_of(out, 4),
          f"job 4 honoured its --exec=lagos pin, got {device_of(out, 4)}")
    expect_re(out, r"\[Kernel\] Job \d+ FINISHED", 4)
    # all qubits returned to their pools afterwards
    expect_absent(out, "[X]")


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
    out = run(sh, [f"qsubmit {BELL} {BELL}", "qrunpack", "qps"])
    # both bells packed onto one device in the same cycle, disjoint qubits
    m1, m2 = mapping_of(out, 1), mapping_of(out, 2)
    check(m1 != m2,
          f"two bells packed onto disjoint blocks ({m1} and {m2})")
    expect_re(out, r"\[Kernel\] Job \d+ FINISHED", 2)


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
                    lambda: run(sh, [f"qsubmit {BELL} {GHZ}", "qrunpack", "qps"]),
                    seconds=25
                )
                done = len(re.findall(r"\[Kernel\] Job \d+ FINISHED", out))
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

    a = run(three_device(seed=42), cmds)
    b = run(three_device(seed=42), cmds)
    check(a == b, "two seed=42 sessions produced byte-identical transcripts")

    c = run(three_device(seed=43), cmds)
    check(a != c, "seed=43 diverges from seed=42")

    # distinct runs of the same circuit must not clone counts
    counts = re.findall(r"\[Kernel\] Job \d+ FINISHED\. Counts: (\{[^}]*\})", a)
    check(len(counts) >= 2, f"at least two count sets recorded ({len(counts)})")
    check(counts[0] != counts[1],
          "identical circuits produced different counts — derived per-run "
          "seeds (seed+k), not one reused seed")


def block_determinism_unseeded():
    '''Without a seed, sessions stay non-deterministic'''
    cmds = ["qerrors q d0", f"qrun {BELL} --exec=d1"]
    a = run(three_device(seed=None), cmds)
    b = run(three_device(seed=None), cmds)
    check(a != b, "two unseeded sessions differ — default path stays random")


def block_bug_fix_witnesses():
    '''Per-device noise models and allocator mappings reach the simulator'''
    out = run(three_device(seed=42),
              [f"qrun {BELL} --exec=nairobi", f"qrun {BELL} --exec=lagos"])

    def error_rate(counts_str):
        d = eval(counts_str)
        return (sum(d.values()) - d.get("00", 0) - d.get("11", 0)) / sum(d.values())

    counts = re.findall(r"\[Kernel\] Job \d+ FINISHED\. Counts: (\{[^}]*\})", out)
    nairobi, lagos = error_rate(counts[0]), error_rate(counts[1])

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
    out2 = run(sh, ["qrunpack", "qps"])
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

    out = run(sh, [f"qrun {BELL}", "qps"])

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


def block_wedged_provider_timeout():
    '''A future that never resolves fails cleanly instead of hanging'''
    from circuits.qasm_loader import load_qasm

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
        qcb = sh.kernel.submit_job(load_qasm(BELL))
        ctx_routed, _ = sh.kernel._route(qcb)
        qcb.v2p_map = ctx_routed.memory_manager.allocate(qcb.circuit)
        sh.kernel._execute(qcb, ctx_routed)
        dispatched_running = ctx_routed.running_jobs
        sh.kernel._wait_for(qcb, poll_interval=0.05, timeout=1)
    out = buf.getvalue()

    check(dispatched_running == 1,
          f"job was dispatched and counted, got {dispatched_running}")
    check(qcb.state.value == "FAILED",
          f"wedged job ends FAILED rather than spinning, "
          f"got {qcb.state.value}")
    expect(out, "did not resolve within", "wedged")

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
        out = run(sh, [f"qrun {BELL}", "qps"])
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

        out = run(sh, ["qconfig", f"qsubmit {BELL} {GHZ}", "qrunpack", "qps"])

        # Named in config, resolved through the registry, and reported
        # under the LABEL the plugin declared rather than its class name.
        expect(out, "scheduler          =  mock", "[Mock Scheduler]")
        expect(out, "allocator          =  mock", "[Mock Allocator]")
        expect(out, "router       =  mock", "[Mock Router]")

        # Actually in the execution path, not merely constructed.
        check(out.count("Dispatching job") == 2,
              "both jobs were dispatched by the plugin scheduler")
        expect_re(out, r"\d+ \| d0\s+\| FINISHED", count=2)

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
    OURS = ("benchmark", "circuits", "config", "hardware", "kernel",
            "providers", "registry", "shell")
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
    from benchmark.spec import validate_spec, SpecError
    import json as _json

    workloads = os.path.join(root, "benchmark", "workloads")
    shipped = sorted(f for f in os.listdir(workloads) if f.endswith(".json"))
    check(shipped, f"workload specs are shipped, found {shipped}")

    for filename in shipped:
        path = os.path.join(workloads, filename)
        try:
            spec = validate_spec(_json.load(open(path)), filename)
            ok, detail = True, ""
        except (SpecError, ValueError) as exc:
            ok, detail = False, str(exc)[:80]
        check(ok, f"shipped spec {filename} validates{': ' + detail if detail else ''}")

        # A spec naming a circuit that does not exist would fail only
        # when someone tried to run it.
        if ok:
            missing = [j["circuit"] for j in spec["jobs"]
                       if not os.path.exists(os.path.join(root, j["circuit"]))]
            check(not missing,
                  f"{filename} references existing circuits, missing {missing}")

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
    rejects("an unknown job key",        lambda s: s["jobs"][0].update(shots=100))
    rejects("a non-integer seed",        lambda s: s.update(seed="42"))
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
    rejects("a non-numeric threshold",   lambda s: s["jobs"][0].update(
                                             max_qubit_error="0.03"))

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
        return buf.getvalue(), sh

    # THE CENTRAL GUARANTEE: attaching a sink must not change what the
    # console prints. If this drifts, every existing block's expected
    # output becomes a function of whether logging is on.
    baseline, _ = session()
    rec = RecordSink()
    logged, shell = session(MultiSink(PrintSink(), rec))
    check(baseline == logged,
          "console output is byte-identical with a RecordSink attached")

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
    from circuits.qasm_loader import load_qasm
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

    circuit = load_qasm(GHZ)
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

    # A non-scoring router reports nothing rather than inventing scores.
    from kernel.router.round_robin_router import RoundRobinRouter
    check(RoundRobinRouter().explain(qcb, contexts) is None,
          "a router without scores returns None from explain()")


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
    after = run(sh, [f"qrun {BELL}"])
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
                   f"qrun {BELL} --no-exec=nairobi,lagos,casablanca,jakarta",
                   "qps"])

    # d4 is unnamed, so the deny-list leaves it as the only candidate —
    # exercising index/name resolution across a five-device list.
    check("jakarta" in device_of(out, 1),
          f"named device 4 of 5 resolved, got {device_of(out, 1)}")
    check(device_of(out, 2).startswith("d4"),
          f"deny-list left only the unnamed d4, got {device_of(out, 2)}")
    expect_re(out, r"\[Kernel\] Job \d+ FINISHED", 2)


BLOCKS = [
    ("devices_and_config",       block_devices_and_config),
    ("noise_routing",            block_noise_routing),
    ("name_index_equivalence",   block_name_index_equivalence),
    ("name_validation",          block_name_validation),
    ("rejection_semantics",      block_rejection_semantics),
    ("edge_threshold_semantics", block_edge_threshold_semantics),
    ("combined_thresholds",      block_combined_thresholds),
    ("packing_across_devices",   block_packing_across_devices),
    ("parser_errors",            block_parser_errors),
    ("round_robin_router",       block_round_robin_router),
    ("per_device_config",        block_per_device_config),
    ("weight_normalisation",     block_weight_normalisation),
    ("zero_weight_fallback",     block_zero_weight_fallback),
    ("config_validation",        block_config_validation),
    ("provider_global_key",      block_provider_global_key_rejected),
    ("lifecycle_waiting",        block_lifecycle_waiting),
    ("lifecycle_failed",         block_lifecycle_failed),
    ("wedged_provider_timeout",  block_wedged_provider_timeout),
    ("mock_topologies",          block_mock_topologies),
    ("backend_factory_errors",   block_backend_factory_errors),
    ("shell_input_handling",     block_shell_input_handling),
    ("many_device_federation",   block_many_device_federation),
    ("single_device_ibm",        block_single_device_ibm),
    ("single_device_named",      block_single_device_named),
    ("single_device_batch",      block_single_device_batch),
    ("single_device_rejection",  block_single_device_rejection),
    ("single_device_devq",       block_single_device_devq_provider),
    ("plugin_matrix",            block_plugin_matrix),
    ("determinism_seeded",       block_determinism_seeded),
    ("determinism_unseeded",     block_determinism_unseeded),
    ("bug_fix_witnesses",        block_bug_fix_witnesses),
    ("registry_plugin_components", block_registry_plugin_components),
    ("registry_validation",      block_registry_validation),
    ("registry_frozen",          block_registry_frozen),
    ("plugin_config_keys",       block_plugin_config_keys),
    ("plugin_normalise_group",   block_plugin_normalise_group),
    ("component_labels",         block_component_labels),
    ("shipped_workloads",        block_shipped_workloads),
    ("repo_hygiene",             block_repo_hygiene),
    ("benchmark_runner",         block_benchmark_runner),
    ("workload_spec",            block_workload_spec),
    ("event_log",                block_event_log),
    ("router_scoring",           block_router_scoring),
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