'''
Tags: Main

DevQ sanity test runner — executes the blocks in docs/test_blocks.md
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

from circuits.execution_result import shutdown_executor
from devq import DevQ, DevQError
from providers.devq.devq_simulated_provider import DevQSimulatedProvider
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

CONFIG = "./config/config_examples/"
BELL   = "test_circuits/bell.qasm"
GHZ    = "test_circuits/ghz.qasm"

SEED = 42   # fixed everywhere so mock-device topologies never flap



# ── Session construction ──────────────────────────────────────────────────────

def ibm_provider(seed=SEED):
    return IBMSimulatedProvider(seed=seed)


def session(config=None, devices=None, seed=SEED):
    '''
    Build a shell for a fresh session.

    Args:
        config:  global config filename in config_examples/, or None
        devices: list of specs, each one of
                     ("devq", kind, num_qubits, name, device_config)
                     ("ibm",  backend_name,     name, device_config)
                 name and device_config may be None.
        seed:    provider seed; None for unseeded

    Returns:
        QShell, ready for onecmd().
    '''
    devices = devices or []
    path    = (CONFIG + config) if config else None
    dq      = DevQ(config_path=path)
    ibm     = ibm_provider(seed)

    for spec in devices:
        if spec[0] == "devq":
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
        ("devq", "random", 7, None, None),
        ("ibm", "FakeNairobiV2", "nairobi", d1_config),
        ("ibm", "FakeLagosV2",   "lagos",   None),
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


# ── Blocks ────────────────────────────────────────────────────────────────────
# Each returns None on success and raises Failure with a specific message
# otherwise. Docstring first line is the description printed by the runner.

def block_devices_and_config():
    '''Devices, alias column, calibration data and config provenance'''
    sh  = three_device()
    out = run(sh, ["qdevices", "qconfig", "qerrors q d2", "qerrors e d2",
                   "qtopology d1 1"])

    expect(out, "random_backend", "fakenairobiv2", "fakelagosv2")
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
                  [("ibm", "FakeNairobiV2", None, None)])
    out = run(sh, ["qdevices", "qconfig", "qerrors q d0", "qtopology d0",
                   f"qrun {BELL}", "qmap 1", "qps", "qmem"])

    # the only device is d0 — nothing should refer to d1
    expect(out, "fakenairobiv2")
    expect_absent(out, "d1", "d2")
    # noise_graph still picks Nairobi's best pair
    check(mapping_of(out, 1) == "{0: 1, 1: 2}",
          f"noise_graph still picks {{0: 1, 1: 2}} with no peer devices, "
          f"got {mapping_of(out, 1)}")
    expect(out, "FINISHED")


def block_single_device_named():
    '''Naming works with one device, and the index still resolves'''
    sh  = session("router_only.config.json",
                  [("ibm", "FakeNairobiV2", "solo", None)])
    out = run(sh, ["qdevices", "qerrors q solo", f"qrun {BELL} --exec=solo"])
    expect(out, "solo (d0)")
    check("solo" in device_of(out, 1),
          f"job routed to the named sole device, got {device_of(out, 1)}")

    sh2  = session("router_only.config.json",
                   [("ibm", "FakeNairobiV2", "solo", None)])
    out2 = run(sh2, [f"qrun {BELL} --exec=d0"])
    check(mapping_of(out2, 1) == mapping_of(out, 1),
          "--exec=solo and --exec=d0 produce the same mapping")


def block_single_device_batch():
    '''Batch submission and packing on a single device'''
    sh  = session("router_only.config.json",
                  [("ibm", "FakeNairobiV2", None, None)])
    out = run(sh, [f"qsubmit {BELL} {BELL}", "qrunpack", "qps"])
    # both bells packed onto one device in the same cycle, disjoint qubits
    m1, m2 = mapping_of(out, 1), mapping_of(out, 2)
    check(m1 != m2,
          f"two bells packed onto disjoint blocks ({m1} and {m2})")
    expect_re(out, r"\[Kernel\] Job \d+ FINISHED", 2)


def block_single_device_rejection():
    '''Rejection on a single device names that device in the reason'''
    sh  = session("router_only.config.json",
                  [("ibm", "FakeLagosV2", "lagos", None)])
    out = run(sh, [f"qrun {BELL} --max-qubit-error=0.03", "qps"])
    expect(out, "REJECTED")


def block_single_device_devq_provider():
    '''The mock provider alone — no Qiskit involved in execution'''
    sh  = session(None, [("devq", "fully_connected", 5, "mock", None)])
    out = run(sh, ["qdevices", "qtopology d0", f"qrun {BELL}", "qps"])
    expect(out, "mock (d0)", "DevQSimulatedProvider", "FINISHED")


# ── Plugin matrix ─────────────────────────────────────────────────────────────

def block_plugin_matrix():
    '''Every scheduler × allocator × router combination runs to completion'''
    import devq as D
    import json
    import os
    import tempfile

    schedulers = list(D._SCHEDULER_MAP)
    allocators = list(D._ALLOCATOR_MAP)
    routers    = list(D._ROUTER_MAP)
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
                sh  = (DevQ(config_path=path)
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


BLOCKS = [
    ("devices_and_config",       block_devices_and_config),
    ("noise_routing",            block_noise_routing),
    ("name_index_equivalence",   block_name_index_equivalence),
    ("name_validation",          block_name_validation),
    ("rejection_semantics",      block_rejection_semantics),
    ("packing_across_devices",   block_packing_across_devices),
    ("parser_errors",            block_parser_errors),
    ("round_robin_router",       block_round_robin_router),
    ("per_device_config",        block_per_device_config),
    ("weight_normalisation",     block_weight_normalisation),
    ("zero_weight_fallback",     block_zero_weight_fallback),
    ("single_device_ibm",        block_single_device_ibm),
    ("single_device_named",      block_single_device_named),
    ("single_device_batch",      block_single_device_batch),
    ("single_device_rejection",  block_single_device_rejection),
    ("single_device_devq",       block_single_device_devq_provider),
    ("plugin_matrix",            block_plugin_matrix),
    ("determinism_seeded",       block_determinism_seeded),
    ("determinism_unseeded",     block_determinism_unseeded),
    ("bug_fix_witnesses",        block_bug_fix_witnesses),
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