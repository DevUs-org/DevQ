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

from plugin_bases.base_router import BaseRouter
from kernel.memory.qubit_pool import QubitPool
from plugin_bases.base_allocator import AllocationError

class NoiseRouter(BaseRouter):

    # Human-readable name shown by qconfig. Any registered component
    # may define one; the registry falls back to the class name.
    LABEL = "Noise Aware Router"

    def select(self, qcb, candidates):
        '''
        Route live by the SAME path a sweep replays: compute each
        candidate's raw terms once, then score-and-select from them at the
        router's live params. select(), explain_decision() and the sweep
        driver therefore all funnel through _sweep_score/_sweep_rank and
        cannot drift.
        '''
        decision = (qcb, candidates)
        tagged   = self._sweep_terms(decision)          # [(index, terms), ...]
        chosen   = self.sweep_decision(tagged, self.live_params())
        return self._by_index[chosen]

    # ── Sweepable hooks ───────────────────────────────────────────────────────

    def live_params(self):
        '''This router's live SWEPT scoring parameters — the sweep anchor.

        Only the qubit/edge cost split is returned: those are the weights a
        sweep varies. The queue/noise mix (router_queue_weight,
        router_noise_weight) is a FIXED scoring input, held constant by a
        sweep, so it is deliberately kept OUT of live_params() — the same
        contract NAQJS follows for its fixed eta/default_shots. Keeping fixed
        inputs out is what lets the sweep derive the swept key set generically
        from live_params() (docs/COST_MODEL.md), rather than each axis
        hardcoding it. The fixed weights are still logged into each decision's
        terms by _sweep_rank, so a replay recovers them from the recorded
        run, not from this instance.'''
        return {
            "qubit_error_weight": self.qubit_error_weight,
            "edge_error_weight" : self.edge_error_weight,
        }

    def _sweep_terms(self, decision):
        '''
        Per-candidate RAW terms for one routing decision, tagged by device
        index. This is the expensive half — one allocator dry-run per
        candidate — and it runs ONCE per live decision; the sweep re-scores
        these recorded terms without repeating it.

        Records the α/β-free inputs to the noise cost (qubit_error_sum,
        edge_error_sum) and the param-free queue pressure, which is exactly
        what lets a sweep recompute the score at any weights from the log.
        '''
        qcb, candidates = decision

        # Stash the index -> ctx map so select() can resolve the winning
        # key back to a context. Rebuilt each decision; never stale.
        self._by_index = {ctx.index: ctx for ctx in candidates}

        tagged = []
        for ctx in candidates:
            pressure = self._queue_pressure(ctx)
            _weighted, q_sum, e_sum = self._best_case_cost(ctx, qcb)
            tagged.append((ctx.index, {
                "queue_pressure" : pressure,
                # α/β-free inputs to S = α·Σq + β·Σe. None when allocation
                # failed (no mapping to decompose) — the score is inf then,
                # weight-invariant. See docs/COST_MODEL.md.
                "qubit_error_sum": q_sum,
                "edge_error_sum" : e_sum,
            }))
        return tagged

    def _sweep_score(self, terms, params):
        '''
        One candidate's raw score components under `params`, as a
        (pressure, cost) pair. NOT a scalar and NOT normalised: the two
        components normalise independently across the candidate set, which
        only _sweep_rank can do, so scoring stops at the raw pair here.

        cost = α·Σq + β·Σe at the params' α/β; inf when the candidate had
        no mapping (qubit_error_sum is None), which is weight-invariant.
        pressure is parameter-independent and passes straight through.
        '''
        pressure = terms["queue_pressure"]
        q_sum    = terms["qubit_error_sum"]
        if q_sum is None:
            return (pressure, float("inf"))
        cost = (params["qubit_error_weight"] * q_sum
                + params["edge_error_weight"] * terms["edge_error_sum"])
        return (pressure, cost)

    def _sweep_rank(self, scored, params):
        '''
        Across-candidate ranking for the decision, given
        [(index, terms, (pressure, cost)), ...] and the parameters. Returns
        [(index, final_score, enriched_terms), ...] where final_score is
        w_queue·p̂ + w_noise·ĉ (both min-max normalised across the candidate
        set) and enriched_terms is the raw terms plus the weighted
        best_case_cost and both normalised forms — the log schema 5.5's
        sweep and the router_scoring block read.

        Selection is the base's argmin over (final_score, index), so the
        lower-index tie-break falls out there; this method only produces
        the comparable scores and the detail.
        '''
        pressures = [s[2][0] for s in scored]
        costs     = [s[2][1] for s in scored]

        p_norm = _min_max(pressures)
        c_norm = _min_max(costs)

        # The queue/noise mix is a FIXED input, not a swept weight, so it is
        # not in `params` (live_params() no longer carries it). On a REPLAY it
        # is recovered from the recorded terms (every candidate logged it
        # below); on the LIVE pass the terms do not carry it yet, so fall back
        # to this instance's own value. Reading terms-first keeps a sweep
        # faithful to the run's actual weights rather than this scoring
        # engine's defaults.
        first_terms = scored[0][1] if scored else {}
        w_queue = first_terms.get("router_queue_weight", self.router_queue_weight)
        w_noise = first_terms.get("router_noise_weight", self.router_noise_weight)

        ranked = []
        for (key, terms, (p_raw, c_raw)), p, c in zip(scored, p_norm, c_norm):
            final = w_queue * p + w_noise * c
            enriched = {
                "queue_pressure"     : p_raw,
                "best_case_cost"     : c_raw,
                "qubit_error_sum"    : terms["qubit_error_sum"],
                "edge_error_sum"     : terms["edge_error_sum"],
                "queue_pressure_norm": p,
                "best_case_cost_norm": c,
                "router_queue_weight": w_queue,
                "router_noise_weight": w_noise,
                "qubit_error_weight" : params["qubit_error_weight"],
                "edge_error_weight"  : params["edge_error_weight"],
            }
            ranked.append((key, final, enriched))
        return ranked

    # ── Raw term computation (the expensive, live-only half) ──────────────────

    def _queue_pressure(self, ctx):
        return ctx.queue_depth() + ctx.running_jobs

    def _best_case_cost(self, ctx, qcb):
        '''
        Dry-run this context's configured allocator on an empty pool
        clone; score the resulting mapping with the S yardstick.
        Feasibility was already established by the base pipeline, so
        allocation on a free pool is expected to succeed; a surprise
        failure scores worst rather than crashing routing.

        Returns the DECOMPOSITION, not just the total:

            (weighted_cost, qubit_error_sum, edge_error_sum)

        weighted_cost = α·qubit_error_sum + β·edge_error_sum is what
        scoring consumes — routing is unchanged by this method returning
        the pieces alongside it. The two raw sums are logged in explain()
        so an α/β weight sweep can recompute S at ANY ratio from one
        recorded run: they are the α- and β-free inputs to S, and the
        total alone cannot be re-split into them. On allocation failure
        the cost is float('inf') (scores worst, exactly as before) and
        the sums are None — there is no mapping to decompose, and an
        inf-cost candidate stays worst at every weight regardless.
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
                max_edge_error=qcb.max_edge_error,
                max_1q_gate_error=qcb.max_1q_gate_error
            )
        except AllocationError:
            # This candidate device cannot host the job — infinite cost, so
            # the router ranks it last. An allocator BUG (any other
            # exception) propagates rather than silently making the device
            # look merely infeasible.
            return float("inf"), None, None

        qubits = list(mapping.values())
        qubit_cost = sum(ctx.device.qubit_error(q) for q in qubits)

        edge_cost = 0.0
        qubit_set = set(qubits)
        for u, v in ctx.device.edges():
            if u in qubit_set and v in qubit_set:
                edge_cost += ctx.device.edge_error(u, v)

        return ALPHA * qubit_cost + BETA * edge_cost, qubit_cost, edge_cost


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