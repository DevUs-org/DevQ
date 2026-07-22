# DevQ Shell Reference

Every QShell command, and the JobSpec syntax for per-job noise thresholds
and device constraints.

Kept out of the README so that the README stays an overview rather than a
manual. Start a session with `python example.py`, or drive one
programmatically with `DevQ(...).build()` and `shell.onecmd(...)`.

Related: [`configuration.md`](configuration.md) for the config keys these
commands report, [`cost-model.md`](cost-model.md) for the scoring
mathematics behind routing and allocation decisions.

---

## QShell Command Reference

QShell commands deliberately mirror classical OS tools. Commands marked
`[dN]` take an optional device argument: with it, output covers that device
only; without it, output is sectioned per attached device (a single-device
session simply shows one `d0` section — the format is uniform).

| Command | Classical analogue | Purpose |
|---|---|---|
| `qrun` | — | Priority-execute a **single** job immediately, bypassing the queue |
| `qsubmit` | — | Enqueue one or more jobs without executing |
| `qrunpack` | — | Drain all queues via the router and per-device schedulers |
| `qdevices` | `lscpu` | List attached devices: index, name, provider, qubits, queued/running load |
| `qps` | `ps` | List all jobs with device binding and lifecycle state |
| `qmap <job_id>` | — | Show a job's device and virtual → physical qubit mapping |
| `qmem [dN]` | `free` | Show free `[]` vs allocated `[X]` qubits |
| `qtopology [dN] [q …]` | — | Show device coupling map(s) (qubit filtering requires a device) |
| `qerrors [q\|e\|b] [dN]` | `iostat` | Show qubit errors, edge errors, or both (default `b`) |
| `qconfig [dN]` | — | Show global router policy and each device's scheduler/allocator/shots with the source of every value |
| `!!` | `!!` | Repeat the last command |
| `exit` / Ctrl-D | — | Exit DevQ |

### Examples

```
devq> qdevices

  d0   random_backend       DevQSimulatedProvider     7 qubits   queued: 0  running: 0
  d1   fakenairobiv2        IBMSimulatedProvider      7 qubits   queued: 0  running: 0
  d2   fakelagosv2          IBMSimulatedProvider      7 qubits   queued: 0  running: 0

devq> qrun test_circuits/bell.qasm --exec=d1,d2
Job 1 submitted to queue.
[Kernel] Dispatching job 1 → d1 (fakenairobiv2) qubits {0: 1, 1: 2}
[Kernel] Job 1 FINISHED. Counts: {'00': 1007, '11': 989, '01': 26, '10': 26}
[+] Job 1 FINISHED.

devq> qrun test_circuits/bell.qasm --max-qubit-error=0.03 --exec=d2
Job 2 submitted to queue.
[x] Job 2 REJECTED: unsatisfiable on every allowed device — d2: no connected
    block of 2 qubits exists on this device under max_qubit_error=0.03,
    max_edge_error=None

devq> qps
1 | d1  | FINISHED
2 | -   | REJECTED

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

`qrun` vs `qsubmit`/`qrunpack`: `qrun` is a priority path — it routes and
attempts allocation immediately, executes, blocks until its own result
resolves, and leaves all queued jobs untouched. If allocation fails but the
job is feasible on its routed device, it stays WAITING in that device's
queue for a later `qrunpack`; if it is unsatisfiable everywhere allowed, it
is REJECTED. `qrun` accepts exactly one job (all flags, including
`--exec`/`--no-exec`, are supported).

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
        exec_on=None, no_exec_on=None)
```

**Noise thresholds** are **hard constraints**: any qubit whose readout error
exceeds `max_qubit_error`, or edge whose gate error exceeds
`max_edge_error`, is excluded from allocation for that job. `None` means no
filtering on that dimension. Thresholds are **job-level only** — a
deliberate design decision. Error filtering is a per-job user intent, not a
platform property, so it is expressed at submission time; bracket groups
cover applying one threshold across many jobs.
(StaticAllocator applies the qubit threshold only — it has no topology
concept, so the edge threshold is ignored there by design.)

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

If constraints or filtering make allocation *temporarily* impossible on the
routed device (resources busy), the job is set WAITING and retried. If they
make allocation *permanently* impossible on every allowed device, the job is
REJECTED with one router-aggregated reason per candidate device — detected
via each device's allocator `feasible()` check, which deliberately ignores
pool state.

### Syntax

```
# Bare jobs — no thresholds, any device
qsubmit bell.qasm
qsubmit bell.qasm ghz.qasm

# Trailing flags — bind ONLY to the job immediately before them
qsubmit bell.qasm --max-qubit-error=0.05
qsubmit bell.qasm --max-edge-error=0.1 --no-exec=d0
qsubmit bell.qasm --exec=d1,d2

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