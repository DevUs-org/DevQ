'''
Tags: Main

BaseRouter — Abstract base class for all device routers.

Defines the routing contract the kernel depends on, mirroring the
BaseAllocator / BaseScheduler pattern: routing policy is a pluggable,
inspectable decision layer, swappable via config (and benchmarkable
via qbench, Phase 5).

Contract:
    route(qcb, contexts) -> (DeviceContext, None) | (None, reason)

    - Returns (context, None) when a feasible device was chosen.
    - Returns (None, reason) when the job is unsatisfiable on EVERY
      device it is allowed to run on — the caller classifies this
      REJECTED. The reason aggregates one line per candidate device.
    - The router NEVER mutates QCB state — it returns a decision;
      the kernel applies it.
    - Routing is sticky: the kernel routes each job exactly once and
      records the binding on the QCB. Re-routing WAITING jobs to
      less-loaded devices (work migration) is deliberate future work.

The base class implements the shared candidate pipeline:
  1. filter contexts by the job's exec_on / no_exec_on constraints
  2. keep only devices where the job is feasible, per that context's
     allocator feasible() — pool-state-independent by design, so
     transient contention can never pollute the routing decision
  3. delegate the final choice among feasible candidates to the
     subclass via select().
'''

from abc import ABC, abstractmethod


class BaseRouter(ABC):

    def route(self, qcb, contexts):
        '''
        Choose a device for a job, or explain why none is possible.

        Args:
            qcb:      the job to route
            contexts: list of all attached DeviceContexts

        Returns:
            (DeviceContext, None) on success,
            (None, reason_string) if unsatisfiable everywhere allowed.
        '''
        candidates, reason = self._candidates(qcb, contexts)

        if not candidates:
            return None, reason

        return self.select(qcb, candidates), None

    @abstractmethod
    def select(self, qcb, candidates):
        '''
        Choose one context from a non-empty list of feasible candidates.
        Must be implemented by subclasses. Must be deterministic for
        identical inputs (ties broken by lower device index).
        '''
        pass

    # ── Shared candidate pipeline ─────────────────────────────────────────────

    def _candidates(self, qcb, contexts):
        '''
        Apply device constraints and per-device feasibility.

        Returns:
            (feasible_contexts, None) if any survive,
            ([], aggregated_reason) otherwise.
        '''
        allowed = self._allowed(qcb, contexts)

        if not allowed:
            return [], ("no device satisfies the job's device constraints "
                        f"(exec={_fmt(qcb.exec_on)}, "
                        f"no-exec={_fmt(qcb.no_exec_on)})")

        feasible = []
        reasons  = []

        for ctx in allowed:
            reason = ctx.memory_manager.unsatisfiable_reason(
                qcb.circuit,
                max_qubit_error=qcb.max_qubit_error,
                max_edge_error=qcb.max_edge_error
            )
            if reason is None:
                feasible.append(ctx)
            else:
                reasons.append(f"d{ctx.index}: {reason}")

        if feasible:
            return feasible, None

        return [], "unsatisfiable on every allowed device — " + "; ".join(reasons)

    def _allowed(self, qcb, contexts):
        '''Filter contexts by the job's allow/deny lists.'''
        if qcb.exec_on is not None:
            return [c for c in contexts if c.index in qcb.exec_on]
        if qcb.no_exec_on is not None:
            return [c for c in contexts if c.index not in qcb.no_exec_on]
        return list(contexts)


def _fmt(indices):
    if indices is None:
        return "-"
    return ",".join(f"d{i}" for i in indices)