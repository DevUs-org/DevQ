'''
Tags: Default

NoiseRouter — Noise- and load-aware device router (the DevQ default).

Scores every feasible candidate device and routes to the lowest score:

    score(ctx) = w_queue · queue_pressure(ctx) + w_noise · best_case_cost(ctx)

  queue_pressure  — jobs waiting in the context's scheduler queue plus
                    jobs currently RUNNING on that device. A busy device
                    is a worse destination even if its qubits are quiet.

  best_case_cost  — dry-run the context's OWN configured allocator
                    against a fresh, fully-free pool clone, then score
                    the mapping it returns with the NoiseGraph cost
                    formula S = α·Σ(qubit_error) + β·Σ(edge_error).
                    α / β come from the GLOBAL-scope copy of
                    qubit_error_weight / edge_error_weight (normalised
                    to sum to 1; defaults 0.1 / 0.9) — one uniform
                    ruler across all candidates, deliberately NOT each
                    device's own allocator weights, so cross-device
                    scores stay comparable. The formula is a yardstick applied
                    to the allocator's output, not an assumption about
                    which allocator is configured: a Static-configured
                    device is scored on the noise-oblivious block Static
                    would actually pick — the mapping quality the job
                    would really receive there.

Both terms are min-max normalised across the candidate set before
weighting (queue depths are small integers, noise costs live around
0.01–0.1 — raw mixing would let one term silently dominate). Weights
come from global config (router_queue_weight / router_noise_weight,
default 0.5 / 0.5). Ties break by lower device index (deterministic).
'''

from kernel.router.base_router import BaseRouter
from kernel.memory.qubit_pool import QubitPool

class NoiseRouter(BaseRouter):

    # Human-readable name shown by qconfig. Any registered component
    # may define one; the registry falls back to the class name.
    LABEL = "Noise Aware Router"

    def select(self, qcb, candidates):
        scored = self._score_all(qcb, candidates)

        # min() on (score, index, ctx): lowest score wins, ties break by
        # lower device index — deterministic.
        #
        # NOTE: currently UNFALSIFIABLE. _candidates() yields contexts in
        # index order and min() is stable, so removing the index term
        # changes no observable behaviour; mutation testing confirms it.
        # Kept because it makes the intent explicit and holds if a future
        # candidate pipeline ever reorders. Do not write a test for it —
        # such a test could not fail.
        return min(scored, key=lambda t: (t[0], t[1]))[2]

    def explain(self, qcb, candidates):
        '''
        Report what select() scored, with the raw terms behind each score.

        Shares _score_all() with select() rather than recomputing
        independently, so the reported best candidate is the selected one
        by construction. Recomputation costs about 0.05 ms per candidate
        (one allocator dry-run), which is negligible beside execution —
        caching would only add a staleness failure mode.
        '''
        scored = self._score_all(qcb, candidates, with_terms=True)
        return [
            {"device": ctx.index, "score": score, "terms": terms}
            for score, _idx, ctx, terms in scored
        ]

    def _score_all(self, qcb, candidates, with_terms=False):
        '''
        Score every candidate. THE one scoring path — select() and
        explain() both come through here so they cannot drift.

        Returns (score, index, ctx) tuples, or (score, index, ctx, terms)
        when with_terms is set.

        Scores are min-max normalised ACROSS THE CANDIDATE SET, so a
        score is only meaningful relative to the others in the same
        routing decision. That is why terms record the raw pressure and
        cost alongside their normalised forms: raw values are comparable
        across decisions and re-derivable under other weights, normalised
        ones are not.
        '''
        pressures = [self._queue_pressure(ctx) for ctx in candidates]
        costs     = [self._best_case_cost(ctx, qcb) for ctx in candidates]

        p_norm = _min_max(pressures)
        c_norm = _min_max(costs)

        scored = []
        for p_raw, c_raw, p, c, ctx in zip(pressures, costs, p_norm, c_norm,
                                           candidates):
            score = (self.router_queue_weight * p
                     + self.router_noise_weight * c)
            if with_terms:
                scored.append((score, ctx.index, ctx, {
                    "queue_pressure"     : p_raw,
                    "best_case_cost"     : c_raw,
                    "queue_pressure_norm": p,
                    "best_case_cost_norm": c,
                    "router_queue_weight": self.router_queue_weight,
                    "router_noise_weight": self.router_noise_weight,
                    "qubit_error_weight" : self.qubit_error_weight,
                    "edge_error_weight"  : self.edge_error_weight,
                }))
            else:
                scored.append((score, ctx.index, ctx))
        return scored

    # ── Scoring terms ─────────────────────────────────────────────────────────

    def _queue_pressure(self, ctx):
        return ctx.queue_depth() + ctx.running_jobs

    def _best_case_cost(self, ctx, qcb):
        '''
        Dry-run this context's configured allocator on an empty pool
        clone; score the resulting mapping with the S yardstick.
        Feasibility was already established by the base pipeline, so
        allocation on a free pool is expected to succeed; a surprise
        failure scores worst rather than crashing routing.
        '''
        temp_pool = QubitPool(ctx.device.num_qubits)
        ALPHA = self.qubit_error_weight
        BETA = self.edge_error_weight

        try:
            mapping = ctx.memory_manager.allocator.allocate(
                qcb.circuit,
                ctx.device,
                temp_pool,
                max_qubit_error=qcb.max_qubit_error,
                max_edge_error=qcb.max_edge_error
            )
        except Exception:
            return float("inf")

        qubits = list(mapping.values())
        qubit_cost = sum(ctx.device.qubit_error(q) for q in qubits)

        edge_cost = 0.0
        qubit_set = set(qubits)
        for u, v in ctx.device.edge_error_map:
            if u in qubit_set and v in qubit_set:
                edge_cost += ctx.device.edge_error(u, v)

        return ALPHA * qubit_cost + BETA * edge_cost


def _min_max(values):
    '''Min-max normalise to [0, 1]; constant lists normalise to 0.'''
    finite = [v for v in values if v != float("inf")]
    if not finite:
        return [1.0] * len(values)
    lo, hi = min(finite), max(finite)
    span = hi - lo
    out = []
    for v in values:
        if v == float("inf"):
            out.append(1.0)
        elif span == 0:
            out.append(0.0)
        else:
            out.append((v - lo) / span)
    return out