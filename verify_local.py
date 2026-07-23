'''
DevQ local verification — run this on YOUR machine, not the sandbox.

Three past bugs were invisible in a 1-CPU Linux sandbox with GNU
readline and only appeared on macOS with libedit. run_tests.py covers
correctness; this covers the things a headless test suite structurally
cannot:

  1. the INTERACTIVE shell actually starts and responds
  2. readline/libedit behaviour under your Python
  3. concurrency on 10 cores rather than 1
  4. the event log and spec runner end to end
  5. seeded determinism reproduces the pinned reference values

Usage:
    python verify_local.py            # all checks
    python verify_local.py --quick    # skip the slower concurrency check

Exit code 0 means everything matched. Anything else prints what differed.
'''

import io
import contextlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "  \033[32m✓\033[0m" if sys.stdout.isatty() else "  [PASS]"
FAIL = "  \033[31m✗\033[0m" if sys.stdout.isatty() else "  [FAIL]"

_results = []


def check(ok, label, detail=""):
    _results.append(bool(ok))
    print(f"{PASS if ok else FAIL} {label}")
    if not ok and detail:
        print(f"      {detail}")
    return ok


def section(title):
    print(f"\n\033[1m{title}\033[0m" if sys.stdout.isatty() else f"\n{title}")


# ── 1. Environment ────────────────────────────────────────────────────────────

def check_environment():
    section("1. Environment")
    print(f"      python {sys.version.split()[0]} on {sys.platform}")

    try:
        import readline
        backend = ("libedit" if "libedit" in getattr(readline, "__doc__", "") or ""
                   else "GNU readline")
        # The reliable probe: libedit and GNU readline disagree here.
        backend = "libedit" if "libedit" in str(readline.__doc__) else "GNU readline"
        print(f"      readline backend: {backend}")
        check(True, "readline imports")
    except ImportError:
        check(True, "readline absent (fine — history is optional)")

    import multiprocessing
    print(f"      cores: {multiprocessing.cpu_count()}")

    try:
        import qiskit, qiskit_aer
        print(f"      qiskit {qiskit.__version__}, aer {qiskit_aer.__version__}")
        check(True, "qiskit stack importable")
        return True
    except ImportError as exc:
        check(False, "qiskit stack importable", str(exc))
        return False


# ── 2. Interactive shell ──────────────────────────────────────────────────────

def check_interactive():
    '''
    build(interactive=True) is the path run_tests.py NEVER exercises —
    every block uses interactive=False. Readline history setup lives
    here, and it is where the libedit differences bite.
    '''
    section("2. Interactive shell (never covered by run_tests.py)")

    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            shell = (DevQ()
                     .add_device(DevQSimulatedProvider(seed=42)
                                 .get_device("random", 7), name="local")
                     .build(interactive=True))
        check(True, "build(interactive=True) completes")
    except Exception as exc:
        check(False, "build(interactive=True) completes",
              f"{type(exc).__name__}: {exc}")
        return

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            shell.onecmd("qdevices")
            shell.onecmd("qrun test_circuits/bell.qasm")
        out = buf.getvalue()
        check("local (d0)" in out, "interactive shell responds to commands")
        check("FINISHED" in out, "a job completes through the interactive shell")
    except Exception as exc:
        check(False, "interactive shell responds",
              f"{type(exc).__name__}: {exc}")


# ── 3. Determinism against pinned values ──────────────────────────────────────

# Calibration is pinned: these come from the IBM fake backends and must
# not drift. A mismatch means the qiskit-ibm-runtime version differs
# from the pinned one, NOT that DevQ is broken.
PINNED_NAIROBI_Q0 = 0.0580
PINNED_BELL_COUNTS = {"01": 49, "10": 58, "00": 1004, "11": 937}


def check_determinism():
    section("3. Seeded determinism (pinned reference values)")

    from devq import DevQ
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

    def run_once():
        provider = IBMSimulatedProvider(seed=42)
        session = (DevQ()
                   .add_device(provider.get_device(backend_name="FakeNairobiV2"),
                               name="nairobi"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shell = session.build(interactive=False)
            shell.onecmd("qrun test_circuits/bell.qasm")
        return buf.getvalue(), shell

    first, shell = run_once()
    second, _ = run_once()

    check(first == second,
          "two seeded sessions produce byte-identical transcripts")

    device = shell.kernel.contexts[0].device
    q0 = round(device.qubit_error(0), 4)
    check(abs(q0 - PINNED_NAIROBI_Q0) < 1e-6,
          f"nairobi qubit 0 error is the pinned {PINNED_NAIROBI_Q0}",
          f"got {q0} — likely a qiskit-ibm-runtime version difference")

    counts = None
    for line in first.splitlines():
        if "FINISHED. Counts:" in line and "[Kernel]" in line:
            counts = eval(line.split("Counts: ")[1])
            break
    check(counts == PINNED_BELL_COUNTS,
          "bell counts match the pinned reference",
          f"got {counts}")


# ── 4. Event log ──────────────────────────────────────────────────────────────

def check_event_log():
    section("4. Event log")

    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider
    from kernel.events import PrintSink, RecordSink, MultiSink

    def session(sink=None):
        provider = DevQSimulatedProvider(seed=42)
        dq = DevQ().add_device(provider.get_device("fully_connected", 7),
                               name="alpha")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shell = dq.build(interactive=False)
            if sink is not None:
                shell.kernel.sink = sink
            shell.onecmd("qsubmit test_circuits/bell.qasm test_circuits/ghz.qasm")
            shell.onecmd("qrunpack")
        return buf.getvalue(), shell

    baseline, _ = session()
    records = RecordSink()
    logged, shell = session(MultiSink(PrintSink(), records))

    check(baseline == logged,
          "console output is unchanged when a sink is attached")

    kinds = {r["event"] for r in records.records}
    check({"submit", "route", "dispatch", "resolve", "cycle_end"} <= kinds,
          f"all event kinds emitted, got {sorted(kinds)}")

    seqs = [r["seq"] for r in records.records]
    check(seqs == list(range(len(seqs))),
          "seq is dense and monotonic — nothing bypassed _emit")

    finished = [j for j in shell.kernel.process_table.list_jobs()
                if j.state.value == "FINISHED"]
    check(all(j.turnaround_time is not None and j.turnaround_time > 0
              for j in finished),
          "finished jobs carry wall-clock timings")


# ── 5. Workload spec ──────────────────────────────────────────────────────────

def check_spec_runner():
    section("5. Workload spec runner")

    from devq import DevQ
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
    from benchmark.spec import (load_spec, build_session, submit_jobs, drain,
                                SpecError)

    spec_dict = {
        "name": "local_verify",
        "seed": 42,
        "devices": [
            {"id": "sim", "provider": "devq",
             "backend": {"kind": "random", "num_qubits": 7}},
            {"id": "nairobi", "provider": "ibm",
             "backend": {"backend_name": "FakeNairobiV2"}},
        ],
        "jobs": [
            {"circuit": "test_circuits/bell.qasm", "repeat": 3},
            {"circuit": "test_circuits/ghz.qasm", "repeat": 2,
             "no_exec_on": ["sim"]},
        ],
    }

    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(spec_dict, handle)
    handle.close()

    try:
        spec = load_spec(handle.name)
        check(True, "spec file loads and validates")

        dq = DevQ()
        dq.register_provider("ibm", IBMSimulatedProvider)
        with contextlib.redirect_stdout(io.StringIO()):
            shell, meta = build_session(spec, dq, handle.name)
            jobs = submit_jobs(shell, spec, handle.name)
            started = time.monotonic()
            cycles = drain(shell)
            elapsed = time.monotonic() - started

        check(len(jobs) == 5, f"repeat expanded to 5 jobs, got {len(jobs)}")
        check(all(j.state.value == "FINISHED" for j in jobs),
              f"all jobs finished, got {sorted({j.state.value for j in jobs})}")
        check(cycles < 200,
              f"drain did not spin — {cycles} cycles in {elapsed:.2f}s")
        check([d["seed_effective"] for d in meta["devices"]] == [42, 42],
              "both devices took the spec's seed")

        # A typo must be refused, not guessed at.
        broken = dict(spec_dict)
        broken["jobs"] = [{"circuit": "test_circuits/bell.qasm", "repeats": 3}]
        try:
            from benchmark.spec import validate_spec
            validate_spec(broken)
            check(False, "an unknown spec key is refused")
        except SpecError:
            check(True, "an unknown spec key is refused")
    finally:
        os.unlink(handle.name)


# ── 6. Concurrency ────────────────────────────────────────────────────────────

def check_concurrency():
    '''
    The sandbox has one core; the user's machine has ten. Genuine
    parallel dispatch only happens here.
    '''
    section("6. Concurrency (multi-core)")

    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider

    provider = DevQSimulatedProvider(seed=42)
    dq = DevQ()
    for i in range(4):
        dq.add_device(provider.get_device("fully_connected", 7),
                      name=f"dev{i}")

    with contextlib.redirect_stdout(io.StringIO()):
        shell = dq.build(interactive=False)
        shell.onecmd("qsubmit " + " ".join(["test_circuits/bell.qasm"] * 8))
        started = time.monotonic()
        shell.onecmd("qrunpack")
        elapsed = time.monotonic() - started

    jobs = shell.kernel.process_table.list_jobs()
    finished = [j for j in jobs if j.state.value == "FINISHED"]
    check(len(finished) == 8,
          f"8 jobs across 4 devices all finished, got {len(finished)}")
    print(f"      wall clock: {elapsed:.2f}s")

    devices_used = {j.device_index for j in finished}
    check(len(devices_used) > 1,
          f"work spread across {len(devices_used)} devices")


def main():
    quick = "--quick" in sys.argv

    print("DevQ local verification")
    print("=" * 60)

    have_qiskit = check_environment()
    check_interactive()
    if have_qiskit:
        check_determinism()
    check_event_log()
    if have_qiskit:
        check_spec_runner()
    if not quick:
        check_concurrency()

    print("\n" + "=" * 60)
    passed, total = sum(_results), len(_results)
    if passed == total:
        print(f"All {total} checks passed.")
        return 0
    print(f"{total - passed} of {total} checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())