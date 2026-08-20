# DevQ Shell Reference

Every QShell command, and the JobSpec syntax for per-job noise thresholds
and device constraints.

Kept out of the README so that the README stays an overview rather than a
manual. Start a session with `python example.py`, or drive one
programmatically with `DevQ(...).build()` and `shell.onecmd(...)`.

Related: [`CONFIGURATION.md`](CONFIGURATION.md) for the config keys these
commands report, [`COST_MODEL.md`](COST_MODEL.md) for the scoring
mathematics behind routing and allocation decisions.

---

## QShell Command Reference

QShell commands deliberately mirror classical OS tools. Commands marked
`[dN]` take an optional device argument: with it, output covers that device
only; without it, output is sectioned per attached device (a single-device
session simply shows one `d0` section — the format is uniform).

| Command | Classical analogue | Purpose |
|---|---|---|
| `qrun` | — | Priority-dispatch a **single** job, bypassing the queue; returns immediately (async) |
| `qsubmit` | — | Enqueue one or more jobs without dispatching |
| `qrunpack` | — | Dispatch all queued jobs via the router and per-device schedulers; returns immediately (async) |
| `qdevices` | `lscpu` | List attached devices: index, name, provider, qubits, queued/running load |
| `qps [id …]` | `ps` | List jobs with device binding and lifecycle state; folds in counts/reason once resolved. Optional ids filter the view |
| `qmap <job_id>` | — | Show a job's device and virtual → physical qubit mapping |
| `qmem [dN]` | `free` | Show free `[]` vs allocated `[X]` qubits |
| `qtopology [dN] [q …]` | — | Show device coupling map(s) (qubit filtering requires a device) |
| `qerrors [q\|e\|b] [dN]` | `iostat` | Show qubit errors, edge errors, or both (default `b`) |
| `qconfig [dN]` | — | Show global router policy and each device's scheduler/allocator/shots with the source of every value |
| `qregistry [p r s a f]` | — | List registered components — providers, routers, schedulers, allocators, frontends (built-in and externally registered); flags filter by kind, no flag shows all |
| `!!` | `!!` | Repeat the last command |
| `exit` / Ctrl-D | — | Exit DevQ |

> **Note (WIP):** `qerrors` currently reports the core per-qubit and per-edge
> error rates. Some of the newer calibration fields exposed by recent provider
> snapshots are not yet surfaced in this view; broader calibration reporting is
> in progress.

### Examples

```
devq> qdevices

  d0   random_backend       DevQSimulatedProvider     7 qubits   queued: 0  running: 0
  d1   fakenairobiv2        IBMSimulatedProvider      7 qubits   queued: 0  running: 0
  d2   fakelagosv2          IBMSimulatedProvider      7 qubits   queued: 0  running: 0

devq> qrun test_circuits/bell.qasm --exec=d1,d2
Job 1 submitted to queue.
[Kernel] Dispatching job 1 → d1 (fakenairobiv2) qubits {0: 1, 1: 2}
[>] Job 1 dispatched to d1 (fakenairobiv2). Check status with qps 1.

devq> qrun test_circuits/bell.qasm --max-qubit-error=0.03 --exec=d2
Job 2 submitted to queue.
[x] Job 2 REJECTED: unsatisfiable on every allowed device — d2: no connected
    block of 2 qubits exists on this device under max_qubit_error=0.03,
    max_edge_error=None

devq> qps
1 | d1  | RUNNING
2 | -   | REJECTED | Reason: unsatisfiable on every allowed device — d2: …

devq> qps 1
1 | d1  | FINISHED | Counts: {'00': 1007, '11': 989, '01': 26, '10': 26}

devq> qmap 1

Job 1 mapping

device: d1 (fakenairobiv2)

virtual → physical

  0 → 1
  1 → 2

devq> qerrors e d1

  d1 (fakenairobiv2):

  Edge Error Map:

    (0, 1) -> 0.0086
    (1, 2) -> 0.0070
    ...
```

### Asynchronous execution

`qrun` and `qrunpack` are **non-blocking**. They route, allocate, and
dispatch jobs onto a shared background executor and then return
immediately — the shell stays responsive while circuits run, so you can
submit more work (to the same device or another) or inspect state
without waiting for any result. A synchronous shell would freeze between
`qrun` and its result; this one does not.

Because dispatch and result are decoupled, a dispatched job comes back
`RUNNING`, and its result surfaces later through `qps`:

- `qrun` routes and attempts allocation immediately (the priority path,
  bypassing the queue). On success it prints `[>] Job N dispatched …`
  and returns with the job `RUNNING`. If allocation fails but the job is
  feasible on its routed device, it stays `WAITING` in that device's
  queue (transient contention); if it is unsatisfiable everywhere
  allowed, it is `REJECTED` immediately. `qrun` accepts exactly one job.
- `qrunpack` dispatches every queued job it can place right now, prints
  one `[>] Job N dispatched …` per job, and returns. It does **not**
  wait on futures and does not block on a job left `WAITING`.

Before allocating, `qrun` first collects any earlier jobs whose futures
have *already* resolved, returning their qubits to the pool. This is a
non-blocking sweep — it waits on nothing still in flight — but it means a
fast provider (the built-in simulator resolves near-instantly) has its
finished jobs' qubits reclaimed in time for the next job to use them,
rather than that job waiting on capacity that is logically free but not
yet collected. A slow provider's futures are simply not done yet, so
nothing is collected and the job dispatches or waits on real contention
exactly as it would otherwise.

**`qps` is the reporting surface.** It is a snapshot: it shows every
job's current lifecycle state (`RUNNING`, `WAITING`, `REJECTED`,
`FINISHED`, `FAILED`), and once a job has resolved it folds the outcome
into that job's row — `Counts: {…}` for `FINISHED`, `Error: …` for
`FAILED`, `Reason: …` for `REJECTED`. A job still executing simply reads
`RUNNING`; run `qps` again a moment later to see it settle. `qps` never
waits. It accepts an optional id filter — `qps 3 7` shows only those
jobs; an unknown id prints `Job N does not exist.` and a non-integer
token is flagged, neither aborting the rest of the view.

Results reach the interactive console **only** through `qps`. The kernel
prints the `[Kernel] Dispatching job N → …` placement line when a job is
dispatched, but it does not echo a job's completion — the resolve event
is still recorded in the event log (see
[`EVENT_LOG.md`](EVENT_LOG.md)) for benchmarks and metrics, but the
console does not print a `[Kernel] Job N FINISHED` line, since `qps`
already reports the outcome on the job's row. This avoids the duplicate
that would otherwise appear when a `qps` (or any command) collects a
finished future.

**Waiting jobs self-heal.** A job that is `WAITING` on qubits held by a
still-running job is retried automatically the moment that holder
completes and frees them — no need to re-issue `qrunpack`. The retry is
tied to a job's resolution: whenever a completion is observed (including
by a plain `qps` snapshot, which resolves any finished futures as it
reports), the freed qubits are offered to that device's waiting jobs and
the next one dispatches. So a `qps` you run purely to check status can
be the thing that lets a waiter proceed — you may see a job that was
`WAITING` a moment ago now `RUNNING`. This is a consequence of capacity
freeing, not a scheduling decision `qps` makes; `qps` never chooses to
run anything, it only observes completions, and freeing qubits is what
advances the waiters.

**Command history.** Interactive sessions keep readline history in
`~/.devq_history`, capped at the last 1000 commands. A file that has
grown past 4 MB is trimmed on startup before being read, so an
oversized history repairs itself rather than slowing every launch.
Shells built programmatically via `DevQ.build()` skip readline
entirely — history is meaningless for a driven session, and on macOS
(where `readline` is backed by libedit) reading a large history file
costs enormous amounts of memory.

---

## JobSpec: Job-Level Noise Thresholds & Device Constraints

`qrun` and `qsubmit` arguments are parsed into **JobSpec** objects:

```python
JobSpec(file_path, max_qubit_error=None, max_edge_error=None,
        max_1q_gate_error=None, exec_on=None, no_exec_on=None)
```

**Noise thresholds** are **hard constraints**: any qubit whose readout error
exceeds `max_qubit_error`, any qubit whose single-qubit gate error exceeds
`max_1q_gate_error`, or any edge whose two-qubit gate error exceeds
`max_edge_error`, is excluded from allocation for that job. The two
per-qubit thresholds are **ANDed** — a qubit must clear both its readout and
its 1-qubit-gate threshold to be eligible. `None` means no filtering on that
dimension. Thresholds are **job-level only** — a deliberate design decision.
Error filtering is a per-job user intent, not a platform property, so it is
expressed at submission time; bracket groups cover applying one threshold
across many jobs.
(StaticAllocator applies the qubit thresholds only — it has no topology
concept, so the edge threshold is ignored there by design.)

Note that DevQ's device model also carries T2 coherence and gate duration,
but these are **not** job-level filters — they are scoring/estimation inputs
a scheduler or router reads, not eligibility knobs a user imposes. Only the
three error terms have `--max-*` filters.

**Device constraints** bind jobs to devices:
- `--exec=d0,d2` — allow-list: the job may **only** run on the listed
  devices. If it is infeasible on all of them, it is REJECTED — never
  re-routed elsewhere.
- `--no-exec=d1` — deny-list: the job is never routed to the listed devices.
- The two flags are mutually exclusive on the same job or group (an
  allow-list already implies exclusion of every other device).
- Device lists are comma-separated without brackets (brackets are reserved
  for job grouping). Device *existence* is validated at submission —
  referencing a device that is not attached rejects the whole batch.

**Frontend selection** picks which source-language reader parses a job:
- `--frontend=<name>` — read this job's file with the named registered
  frontend (`qregistry f` lists them). Needed **only** when the file's
  extension is claimed by more than one frontend — for example, both a
  2.0 and a future 3.0 reader claim `.qasm`, and DevQ cannot tell which
  dialect the file is. When exactly one frontend claims the extension it
  is used automatically, so the flag is unnecessary in the common case.
- Without the flag, a job whose extension is ambiguous is rejected with an
  error naming the competing frontends; a job whose extension no frontend
  claims is rejected too. An unknown frontend name is rejected, listing
  what is registered. Binds per job and per group exactly like the other
  flags.

**Shot count** overrides how many times the job's circuit is executed:
- `--shots=<N>` — run this job with `N` shots, a positive integer. Without
  the flag, the job uses the device-resolved `shots` config value (the
  four-level cascade — see [`CONFIGURATION.md`](CONFIGURATION.md)). A
  per-job `--shots` sits **above** that cascade: it overrides the device
  value for this job only, whole (not blended or capped), because shot
  count is a statistical requirement of the circuit, not device policy.
- A non-integer (`--shots=10.5`), non-positive (`--shots=0`), or
  non-numeric value is rejected, killing the whole batch like any other
  malformed flag. Binds per job and per group exactly like the other flags.

If constraints or filtering make allocation *temporarily* impossible on the
routed device (resources busy), the job is set WAITING and retried
automatically once a running job on that device completes and frees its
qubits (see [Asynchronous execution](#asynchronous-execution) above — no
`qrunpack` re-issue is needed). If they make allocation *permanently*
impossible on every allowed device, the job is REJECTED with one
router-aggregated reason per candidate device — detected via each device's
allocator `feasible()` check, which deliberately ignores pool state.

### Syntax

```
# Bare jobs — no thresholds, any device
qsubmit bell.qasm
qsubmit bell.qasm ghz.qasm

# Trailing flags — bind ONLY to the job immediately before them
qsubmit bell.qasm --max-qubit-error=0.05
qsubmit bell.qasm --max-edge-error=0.1 --no-exec=d0
qsubmit bell.qasm --max-1q-gate-error=0.0005
qsubmit bell.qasm --exec=d1,d2
qsubmit bell.qasm --shots=8192

# Bracket group — flags apply to ALL jobs in the group
qsubmit [a.qasm b.qasm --max-qubit-error=0.05 --no-exec=d0]
qsubmit [a.qasm b.qasm]                          # valid: group, no flags

# Mixed — groups and bare jobs combine; flags never leak across
qsubmit [a.qasm b.qasm --max-qubit-error=0.05] c.qasm d.qasm --exec=d2 e.qasm
#   a: qe=0.05 | b: qe=0.05 | c: defaults | d: exec=d2 | e: defaults
```

Threshold values must be floats in `[0, 1]`; device references must match
`d<int>`. Malformed input (unclosed brackets, unknown flags, out-of-range
values, flags with no preceding file, bracketed or malformed device lists,
`--exec` with `--no-exec`, references to unattached devices) is rejected
with a clear error and no job is created.

---