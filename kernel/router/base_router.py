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

    def __init__(self, router_queue_weight=0.5, router_noise_weight=0.5, qubit_error_weight=0.1, edge_error_weight=0.9):
        self.router_queue_weight = router_queue_weight
        self.router_noise_weight = router_noise_weight
        self.qubit_error_weight = qubit_error_weight
        self.edge_error_weight  = edge_error_weight


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

    def explain(self, qcb, candidates):
        '''
        Per-candidate scoring detail for the decision select() would make
        on this same candidate list. Used only by the event log; routing
        never depends on it.

        Returns None (the default) for routers that do not score — a
        round-robin policy has no scores to report, and inventing them
        would be worse than reporting none.

        Scoring routers return one dict per candidate:

            [{"device": 1,                    # device index
              "score": 0.42,                  # final comparable score
              "terms": {...}}, ...]           # router-specific components

        `terms` carries the RAW inputs to the score, not just the total.
        The total alone is not enough to re-derive routing under
        different cost weights, which is the whole point of logging
        scores: a weight sweep should be answerable from one recorded
        run rather than by re-executing every job.

        Contract:
          - MUST NOT mutate router or context state. It runs only when
            logging is enabled, so any state it changed would make
            logged runs diverge from unlogged ones.
          - MUST be consistent with select(): the candidate this reports
            as best must be the one select() returns for the same input.
            Deriving both from one shared scoring helper is the reliable
            way to guarantee that; two parallel implementations drift.
          - Receives the SAME filtered candidate list select() gets, so
            devices excluded by exec_on/feasibility never appear.

        Args:
            qcb:        the job being routed
            candidates: non-empty list of feasible DeviceContexts

        Returns:
            list[dict] or None
        '''
        return None

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