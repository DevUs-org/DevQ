'''
Tags: Main

DevQ event sinks — where kernel events go.

The kernel emits STRUCTURED records; sinks decide what to do with them.
This split is deliberate: the kernel never formats a string for display,
so adding a field to a record cannot change console output, and adding a
sink cannot change kernel behaviour.

Sinks in this module:

  PrintSink   renders records as the console lines DevQ has always
              printed. This is the DEFAULT, so an interactive session
              behaves exactly as it did before events existed.
  RecordSink  keeps records in memory — what a benchmark runner reads.
  JSONLSink   appends one JSON object per line to a file.
  MultiSink   fans out to several sinks, isolating their failures.

A sink is anything with an emit(record) method; these are conveniences,
not a required base class.

FAILURE ISOLATION. A sink is observability, not execution: a sink that
raises must never kill a job. MultiSink therefore catches and reports
per-sink exceptions rather than propagating them. The kernel wraps its
own sink call the same way, so a bare (non-Multi) sink is equally safe.
'''

import json
import sys


class PrintSink:
    '''
    Renders records as DevQ's existing console output.

    Only the event kinds that historically printed produce output;
    everything else is silently accepted. That is what keeps the console
    stable as the schema grows — a new event kind is invisible here
    until someone deliberately renders it.
    '''

    # Event kinds that produce console output. Anything absent is
    # accepted and ignored, by design.
    def emit(self, record):
        kind = record.get("event")

        if kind == "dispatch":
            print(f"[Kernel] Dispatching job {record['job_id']} → "
                  f"{record['device_label']} qubits {record['v2p_map']}")

        elif kind == "resolve":
            if record.get("success"):
                print(f"[Kernel] Job {record['job_id']} FINISHED. "
                      f"Counts: {record['counts']}")
            else:
                print(f"[Kernel] Job {record['job_id']} FAILED. "
                      f"Error: {record['error']}")


class RecordSink:
    '''
    Accumulates records in memory.

    The benchmark runner reads .records after a session to compute
    metrics without re-executing anything.
    '''

    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def clear(self):
        self.records = []


class JSONLSink:
    '''
    Appends one JSON object per line to an open text stream.

    Takes a stream rather than a path so the caller owns the file's
    lifetime — a runner writing several sessions to one log should not
    have each session truncate it.

    Records containing values JSON cannot represent are written with
    those values stringified rather than dropped: a log that loses a
    field silently is worse than one with an ugly field.
    '''

    def __init__(self, stream):
        self.stream = stream

    def emit(self, record):
        self.stream.write(json.dumps(record, default=str) + "\n")


class MultiSink:
    '''
    Fans one record out to several sinks.

    Each sink is isolated: one raising cannot prevent the others from
    receiving the record, and none can interrupt the job that produced
    it. Failures are reported once per sink per session, so a broken
    sink is visible without flooding the console on every event.
    '''

    def __init__(self, *sinks):
        self.sinks   = list(sinks)
        self._broken = set()

    def emit(self, record):
        for sink in self.sinks:
            try:
                sink.emit(record)
            except Exception as exc:
                key = id(sink)
                if key not in self._broken:
                    self._broken.add(key)
                    print(f"[DevQ Warning] event sink "
                          f"{type(sink).__name__} raised "
                          f"{type(exc).__name__}: {exc}. Further failures "
                          f"from this sink will be suppressed; execution "
                          f"is unaffected.", file=sys.stderr)