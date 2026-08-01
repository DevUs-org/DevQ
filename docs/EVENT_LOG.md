# DevQ Event Log

What the kernel emits during a run, and how runs are recorded. This is the
observability and benchmark-layer reference — the record schema, the seven
event kinds, running a workload, and the two-clock timing model.

It is separate from registration ([`REGISTRY.md`](REGISTRY.md)) and from
the component contracts ([`EXTENDING.md`](EXTENDING.md)). Where an event
carries per-candidate *scores* (the `route`, `allocate`, and `schedule`
events), the
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

A log is a JSON-lines file: one record per line, in the order things
happened. It opens with a `header`, carries a chronological body of
per-event records, and closes with a `summary`. Every **body** record
carries three fields stamped centrally in `_emit`, so no call site can
forget them or disagree:

- `event` — the record kind (below).
- `seq` — a monotonic counter, incremented once per emitted record. It is
  deterministic: identical seeded runs produce identical records in
  identical `seq` order, so `seq` is the axis to compare runs on.
- `cycle` — the kernel scheduling cycle the record was emitted in. Many
  records share a cycle; `cycle` groups them, `seq` orders them.

The `header` and `summary` are framing records and carry their own fields
instead (no `cycle`/`seq`) — they bracket the run rather than belonging to
a cycle.

### The framing records

**`header`** — written once, first. Carries `spec` (the workload spec
verbatim, so a log is self-describing — the devices, jobs, arrival pattern
and seed that produced it are all present) and the device table. A reader
never needs the original spec file; the log contains it.

**`summary`** — written once, last. Carries `jobs` (total submitted),
`cycles` (how many the run took), `states` (a terminal-state histogram,
e.g. `{"FINISHED": 25}`), `devices_attached` (an index→id roster of
*every* attached device, including any that ran nothing — a fleet-spread
metric needs to see idle devices, and this keeps that a one-record read),
and `per_job` — a derived per-job table with each job's terminal `state`,
`device`, `circuit_hash`, the three `*_at` timestamps, and the
`queue_latency`/`execution_time`/`turnaround` derived from them. The body
is chronological because it records what happened; `per_job` is the
by-job view a metrics pass reads.

### The body records

Seven kinds, in the order a job moves through them:

**`submit`** — a job entered the system. Fields: `job_id`, `num_qubits`,
and the per-job constraints as declared — `max_qubit_error`,
`max_1q_gate_error` (per-qubit readout and single-qubit-gate thresholds,
ANDed at allocation; `null` if unset), `max_edge_error` (two-qubit-gate
threshold, `null` if unset), `exec_on`/`no_exec_on` (device allow/deny
lists, `null` if unset), and `shots` (the per-job shot count as *asked
for* — `null` when the job named none and will defer to the device-resolved
value). This is the raw request; the value actually run is on `dispatch`,
and the two differ exactly when a job left `shots` unset.

**`route`** — the router bound a job to a device. Fields: `job_id`,
`device` (the chosen device index — the winner), `candidates` (the
feasible device indices it chose among), and `scores`. `scores` records
**all** candidates, not just the winner — one entry per candidate,
`{device, score, terms}`, where `terms` carries the raw, weight-free
inputs to the score (see *Score terms* below). Recording every candidate
and its raw terms is what lets a weight sweep re-derive routing from one
run instead of re-executing; the winner alone could not. A non-scoring
router (round-robin) records `scores: null` — honest silence, not invented
numbers.

**`allocate`** — a scoring allocator's block choice for the job now being
dispatched. Fields: `job_id`, `device`, `block` (the chosen physical-qubit
block — the winner), and `scores` (one `{block, score, terms}` per
candidate block considered). It is emitted on **dispatch**, once per
placement, and only by a scoring allocator — a cost-oblivious one (Static,
Graph) emits no `allocate` record, the same silence as a non-scoring
router. Because a batch scheduler allocates several jobs before any
dispatch, each job's decision is pinned on the job at allocation time
(`qcb.alloc_decision`) rather than read from the allocator at dispatch,
where the next job's allocation would already have overwritten it.

**`schedule`** — a scoring scheduler's dispatch choice for the job now
being dispatched: the scheduler-layer twin of `allocate`, one level up.
Fields: `job_id`, `device`, `winner` (the dispatched job — equal to
`job_id`, named for parity with `route`/`allocate` where the winner is a
device/block distinct from the job), and `scores` (one `{job_id, score,
terms}` per queued job the scheduler ranked, `terms` carrying the raw,
weight-free inputs — see *Score terms*). It is emitted on **dispatch**,
once per placement, and only by a scoring scheduler (NAQJS): an order-only
scheduler (FCFS, SDF, Packing) emits no `schedule` record, the same
silence as a non-scoring router or allocator. As with `allocate`, the
ranked queue is pinned on the job at the moment it is chosen
(`qcb.sched_decision`) rather than re-read at dispatch, because a batch
scheduler dispatching several jobs in one cycle would otherwise have one
job's ranking overwrite another's; recording every candidate and its raw
terms is what lets a scheduler weight sweep re-derive the dispatch order
from one run.

**`reject`** — a job was refused (terminal). Fields: `job_id`,
`candidates`, `scores` (as for `route` — present when a router scored
before the rejection), and `reason`, a human-readable string naming why
each allowed device could not satisfy the job (e.g. no qubit meets the
error threshold). A rejected job never dispatches, so it produces no
`dispatch`/`resolve`.

**`dispatch`** — a job was sent to its device for execution. Fields:
`job_id`, `device`, `device_label` (the human name, e.g. `alpha (d0)`),
`v2p_map` (the virtual→physical qubit map the allocator produced — the
placement actually applied), and `shots` (the **resolved** shot count the
job ran with: its own per-job `--shots` if it named one, else the
device-resolved value — see the per-job tier in
[`CONFIGURATION.md`](CONFIGURATION.md)). Compare against `submit`'s `shots`
to see whether a per-job override or the device default was used.

**`resolve`** — execution finished. Fields: `job_id`, `device`, `state`
(terminal state, e.g. `FINISHED`), `success`, `counts` (the measured
bitstring→shot-count distribution), `circuit_hash` (identifies the circuit,
and keys a job to its noiseless reference for fidelity), and `error`
(`null` on success).

**`cycle_end`** — a scheduling cycle completed. Fields: `processed` (how
many jobs it acted on). Emitted **even for an idle cycle** (`processed: 0`),
so a consumer can tell an idle cycle from a cycle missing from the log.

### Score terms

Inside a `route`, `allocate`, or `schedule` record, each candidate's
`terms` carries the **raw, weight-free** inputs to its score — not just the
final number. For the noise router: `queue_pressure` and the cost
decomposition `qubit_error_sum`/`edge_error_sum`, plus their normalised
forms and the weights in force (`router_queue_weight`,
`router_noise_weight`, `qubit_error_weight`, `edge_error_weight`). For the
noise-graph allocator: `qubit_error_sum`/`edge_error_sum`, the weighted
`block_cost`, and the `qubit_error_weight`/`edge_error_weight`. For a
scoring scheduler such as NAQJS (a `research/` baseline): the per-job
features `width`/`shots`/`seq`, their normalised forms, and the weights in
force (`naqjs_width_weight`/`naqjs_shots_weight`/`naqjs_seq_weight`) — note
these are queue features, not device-calibration terms, so a scheduler's
`terms` share the raw-summands-plus-weights *shape* without sharing the
noise-cost vocabulary. Because the summands are logged separately from the
weighting, a sweep recomputes the score at any weights from one recorded
run — this is the `Sweepable` contract, documented in
[`EXTENDING.md`](EXTENDING.md#reporting-scores-and-sweeping-weights-the-sweepable-contract),
and the raw terms are exactly what
[`COST_MODEL.md`](COST_MODEL.md#answering-the-sweep-from-one-recorded-run-phase-55a)
re-weights.

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

Beside the logs and manifest sit the **derived artifacts**, each written
by an offline pass that reads the logs and computes — never by the run
itself: `metrics.json` (per-session metrics), `comparison.json` (the
matrix bundle — every session's config, metrics and sweepable axes), and
`sweep_comp.<axis>.json` (an α/β weight sweep of one axis, written when a
sweep is run). These are the reading surface the comparison modes and a
future `qbench` present; see
[`METRICS.md`](METRICS.md#cross-config-comparison-and-the-αβ-sweep).

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