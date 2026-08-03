# DevQ Metrics Layer

Definitions for the Phase 5.3 metrics layer: the quantities DevQ computes
from a completed run to describe and compare scheduling, allocation and
routing behaviour. This file is the canonical reference for the metric
formulas and the source for the corresponding sections of any write-up,
the same role [`COST_MODEL.md`](COST_MODEL.md) plays for the routing and
allocation scores.

Notation follows [`COST_MODEL.md`](COST_MODEL.md). The two-clock model
(`seq` vs `*_at`) and the event-log structure it refers to are defined in
[`EVENT_LOG.md`](EVENT_LOG.md).

**Status.** Throughput, queue latency, utilisation, rejection rate and
load imbalance are specified below and implemented in
`benchmark/metrics.py`, verified by the `metrics` test block — the five
offline metrics that closed Phase 5.3. Fidelity, the sixth metric, is
specified below and implemented alongside them, verified by the
`fidelity` test block — the Phase 5.4 metric that compares each job's
measured-bit distribution against its circuit's noiseless ideal.

---

## Foundations

### Input

Metrics are computed **offline** from a finished run: either the JSONL
log in a run directory, or an in-memory `RecordSink` from the same
session. Nothing here runs during execution or touches the kernel; the
layer is a read-only pass over the logged artifact. The five timing and
counting metrics are each derived from the closing `summary` record —
mostly its per-job rows, and for load balance also its
`devices_attached` roster — so each reads a single record type and none
requires a change to the execution path. **Fidelity is the exception**:
it joins three record types (the `summary` per-job rows, the `resolve`
records carrying measured counts and each job's circuit hash, and the
`reference` records carrying ideals), because a measured-vs-ideal
comparison needs data the summary alone does not hold. It remains a
**pure, offline read** — no execution, no simulation, no qiskit — which
is the invariant that actually matters; "one record type" was a property
of the timing metrics, not a requirement of the layer.

### The two clocks

Timing metrics are built on `*_at` wall-clock fields, never on `seq` or
`cycle`. `seq` answers *what happened and in what order* and is
deterministic; `*_at` answers *how long it took* and is not. Cycle
position is an artifact of polling frequency, not a physical quantity,
and is never a denominator. Under simulation these timings measure the
Aer simulator on the host CPU, not quantum runtime, and must be reported
as policy-comparison figures under identical conditions, not as device
timings.

### Population rule

Every derived timing is `None` for a job that never reached the relevant
lifecycle point — a `REJECTED` job has no `dispatched_at` or
`resolved_at`. Such jobs are **skipped**, never averaged in as a zero: a
rejected job did not wait zero or run instantly, it never entered that
stage at all. Rejection is a *result*, not an error (a threshold sweep is
meant to reject jobs), and is counted separately, not folded into timing
metrics.

When a metric's population is empty — no job reached the stage it
measures — the metric is `None`, not `0`. A run that rejected every job
has an honestly undefined execution throughput, not a throughput of zero.

### Reproducibility obligation

The metric bundle is a published-artifact surface, like the log header.
Two people computing metrics from the same log must get byte-identical
numbers. The bundle is therefore plain data — deterministic key order,
`None` preserved rather than zero-filled — and any aggregation with a
choice of convention (for example a percentile interpolation method) has
that convention fixed and documented here, not inherited silently from a
library default. The human-readable report is a separate render layered
on top of this data.

---

## Throughput

Input: `summary` per-job rows only.

Two throughput figures, each pairing a job count with a wall-clock
*span*. "Span" is a single batch-level interval, distinct from the
per-job `execution_time` and `turnaround` fields that share those roots.

**Execution throughput** — jobs that completed without rejection, over
the span in which devices were actively working:

$${\text{execution}\_\text{throughput}} = \frac{|\text{completed, not rejected}|}{t_{\text{exec}}}$$

$$t_{\text{exec}} = \max_j({\text{resolved}\_\text{at}}_{j}) - \min_j({\text{dispatched}\_\text{at}}_{j})$$

both extrema taken over dispatched jobs. This is the internally coherent
figure: numerator and both span endpoints range over the same
dispatched-and-resolved set. It is the canonical throughput. `None` if no
job dispatched.

**Turnaround throughput** — all submitted jobs, over the span from the
first submission to the last resolution:

$${\text{turnaround}\_\text{throughput}} = \frac{|\text{all submitted}|}{t_{\text{turn}}}$$

$$t_{\text{turn}} = \max_j({\text{resolved}\_\text{at}}_{j}) - \min_j({\text{submitted}\_\text{at}}_{j})$$

This answers "how fast did the whole batch clear," queue wait included.
The span start, `min(submitted_at)`, exists for every job; the end,
`max(resolved_at)`, is over jobs that resolved — correct here, since the
batch clears when the last thing that could finish did. `None` if no job
resolved.

The two dropped 2×2 cells (all-submitted / execution-span and
completed-not-rejected / turnaround-span) paired a numerator with a span
whose endpoints ranged over a different population, for no interpretable
question, and are deliberately omitted.

---

## Queue latency

Input: `summary` per-job rows only.

Per job, the pure queue wait before running — the time spent enqueued
before dispatch:

$${\text{queue}\_\text{latency}}_{j} = {\text{dispatched}\_\text{at}}_{j} - {\text{submitted}\_\text{at}}_{j}$$

This is the `queue_latency` field already present in the per-job summary,
so the metric is aggregation, not re-derivation. It is the wait *before*
running, deliberately excluding execution — including execution would
duplicate `turnaround`.

This wait **includes any WAITING retry time**. A job whose allocation
fails on a busy pool is set WAITING and retried on later cycles;
`dispatched_at` is stamped only at the cycle it actually dispatches, after
all retries, so contention delay is captured here rather than in a
separate metric. A high latency mean can therefore come from either queue
depth or allocation contention — both are genuine "the job waited"
signals, and folding them together is intended.

Reported as a distribution, not a single number: **min, median, mean,
max, and p95**. The mean-versus-median gap is the signal a scheduler
comparison turns on — one job stuck behind a slow device drags the mean
while the median holds — so a lone mean would hide exactly what the
metric exists to show.

Jobs with `dispatched_at = None` (rejected or never dispatched) are
skipped. If no job dispatched, every field of the distribution is `None`.

**Percentile convention.** p95 uses **nearest-rank**, no interpolation:
for `n` sorted values the rank is `ceil(0.95 · n)`, clamped to `[1, n]`,
and the value at that rank is returned. The runs here have small job
counts, so interpolating between samples would invent precision the data
does not have; nearest-rank keeps p95 of five sorted values equal to the
fifth, checkable by hand. This convention is fixed and pinned in one
shared helper — it is load-bearing for reproducibility and must not be
replaced by a library default, which would silently change the number.

A consequence worth stating: at small `n`, **p95 equals max**. The rank
`ceil(0.95 · n)` first drops below `n` only at `n = 21`
(`ceil(0.95 · 20) = 19` is still not `20`; `ceil(0.95 · 25) = 24`), so
any run of twenty or fewer jobs reports p95 as its largest wait. This is
correct, not a defect — a handful of samples does not contain a 95th
percentile distinct from the maximum, and nearest-rank is honest about
that rather than fabricating an interpolated value no job experienced.
p95 becomes a distinct, useful number once workloads grow; the shipped
`contention.json` (25 jobs) is the fixture where p95 falls below max.

---

## Utilisation

Input: `summary` per-job rows only.

The busy fraction of a device over the run, reported both **per device**
and **system-wide**.

A device is busy on `[dispatched_at, resolved_at)` for each job it ran.
Because concurrent jobs on one device overlap, its busy time is the
**union** of those intervals, not their sum — summing double-counts the
overlap and can exceed the elapsed window, reporting over 100%. The union
is computed by sorting intervals by start, merging any that overlap or
touch, and summing the merged lengths; a zero-length interval contributes
nothing.

Both fractions are taken against one **shared run window**, the
execution-span

$$t_{\text{exec}} = \max_j({\text{resolved}\_\text{at}}_{j}) - \min_j({\text{dispatched}\_\text{at}}_{j})$$

over all dispatched jobs — the same span as execution throughput. The
pre-dispatch queue period is excluded because no device could have been
busy then. Using a shared window rather than each device's own
active window is deliberate: a device-local window would let a device
that ran once and then idled score ~100%, inverting the load-balance
story, whereas the shared window keeps per-device fractions comparable to
each other and to the system figure. The device-specific part lives in
the numerator (a device's own intervals); the denominator is shared.

$$\text{util}(d) = \frac{\bigcup_{j \text{ on } d} [{\text{dispatched}\_\text{at}}_{j}, {\text{resolved}\_\text{at}}_{j})}{t_{\text{exec}}}$$

The system-wide figure is total union-busy across all devices over the
window times the number of devices that ran work:

$$\text{util}_{\text{sys}} = \frac{\sum_d \text{union-busy}(d)}{t_{\text{exec}} \times |D|}$$

This is the busy-weighted mean of the per-device fractions, so the two
figures are consistent by construction rather than defined separately.

The per-device map is labelled by **device id** (from the summary
roster), not by bare index, matching load imbalance and keeping
`metrics.json` readable. Only devices that *ran* appear here — the
population is the same as the system denominator. A device that ran
nothing has an undefined utilisation over this window and is simply
absent; whether the fleet left a device idle is load imbalance's
question, not utilisation's.

A job dispatched but never resolved (`resolved_at = None`, possible on a
crash) contributes no interval, the same as an undispatched job: an
open-ended interval has no length. If no job dispatched at all, the
window is undefined and utilisation is `None`, not `0` — per-device is an
empty map and the system figure is `None`.
---

## Rejection rate

Input: `summary` per-job rows only.

The fraction of submitted jobs the system terminally **refused**:

$${\text{rejection}\_\text{rate}} = \frac{|\text{REJECTED}|}{|\text{all submitted}|}$$

reported with its raw counts:

```
"rejection_rate": {"rejected": r, "submitted": n, "rate": r/n}
```

Only the terminal `REJECTED` state counts. A `WAITING` job is *not*
rejected — routing found it a feasible device and allocation is retrying;
it was accepted and merely delayed, and its retry time already appears in
queue latency. Counting WAITING here would penalise a busy config twice,
once as latency and once as a phantom rejection. REJECTED and WAITING
partition cleanly: a WAITING-then-dispatched job has a `dispatched_at` and
is counted in latency, never here; a REJECTED job has no `dispatched_at`,
is skipped by latency, and is counted only here.

Every job is terminal at summary time — the run does not finish while any
job is still WAITING — so the denominator is simply all submitted jobs,
with no stuck-WAITING population to special-case.

The counts are always truthful integers, including on an empty run, where
`rejected` and `submitted` are both `0`. Only the **rate** is `None`
there: a run with no jobs has no meaningful fraction to report, but it
did, truthfully, reject zero of zero. This is a slightly different rule
from the timing metrics, where the whole result is `None` on an empty
population — here the counts are genuinely known even when the ratio is
not, so only the ratio is withheld.

A reason breakdown (rejected *why*) is deferrable: rejection reasons are
free-text router strings today and REJECTED is uniformly "no feasible
device", so a breakdown would couple the metric to unstructured messages.
It lands once reasons are structured.
---

## Load imbalance

Input: `summary` per-job rows and the `devices_attached` roster.

The bundle group is `load_imbalance` — its primary statistic is a
coefficient of variation, which *grows* with imbalance, so the group is
named for what the number measures. Each basis also carries a
`load_balance` convenience field, the inverted higher-is-better reading.

How evenly work spread across **all attached devices**, including idle
ones. The idle device is the whole point: a router that sends everything
to one device and starves another is the opposite of balanced, and that
is only visible if the idle device counts as zero load. Measuring spread
over only the devices that *ran* would report a starved fleet as
perfectly balanced.

This is why the summary records `devices_attached` (an index → id map of
the full roster): the per-job rows name only devices that ran, so an idle
device is invisible without it. Recording the roster in the summary keeps
every metric reading a single record, and lets the per-device output be
labelled by device id rather than bare index.

Reported on two bases, because they can disagree — a device running one
long job versus three short ones is balanced by count but not by busy
time:

- **by_count** — dispatched job count per device.
- **by_busy_time** — union-busy time per device, reusing utilisation's
  interval union so "busy" means one thing across the metrics.

Each basis carries the per-device distribution (idle devices at `0`), the
spread as a **population coefficient of variation**, and a convenience
reading:

$$\text{cv} = \frac{\sigma}{\mu}, \qquad {\text{load}\_\text{balance}} = \frac{1}{1 + \text{cv}}$$

where $\sigma$ is the **population** standard deviation (denominator $n$,
not $n-1$: we measure the actual device set, not a sample from a larger
population — and it keeps the value reproducible and hand-checkable). CV
is `0` for a perfectly even spread and grows without bound as imbalance
increases; it is the standard load-imbalance measure and is scale-free,
so it compares across runs with different job counts. Because a high CV
means *worse* balance, the `load_balance` field inverts it into $(0, 1]$
— `1.0` perfectly balanced, approaching `0` as one device is starved —
for whoever wants a higher-is-better reading without inverting the CV
themselves.

Edge cases follow the population rule. A single attached device has no
spread, so CV is `0` and load_balance `1.0` — one device cannot be
imbalanced. When there is no load to spread (every job rejected, so the
per-device mean is zero), CV is undefined and both `cv` and
`load_balance` are `None`, not `0`.

---

## Fidelity

Input: the `summary` per-job rows, the `resolve` records (measured counts
+ each job's `circuit_hash`), and the `reference` records (ideals keyed by
`circuit_hash`). See the Foundations note — fidelity is the one metric
that joins across record types.

How close each job's measured distribution came to its circuit's
noiseless ideal. This is the metric that decides whether a scheduling or
allocation win preserved answer quality rather than trading correctness
for speed: a policy that finishes faster but on noisier qubits can post a
better throughput and a *worse* fidelity, and only this metric exposes
that.

### The ideal

The ideal is a circuit's distribution on a perfect, noiseless machine —
what the measured distribution is compared against. It is computed by a
**reference-capable provider** (`BaseProvider.reference_ideal`), which a
provider overrides if it can faithfully simulate a circuit noiselessly;
`IBMSimulatedProvider` does, via a **noiseless Aer density-matrix**
simulation reading exact probabilities. Density-matrix, not statevector,
because it honours mid-circuit `reset` — a non-unitary operation whose
post-reset reduced state can be *mixed* (a reset after entanglement leaves
the partner qubit in a classical mixture), which a pure statevector cannot
represent.

Two properties make the ideal a clean artifact. It is **exact** (read from
the density matrix, not sampled), so it carries no sampling noise and no
reference seed to pin — `metrics.json` stays byte-reproducible. And it is
a **property of the circuit, not the job or device**: one reference-capable
provider computes it once per distinct circuit for the whole run, keyed by
a content hash of the circuit (see `benchmark/reference.circuit_hash`), so
two jobs running the same circuit on different devices share one ideal and
their fidelities are comparable. The run records one `reference` record per
distinct circuit; a provider that cannot simulate a given circuit
contributes no ideal for it.

When no reference-capable provider ran (for example a `devq.simulated`-only
session, whose uniform mock has no meaningful ideal), no ideals are
recorded and fidelity is uniformly `None` — an honest undefined, not a
fabricated score.

### The distance measures

Two are reported per job, because they answer slightly different questions
and together guard against a formula slip. Full attribution in
[`REFERENCES.md`](REFERENCES.md).

**Hellinger fidelity** `[Qiskit-HF]` — the headline number. From the
Hellinger distance `[Hellinger]`

$$H(P, Q) = \frac{1}{\sqrt 2}\sqrt{\sum_k (\sqrt{p_k} - \sqrt{q_k})^2}$$

the fidelity is $(1 - H^2)^2$, in $[0, 1]$ with **higher better** (`1.0`
identical). This is exactly Qiskit's `hellinger_fidelity`, which is the
definition QOS `[QOS]` reports — DevQ matches it so a cross-system
comparison is like-for-like, and the `fidelity` test asserts DevQ's value
equals Qiskit's on shared inputs (DevQ computes it from the formula
directly, never importing Qiskit into this pure layer). Hellinger is well
defined on **differing support**, which is why it suits GHZ-like circuits
`[GHZ-rationale]`: the ideal concentrates on two bitstrings while the noisy
result smears across many, and Hellinger accounts for the zero-probability
outcomes classical fidelity neglects.

**Total variation distance** `[TVD]` — the companion.

$$\text{TVD}(P, Q) = \frac{1}{2}\sum_k |p_k - q_k|$$

in $[0, 1]$ with **lower better** (`0` identical). Reported alongside
Hellinger because it is trivially checkable by hand (the "probability mass
in the wrong bucket") and is a numerically distinct quantity on the same
inputs — so the two together catch a swapped or square-dropped formula
that a single measure could not.

Both treat a key present in one distribution and absent in the other as
probability `0` there; measured and ideal keys align directly because both
are Option-B-width bitstrings rendered the same way (the reference
marginalises through the same measure map the run measured with, so a
measured `001` and an ideal `001` denote the same classical bits).

### Population rule and reporting

A job has a fidelity only if it **both** produced measured counts
(finished, non-empty) **and** has a recorded ideal for its circuit. A
rejected job (no counts), a failed job (empty counts), and a job whose
circuit had no reference ideal are each **skipped** — per-job fidelity
`None`, and left out of the session aggregate, never folded in as `0`. A
`0` would be a lie: it means "measured, and maximally wrong", the opposite
of "never measured".

Reported like the other distributional metrics — **per job** (each job's
`hellinger` and `tvd`, so the circuits that degraded are visible) and as a
**session distribution** (`min`, `median`, `mean`, `max`, `p95`) over the
qualifying jobs, using the same nearest-rank p95 convention as queue
latency. When no job qualifies, every aggregate field is `None`.
---

## Cross-config comparison and the weight sweep

`benchmark/comparison.py` reads a finished matrix run and produces two
views, both offline and pure in the same sense as the metrics above — they
read logs and the manifest and compute, executing nothing. Each is written
as an artifact beside the manifest, because a computed view worth reading
is worth persisting: a user who ran a sweep and disconnected would
otherwise lose it, exactly as `metrics.json` exists so a run's numbers
survive the process that made them.

### The matrix bundle — `comparison.json`

`assemble_matrix(run_dir)` collects every session's config, metrics and
sweepable axes into one map keyed by session id. It recomputes nothing —
it reads each session's `metrics.json` entry and the manifest's per-session
config — so `write_metrics` runs first. This is the inter-component
surface: a reader diffs config A against config B (does packing beat FCFS
on rejection rate; does the noise router lower load imbalance) from one
file rather than re-reading logs. Each row also records `sweepable_axes` —
which of `router`/`allocator` left scores-bearing decisions in that
session — so a reader knows where an intra-component sweep is available
without opening the log.

### The weight sweep — `sweep_comp.<axis>.json`

`sweep(run_dir, session_id, axis, coarse_m=20, bisect=False,
registry_map=None)` re-derives one session's decisions on one axis
(`router`, `allocator`, or `scheduler`) across that component's **weight-group
simplex**, **from the recorded scores**, and writes `sweep_comp.router.json`,
`sweep_comp.allocator.json`, or `sweep_comp.scheduler.json` (one artifact per
axis, so sweeps of different axes coexist). The weight group of *n* terms is
enumerated over the Scheffé {n, m} simplex-lattice, with `coarse_m` the lattice
resolution *m*; at n=2 (the router and allocator, which sweep the shared qubit/
edge pair) this lattice is exactly the historical (α, 1−α) grid. One axis at a
time, because the shared-scope α/β feeds both the router yardstick and each
device's allocator, so "sweep α/β" is ambiguous about which consumer — the axis
argument disambiguates, as [`COST_MODEL.md`](COST_MODEL.md) describes. A scored
scheduler's weight keys are plugin-specific, so the scheduler axis derives them
from the component's `live_params()` rather than assuming the qubit/edge pair.

The result carries two layers. The **primitive** is per recorded decision,
the winner at each grid point — the honest raw result. The **aggregate**,
derived in the same pass, is per grid point the distribution of winners
across decisions, plus the α values where that distribution flips; with
`bisect`, each flip is localised between its bracketing grid points by
binary search to a small tolerance, so the reported flip is exact rather
than grid-limited. Bisection uses only the same sweep hooks, so it stays
component-agnostic — no closed-form per-component breakpoint math, which
could not generalise to a third-party scoring component.

The sweep borrows the session's component purely as a scoring engine: it
reconstructs the registered class by name (from the session config) and
calls its `Sweepable` hooks on the logged terms, computing no score
itself. A registered third-party scoring component sweeps identically —
see the `Sweepable` contract in
[`EXTENDING.md`](EXTENDING.md#reporting-scores-and-sweeping-weights-the-sweepable-contract).

### Faithfulness is guarded, not assumed

Before emitting any swept result, the driver replays each decision at the
run's **own** recorded weights (read from the logged terms) and requires
the winner the log recorded. A component whose decision is not a pure
function of its logged terms — a stochastic or stateful policy — fails
this, and the session is refused with a reason (`faithful: false`) rather
than emitting fiction. A non-scoring component (a round-robin router, a
cost-oblivious allocator) is refused the same way, named as non-scoring.
This is the decision-determinism the whole benchmark layer rests on: the
sweep's claim to answer other weights from one recorded run is only valid
if the recorded run is itself a faithful function of what it logged.

### The comparison modes — reading the results (Phase 5.5b)

`benchmark/comparison_modes.py` is the reading surface over the two
artifacts above — pure presentation, deriving no new numbers. Each mode
returns structured data; `render_text(result, to=path)` turns it into a
plain-text table or report and, given a path, writes a `.txt`. The split
is deliberate: the mode returns data so a second consumer — a test, a
notebook, the `qbench` shell (5.7) — renders it its own way without
parsing a string, and the text renderer is one view over that data rather
than the data itself.

**Inter-component — `rank_sessions(bundle, metric, descending)`.** Orders
the matrix's sessions by one metric, named as a dotted path into a
session's metrics (`"rejection_rate.rate"`, `"utilisation.system"`,
`"load_imbalance.by_busy_time.load_balance"`). It does not presume which
end is good — lower is better for rejection rate, higher for utilisation —
so it ranks by the number and the caller sets `descending`. A session
whose metric is absent, null, or not a scalar is listed under `missing`
rather than sorted as a zero, the same honesty metrics.py gives an
unmeasured population; ties break on session id for a deterministic order.

**Intra-component — `present_sweep(sweep_result)`.** Reads out one
session's weight sweep: a refused sweep (`faithful: false`) is presented as a
refusal carrying its reason, not dropped; a faithful sweep is presented as
its flips — the weight-vector points where the winning distribution changes,
the actionable output — plus the per-point distribution, with a `stable` flag
when nothing flips across the whole lattice. (At n=2 a point is the familiar
(α, 1−α); at n≥3 it is the full weight vector.) The *absolute* view (one
session's own metric bundle) is not a mode: it is the 5.3 bundle, already
shipped.