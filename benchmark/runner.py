'''
Tags: Main

DevQ benchmark runner — workload specs in, event logs out.

One invocation produces a RUN DIRECTORY: one JSONL event log per
session, plus a manifest describing what varied and how each session
ended.

    results/matrix_20260723_142530/
        manifest.json
        metrics.json
        packing__noise_graph__noise.jsonl
        packing__noise_graph__round_robin.jsonl
        ...

WHY A DIRECTORY, NOT ONE FILE. Three reasons, in order of weight.

  Partial failure. A matrix takes real time and any session can die —
  a wedged provider, an interrupt. One file per session means the
  completed ones stay complete and readable, and only the rest need
  re-running. A single combined file would end in a truncated record
  with no clean resume point.

  A session is the unit of comparison. Phase 5.5 asks how config A
  differs from config B, which means loading two logs and diffing.
  Separate files make that two reads rather than one read plus a filter
  plus trust in the boundary markers.

  Streams append cleanly; boundaries do not. A crash mid-write costs
  one line of JSONL, not the file — but only if a file holds one
  session.

A single-spec run produces the same structure with one session in it.
There is no special case, so a reader never branches on "is this a
matrix".

RESUME IS SESSION-LEVEL, AND THAT IS A HARD BOUNDARY. --resume skips
sessions the manifest records as completed and runs the rest.
Mid-session resume is NOT offered, and not because it is fiddly:
seeding is sequential. IBM derives each run's seed as seed + k from a
submission counter, so restarting at job 7 would reproduce different
noise than an uninterrupted run. The resumed half would not be
comparable to the first half, which defeats the purpose. A partially
run session is discarded and re-run whole.

ATOMIC WRITES. Each log is written to a .partial file and renamed on
successful completion. Rename is atomic on POSIX, so a log is either
absent or whole — a half-written file can never be mistaken for a
finished session.
'''

import argparse
import datetime
import itertools
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devq import DevQ
from kernel.events import JSONLSink, MultiSink, RecordSink
from benchmark.spec import (load_spec, build_session,
                            submit_jobs, drain, SpecError)
from circuits.execution_result import shutdown_executor
from benchmark.reference import select_reference_provider, compute_ideals


# Session outcomes recorded in the manifest. The distinction between
# the first two matters for Phase 5.3: a config that rejects 40% of its
# jobs is a RESULT, not a broken run, and must not be confused with a
# session that crashed.
COMPLETED       = "completed"
WITH_FAILURES   = "completed_with_failures"
CRASHED         = "crashed"


def _session_id(config):
    '''
    Stable identifier for one session, derived from what varies rather
    than from position in a list. Resume matches on this, so inserting
    a component into the matrix must not silently re-map existing
    sessions onto different configs.
    '''
    if config is None:
        return "default"
    return "__".join(str(config[k]) for k in sorted(config))


def _run_one(spec, config, out_dir, session_id, register_providers=None,
             verbatim=None, register_schedulers=None, register_allocators=None,
             register_routers=None, register_frontends=None):
    '''
    Run one session to completion and write its event log.

    Returns a manifest entry. Never raises for an in-session failure —
    a crashed session is recorded and the matrix continues, because
    losing seventeen good sessions to one bad one helps nobody.

    The four register_* component maps (name -> class) are applied to this
    session's own fresh DevQ before build, exactly as register_providers
    is. A matrix session names components by string in its config, so any
    non-built-in component (a research/ baseline like NAQJS) must be
    registered here or build_session cannot resolve the name. Each session
    gets a fresh DevQ and re-applies the maps, so the per-session isolation
    the matrix relies on is preserved — the maps are class references, not
    shared instances.
    '''
    log_path     = os.path.join(out_dir, f"{session_id}.jsonl")
    partial_path = log_path + ".partial"

    entry = {
        "session_id": session_id,
        "config"    : config,
        "log"       : os.path.basename(log_path),
        "outcome"   : CRASHED,
    }

    config_path = None
    if config is not None:
        config_path = os.path.join(out_dir, f"{session_id}.config.json")
        with open(config_path, "w") as handle:
            json.dump(config, handle, indent=2)
        # A matrix session overrides the spec's own global config: the
        # matrix is what is being varied, and the spec supplies
        # everything else. Only the RESOLVED spec is mutated — verbatim
        # stays as written, so the header shows the user's spec, not the
        # runner's injected config path (which the separate `config`
        # field already records).
        spec = dict(spec)
        spec["config"] = config_path

    records = RecordSink()

    try:
        with open(partial_path, "w") as stream:
            sink = MultiSink(JSONLSink(stream), records)

            dq = DevQ()
            if register_providers:
                for name, provider in register_providers.items():
                    dq.register_provider(name, provider)
            if register_schedulers:
                for name, scheduler in register_schedulers.items():
                    dq.register_scheduler(name, scheduler)
            if register_allocators:
                for name, allocator in register_allocators.items():
                    dq.register_allocator(name, allocator)
            if register_routers:
                for name, router in register_routers.items():
                    dq.register_router(name, router)
            if register_frontends:
                for name, frontend in register_frontends.items():
                    dq.register_frontend(name, frontend)

            shell, meta = build_session(spec, dq, session_id,
                                        verbatim=verbatim)
            shell.kernel.sink = sink

            # The header is written ONCE per log rather than repeated on
            # every record: the spec verbatim, so the log is
            # self-describing, and the device table so records can carry
            # a bare index. Anything a reader needs to interpret the
            # stream lives here.
            sink.emit({
                "event"         : "header",
                "spec"          : meta["spec"],
                "session_id"    : session_id,
                "config"        : config,
                "devices"       : meta["devices"],
                "seed_requested": meta["seed_requested"],
                "warnings"      : meta["warnings"],
                "devq_started"  : datetime.datetime.now().isoformat(timespec="seconds"),
            })

            jobs   = submit_jobs(shell, spec, session_id)
            cycles = drain(shell)

            # ── reference ideals (the fidelity yardstick) ──────────────
            # After the run, compute each distinct circuit's NOISELESS
            # ideal and record it, so the fidelity metric (offline, pure)
            # can join a job's measured counts to its circuit's ideal by
            # hash without recomputing anything. The ideal is a property
            # of the circuit, so one reference-capable provider computes
            # all of them; the provider instances actually in play are the
            # attached devices' providers. A run with no reference-capable
            # provider (e.g. devq.simulated only) records no ideals, and
            # fidelity is then reported as None — an honest undefined.
            provider = select_reference_provider(
                [ctx.device.provider for ctx in shell.kernel.contexts])
            # A REJECTED job (unsatisfiable: no valid allocation exists on
            # any attached device) never runs and never produces measured
            # counts, so it has no fidelity to compute and needs no ideal.
            # Rejection is a run-level fact — the same circuit may be
            # REJECTED under contention here and RUNNING elsewhere — so it
            # is filtered at the call site, not inside compute_ideals (which
            # is circuit-level and job-agnostic). Skipping these mirrors the
            # unrunnable_reason skip already inside compute_ideals: no ideal,
            # exactly as for a circuit whose provider returns None. Distinct
            # circuits with at least one non-REJECTED job still get an ideal.
            runnable = (j.circuit for j in jobs
                        if j.state.value != "REJECTED")
            ideals = compute_ideals(runnable, provider, dq._registry)

            # A hash -> human label map, sourced from the jobs' stamped
            # labels (the spec path each circuit came from). Cosmetic: the
            # hash is the join key; the label just makes the log readable.
            hash_labels = {}
            for j in jobs:
                hash_labels.setdefault(j.circuit_hash, j.circuit_label)

            # Emit one reference record per distinct circuit with an ideal.
            for chash, data in ideals.items():
                sink.emit({
                    "event"       : "reference",
                    "circuit_hash": chash,
                    "ideal"       : data["ideal"],
                    "label"       : hash_labels.get(chash),
                })

            states = {}
            for job in jobs:
                states[job.state.value] = states.get(job.state.value, 0) + 1

            sink.emit({
                "event" : "summary",
                "jobs"  : len(jobs),
                "cycles": cycles,
                "states": states,
                # The full attached-device roster, index -> id, in device
                # order. per_job only names devices that RAN, so a metric
                # measuring spread across the fleet (load balance) cannot
                # see an idle device from per_job alone. Recording the
                # roster here keeps the summary self-sufficient — every
                # metric reads one record, not the header too — and lets
                # metrics.json label per-device output by id rather than
                # bare index. Every device has an id (the spec requires
                # it), and indices are dense in add order.
                "devices_attached": {
                    str(i): d["id"] for i, d in enumerate(meta["devices"])
                },
                "per_job": [{
                    "job_id"        : j.job_id,
                    "state"         : j.state.value,
                    "device"        : j.device_index,
                    "circuit_hash"  : j.circuit_hash,
                    # The circuit's readable name, carried on EVERY job here
                    # so a consumer can name it regardless of whether it
                    # produced an ideal or was rejected. Sourced from the
                    # job's stamped circuit_label (the spec path, set for
                    # every submitted job including parse-failure
                    # placeholders), reduced to its basename: the display
                    # form callers want, and it drops any directory-borne
                    # ${SECRET} the way the manifest masks paths elsewhere.
                    # Previously names were reconstructed only from
                    # `reference`/`reject` records, which a FINISHED job with
                    # no ideal emits neither of — so it fell back to a raw
                    # hash. The summary is the per-job authority the runner
                    # already reads, so the name belongs here.
                    "circuit_label" : (os.path.basename(j.circuit_label)
                                       if j.circuit_label else None),
                    "submitted_at"  : j.submitted_at,
                    "dispatched_at" : j.dispatched_at,
                    "resolved_at"   : j.resolved_at,
                    "queue_latency" : j.queue_latency,
                    "execution_time": j.execution_time,
                    "turnaround"    : j.turnaround_time,
                } for j in sorted(jobs, key=lambda j: j.job_id)],
            })

            # Reclaim executor threads between sessions. Workers are
            # non-daemon, so a matrix that skipped this would accumulate
            # idle threads and appear to hang after its final output.
            shutdown_executor()

        # Rename only after the stream closed cleanly. Either the log
        # is whole or it is not there.
        os.replace(partial_path, log_path)

        failed = states.get("FAILED", 0) + states.get("REJECTED", 0)
        entry.update({
            "outcome": WITH_FAILURES if failed else COMPLETED,
            "jobs"   : len(jobs),
            "cycles" : cycles,
            "states" : states,
            "seed_effective": [d["seed_effective"] for d in meta["devices"]],
            "records": len(records.records),
        })

    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["traceback"] = traceback.format_exc(limit=6)
        if os.path.exists(partial_path):
            # Keep it for inspection, but under a name no reader will
            # mistake for a finished log.
            os.replace(partial_path, log_path + ".crashed")
            entry["log"] = os.path.basename(log_path) + ".crashed"

    return entry


_MATRIX_KINDS = ("scheduler", "allocator", "router")


def matrix_configs(dq=None, select=None):
    '''
    Every scheduler x allocator x router combination, derived from the
    registry rather than hardcoded — a registered plugin joins the
    matrix automatically, which is the point of Phase 5.6.

    `select` narrows the matrix to named components per kind:

        matrix_configs(select={"router": ["noise"],
                               "scheduler": ["packing", "fcfs"]})

    A kind listed in `select` contributes only its named components; a
    kind ABSENT from `select` contributes all registered ones, so you
    can pin just the router and let the other axes fan out fully.
    `select=None` is the full cross-product — unchanged behaviour for
    every existing caller.

    Validation is loud and up front, never a silently-empty matrix: an
    unknown component name, or a `select` key that is not one of the
    three matrix kinds (a typo'd "routers"), raises with the legal set
    listed. Silently dropping a misspelled kind would run the full
    cross-product the caller was trying to narrow — the exact confusion
    a benchmark run cannot afford.

    Selected names are sorted regardless of the order given, because
    session ids and --resume matching both depend on a stable session
    ordering; caller list order is not preserved.
    '''
    probe = dq or DevQ()
    select = select or {}

    unknown_kinds = [k for k in select if k not in _MATRIX_KINDS]
    if unknown_kinds:
        raise SpecError(
            f"matrix select names unknown kind(s) {sorted(unknown_kinds)}; "
            f"the matrix varies {list(_MATRIX_KINDS)}"
        )

    chosen = {}
    for kind in _MATRIX_KINDS:
        registered = sorted(probe._registry.names(kind))
        if kind not in select:
            chosen[kind] = registered
            continue
        requested = list(select[kind])
        unknown = [n for n in requested if n not in registered]
        if unknown:
            raise SpecError(
                f"matrix select for {kind} names unknown component(s) "
                f"{unknown}; registered {kind}s are {registered}"
            )
        chosen[kind] = sorted(set(requested))

    return [
        {"scheduler": s, "allocator": a, "router": r}
        for s, a, r in itertools.product(
            chosen["scheduler"],
            chosen["allocator"],
            chosen["router"],
        )
    ]


def run(spec_path, out_dir=None, matrix=False, resume=False,
        register_providers=None, quiet=False, select=None,
        register_schedulers=None, register_allocators=None,
        register_routers=None, register_frontends=None):
    '''
    Run a workload spec, optionally across the full component matrix.

    `select` narrows which components the matrix ranges over — see
    matrix_configs(). It only applies to a matrix run; naming components
    implies a matrix, so a non-None select turns one on even if `matrix`
    was not set explicitly.

    The four register_* maps (name -> class) register non-built-in
    components — a research/ baseline scheduler, allocator, router or
    frontend — so they are addressable by name in a spec's config and
    JOIN THE MATRIX cross-product automatically (matrix_configs derives
    its axes from the registry, so a registered plugin fans out over the
    other axes with no further wiring). This is the same public path
    register_providers uses for providers; providers stay a separate
    argument because they are the device axis, not a matrix-varied kind.

    Returns the manifest dict. Writes one JSONL log per session plus
    manifest.json into out_dir.
    '''
    spec, verbatim = load_spec(spec_path)

    if out_dir is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join("results", f"{spec['name']}_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    # Naming components is a matrix intent, so a select implies --matrix
    # without the caller having to pass both.
    matrix = matrix or select is not None

    # The matrix cross-product is derived from a registry, so a registered
    # plugin only joins it if matrix_configs sees a DevQ that knows the
    # plugin. Build a throwaway probe carrying the same registrations each
    # session will get, purely to enumerate the axes. It is discarded
    # immediately and never becomes a session's DevQ — each session builds
    # its own fresh instance in _run_one, so isolation is untouched. (A
    # non-matrix run has no cross-product to enumerate, so it skips this.)
    if matrix:
        probe = DevQ()
        for reg, method in (
            (register_schedulers, "register_scheduler"),
            (register_allocators, "register_allocator"),
            (register_routers,    "register_router"),
            (register_frontends,  "register_frontend"),
        ):
            if reg:
                for name, cls in reg.items():
                    getattr(probe, method)(name, cls)
        configs = matrix_configs(dq=probe, select=select)
    else:
        configs = [None]

    manifest_path = os.path.join(out_dir, "manifest.json")
    previous = {}
    if resume and os.path.exists(manifest_path):
        with open(manifest_path) as handle:
            for entry in json.load(handle).get("sessions", []):
                if entry.get("outcome") in (COMPLETED, WITH_FAILURES):
                    previous[entry["session_id"]] = entry

    manifest = {
        # Verbatim, not resolved: the manifest is written to disk like
        # the log, so a resolved ${SECRET} here would leak just as surely.
        "spec"       : verbatim,
        "spec_path"  : os.path.abspath(spec_path),
        # Recorded rather than reconstructed by the caller: session logs
        # are stored as bare filenames, so deriving the directory from
        # one gives "" and silently falls back to a literal "results".
        "out_dir"    : os.path.abspath(out_dir),
        "started"    : datetime.datetime.now().isoformat(timespec="seconds"),
        "matrix"     : matrix,
        "sessions"   : [],
    }

    for i, config in enumerate(configs, 1):
        session_id = _session_id(config)

        if session_id in previous:
            entry = dict(previous[session_id])
            entry["skipped"] = "already completed"
            manifest["sessions"].append(entry)
            if not quiet:
                print(f"  [{i}/{len(configs)}] {session_id} — skipped "
                      f"(resumed)")
            continue

        if not quiet:
            print(f"  [{i}/{len(configs)}] {session_id} ...", end="", flush=True)

        entry = _run_one(spec, config, out_dir, session_id,
                         register_providers, verbatim,
                         register_schedulers=register_schedulers,
                         register_allocators=register_allocators,
                         register_routers=register_routers,
                         register_frontends=register_frontends)
        manifest["sessions"].append(entry)

        # Written after EVERY session, not once at the end: an
        # interrupted matrix must leave a manifest that --resume can
        # read.
        manifest["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(manifest_path, "w") as handle:
            json.dump(manifest, handle, indent=2, default=str)

        if not quiet:
            if entry["outcome"] == CRASHED:
                print(f" CRASHED — {entry.get('error', '')[:60]}")
            else:
                print(f" {entry['outcome']} "
                      f"({entry.get('jobs', 0)} jobs, "
                      f"{entry.get('cycles', 0)} cycles)")

    # Metrics are computed AFTER the manifest is whole, and their failure
    # is swallowed by design: an expensive run's logs must never be lost
    # to a bug in the metrics pass, the same isolation the per-session
    # crash handling gives. A missing metrics.json is recoverable — the
    # logs are intact and `metrics.write_metrics(out_dir)` reruns it —
    # whereas a lost log is not. write_metrics itself skips crashed and
    # summary-less sessions, so it degrades to whatever succeeded.
    try:
        from benchmark.metrics import write_metrics
        write_metrics(out_dir)
    except Exception as exc:      # noqa: BLE001 — observability, not control
        if not quiet:
            print(f"  metrics pass failed ({type(exc).__name__}: {exc}); "
                  f"logs are intact, rerun write_metrics(out_dir)")

    return manifest


def _summarise(manifest, out_dir):
    sessions = manifest["sessions"]
    by = {}
    for entry in sessions:
        by[entry["outcome"]] = by.get(entry["outcome"], 0) + 1

    print()
    print(f"  {len(sessions)} session(s) → {out_dir}")
    for outcome in (COMPLETED, WITH_FAILURES, CRASHED):
        if by.get(outcome):
            print(f"    {outcome:26} {by[outcome]}")

    crashed = [e for e in sessions if e["outcome"] == CRASHED]
    if crashed:
        print()
        print("  Crashed sessions (re-run with --resume to retry only these):")
        for entry in crashed:
            print(f"    {entry['session_id']}: {entry.get('error', '')[:70]}")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="devq-bench",
        description="Run a DevQ workload spec and record its event log.",
    )
    parser.add_argument("spec", help="path to a workload spec (JSON)")
    parser.add_argument("--out", dest="out_dir", default=None,
                        help="run directory. Default: "
                             "results/<spec name>_<timestamp>/ in the current "
                             "directory, which is gitignored — delete it when "
                             "you are done with a run")
    parser.add_argument("--matrix", action="store_true",
                        help="run every scheduler x allocator x router combination")
    # Repeatable per-kind narrowing. Naming any of these implies --matrix
    # (a component list only means something for a matrix run), and a kind
    # left unnamed fans out over everything registered for it. Pass a flag
    # more than once to list several: --router noise --router round_robin.
    parser.add_argument("--scheduler", action="append", dest="schedulers",
                        metavar="NAME",
                        help="limit the matrix's scheduler axis to this "
                             "component (repeatable); implies --matrix")
    parser.add_argument("--allocator", action="append", dest="allocators",
                        metavar="NAME",
                        help="limit the matrix's allocator axis to this "
                             "component (repeatable); implies --matrix")
    parser.add_argument("--router", action="append", dest="routers",
                        metavar="NAME",
                        help="limit the matrix's router axis to this "
                             "component (repeatable); implies --matrix")
    parser.add_argument("--resume", action="store_true",
                        help="skip sessions this run directory records as "
                             "completed; a partially run session is re-run whole, "
                             "since seeding is sequential and cannot resume mid-way")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # Only the kinds actually named become select keys; an unnamed kind
    # stays absent so matrix_configs() fans it out over all registered.
    select = {}
    if args.schedulers:
        select["scheduler"] = args.schedulers
    if args.allocators:
        select["allocator"] = args.allocators
    if args.routers:
        select["router"] = args.routers
    select = select or None

    try:
        manifest = run(args.spec, out_dir=args.out_dir, matrix=args.matrix,
                       resume=args.resume, quiet=args.quiet, select=select)
    except SpecError as exc:
        print(f"[Spec error] {exc}", file=sys.stderr)
        return 2

    return _summarise(manifest, manifest["out_dir"])


if __name__ == "__main__":
    sys.exit(main())