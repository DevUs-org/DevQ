"""QOS router baseline — a spatial which-QPU device selector.

Tags: Research

Ports the device-selection policy of QOS (Giortamis, Romao, Tornow &
Bhatotia, "QOS: Quantum Operating System", OSDI '25, pp. 429-447;
arXiv:2406.19120 — see docs/REFERENCES.md [QOS]) as a DevQ router baseline.

QOS decides *which QPU* a job runs on by combining a per-QPU fidelity
prediction (its estimator, paper Sec. 6) with a fidelity-vs-waiting-vs-
utilisation trade-off (its scheduler's formula-based policy, paper Sec. 8).
That spatial which-QPU decision is precisely a DevQ *router*'s job, so QOS
ports to the router axis (the reference records this framing; NoiseRouter is
the built-in scoring-router precedent).

This is a pure research/ plugin: it subclasses BaseRouter, implements
select() and the Sweepable hooks, declares its own namespaced weights, and
touches no DevQ core. It is written against the documented contract
(docs/EXTENDING.md router section + "Runtime objects a component reads",
docs/REGISTRY.md, docs/COST_MODEL.md), not by mirroring a built-in router.

Two faithfulness caveats, each the QOS-router analogue of the caveats the
NAQJS and Mapomatic baselines carry (recorded here at the use-site and in
docs/REFERENCES.md [QOS]):

  1. NO CROSSTALK TERM.  Paper Sec. 6's fidelity product includes a
     crosstalk factor prod e_ct(k,l).  DevQ's calibration model carries no
     crosstalk term (readout, 1q-gate, 2q-gate, T2, gate-duration — see
     docs/EXTENDING.md "Device calibration model"), and a Qiskit fake
     backend publishes no crosstalk data, so a synthesised value would be
     fabricated calibration, not a measurement.  The crosstalk product is
     therefore omitted, not invented.  Closing this is the documented
     "adding a calibration term" path, a future core sub-phase.

  2. DEVICE-REPRESENTATIVE, MAPPING-FREE FIDELITY.  Paper Sec. 6 evaluates
     fidelity over a concrete transpiled layout (logical->physical mapping +
     gate schedule).  In DevQ, placement is the *allocator*'s job, layered
     BELOW the router; at select() time a job has no v2p_map yet, and the
     free-pool allocator dry-run a built-in router uses to preview a mapping
     is not exposed to plugins.  So this baseline scores each device's
     *capability* for the circuit's shape — the fidelity of the device's
     best available n-qubit region — rather than a specific mapping.  This
     is faithful to QOS's ROLE (predict this QPU's fidelity for this job)
     within DevQ's layering, and is the router-level analogue of NAQJS's
     unported stochastic stage and Mapomatic's VF2->BFS substitution.  Idle
     time for the decoherence factor is estimated from circuit depth x gate
     duration rather than a per-qubit gate schedule (same reason).

  3. INVERTED UTILISATION SIGN.  Paper Sec. 8 REWARDS higher utilisation
     (the +beta*(u2-u1)/u1 term), because QOS drives utilisation up on
     purpose: its multi-programmer packs compatible jobs onto the same QPU,
     and the utilisation reward serves that packing goal, balanced against
     crosstalk-induced fidelity loss.  DevQ's router does NOT multi-program —
     a job takes a block on one device, with no co-location or compatibility
     scoring — so on DevQ a high-occupancy device is simply a fuller, busier
     device, not a packing opportunity.  Porting Sec. 8's sign verbatim would
     make the router prefer the more-loaded QPU with none of the machinery
     that justifies it, degrading routing against its own queue term.  This
     baseline therefore INVERTS the sign (-beta): higher occupancy is
     penalised, spreading load — the sensible port for a non-multi-programming
     router.  This is a deliberate deviation from Sec. 8, recorded because the
     entanglement of QOS's utilisation term with multi-programming is exactly
     the kind of cross-layer coupling a faithful port must surface rather than
     hide.  (A separate, milder adaptation: the min-of-field reference for the
     relative deltas, since DevQ scores candidates independently rather than
     pairwise — see the class docstring.)
"""

from __future__ import annotations

import math

from kernel.router.base_router import BaseRouter
from registry.keyspec import KeySpec, unit_interval, non_negative


class QOSRouter(BaseRouter):
    """Device selector using QOS's estimator (Sec. 6) + formula policy (Sec. 8).

    Per candidate device d, three raw terms are computed from the documented
    read surface only:

      f_d : device-representative fidelity, QOS Sec. 6 numerical-cost policy
            (readout x decoherence x 1q-gate x 2q-gate error products,
            crosstalk omitted), higher is better.
      t_d : queue pressure = queue_depth + running_jobs (the same waiting-time
            proxy docs/COST_MODEL.md defines for the router), lower is better.
      u_d : live spatial occupancy = (num_qubits - free) / num_qubits, QOS
            Sec. 7's spatial-utilisation notion, computed at decision time
            from the pool.  Lower is preferred here (see caveat 3): QOS Sec. 8
            REWARDS higher utilisation because it is simultaneously packing
            QPUs via multi-programming; DevQ's router does not multi-program,
            so the faithful-to-role port spreads load instead.

    The QOS Sec. 8 formula compares two assignments by *relative* deltas:

        Score = c*(f2-f1)/f1 - (1-c)*(t2-t1)/t1 + beta*(u2-u1)/u1

    with the utilisation sign INVERTED for DevQ (caveat 3).  DevQ scores each
    candidate independently and ranks across the set, so the pairwise
    reference is fixed to the field minimum of each term (min-of-field): every
    candidate's delta is measured against the best value any candidate offers
    on that axis, keeping the reference order-independent (unlike
    first-candidate) and the deltas single-signed.  Higher QOS score is
    better; the score is negated in _sweep_rank so the base's argmin seam
    selects QOS's argmax.  This min-of-field adaptation is a recorded
    faithfulness caveat (see module docstring / [QOS]).

    Weights (paper Sec. 8): c = fidelity priority, beta = utilisation
    priority.  Declared as namespaced, global-scope keys (routers may declare
    global/common scope only — docs/REGISTRY.md).  qos.fidelity_weight and
    qos.util_weight are what a sweep varies.
    """

    LABEL = "QOS (which-QPU baseline)"

    # Namespaced, global-scope knobs (router scope rule: global/common only).
    # Named to be injected at construction (docs/REGISTRY.md constructor table:
    # a schema key whose parameter name matches an __init__ param is passed in,
    # dotted key -> ___).  c and beta are independent priorities in [0,1]; they
    # are NOT a normalise group (they weight different, already-relative terms,
    # and 1-c supplies the waiting-time weight, so they need not sum to 1).
    CONFIG_SCHEMA = {
        "qos.fidelity_weight": KeySpec(
            "global", 0.5, unit_interval,
            "QOS fidelity priority c (paper Sec. 8); 1-c weights waiting time",
        ),
        "qos.util_weight": KeySpec(
            "global", 0.5, non_negative,
            "QOS utilisation priority beta (paper Sec. 8)",
        ),
    }

    def __init__(
        self,
        router_queue_weight=None,
        router_noise_weight=None,
        qubit_error_weight=None,
        edge_error_weight=None,
        qos___fidelity_weight=0.5,
        qos___util_weight=0.5,
    ):
        # Accept the four core router weights DevQ always passes (docs/
        # REGISTRY.md constructor table) so the Level-2 bind check passes;
        # QOS does not use the queue/noise blend weights (those are
        # NoiseRouter's min-max knobs) or the alpha/beta cost weights (QOS's
        # fidelity term is a full Sec. 6 product, not the additive S cost).
        super().__init__(
            router_queue_weight=router_queue_weight,
            router_noise_weight=router_noise_weight,
            qubit_error_weight=qubit_error_weight,
            edge_error_weight=edge_error_weight,
        )
        self.qos_fidelity_weight = qos___fidelity_weight
        self.qos_util_weight = qos___util_weight

    # ---- QOS Sec. 6 estimator: device-representative fidelity ------------

    @staticmethod
    def _decoherence_error(t2_us, idle_ns):
        """QOS Sec. 6 decoherence factor e_d = 1 - exp(-t/T2).

        t2 in microseconds, idle in nanoseconds; convert to a common unit.
        A non-positive or absent T2 yields no decoherence (factor 0)."""
        if t2_us is None or t2_us <= 0 or idle_ns <= 0:
            return 0.0
        t2_ns = t2_us * 1000.0
        return 1.0 - math.exp(-idle_ns / t2_ns)

    def _device_fidelity(self, device, circuit):
        """QOS Sec. 6 numerical-cost fidelity for `circuit` on `device`.

        Mapping-free (caveat 2): the representative region is the device's
        best n qubits by combined single-qubit survival and its best (n-1)
        edges by 2q survival, where n is the circuit width.  All error rates
        come from the documented calibration accessors.  Crosstalk omitted
        (caveat 1).

            fid = 1 - PROD_qubits (1-e_readout)(1-e_1q)(1-e_decoh)
                    * PROD_edges  (1-e_2q) ^ (m2q scaling)

        Returns fidelity in [0,1]; higher is better.
        """
        n = circuit.num_qubits
        qubits = list(range(device.num_qubits))

        # Per-qubit single-qubit survival prob = (1-readout)(1-1q_gate).
        # Pick the n qubits with the highest survival (device's best region).
        def q_survival(q):
            return (1.0 - device.qubit_error(q)) * (1.0 - device.gate_error(q))

        best_qubits = sorted(qubits, key=q_survival, reverse=True)[:n]

        # Idle time proxy: circuit depth * a representative gate duration.
        # Depth counts unitary layers (get_depth filters op=="gate"); each
        # layer costs at most a 2q-gate duration.  This is the depth-based
        # idle estimate the mapping-free caveat records.
        depth = circuit.get_depth()
        gate_ns = device.gate_duration(2)
        idle_ns = depth * gate_ns

        readout_decoh = 1.0
        for q in best_qubits:
            e_1q_combined = 1.0 - q_survival(q)          # 1-(1-ro)(1-g1q)
            e_decoh = self._decoherence_error(device.t2(q), idle_ns)
            readout_decoh *= (1.0 - e_1q_combined) * (1.0 - e_decoh)

        # Two-qubit term: the circuit's 2q gates run on the device's best
        # edges.  Use the best (n-1) edges (a spanning connectivity for n
        # qubits) as the representative edge set, and raise the product to
        # account for the actual 2q-gate count (more 2q gates => more error
        # exposure), normalised by the representative edge count.
        edges = list(device.edges())
        two_q = 1.0
        if edges and n >= 2:
            best_edges = sorted(
                edges, key=lambda uv: device.edge_error(*uv)
            )[: max(1, n - 1)]
            edge_survival = 1.0
            for (u, v) in best_edges:
                edge_survival *= (1.0 - device.edge_error(u, v))
            m2q = self._count_two_qubit_gates(circuit)
            # scale exponent by 2q-gate count per representative edge
            exponent = m2q / len(best_edges) if best_edges else 0.0
            two_q = edge_survival ** exponent if exponent > 0 else 1.0

        return 1.0 - readout_decoh * two_q

    @staticmethod
    def _count_two_qubit_gates(circuit):
        """Number of 2-qubit gate ops in the circuit (op=='gate', 2 qubits)."""
        count = 0
        for instr in circuit.instructions:
            if instr.get("op") != "gate":
                continue
            qubits = instr.get("qubits") or instr.get("qubit") or []
            if isinstance(qubits, (list, tuple)) and len(qubits) == 2:
                count += 1
        return count

    # ---- decision-time raw terms per candidate ---------------------------

    def _raw_terms(self, qcb, context):
        """The three QOS raw terms for one candidate, from the read surface.

        Returns a dict of weight-free inputs; the relative deltas and the
        weighting happen across the field in _sweep_rank.
        """
        device = context.device
        f_d = self._device_fidelity(device, qcb.circuit)
        # queue_depth() is a method; running_jobs is an attribute (verified
        # against the live DeviceContext).  Queue pressure = waiting + running
        # (docs/COST_MODEL.md).
        t_d = context.queue_depth() + context.running_jobs
        free = len(context.memory_manager.pool.available())
        total = device.num_qubits
        u_d = (total - free) / total if total else 0.0
        return {"fidelity": f_d, "queue_pressure": t_d, "occupancy": u_d}

    # ---- required BaseRouter method --------------------------------------

    def select(self, qcb, candidates):
        """Choose a device for `qcb` from `candidates` (docs/EXTENDING.md).

        Routes the live decision through the same Sweepable hooks the log and
        a weight sweep use, so the chosen device is exactly the one the logged
        scores explain. Returns one of the offered candidates.
        """
        params = self.live_params()
        decision = (qcb, candidates)
        tagged = self._sweep_terms(decision)              # [(key, terms), ...]
        scored = [(key, terms, self._sweep_score(terms, params))
                  for (key, terms) in tagged]
        ranked = self._sweep_rank(scored, params)         # [(key, final, ...)]
        winner_key = min(ranked, key=lambda r: (r[1], r[0]))[0]
        by_key = {self._candidate_key(ctx): ctx for ctx in candidates}
        return by_key[winner_key]

    @staticmethod
    def _candidate_key(context):
        """Stable per-candidate key for tie-break and lookup: device index."""
        return context.device.index

    # ---- Sweepable hooks (docs/EXTENDING.md) -----------------------------

    def live_params(self):
        """The weights QOS scores with now (paper Sec. 8: c and beta)."""
        return {
            "qos.fidelity_weight": self.qos_fidelity_weight,
            "qos.util_weight": self.qos_util_weight,
        }

    def _sweep_terms(self, decision):
        """Per-candidate RAW, weight-free terms for one live decision.

        `decision` is the router's live form, the tuple (qcb, candidates) the
        base's explain() hands in.  Returns [(device_index, terms_dict), ...]
        with the three QOS raw inputs read from the documented DeviceContext /
        QubitPool surface — the same list a weight sweep replays.
        """
        qcb, candidates = decision
        out = []
        for ctx in candidates:
            out.append((self._candidate_key(ctx), self._raw_terms(qcb, ctx)))
        return out

    def _sweep_score(self, terms, params):
        """One candidate's pre-selection scalar from its recorded terms.

        QOS's final score is relative to the field (min-of-field), so the
        set-relative combination lives in _sweep_rank (as NoiseRouter's
        min-max does).  Here we return a stable per-candidate scalar that
        carries the raw terms forward; _sweep_rank owns the relative-delta and
        weighting.  A convenient pure scalar is the negative raw fidelity, so
        that in the absence of any field context a higher-fidelity candidate
        already sorts better — but the authoritative combination is in rank.
        Must be pure in (terms, params).
        """
        # pure function of terms; used only to carry a comparable scalar.
        return -float(terms["fidelity"])

    def _sweep_rank(self, scored, params):
        """Across-candidate: min-of-field relative deltas, weight, rank.

        `scored` is [(key, terms, raw_score), ...] for the whole decision.
        Compute the field minimum of each QOS term, form the relative deltas
        against it (paper Sec. 8), combine with c/beta, and return
        [(key, final_score, enriched_terms), ...] where final_score is the
        comparable scalar the base ranks on (LOWEST wins).  QOS is
        higher-is-better, so final_score = -qos_score.  The utilisation sign
        is INVERTED vs Sec. 8 (caveat 3).
        """
        live = self.live_params()
        c = params.get("qos.fidelity_weight", live["qos.fidelity_weight"])
        beta = params.get("qos.util_weight", live["qos.util_weight"])

        fids = [t["fidelity"] for (_k, t, _s) in scored]
        queues = [t["queue_pressure"] for (_k, t, _s) in scored]
        occs = [t["occupancy"] for (_k, t, _s) in scored]
        f_min = min(fids)
        t_min = min(queues)
        u_min = min(occs)

        def rel(x, ref):
            # relative delta vs field min; ref==0 => absolute (avoid /0),
            # a zero reference means "best is already ideal on this axis".
            return (x - ref) / ref if ref else (x - ref)

        out = []
        for (key, terms, _raw_score) in scored:
            f_d = terms["fidelity"]
            t_d = terms["queue_pressure"]
            u_d = terms["occupancy"]
            qos_score = (
                c * rel(f_d, f_min)
                - (1.0 - c) * rel(t_d, t_min)
                - beta * rel(u_d, u_min)   # sign INVERTED vs Sec. 8 (caveat 3)
            )
            enriched = dict(terms)
            enriched.update(
                qos_score=qos_score,
                rel_fidelity=rel(f_d, f_min),
                rel_queue=rel(t_d, t_min),
                rel_occupancy=rel(u_d, u_min),
                # Log the live weight values into the terms so a sweep can
                # recover the run's own params from a recorded decision
                # (the sweep's anchor step reads live_params() key names OUT
                # of the recorded terms — a weight not logged here is a
                # KeyError at replay).
                **{"qos.fidelity_weight": c, "qos.util_weight": beta},
            )
            # final_score = -qos_score so the base's argmin picks QOS's argmax
            out.append((key, -qos_score, enriched))

        return out