'''
Tags: Main

DevQ Kernel — Core execution engine and federation host.

The kernel hosts one DeviceContext per attached device (each bundling
that device's MemoryManager, allocator and scheduler instance) plus a
Router that binds jobs to devices. Two-level scheduling, the classical
cluster pattern: the router decides WHICH device a job runs on; each
context's local scheduler decides WHEN it runs there.

Responsibilities:
  - Accept job submissions and create QCBs via the (global) process table
  - Route READY jobs to device contexts (sticky — routed once, never
    re-routed; work migration is deliberate future work)
  - Drive every context's scheduler via step()
  - Dispatch execution to the owning device via device.execute()
  - Resolve pending futures (sync or async) and update QCB state
  - Expose per-device metadata to QShell

The kernel never knows which provider backs a device, and never knows
which scheduler, allocator or router policy is configured — it speaks
only to the DeviceContext, Router and QuantumDevice contracts.

REJECTED is the umbrella terminal state for any kernel-level rejection,
whatever stage produced it: routing (unsatisfiable on every allowed
device, or device constraints exclude everything) or allocation
classification inside a scheduler. With sticky routing, rejection
concentrates at the router — post-routing allocation failures classify
WAITING, since routing already established feasibility on the chosen
device and feasible() ignores pool state.
'''

import sys
import time

from circuits.execution_result import ExecutionResult
from kernel.process.process_table import ProcessTable
from kernel.process.lifecycle import JobStates
from kernel.events import PrintSink


class Kernel:
    def __init__(self, contexts, router, sink=None):
        '''
        Args:
            contexts: list of DeviceContext, indexed d0..dn in add order
            router:   BaseRouter instance (from global config)
            sink:     event sink (see kernel/events.py). None means
                      PrintSink — the console output DevQ has always
                      produced.
        '''
        self.contexts      = contexts
        self.router        = router
        self.process_table = ProcessTable()
        self.router_queue  = []   # READY QCBs awaiting device binding
        self._pending      = []   # RUNNING QCBs awaiting future resolution

        # Event sink. Defaults to PrintSink so an interactive session
        # behaves exactly as it did before events existed; a benchmark
        # runner swaps in a MultiSink(PrintSink(), RecordSink()).
        self.sink = sink if sink is not None else PrintSink()

        # Cycle counter. Incremented once per scheduling cycle — at the
        # top of step(), and once per run_job() since qrun bypasses
        # step() entirely. Every event belongs to exactly one cycle.
        self._cycle = 0

        # Monotonic event sequence. This is the log's notion of TIME:
        # deterministic and byte-comparable across identical seeded
        # runs, which wall-clock timestamps are not. Real durations
        # belong on QCB timestamps, not here.
        self._seq = 0

    # ── Job submission ────────────────────────────────────────────────────────

    def submit_job(self, circuit, max_qubit_error=None, max_edge_error=None,
                   exec_on=None, no_exec_on=None):
        '''
        Create a QCB and place it in the router queue. Does not route
        and does not execute — the job stays READY until a scheduling
        cycle binds it to a device.

        Job-level noise thresholds and device constraints are stored on
        the QCB; allocators and the router read them from there.
        '''
        qcb = self.process_table.create_job(
            circuit,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error,
            exec_on=exec_on,
            no_exec_on=no_exec_on
        )
        self.router_queue.append(qcb)
        self._emit("submit",
                   job_id          = qcb.job_id,
                   num_qubits      = circuit.num_qubits,
                   max_qubit_error = max_qubit_error,
                   max_edge_error  = max_edge_error,
                   exec_on         = exec_on,
                   no_exec_on      = no_exec_on)
        qcb.submitted_seq = self._seq - 1
        return qcb

    # ── Events ────────────────────────────────────────────────────────────────

    def _emit(self, event, **fields):
        '''
        Emit one structured event record.

        cycle and seq are stamped HERE, not at call sites, so no emit
        site can forget them or disagree about what cycle it is in.

        The sink call is wrapped: observability must never be able to
        kill a job. A raising sink is reported once and then ignored.
        '''
        record = {"event": event, "cycle": self._cycle, "seq": self._seq}
        record.update(fields)
        self._seq += 1

        try:
            self.sink.emit(record)
        except Exception as exc:
            if not getattr(self, "_sink_broken", False):
                self._sink_broken = True
                print(f"[DevQ Warning] event sink "
                      f"{type(self.sink).__name__} raised "
                      f"{type(exc).__name__}: {exc}. Further failures "
                      f"suppressed; execution is unaffected.",
                      file=sys.stderr)
        return record

    # ── Execution cycle ───────────────────────────────────────────────────────

    def step(self):
        '''
        One scheduling cycle:
          1. Resolve any pending futures from previous dispatches
          2. Routing phase — bind every queued READY job to a device
             (or REJECT it if unsatisfiable everywhere allowed)
          3. Scheduling phase — run every context's local scheduler and
             dispatch its RUNNING jobs on that context's device

        Returns all jobs processed this cycle: routing rejections plus
        each context's processed jobs (dispatched and/or rejected).
        Callers must not assume every returned job was dispatched —
        check qcb.state.
        '''
        self._cycle += 1

        self._resolve_pending()

        processed = self._route_ready_jobs()

        for ctx in self.contexts:
            jobs = ctx.scheduler.schedule()

            if not jobs:
                continue

            jobs = jobs if isinstance(jobs, list) else [jobs]

            for job in jobs:
                if job.state != JobStates.REJECTED:
                    self._execute(job, ctx)

            processed.extend(jobs)

        # Emitted even when nothing happened, so a consumer can tell a
        # cycle that did no work from a cycle missing from the log.
        self._emit("cycle_end", processed=len(processed))

        return processed

    def run_job(self, qcb):
        '''
        Immediate priority execution for a single job, bypassing the
        scheduling cycle. Used by qrun.

        Routes the job immediately (respecting its device constraints),
        attempts allocation on the routed context, executes, and blocks
        until this job's own future resolves — qrun's contract is
        synchronous by definition. Other pending futures resolve
        opportunistically. On allocation failure the job stays WAITING
        in the routed context's queue for a later qrunpack.
        '''
        # qrun bypasses step(), so it owns its cycle. Without this every
        # qrun event would carry the previous cycle's number and appear
        # to belong to a scheduling cycle it took no part in.
        self._cycle += 1

        self.router_queue.remove(qcb)
        ctx, reason = self._route(qcb)

        if ctx is None:
            self._reject(qcb, reason)
            return

        try:
            mapping = ctx.memory_manager.allocate(
                qcb.circuit,
                max_qubit_error=qcb.max_qubit_error,
                max_edge_error=qcb.max_edge_error
            )
            qcb.v2p_map = mapping
        except Exception:
            # Routing established feasibility on this device, and
            # feasible() ignores pool state — so this failure is
            # transient contention by construction.
            qcb.state = JobStates.WAITING
            ctx.scheduler.enqueue(qcb)
            return

        self._execute(qcb, ctx)
        self._wait_for(qcb)

    def _route_ready_jobs(self):
        '''Drain the router queue, binding or rejecting every job.'''
        rejected = []

        for qcb in list(self.router_queue):
            ctx, reason = self._route(qcb)
            self.router_queue.remove(qcb)

            if ctx is None:
                self._reject(qcb, reason)
                rejected.append(qcb)
            else:
                ctx.scheduler.enqueue(qcb)

        return rejected

    def _route(self, qcb):
        '''
        Bind a job to a device context (sticky) or return a reason.
        Does NOT touch the router queue — callers own queue membership.
        '''
        # Recompute the candidate set so the log can record what the
        # router was choosing BETWEEN, not just what it chose. Scores
        # come from explain(), which returns None for routers that do
        # not score — a round-robin decision has no margin to report.
        candidates, _ = self.router._candidates(qcb, self.contexts)
        scores = self.router.explain(qcb, candidates) if candidates else None

        ctx, reason = self.router.route(qcb, self.contexts)

        if ctx is not None:
            qcb.device_index = ctx.index
            self._emit("route",
                       job_id     = qcb.job_id,
                       device     = ctx.index,
                       candidates = [c.index for c in candidates],
                       scores     = scores)
        else:
            self._emit("reject",
                       job_id     = qcb.job_id,
                       candidates = [c.index for c in candidates],
                       scores     = scores,
                       reason     = reason)

        return ctx, reason

    def _reject(self, qcb, reason):
        qcb.state         = JobStates.REJECTED
        qcb.reject_reason = reason

    def _execute(self, qcb, ctx):
        self._emit("dispatch",
                   job_id       = qcb.job_id,
                   device       = ctx.index,
                   device_label = ctx.label,
                   v2p_map      = qcb.v2p_map,
                   shots        = ctx.shots)
        qcb.dispatched_seq = self._seq - 1
        qcb.future = ctx.device.execute(qcb.circuit, qcb.v2p_map,
                                        shots=ctx.shots)
        qcb.state  = JobStates.RUNNING
        ctx.running_jobs += 1
        self._pending.append(qcb)

    def _resolve_pending(self):
        '''
        Check all pending futures and finalise any that are done.
        Frees allocated qubits on the OWNING device's pool and sets
        final job state on completion. Non-blocking — futures still
        in flight stay pending (the async case).
        '''
        still_pending = []

        for qcb in self._pending:
            if qcb.future and qcb.future.done():
                result     = qcb.future.result()
                qcb.result = result
                ctx        = self.contexts[qcb.device_index]

                ctx.memory_manager.free(list(qcb.v2p_map.values()))
                ctx.running_jobs -= 1

                qcb.state = (JobStates.FINISHED if result.success
                             else JobStates.FAILED)
                qcb.resolved_seq = self._seq
                self._emit("resolve",
                           job_id  = qcb.job_id,
                           device  = ctx.index,
                           state   = qcb.state.value,
                           success = result.success,
                           counts  = result.counts,
                           error   = result.error)
            else:
                still_pending.append(qcb)

        self._pending = still_pending

    def _wait_for(self, qcb, poll_interval=0.02, timeout=300):
        '''
        Block until a specific job's future resolves (qrun path).

        Bounded by `timeout`: a future that never resolves — a wedged
        provider, a dead executor — would otherwise spin here forever,
        and the caller has no way to distinguish that from slow work.
        Failing loudly after five minutes is strictly better than a
        process that appears to hang.
        '''
        deadline = time.monotonic() + timeout

        while qcb in self._pending:
            self._resolve_pending()
            if qcb not in self._pending:
                return
            if time.monotonic() > deadline:
                self._pending.remove(qcb)
                ctx = self.contexts[qcb.device_index]
                ctx.memory_manager.free(list(qcb.v2p_map.values()))
                ctx.running_jobs -= 1
                qcb.state  = JobStates.FAILED
                qcb.result = ExecutionResult(
                    counts  = None,
                    success = False,
                    error   = (f"execution did not resolve within {timeout}s "
                               f"— provider or executor may be wedged")
                )
                qcb.resolved_seq = self._seq
                self._emit("resolve",
                           job_id  = qcb.job_id,
                           device  = qcb.device_index,
                           state   = qcb.state.value,
                           success = False,
                           counts  = None,
                           error   = qcb.result.error)
                return
            time.sleep(poll_interval)

    # ── QShell API ────────────────────────────────────────────────────────────

    def list_devices(self):
        return self.contexts

    def has_pending(self):
        '''True while any dispatched future is still unresolved.'''
        return bool(self._pending)

    def has_queued(self):
        '''True while any job sits in the router or a scheduler queue.'''
        if self.router_queue:
            return True
        return any(ctx.queue_depth() for ctx in self.contexts)

    def list_jobs(self):
        return self.process_table.list_jobs()

    def get_job(self, job_id):
        return self.process_table.jobs.get(job_id)

    def get_job_mapping(self, job_id):
        job = self.process_table.jobs.get(job_id)
        return job.v2p_map if job else None

    def get_job_result(self, job_id):
        job = self.process_table.jobs.get(job_id)
        return job.result if job else None

    def get_topology(self, device_index):
        return self.contexts[device_index].device.coupling_map

    def get_free_qubits(self, device_index):
        return self.contexts[device_index].memory_manager.pool.free_qubits

    def get_error_map(self, device_index):
        return self.contexts[device_index].device.error_map

    def get_edge_error_map(self, device_index):
        return self.contexts[device_index].device.edge_error_map