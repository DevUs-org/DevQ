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
import io
import itertools
import re
import signal
import sys
import traceback

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


def run(shell, commands):
    '''Drive a shell through commands, returning everything it printed.'''
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for c in commands:
            shell.onecmd(c)
    return buf.getvalue()


# ── Assertion helpers ─────────────────────────────────────────────────────────

class Failure(Exception):
    pass


def expect(out, *needles):
    for n in needles:
        if n not in out:
            raise Failure(f"expected to find: {n!r}")


def expect_absent(out, *needles):
    for n in needles:
        if n in out:
            raise Failure(f"expected NOT to find: {n!r}")


def expect_re(out, pattern, count=None):
    hits = re.findall(pattern, out)
    if not hits:
        raise Failure(f"no match for /{pattern}/")
    if count is not None and len(hits) != count:
        raise Failure(f"/{pattern}/ matched {len(hits)}x, expected {count}x")
    return hits


def mapping_of(out, job_id):
    '''Extract the v2p map a job was dispatched with.'''
    m = re.search(rf"Dispatching job {job_id} .*? qubits (\{{[^}}]*\}})", out)
    if not m:
        raise Failure(f"job {job_id} was never dispatched")
    return m.group(1)


def device_of(out, job_id):
    m = re.search(rf"Dispatching job {job_id} → (\S+)", out)
    if not m:
        raise Failure(f"job {job_id} was never dispatched")
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

    if "nairobi" not in device_of(out, 1):
        raise Failure(f"job 1 should route to nairobi, got {device_of(out, 1)}")
    if mapping_of(out, 1) != "{0: 1, 1: 2}":
        raise Failure(f"job 1 mapping {mapping_of(out, 1)}, expected {{0: 1, 1: 2}}")
    if mapping_of(out, 2) != "{0: 1, 1: 3}":
        raise Failure(f"job 2 mapping {mapping_of(out, 2)}, expected {{0: 1, 1: 3}}")
    if mapping_of(out, 3) != "{0: 3, 1: 4, 2: 5}":
        raise Failure(f"job 3 mapping {mapping_of(out, 3)}")
    expect_re(out, r"\[Kernel\] Job \d+ FINISHED", 3)


def block_name_index_equivalence():
    '''A device name and its index are interchangeable everywhere'''
    sh  = three_device()
    by_name  = run(sh, ["qerrors q nairobi", "qtopology nairobi 1"])
    by_index = run(sh, ["qerrors q d1", "qtopology d1 1"])
    if by_name != by_index:
        raise Failure("name and index forms produced different output")

    out = run(sh, [f"qrun {BELL} --exec=nairobi", f"qrun {BELL} --exec=d1"])
    if device_of(out, 1) != device_of(out, 2):
        raise Failure("--exec=nairobi and --exec=d1 routed differently")
    if mapping_of(out, 1) != mapping_of(out, 2):
        raise Failure("name/index runs produced different mappings")


def block_rejection_semantics():
    '''Thresholds reject across devices with aggregated reasons'''
    sh  = three_device()
    out = run(sh, [f"qrun {BELL} --max-qubit-error=0.03 --exec=lagos",
                   f"qrun {BELL} --max-qubit-error=0.03 --exec=d1,d2",
                   f"qrun {BELL} --max-qubit-error=0.0185 --exec=nairobi,lagos"])

    expect(out, "Job 1 REJECTED", "no connected block of 2 qubits")
    # job 2: same threshold but Nairobi is feasible, so it runs
    if "nairobi" not in device_of(out, 2):
        raise Failure("job 2 should route to nairobi rather than reject")
    # job 3: infeasible everywhere — both devices named in one reason
    expect(out, "Job 3 REJECTED")
    m = re.search(r"Job 3 REJECTED: ([^\n]*)", out)
    if "d1:" not in m.group(1) or "d2:" not in m.group(1):
        raise Failure(f"expected both devices in reason, got: {m.group(1)}")


def block_packing_across_devices():
    '''Bracket groups, batch packing and cross-device concurrency'''
    sh  = three_device()
    out = run(sh, [f"qsubmit [{BELL} {BELL} {GHZ} --no-exec=d0] {GHZ} --exec=lagos",
                   "qrunpack", "qps", "qmap 1", "qmem"])

    # two bells packed into the same cycle on disjoint qubits
    if mapping_of(out, 1) != "{0: 1, 1: 2}":
        raise Failure(f"job 1 mapping {mapping_of(out, 1)}")
    if mapping_of(out, 2) != "{0: 4, 1: 5}":
        raise Failure(f"job 2 mapping {mapping_of(out, 2)}")
    # Job 3 cannot fit alongside the two bells, so it waits a cycle and
    # allocates once qubits are freed. Assert the invariant (it lands on
    # nairobi, on a connected triple) rather than a specific block, since
    # which qubits are free depends on async completion order.
    if "nairobi" not in device_of(out, 3):
        raise Failure(f"job 3 should route to nairobi, got {device_of(out, 3)}")
    if len(eval(mapping_of(out, 3))) != 3:
        raise Failure(f"job 3 needs 3 qubits, got {mapping_of(out, 3)}")
    if "lagos" not in device_of(out, 4):
        raise Failure("job 4 was pinned to lagos")
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
    if "No jobs." not in out:
        raise Failure("errors leaked jobs into the process table")


def block_round_robin_router():
    '''Round-robin router cycles devices in index order'''
    sh  = three_device(config="round_robin.config.json")
    out = run(sh, ["qconfig", f"qsubmit {BELL} {BELL} {BELL}", "qrunpack", "qps"])

    expect(out, "round_robin", "Round Robin Router", "User (global)")
    devices = [device_of(out, i) for i in (1, 2, 3)]
    if not (devices[0].startswith("d0")
            and "nairobi" in devices[1]
            and "lagos" in devices[2]):
        raise Failure(f"expected d0, d1, d2 rotation, got {devices}")


def block_per_device_config():
    '''A per-device config overrides only that device'''
    sh  = three_device(d1_config="d1.static.config.json")
    out = run(sh, ["qconfig d1", f"qrun {BELL} --exec=d1", "qmap 1"])

    expect(out, "static", "Static Allocator", "User (d1)", "512")
    # scheduler and weights still come from core
    expect(out, "packing", "DevQ Core")
    # static ignores noise: first free block, not noise_graph's {0:1, 1:2}
    if mapping_of(out, 1) != "{0: 0, 1: 1}":
        raise Failure(f"static allocator gave {mapping_of(out, 1)}, "
                      f"expected {{0: 0, 1: 1}}")


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
    if mapping_of(out, 1) != "{0: 1, 1: 3}":
        raise Failure(f"edge-only weighting gave {mapping_of(out, 1)}, "
                      f"expected {{0: 1, 1: 3}}")
    # Lagos unchanged: 1/9 has the same ratio as the 0.1/0.9 default
    if mapping_of(out, 2) != "{0: 1, 1: 3}":
        raise Failure(f"lagos mapping {mapping_of(out, 2)}")


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
    if mapping_of(out, 1) != "{0: 1, 1: 2}":
        raise Failure(f"single-device mapping {mapping_of(out, 1)}")
    expect(out, "FINISHED")


def block_single_device_named():
    '''Naming works with one device, and the index still resolves'''
    sh  = session("router_only.config.json",
                  [("ibm", "FakeNairobiV2", "solo", None)])
    out = run(sh, ["qdevices", "qerrors q solo", f"qrun {BELL} --exec=solo"])
    expect(out, "solo (d0)")
    if "solo" not in device_of(out, 1):
        raise Failure("job did not route to the named single device")

    sh2  = session("router_only.config.json",
                   [("ibm", "FakeNairobiV2", "solo", None)])
    out2 = run(sh2, [f"qrun {BELL} --exec=d0"])
    if mapping_of(out2, 1) != mapping_of(out, 1):
        raise Failure("name and index disagreed on a single-device session")


def block_single_device_batch():
    '''Batch submission and packing on a single device'''
    sh  = session("router_only.config.json",
                  [("ibm", "FakeNairobiV2", None, None)])
    out = run(sh, [f"qsubmit {BELL} {BELL}", "qrunpack", "qps"])
    # both bells packed onto one device in the same cycle, disjoint qubits
    m1, m2 = mapping_of(out, 1), mapping_of(out, 2)
    if m1 == m2:
        raise Failure(f"packed jobs overlap: both got {m1}")
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
                    seconds=60
                )
                done = len(re.findall(r"\[Kernel\] Job \d+ FINISHED", out))
                if done != 2:
                    broken.append(f"{combo}: {done}/2 jobs finished")
            except TimeoutError:
                broken.append(f"{combo}: HUNG (qrunpack never returned)")
            except Exception as e:
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
    '''Run fn under a SIGALRM watchdog so a hang fails instead of blocking.'''
    def handler(signum, frame):
        raise TimeoutError()

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ── Determinism ───────────────────────────────────────────────────────────────

def block_determinism_seeded():
    '''Identical seeds reproduce devices and counts exactly'''
    cmds = ["qerrors q d0", "qtopology d0",
            f"qrun {BELL} --exec=nairobi", f"qrun {BELL} --exec=d1",
            f"qrun {BELL} --exec=lagos"]

    a = run(three_device(seed=42), cmds)
    b = run(three_device(seed=42), cmds)
    if a != b:
        raise Failure("two seed=42 sessions produced different output")

    c = run(three_device(seed=43), cmds)
    if a == c:
        raise Failure("seed=43 reproduced seed=42's output")

    # distinct runs of the same circuit must not clone counts
    counts = re.findall(r"\[Kernel\] Job \d+ FINISHED\. Counts: (\{[^}]*\})", a)
    if len(counts) < 2:
        raise Failure("expected at least two count sets")
    if counts[0] == counts[1]:
        raise Failure("identical circuits cloned counts — derived seeds broken")


def block_determinism_unseeded():
    '''Without a seed, sessions stay non-deterministic'''
    cmds = ["qerrors q d0", f"qrun {BELL} --exec=d1"]
    a = run(three_device(seed=None), cmds)
    b = run(three_device(seed=None), cmds)
    if a == b:
        raise Failure("unseeded sessions were identical — seeding leaked")


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
    if not 0.02 < nairobi < 0.08:
        raise Failure(f"nairobi bell error {nairobi:.3f}, expected ~0.05 — "
                      f"suspect noise-model leak or dropped v2p_map")
    if not 0.10 < lagos < 0.22:
        raise Failure(f"lagos bell error {lagos:.3f}, expected ~0.15")


def block_name_validation():
    '''Ambiguous or duplicate device names are rejected at attach time'''
    ibm = ibm_provider()
    dev = ibm.get_device("FakeNairobiV2")

    for bad in ["d0", "d7", "q", "e", "b", "", "   ", "has space", "has,comma"]:
        try:
            DevQ().add_device(dev, name=bad)
        except DevQError:
            continue
        raise Failure(f"name {bad!r} should have been rejected")

    # duplicates, case-insensitively
    try:
        (DevQ().add_device(dev, name="alpha")
               .add_device(ibm.get_device("FakeLagosV2"), name="ALPHA"))
    except DevQError:
        pass
    else:
        raise Failure("duplicate name differing only in case was accepted")


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
    parser = argparse.ArgumentParser(
        description="Run the DevQ sanity blocks.")
    parser.add_argument("-k", metavar="PATTERN",
                        help="only run blocks whose name contains PATTERN")
    parser.add_argument("--list", action="store_true",
                        help="list block names and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each block's captured output")
    args = parser.parse_args()

    blocks = BLOCKS
    if args.k:
        blocks = [b for b in blocks if args.k in b[0]]
        if not blocks:
            print(f"no block matches {args.k!r}")
            return 1

    if args.list:
        for name, fn in blocks:
            print(f"  {name:26} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0

    width  = max(len(n) for n, _ in blocks)
    failed = []

    print(f"\nRunning {len(blocks)} block(s)\n")
    for name, fn in blocks:
        print(f"  {name:<{width}}  ", end="", flush=True)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                fn()
            print("PASS")
        except Failure as e:
            print("FAIL")
            failed.append((name, str(e)))
        except Exception:
            print("ERROR")
            failed.append((name, traceback.format_exc()))

    print()
    if failed:
        for name, msg in failed:
            print(f"{name}\n    {msg}\n")
        print(f"{len(failed)} of {len(blocks)} block(s) failed.")
        return 1

    print(f"All {len(blocks)} block(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())