# DevQ Metrics Layer

Definitions for the Phase 5.3 metrics layer: the quantities DevQ computes
from a completed run to describe and compare scheduling, allocation and
routing behaviour. This file is the canonical reference for the metric
formulas and the source for the corresponding sections of any write-up,
the same role [`COST_MODEL.md`](COST_MODEL.md) plays for the routing and
allocation scores.

Notation follows [`COST_MODEL.md`](COST_MODEL.md). The two-clock model
(`seq` vs `*_at`) and the event-log structure it refers to are defined in
[`REGISTRY.md`](REGISTRY.md).

**Status.** Throughput, queue latency, utilisation, rejection rate and
load imbalance are specified below and implemented in
`benchmark/metrics.py`, verified by the `metrics` test block. Fidelity is
named but not yet specified, and will be added once its noiseless
reference (Phase 5.4) exists.

---

## Foundations

### Input

Metrics are computed **offline** from a finished run: either the JSONL
log in a run directory, or an in-memory `RecordSink` from the same
session. Nothing here runs during execution or touches the kernel; the
layer is a read-only pass over the logged artifact. Every metric below is
derived from the closing `summary` record — mostly its per-job rows, and
for load balance also its `devices_attached` roster — so none requires a
change to the execution path, and each reads a single record type.

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

$$\text{execution\_throughput} = \frac{|\text{completed, not rejected}|}{t_{\text{exec}}}$$

$$t_{\text{exec}} = \max_j(\text{resolved\_at}_j) - \min_j(\text{dispatched\_at}_j)$$

both extrema taken over dispatched jobs. This is the internally coherent
figure: numerator and both span endpoints range over the same
dispatched-and-resolved set. It is the canonical throughput. `None` if no
job dispatched.

**Turnaround throughput** — all submitted jobs, over the span from the
first submission to the last resolution:

$$\text{turnaround\_throughput} = \frac{|\text{all submitted}|}{t_{\text{turn}}}$$

$$t_{\text{turn}} = \max_j(\text{resolved\_at}_j) - \min_j(\text{submitted\_at}_j)$$

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

$$\text{queue\_latency}_j = \text{dispatched\_at}_j - \text{submitted\_at}_j$$

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

$$t_{\text{exec}} = \max_j(\text{resolved\_at}_j) - \min_j(\text{dispatched\_at}_j)$$

over all dispatched jobs — the same span as execution throughput. The
pre-dispatch queue period is excluded because no device could have been
busy then. Using a shared window rather than each device's own
active window is deliberate: a device-local window would let a device
that ran once and then idled score ~100%, inverting the load-balance
story, whereas the shared window keeps per-device fractions comparable to
each other and to the system figure. The device-specific part lives in
the numerator (a device's own intervals); the denominator is shared.

$$\text{util}(d) = \frac{\bigcup_{j \text{ on } d} [\text{dispatched\_at}_j, \text{resolved\_at}_j)}{t_{\text{exec}}}$$

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

$$\text{rejection\_rate} = \frac{|\text{REJECTED}|}{|\text{all submitted}|}$$

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

$$\text{cv} = \frac{\sigma}{\mu}, \qquad \text{load\_balance} = \frac{1}{1 + \text{cv}}$$

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