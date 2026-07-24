'''
Tags: Main

DevQ benchmark runner — workload specs in, event logs out.

One invocation produces a RUN DIRECTORY: one JSONL event log per
session, plus a manifest describing what varied and how each session
ended.

    results/matrix_20260723_142530/
        manifest.json
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


def _run_one(spec, config, out_dir, session_id, register_providers=None):
    '''
    Run one session to completion and write its event log.

    Returns a manifest entry. Never raises for an in-session failure —
    a crashed session is recorded and the matrix continues, because
    losing seventeen good sessions to one bad one helps nobody.
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
        # everything else.
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

            shell, meta = build_session(spec, dq, session_id)
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

            states = {}
            for job in jobs:
                states[job.state.value] = states.get(job.state.value, 0) + 1

            sink.emit({
                "event" : "summary",
                "jobs"  : len(jobs),
                "cycles": cycles,
                "states": states,
                "per_job": [{
                    "job_id"        : j.job_id,
                    "state"         : j.state.value,
                    "device"        : j.device_index,
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


def matrix_configs(dq=None):
    '''
    Every scheduler x allocator x router combination, derived from the
    registry rather than hardcoded — a registered plugin joins the
    matrix automatically, which is the point of Phase 5.6.
    '''
    probe = dq or DevQ()
    return [
        {"scheduler": s, "allocator": a, "router": r}
        for s, a, r in itertools.product(
            sorted(probe._registry.names("scheduler")),
            sorted(probe._registry.names("allocator")),
            sorted(probe._registry.names("router")),
        )
    ]


def run(spec_path, out_dir=None, matrix=False, resume=False,
        register_providers=None, quiet=False):
    '''
    Run a workload spec, optionally across the full component matrix.

    Returns the manifest dict. Writes one JSONL log per session plus
    manifest.json into out_dir.
    '''
    spec = load_spec(spec_path)

    if out_dir is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join("results", f"{spec['name']}_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    configs = matrix_configs() if matrix else [None]

    manifest_path = os.path.join(out_dir, "manifest.json")
    previous = {}
    if resume and os.path.exists(manifest_path):
        with open(manifest_path) as handle:
            for entry in json.load(handle).get("sessions", []):
                if entry.get("outcome") in (COMPLETED, WITH_FAILURES):
                    previous[entry["session_id"]] = entry

    manifest = {
        "spec"       : spec,
        "spec_path"  : os.path.abspath(spec_path),
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
                         register_providers)
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
                        help="run directory (default: results/<name>_<timestamp>)")
    parser.add_argument("--matrix", action="store_true",
                        help="run every scheduler x allocator x router combination")
    parser.add_argument("--resume", action="store_true",
                        help="skip sessions this run directory records as "
                             "completed; a partially run session is re-run whole, "
                             "since seeding is sequential and cannot resume mid-way")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = run(args.spec, out_dir=args.out_dir, matrix=args.matrix,
                       resume=args.resume, quiet=args.quiet)
    except SpecError as exc:
        print(f"[Spec error] {exc}", file=sys.stderr)
        return 2

    out_dir = args.out_dir
    if out_dir is None:
        out_dir = os.path.dirname(manifest["sessions"][0]["log"]) or "results"
    return _summarise(manifest, out_dir)


if __name__ == "__main__":
    sys.exit(main())