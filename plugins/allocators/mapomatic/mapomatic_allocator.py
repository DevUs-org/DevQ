'''
Tags: Plugin

Mapomatic — calibration-aware layout selection (Nation & Treinish,
PRX Quantum 4, 010327 (2023); see docs/REFERENCES.md [Mapomatic]),
ported to DevQ as an allocator baseline.

WHAT MAPOMATIC IS. A post-compilation routine: enumerate the sub-graphs of
a device's topology onto which a compiled circuit can be placed, score each
by a heuristic built from the backend's calibration data, and pick the
lowest-scoring (least-noisy) one. The canonical heuristic is the layout's
estimated total error as one minus the product of the per-operation
fidelities entering it:

    S(block) = 1 - ( Π_q (1 - e_readout(q))
                   · Π_q (1 - e_1q_gate(q))
                   · Π_(u,v)∈block (1 - e_2q_gate(u,v)) )

over the block's qubits and its internal two-qubit edges. Lower is better;
a perfect block scores 0, and the score is bounded in [0, 1). All three
error terms come straight from the device's calibration accessors
(qubit_error / gate_error / edge_error) — the same standard IBM-Target
surface every DevQ device exposes — so scoring adds no quantum overhead,
exactly as the paper intends.

FAITHFULNESS — WHAT IS AND IS NOT PORTED. DevQ splits placement into two
levels: a scheduler supplies the order, an allocator supplies the mapping.
Mapomatic lives entirely at the allocator level, so what ports is its
SCORING HEURISTIC — the product-of-fidelities cost over a candidate block.
What does NOT port is the paper's VF2 subgraph-isomorphism search over an
arbitrary compiled two-qubit interaction graph. DevQ's benchmark circuits
are placed as a CONNECTED BLOCK of the required width (the same candidate
notion every DevQ allocator uses), and at that scope "connected block of k
qubits" and "subgraph isomorphic to a k-qubit line/path" coincide for the
circuits under test, so the connected-block enumeration is the faithful
candidate set here. Substituting DevQ's candidate generator for VF2 is the
allocator-level analogue of the faithfulness caveat the NAQJS scheduler
baseline carries for its unported stochastic initial-mapping stage: the
decision RULE is the paper's; the surrounding DevQ plumbing is DevQ's.

Consequences worth stating plainly:
  - Mapomatic has NO tunable weights. The product-of-fidelities cost is a
    fixed policy — there is no α/β to expose or sweep. It is therefore a
    non-scoring policy in the Sweepable sense (it deliberately does not
    implement the sweep/explain hooks; the base reports it as
    not-sweepable, which is the honest outcome for a parameter-free
    policy, per docs/EXTENDING.md). This is the whole methodological
    contrast with DevQ's NoiseGraphAllocator, whose cost is a *tunable
    weighted SUM* α·Σq + β·Σe: same calibration inputs, a different
    aggregation (multiplicative fidelity vs. additive weighted error), and
    only one of the two carries a knob. Comparing them isolates the effect
    of the aggregation rule.
  - Thresholds are honoured as hard constraints BEFORE scoring, via the
    shared filtering helpers, so Mapomatic composes with the job-level
    threshold system like any other allocator.

Built entirely through the documented plugin API — BaseAllocator +
the shared filtering helpers + the device calibration accessors — with no
edits to DevQ core. Register with
devq.register_allocator("mapomatic", MapomaticAllocator) and benchmark
against NoiseGraph / Graph / Static.
'''

from collections import deque

from kernel.memory.allocators.base_allocator import (
    BaseAllocator, AllocationError,
)
from kernel.memory.allocators.filtering import (
    eligible_qubits, edge_allowed, has_connected_block,
)


class MapomaticAllocator(BaseAllocator):

    # Human-readable name shown by qconfig; the registry falls back to the
    # class name if absent.
    LABEL = "Mapomatic (calibration-aware layout)"

    # ── The scoring heuristic ────────────────────────────────────────────────

    @staticmethod
    def _layout_score(device, block):
        '''
        Mapomatic's estimated layout error for one connected block:

            S = 1 - Π (1 - e) over readout + 1q-gate error on each qubit
                    and 2q-gate error on each INTERNAL edge of the block.

        An internal edge is one with BOTH endpoints in the block (counted
        once); an edge leaving the block is not charged, because the placed
        circuit never uses it. Returns a float in [0, 1). Lower is better.

        Pure: reads only calibration accessors, mutates nothing.
        '''
        fidelity = 1.0
        for q in block:
            fidelity *= (1.0 - device.qubit_error(q))   # readout
            fidelity *= (1.0 - device.gate_error(q))    # 1-qubit gate
        block_set = set(block)
        for u in block:
            for v in device.graph.neighbors(u):
                if v in block_set and u < v:            # internal edge, once
                    fidelity *= (1.0 - device.edge_error(u, v))
        return 1.0 - fidelity

    # ── Candidate enumeration ────────────────────────────────────────────────

    def _candidate_blocks(self, circuit, device, pool,
                          max_qubit_error, max_edge_error, max_1q_gate_error):
        '''
        Every connected block of the required width drawn from currently
        FREE, threshold-eligible qubits, over threshold-allowed edges. The
        block is a sorted tuple of physical qubits — the stable candidate
        key. Candidacy is pool-dependent (only blocks free right now), so
        the layout Mapomatic scores is one it could actually place.

        Mirrors the standard DevQ allocator candidate notion: a 2-qubit
        fast path enumerates threshold-allowed connected pairs directly;
        wider circuits grow a connected block by breadth-first search over
        eligible qubits. Pure: reserves nothing.
        '''
        required = circuit.num_qubits
        usable   = eligible_qubits(device, pool.free_qubits,
                                   max_qubit_error, max_1q_gate_error)
        G        = device.graph

        blocks = []

        if required == 2:
            for (u, v) in device.edges():
                if (u in usable and v in usable
                        and edge_allowed(device, u, v, max_edge_error)):
                    blocks.append((u, v) if u < v else (v, u))
            return blocks

        if required == 1:
            return [(q,) for q in sorted(usable)]

        seen = set()
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
                if block not in seen:
                    seen.add(block)
                    blocks.append(block)
        return blocks

    # ── The allocator contract ───────────────────────────────────────────────

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None,
                 max_1q_gate_error=None):
        '''
        Enumerate placeable connected blocks, score each with Mapomatic's
        product-of-fidelities heuristic, reserve the lowest-scoring one,
        and return its mapping. Raises AllocationError when no block is
        placeable under the current pool state and thresholds — the
        legitimate "cannot place" the scheduler turns into WAITING or
        REJECTED. Any other failure is a bug and propagates, per the
        BaseAllocator contract.

        Selection is argmin over (score, block): the block tuple is the
        deterministic tie-break (lowest qubit indices first), so the choice
        is reproducible under a fixed seed — the decision-determinism the
        benchmark layer requires.
        '''
        blocks = self._candidate_blocks(
            circuit, device, pool,
            max_qubit_error, max_edge_error, max_1q_gate_error,
        )

        if not blocks:
            raise AllocationError(
                "Mapomatic: no connected qubit block available"
                if circuit.num_qubits != 2
                else "Mapomatic: no connected qubit pair available"
            )

        best_block = min(
            blocks,
            key=lambda b: (self._layout_score(device, b), b),
        )

        pool.allocate(list(best_block))
        return {v: p for v, p in enumerate(best_block)}

    def feasible(self, circuit, device,
                 max_qubit_error=None, max_edge_error=None,
                 max_1q_gate_error=None):
        '''
        Could this circuit EVER be placed on this device under these
        thresholds, pool state aside? Mapomatic needs a connected block of
        eligible qubits, so it strengthens the base's qubit-count check
        with a connectivity requirement — the same necessary condition its
        live enumeration imposes, which is what separates a genuinely
        unsatisfiable job (REJECTED) from one merely blocked on free
        qubits (WAITING).
        '''
        reason = super().feasible(circuit, device,
                                  max_qubit_error, max_edge_error,
                                  max_1q_gate_error)
        if reason:
            return reason

        eligible = eligible_qubits(
            device, range(device.num_qubits),
            max_qubit_error, max_1q_gate_error,
        )
        if not has_connected_block(device, eligible,
                                   circuit.num_qubits, max_edge_error):
            return (f"no connected block of {circuit.num_qubits} qubits "
                    f"exists on this device under "
                    f"max_qubit_error={max_qubit_error}, "
                    f"max_1q_gate_error={max_1q_gate_error}, "
                    f"max_edge_error={max_edge_error}")
        return None