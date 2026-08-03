'''
Tags: Default

NoiseGraphAllocator — BFS connectivity search + weighted noise cost.

Scores every candidate connected block with
    S = α·Σ(qubit_error) + β·Σ(edge_error)
and returns the minimum-cost block. α and β come from the device's
resolved config (qubit_error_weight / edge_error_weight, normalised
to sum to 1; defaults 0.1 / 0.9 — β dominates because two-qubit
gate fidelity is the dominant NISQ noise source). For 2-qubit
circuits, connected pairs are enumerated directly from the edge
error map for efficiency. Thresholds are hard constraints applied
before cost optimisation; feasible() additionally requires a
connected block among eligible qubits.
'''
from collections import deque

from .base_allocator import BaseAllocator, AllocationError
from .filtering import eligible_qubits, edge_allowed, has_connected_block


class NoiseGraphAllocator(BaseAllocator):

    # Human-readable name shown by qconfig. Any registered component
    # may define one; the registry falls back to the class name.
    LABEL = "Noise Aware Graph Allocator"

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None,
                 max_1q_gate_error=None):
        '''
        Choose the minimum-S connected block and reserve it. The block
        CHOICE funnels through the same Sweepable hooks a weight sweep
        replays — _sweep_terms enumerates and decomposes the candidates,
        _sweep_rank scores and ranks them — so live allocation, explain()
        and a sweep cannot drift. The pool RESERVATION is the one
        side-effect and stays strictly outside the pure scoring contract.
        '''
        decision = (circuit, device, pool, max_qubit_error, max_edge_error,
                    max_1q_gate_error)
        tagged   = self._sweep_terms(decision)      # [(block, terms), ...]

        if not tagged:
            # No candidates: record nothing to explain, and let the caller
            # classify the failure (WAITING/REJECTED) as before.
            self._last_decision = None
            raise AllocationError(
                "No connected qubit block available"
                if circuit.num_qubits != 2
                else "No connected qubit pair available"
            )

        best_block = self.sweep_decision(tagged, self.live_params())

        # Stash the decision that placed this job so the kernel can emit an
        # `allocate` event (per-block scores) on dispatch, mirroring how the
        # router's `route` event is emitted. Recorded, not emitted, here —
        # the allocator never touches the sink; the kernel reads this back
        # via explain_decision. Overwritten each allocation; a retried
        # WAITING job that later dispatches carries the decision that
        # actually placed it, which is the one worth logging.
        self._last_decision = tagged

        pool.allocate(list(best_block))
        return {v: p for v, p in enumerate(best_block)}

    # ── Sweepable hooks ───────────────────────────────────────────────────────

    def live_params(self):
        '''This allocator's live cost weights — the sweep anchor.'''
        return {
            "qubit_error_weight": self.qubit_error_weight,
            "edge_error_weight" : self.edge_error_weight,
        }

    def _sweep_terms(self, decision):
        '''
        Enumerate every candidate connected block and decompose its cost
        into the α/β-free sums the sweep re-weights: qubit_error_sum
        (Σ readout error over the block) and edge_error_sum (Σ two-qubit
        error over the block's INTERNAL edges — edges with one endpoint
        outside are not charged, since the circuit never uses them).

        The block itself (a sorted tuple of physical qubits) is the stable
        candidate key, the allocator's analog of the router's device
        index. Candidacy is pool-dependent — only blocks free right now
        are enumerated — so a recorded allocation decision is re-weightable
        among exactly the blocks that were real candidates at that
        placement, which is the honest scope of an allocator sweep.

        Pure: reads pool/device state, reserves nothing.
        '''
        (circuit, device, pool, max_qubit_error, max_edge_error,
         max_1q_gate_error) = decision
        required = circuit.num_qubits
        usable   = eligible_qubits(device, pool.free_qubits, max_qubit_error,
                                   max_1q_gate_error)
        G        = device.graph

        tagged = []

        if required == 2:
            # Connected pairs enumerated directly from the coupling edges —
            # the same fast path allocate() used before the refactor, now via
            # the device's edges() accessor rather than the raw map.
            for (u, v) in device.edges():
                if (u in usable and v in usable
                        and edge_allowed(device, u, v, max_edge_error)):
                    block = (u, v) if u < v else (v, u)
                    tagged.append((block, {
                        "qubit_error_sum": device.qubit_error(u)
                                           + device.qubit_error(v),
                        "edge_error_sum" : device.edge_error(u, v),
                    }))
            return tagged

        seen_blocks = set()
        for start in sorted(usable):
            visited = []
            queue = deque([start])

            while queue and len(visited) < required:
                q = queue.popleft()
                if q not in visited and q in usable:
                    visited.append(q)
                    for neighbor in G.neighbors(q):
                        if (neighbor in usable
                                and neighbor not in visited
                                and edge_allowed(device, q, neighbor,
                                                 max_edge_error)):
                            queue.append(neighbor)

            if len(visited) >= required:
                block = tuple(sorted(visited[:required]))
                if block in seen_blocks:
                    continue
                seen_blocks.add(block)

                qubit_sum = sum(device.qubit_error(q) for q in block)
                edge_sum  = 0.0
                for u in block:
                    for v in G.neighbors(u):
                        if v in block and u < v:
                            edge_sum += device.edge_error(u, v)

                tagged.append((block, {
                    "qubit_error_sum": qubit_sum,
                    "edge_error_sum" : edge_sum,
                }))
        return tagged

    def _sweep_score(self, terms, params):
        '''
        One block's cost S = α·Σq + β·Σe from its decomposition and the
        cost weights. No inf case here: every enumerated block has a real
        decomposition (enumeration already excluded ineligible qubits and
        disallowed edges), unlike the router, whose candidate may have no
        mapping at all.
        '''
        return (params["qubit_error_weight"] * terms["qubit_error_sum"]
                + params["edge_error_weight"] * terms["edge_error_sum"])

    def _sweep_rank(self, scored, params):
        '''
        Rank the blocks by S. Unlike the router there is NO across-candidate
        normalisation — S is directly comparable across blocks on one
        device — so the final score is the raw S and the enriched terms
        carry the decomposition plus the weights used. Selection is the
        base's argmin over (S, block); the block-key tuple gives a
        deterministic tie-break (lower qubits first), matching the old
        allocate()'s sorted-candidate preference.
        '''
        ranked = []
        for key, terms, s in scored:
            ranked.append((key, s, {
                "qubit_error_sum"   : terms["qubit_error_sum"],
                "edge_error_sum"    : terms["edge_error_sum"],
                "block_cost"        : s,
                "qubit_error_weight": params["qubit_error_weight"],
                "edge_error_weight" : params["edge_error_weight"],
            }))
        return ranked

    def feasible(self, circuit, device,
                 max_qubit_error=None, max_edge_error=None,
                 max_1q_gate_error=None):
        reason = super().feasible(circuit, device,
                                  max_qubit_error, max_edge_error,
                                  max_1q_gate_error)
        if reason:
            return reason

        eligible = eligible_qubits(
            device, range(device.num_qubits), max_qubit_error,
            max_1q_gate_error
        )

        if not has_connected_block(device, eligible,
                                   circuit.num_qubits, max_edge_error):
            return (f"no connected block of {circuit.num_qubits} qubits "
                    f"exists on this device under "
                    f"max_qubit_error={max_qubit_error}, "
                    f"max_1q_gate_error={max_1q_gate_error}, "
                    f"max_edge_error={max_edge_error}")

        return None