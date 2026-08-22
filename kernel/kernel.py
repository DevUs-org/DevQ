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
                   exec_on=None, no_exec_on=None, shots=None,
                   max_1q_gate_error=None):
        '''
        Create a QCB and place it in the router queue. Does not route
        and does not execute — the job stays READY until a scheduling
        cycle binds it to a device.

        Job-level noise thresholds and device constraints are stored on
        the QCB; allocators and the router read them from there. A
        job-level `shots` (None = defer to the device config) is likewise
        stored on the QCB and resolved against the device value at
        dispatch — see _execute.
        '''
        qcb = self.process_table.create_job(
            circuit,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error,
            exec_on=exec_on,
            no_exec_on=no_exec_on,
            shots=shots,
            max_1q_gate_error=max_1q_gate_error
        )
        self.router_queue.append(qcb)
        self._emit("submit",
                   job_id            = qcb.job_id,
                   num_qubits        = circuit.num_qubits,
                   max_qubit_error   = max_qubit_error,
                   max_edge_error    = max_edge_error,
                   max_1q_gate_error = max_1q_gate_error,
                   exec_on           = exec_on,
                   no_exec_on        = no_exec_on,
                   shots             = shots)
        qcb.submitted_seq = self._seq - 1
        qcb.submitted_at  = time.time()
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
            processed.extend(self._schedule_ctx(ctx))

        # Emitted even when nothing happened, so a consumer can tell a
        # cycle that did no work from a cycle missing from the log.
        self._emit("cycle_end", processed=len(processed))

        return processed

    def _schedule_ctx(self, ctx):
        '''
        Run one context's scheduler and dispatch the jobs it selects.

        The single place scheduling turns into execution — shared by the
        per-cycle step() loop and the retry that _resolve_pending()
        triggers when a completing job frees qubits. Returns the jobs the
        scheduler processed this pass (dispatched and/or rejected); the
        caller decides what to do with them.
        '''
        jobs = ctx.scheduler.schedule()

        if not jobs:
            return []

        jobs = jobs if isinstance(jobs, list) else [jobs]

        for job in jobs:
            if job.state != JobStates.REJECTED:
                self._execute(job, ctx)

        return jobs

    def run_job(self, qcb):
        '''
        Immediate priority execution for a single job, bypassing the
        scheduling cycle. Used by qrun.

        Routes the job immediately (respecting its device constraints),
        attempts allocation on the routed context, and dispatches —
        then RETURNS without blocking. The job's future resolves on the
        shared executor concurrently with further shell interaction; the
        caller reads its outcome later via poll()/qps. On allocation
        failure the job stays WAITING in the routed context's queue for
        a later qrunpack. On dispatch the job is left RUNNING; the shell
        is free to submit more work or query status meanwhile.
        '''
        # qrun bypasses step(), so it owns its cycle. Without this every
        # qrun event would carry the previous cycle's number and appear
        # to belong to a scheduling cycle it took no part in.
        self._cycle += 1

        # Collect any previously dispatched futures that have ALREADY
        # resolved, freeing their qubits, before this job routes and
        # allocates. This is a non-blocking sweep — it waits on nothing
        # still in flight — but it means a fast provider (the simulator
        # resolves near-instantly) has its finished jobs' qubits returned
        # to the pool in time for THIS job to use them, rather than this
        # job spuriously WAITING on capacity that is logically free but
        # not yet collected. A slow provider's futures are simply not done
        # yet, so nothing is collected and the async contract is intact.
        self._resolve_pending()

        self.router_queue.remove(qcb)

        # Same unrunnable-circuit guard as the scheduling path: a
        # well-formed but unsupported circuit is REJECTED before routing,
        # never executed. qrun must not be a back door around it.
        reason = getattr(qcb.circuit, "unrunnable_reason", None)
        if reason is not None:
            self._emit("reject",
                       job_id        = qcb.job_id,
                       candidates    = [],
                       scores        = None,
                       reason        = reason,
                       circuit_hash  = qcb.circuit_hash,
                       circuit_label = qcb.circuit_label)
            self._reject(qcb, reason)
            return

        ctx, reason = self._route(qcb)

        if ctx is None:
            self._reject(qcb, reason)
            return

        try:
            mapping = ctx.memory_manager.allocate(
                qcb.circuit,
                max_qubit_error=qcb.max_qubit_error,
                max_edge_error=qcb.max_edge_error,
                max_1q_gate_error=qcb.max_1q_gate_error
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

    def _route_ready_jobs(self):
        '''Drain the router queue, binding or rejecting every job.'''
        rejected = []

        for qcb in list(self.router_queue):
            self.router_queue.remove(qcb)

            # A circuit the frontend flagged as unrunnable (a well-formed
            # but unsupported construct — classical control, mid-circuit
            # measurement) is rejected here, before routing: no device can
            # faithfully run it, so binding it to one would be wrong. This
            # is the SAME terminal outcome as an unsatisfiable allocation —
            # REJECTED with a reason — the umbrella covering every "DevQ
            # will not run this", whether the reason came from the circuit
            # layer or from the router/allocator below.
            reason = getattr(qcb.circuit, "unrunnable_reason", None)
            if reason is not None:
                self._emit("reject",
                           job_id        = qcb.job_id,
                           candidates    = [],
                           scores        = None,
                           reason        = reason,
                           circuit_hash  = qcb.circuit_hash,
                           circuit_label = qcb.circuit_label)
                self._reject(qcb, reason)
                rejected.append(qcb)
                continue

            ctx, reason = self._route(qcb)

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
                       job_id        = qcb.job_id,
                       candidates    = [c.index for c in candidates],
                       scores        = scores,
                       reason        = reason,
                       circuit_hash  = qcb.circuit_hash,
                       circuit_label = qcb.circuit_label)

        return ctx, reason

    def _reject(self, qcb, reason):
        qcb.state         = JobStates.REJECTED
        qcb.reject_reason = reason

    def _execute(self, qcb, ctx):
        # Resolve shots once, here, so the logged value and the executed
        # value cannot diverge. A job that named its own shot count wins
        # whole over the device-resolved `ctx.shots`; a job that did not
        # (shots is None) defers to the device cascade exactly as before.
        # This is the per-job tier sitting above the four-level device
        # cascade — see QCB.shots.
        shots = qcb.shots if qcb.shots is not None else ctx.shots

        self._emit("dispatch",
                   job_id       = qcb.job_id,
                   device       = ctx.index,
                   device_label = ctx.label,
                   v2p_map      = qcb.v2p_map,
                   shots        = shots)

        # Allocation decision (Phase 5.5a). Mirrors the router's `route`
        # event: the allocator recorded the candidate blocks it scored to
        # place THIS job during its live allocate(); here, on the dispatch
        # that placement enabled, the kernel reads that decision back and
        # logs the per-block scores, so an α/β sweep is answerable from the
        # log. A scoring allocator exposes it via explain_recorded (built
        # from the stash, not a re-enumeration — the pool has since
        # changed); a cost-oblivious allocator (Static/Graph) is not
        # sweepable and contributes nothing, the same honest silence as a
        # non-scoring router. Emitted only on dispatch — a failed WAITING
        # retry placed nothing and is not a decision worth logging.
        scores = self._allocation_scores(ctx, qcb)
        if scores is not None:
            self._emit("allocate",
                       job_id  = qcb.job_id,
                       device  = ctx.index,
                       block   = list(qcb.v2p_map.values()),
                       scores  = scores)

        # Scheduling decision (Phase 5.6). The scheduler-layer twin of the
        # `allocate` event above: a scoring scheduler recorded the queued
        # jobs it ranked to choose THIS cycle's dispatch during its live
        # schedule(); here, on the dispatch that choice produced, the
        # kernel reads that decision back and logs the per-job scores, so a
        # scheduler weight sweep is answerable from the log. The winner is
        # the dispatched job; the candidates are the jobs the scheduler
        # ranked. A scoring scheduler (NAQJS) exposes it via
        # explain_recorded (built from the pinned stash, not a re-read — the
        # queue has since changed); an order-only scheduler (FCFS/SDF/
        # Packing) is not sweepable and contributes nothing, the same
        # honest silence as a non-scoring router or allocator.
        sched_scores = self._schedule_scores(ctx, qcb)
        if sched_scores is not None:
            self._emit("schedule",
                       job_id  = qcb.job_id,
                       device  = ctx.index,
                       winner  = qcb.job_id,
                       scores  = sched_scores)

        qcb.dispatched_seq = self._seq - 1
        qcb.dispatched_at  = time.time()
        qcb.future = ctx.device.execute(qcb.circuit, qcb.v2p_map,
                                        shots=shots)
        qcb.state  = JobStates.RUNNING
        ctx.running_jobs += 1
        self._pending.append(qcb)

    def _allocation_scores(self, ctx, qcb):
        '''
        Per-block scores for the allocation that placed THIS job, or None
        if the context's allocator does not score. Reads the decision the
        scheduler pinned on the job at allocation time (qcb.alloc_decision)
        — per-job, so a batch scheduler that allocated several jobs before
        dispatch reports each its own decision, not the last one's. The
        block keys are lists (JSON-friendly) rather than the tuple keys the
        allocator uses internally.
        '''
        allocator = ctx.memory_manager.allocator
        if not getattr(allocator, "is_sweepable", lambda: False)():
            return None
        recorded = getattr(qcb, "alloc_decision", None)
        if not recorded:
            return None
        report = allocator.explain_recorded(recorded)
        return [
            {"block": list(row["key"]), "score": row["score"],
             "terms": row["terms"]}
            for row in report
        ]

    def _schedule_scores(self, ctx, qcb):
        '''
        Per-job scores for the scheduling decision that dispatched THIS
        job, or None if the context's scheduler does not score. Reads the
        decision the scheduler pinned on the job at dispatch
        (qcb.sched_decision) — per-job, so a batch scheduler that
        dispatched several jobs in one cycle reports each its own ranked
        queue, not the last one's. Mirrors _allocation_scores exactly, one
        layer up: the candidate keys are job ids (already JSON-friendly,
        unlike the allocator's tuple block keys, so no conversion).
        '''
        scheduler = ctx.scheduler
        if not getattr(scheduler, "is_sweepable", lambda: False)():
            return None
        recorded = getattr(qcb, "sched_decision", None)
        if not recorded:
            return None
        report = scheduler.explain_recorded(recorded)
        return [
            {"job_id": row["key"], "score": row["score"],
             "terms": row["terms"]}
            for row in report
        ]

    def _resolve_pending(self):
        '''
        Check all pending futures and finalise any that are done.
        Frees allocated qubits on the OWNING device's pool and sets
        final job state on completion. Non-blocking — futures still
        in flight stay pending (the async case).
        '''
        still_pending = []
        freed_ctxs    = []

        for qcb in self._pending:
            if qcb.future and qcb.future.done():
                result     = qcb.future.result()
                qcb.result = result
                ctx        = self.contexts[qcb.device_index]

                ctx.memory_manager.free(list(qcb.v2p_map.values()))
                ctx.running_jobs -= 1
                if ctx not in freed_ctxs:
                    freed_ctxs.append(ctx)

                qcb.state = (JobStates.FINISHED if result.success
                             else JobStates.FAILED)
                qcb.resolved_seq = self._seq
                qcb.resolved_at  = time.time()
                self._emit("resolve",
                           job_id  = qcb.job_id,
                           device  = ctx.index,
                           state   = qcb.state.value,
                           success = result.success,
                           counts  = result.counts,
                           circuit_hash = qcb.circuit_hash,
                           error   = result.error)
            else:
                still_pending.append(qcb)

        self._pending = still_pending

        # Freeing qubits is what unblocks a WAITING job, so retry the
        # scheduler on every context that just completed a job. This is
        # what makes async self-healing: a job that waited on contended
        # qubits dispatches as soon as the holder finishes, wherever that
        # completion is observed — step(), a qps snapshot's poll(), or
        # drain(). No caller has to re-issue qrunpack to advance a
        # waiter, and no foreground loop busy-waits to do it. A newly
        # freed context may itself free nothing new, so this does not
        # recurse; a job that still cannot allocate simply stays WAITING
        # until the next completion.
        for ctx in freed_ctxs:
            self._schedule_ctx(ctx)

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
                qcb.resolved_at  = time.time()
                self._emit("resolve",
                           job_id  = qcb.job_id,
                           device  = qcb.device_index,
                           state   = qcb.state.value,
                           success = False,
                           counts  = None,
                           circuit_hash = qcb.circuit_hash,
                           error   = qcb.result.error)
                return
            time.sleep(poll_interval)

    # ── QShell API ────────────────────────────────────────────────────────────

    def list_devices(self):
        return self.contexts

    def poll(self):
        '''
        Resolve any dispatched futures that are DONE and return.

        Non-blocking snapshot primitive: futures still in flight stay
        pending, done ones finalise to FINISHED/FAILED. qps calls this
        so already-complete jobs show settled state, without ever
        waiting on work still running. This is the async counterpart to
        the old synchronous qrun — status is pulled, never pushed.
        '''
        self._resolve_pending()

    def drain(self, timeout=300):
        '''
        Block until the session reaches quiescence — nothing queued and
        no future in flight — or `timeout` seconds elapse. Not used by
        the interactive shell (qps is a pure snapshot and never waits),
        but callers that need a settled view (batch drivers, the test
        harness) use this to reach a deterministic end state.

        Loops on has_queued() OR has_pending(), mirroring the benchmark
        runner's drain: a WAITING job keeps has_queued() true, and each
        _resolve_pending() both collects finished futures and — since a
        completion frees qubits — retries the waiters they unblock. So a
        job contended at dispatch still runs here without any caller
        re-issuing a dispatch command.

        Bounded for the same reason _wait_for is: a wedged provider or
        dead executor must fail loudly rather than spin forever. Each
        still-pending job past the deadline is failed with an explicit
        timeout result.
        '''
        deadline = time.monotonic() + timeout

        while self.has_queued() or self.has_pending():
            if time.monotonic() > deadline:
                for qcb in list(self._pending):
                    self._pending.remove(qcb)
                    ctx = self.contexts[qcb.device_index]
                    ctx.memory_manager.free(list(qcb.v2p_map.values()))
                    ctx.running_jobs -= 1
                    qcb.state  = JobStates.FAILED
                    qcb.result = ExecutionResult(
                        counts  = None,
                        success = False,
                        error   = (f"execution did not resolve within "
                                   f"{timeout}s — provider or executor "
                                   f"may be wedged")
                    )
                    qcb.resolved_seq = self._seq
                    qcb.resolved_at  = time.time()
                    self._emit("resolve",
                               job_id  = qcb.job_id,
                               device  = qcb.device_index,
                               state   = qcb.state.value,
                               success = False,
                               counts  = None,
                               circuit_hash = qcb.circuit_hash,
                               error   = qcb.result.error)
                return

            before_queued  = self.has_queued()
            before_pending = len(self._pending)

            # A full cycle: collect finished futures (which also retries
            # the waiters they unblock) and route/schedule anything still
            # queued. step() begins with its own _resolve_pending(), so
            # this is one coherent pass, not two.
            self.step()

            # If nothing moved, every remaining job is blocked on a future
            # still in flight. Stepping again cannot help and would only
            # emit empty cycles — the 37,923-empty-cycle trap the runner's
            # drain documents — so wait for the executor to make progress.
            made_progress = (self.has_queued() != before_queued
                             or len(self._pending) != before_pending)
            if not made_progress:
                time.sleep(0.02)

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