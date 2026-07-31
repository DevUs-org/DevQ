# DevQ Event Log

What the kernel emits during a run, and how runs are recorded. This is the
observability and benchmark-layer reference — the record schema, the seven
event kinds, running a workload, and the two-clock timing model.

It is separate from registration ([`REGISTRY.md`](REGISTRY.md)) and from
the component contracts ([`EXTENDING.md`](EXTENDING.md)). Where an event
carries per-candidate *scores* (the `route` and `allocate` events), the
scoring contract that produces them — `explain()` and the `Sweepable`
hooks — is documented in [`EXTENDING.md`](EXTENDING.md#reporting-scores-and-sweeping-weights-the-sweepable-contract);
this file describes only what lands in the log.

---

## Sinks

The kernel emits structured records; **sinks** decide what to do with
them (`kernel/events.py`). The default is `PrintSink`, which renders the
console output DevQ has always produced — so an interactive session is
unchanged by the existence of events, and a new event kind is invisible
on the console until someone deliberately renders it.

```python
from kernel.events import PrintSink, RecordSink, MultiSink

records = RecordSink()
shell.kernel.sink = MultiSink(PrintSink(), records)   # print AND capture
```

A sink is anything with `emit(record)`. Sink calls are wrapped at two
levels — in `Kernel._emit` and in `MultiSink` — because observability
must never kill a job: a raising sink is reported once on stderr and
then ignored.

## Records

Seven kinds: `submit`, `route`, `allocate`, `reject`, `dispatch`,
`resolve`, `cycle_end`. Every record carries `event`, `cycle` and `seq`,
stamped centrally in `_emit` so no call site can forget them or disagree
about the current cycle.

`route` records **all candidate device scores**, not just the winner's,
via the router's `explain()`. The winner alone cannot answer how close
the decision was, so a weight sweep would need re-running; with scores,
it is answerable from one recorded run.

`allocate` does the same for the *allocation* decision — all candidate
**blocks** a scoring allocator considered, with the α/β-free cost
decomposition of each, so an allocator weight sweep is likewise
answerable from one recorded run. It is emitted on **dispatch**, once per
placement, carrying the placing job's `block` and the per-block `scores`.
A cost-oblivious allocator (Static, Graph) is not sweepable and emits no
`allocate` record — the same honest silence as a non-scoring router
producing no scores on `route`. Because a batch scheduler allocates
several jobs before any dispatch, each job's allocation decision is
pinned on the job at allocation time (`qcb.alloc_decision`) rather than
read from the allocator at dispatch, where it would already have been
overwritten by the next job's allocation.

`cycle_end` is emitted even when a cycle did nothing, so a consumer can
distinguish an idle cycle from a cycle missing from the log.

## Running a workload

`benchmark/runner.py` turns a spec into a run directory:

```bash
python benchmark/runner.py benchmark/workloads/smoke.json
python benchmark/runner.py benchmark/workloads/smoke.json --matrix
python benchmark/runner.py benchmark/workloads/smoke.json --matrix --resume
```

Example specs live in `benchmark/workloads/`; output goes to
`results/<name>_<timestamp>/` unless `--out` says otherwise.

One JSONL log per session plus a `manifest.json`. The log opens with a
`header` — the spec verbatim and the device table, written once — and
closes with a `summary` carrying a per-job row, the terminal `states`
counts, and a `devices_attached` roster (index → id of every attached
device). The roster is there because the per-job table names only devices
that ran: a metric measuring spread across the fleet needs to see the
devices that ran *nothing*, and recording the roster in the summary keeps
that a one-record read rather than a cross-reference back to the header.
The body between them is chronological, because it records what happened;
the per-job table is a derived view for reading by job.

The manifest distinguishes `completed`, `completed_with_failures` and
`crashed`. The middle one is a result rather than an error: a threshold
sweep is *meant* to reject jobs, and a metrics pass must not treat that
as a broken run.

`--resume` skips sessions the manifest records as completed. It is
session-level only: seeding is sequential, so a session restarted
mid-way would reproduce different noise than an uninterrupted one and
the halves would not be comparable. A partially run session is
re-run whole.

---

## Two clocks

| Field | Deterministic? | Answers |
|---|---|---|
| `seq`, `*_seq` | yes | *what* happened, and in what order |
| `*_at` | no | *how long* it took |

`seq` is a monotonic event counter. Identical seeded runs make identical
decisions in identical order, so `seq`-keyed comparison is stable.
`*_at` is wall clock on the QCB (`submitted_at`, `dispatched_at`,
`resolved_at`), with `queue_latency`, `execution_time` and
`turnaround_time` derived from it.

**DevQ guarantees decision determinism, not completion-order
determinism.** Same seed gives the same routing, allocation and counts.
It does *not* give the same completion order: that belongs to the
executor, and on real hardware to the provider's queue, where jobs
submitted earlier routinely finish later. A log that hid this would be
misrepresenting what ran. Compare runs on `seq` with `*_at` excluded.

Two consequences for metrics. Cycle position is not a valid denominator
— a cycle is an artifact of polling frequency, not a physical quantity —
so throughput and utilisation come from timestamps and job counts.
And every derived timing returns `None` on a job that never dispatched,
so a metrics pass must skip rather than average in a fake zero.

Under simulation these measure Aer on the host CPU, not quantum runtime.
They are valid for comparing policies under identical conditions, and
must not be reported as device timings.

---