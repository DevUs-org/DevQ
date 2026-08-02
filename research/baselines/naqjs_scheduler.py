'''
Tags: Research

NAQJS — Noise-Aware Quantum Job Scheduler (Wu et al., ICCAD 2024,
arXiv:2404.07882), ported to DevQ as a scored, sweepable scheduler baseline.

Faithful port of NAQJS's *queue-rearranging* stage: each cycle it sorts the
queue by the priority score

    S_p(job) = α·Ŝ_width + β·Ŝ_shots + γ·Ŝ_seq

over the three per-job features min-max normalised across the current queue
(Ŝ = MinMaxNorm), lowest-wins, then dispatches jobs in that order, packing
until cumulative circuit width would exceed η·N (N = device qubit count).
Placement itself is delegated to the device's allocator via the shared
_attempt_allocation step — DevQ's two-level split means NAQJS supplies the
ORDER and the η·N cap; the allocator supplies the mapping. NAQJS's stochastic
noise-aware initial-mapping stage is deliberately NOT ported into the swept
path — it would break decision-determinism, which the sweep requires.

Notes on faithfulness (see docs/REFERENCES.md [NAQJS]):
  - The paper writes S_p with negated terms and sorts descending; that is
    identical to the un-negated sum sorted ascending, which is what DevQ's
    lowest-wins Sweepable ranking expects — so no sign flip, the minus signs
    are cosmetic.
  - The paper's t_i is wall-clock submission time; DevQ uses submitted_seq
    (deterministic). Min-max normalisation is monotonic, so normalised seq
    and normalised submission time induce the SAME order as long as seq
    preserves submission order — which it does. The substitution is
    order-preserving and is what makes the sweep deterministic.
  - The three weights are independent non-negative knobs (the paper only
    requires α, β, γ ≥ 0, and studies them one-at-a-time), NOT a normalised
    group — so they are plain float keys, not a NormaliseGroup. The weight
    SWEEP still walks the simplex, because a linear ranking is scale-
    invariant; that is a property of the sweep, not of how NAQJS stores its
    weights.

Built entirely through the documented plugin API — BaseScheduler + the
Sweepable hooks + namespaced config keys — with no edits to DevQ core.
Register with devq.register_scheduler("naqjs", NAQJSScheduler); benchmark
against Packing/FCFS/SDF.
'''

from kernel.scheduler.base_scheduler import BaseScheduler
from kernel.process.lifecycle import JobStates


# Namespaced config keys (cascade + validate through the registry exactly
# like core keys). Independent, non-negative weights default to 1.0 each —
# the paper's balanced operating point — and eta defaults to 1.0, making the
# scheduler-level cap a no-op (≈ pool exhaustion) unless a researcher sets it
# below 1 to reproduce the paper's headroom-leaving packing.
WIDTH_WEIGHT_KEY = "naqjs_width_weight"
SHOTS_WEIGHT_KEY = "naqjs_shots_weight"
SEQ_WEIGHT_KEY   = "naqjs_seq_weight"
ETA_KEY          = "naqjs_eta"


class NAQJSScheduler(BaseScheduler):

    LABEL = "NAQJS (noise-aware priority scheduler)"

    def __init__(self, memory_manager, process_table,
                 naqjs_width_weight=1.0, naqjs_shots_weight=1.0,
                 naqjs_seq_weight=1.0, naqjs_eta=1.0):
        super().__init__(memory_manager, process_table)
        self.naqjs_width_weight = naqjs_width_weight
        self.naqjs_shots_weight = naqjs_shots_weight
        self.naqjs_seq_weight   = naqjs_seq_weight
        self.naqjs_eta          = naqjs_eta

    def is_batch(self):
        # NAQJS packs several jobs per cycle up to the η·N cap.
        return True

    # ── The scheduling cycle ──────────────────────────────────────────────────

    def schedule(self):
        if not self.queue:
            return []

        # Rank the whole queue by the priority score, through the SAME
        # Sweepable hooks a weight sweep replays. explain_recorded returns
        # the full ordering [{key, score, terms}, ...] at live weights, not
        # just the winner — so dispatching in this order IS the scored
        # policy, and the logged scores are exactly the ones that ordered
        # the queue.
        tagged = self._sweep_terms(list(self.queue))
        ranked = self.explain_recorded(tagged)      # sorted ascending by score
        order  = {row["key"]: i for i, row in enumerate(ranked)}
        self.queue.sort(key=lambda qcb: order[qcb.job_id])

        # N = device qubit count; the η·N cap bounds cumulative dispatched
        # width this cycle. eta=1.0 (default) makes it a no-op beyond what
        # the pool itself enforces.
        n_qubits = self.memory_manager.device.num_qubits
        cap      = self.naqjs_eta * n_qubits
        used     = 0

        processed = []
        for qcb in list(self.queue):
            width = qcb.circuit.num_qubits

            # η·N cap: stop dispatching once this job would push cumulative
            # width past the cap. Faithful to NAQJS's "until Σn_i > η·N".
            # Remaining jobs stay queued (untouched) for the next cycle.
            if used + width > cap:
                break

            if self._attempt_allocation(qcb):
                # Dispatched. Pin the FULL ranked-queue terms snapshot on the
                # job — the sweep re-normalises across the whole candidate
                # set (the queue), so a single job's terms could not be
                # swept. Mirrors how the allocator pins its per-block
                # decomposition, one layer up. The kernel reads this on
                # dispatch to emit the `schedule` event.
                qcb.sched_decision = tagged
                used += width
                processed.append(qcb)
                self.queue.remove(qcb)
            else:
                # WAITING (transient) stays queued; REJECTED (terminal) is
                # removed. _attempt_allocation already classified and set the
                # state; we only prune terminal jobs from the queue.
                if qcb.state == JobStates.REJECTED:
                    self.queue.remove(qcb)
                    processed.append(qcb)

        return processed

    # ── Sweepable hooks (mirror NoiseRouter) ──────────────────────────────────

    def live_params(self):
        # Reporting these is also what makes is_sweepable() true, which gates
        # the kernel emitting the `schedule` score event.
        return {
            WIDTH_WEIGHT_KEY: self.naqjs_width_weight,
            SHOTS_WEIGHT_KEY: self.naqjs_shots_weight,
            SEQ_WEIGHT_KEY:   self.naqjs_seq_weight,
        }

    def _sweep_terms(self, decision):
        '''
        Per-job RAW, weight-free terms for the current queue. `decision` is
        the list of QCBs to rank. Keyed by job_id (already JSON-friendly).
        width = circuit qubits, shots = per-job shot count, seq = deterministic
        submission order.
        '''
        return [
            (qcb.job_id, {
                "width": qcb.circuit.num_qubits,
                "shots": qcb.shots,
                "seq":   qcb.submitted_seq,
            })
            for qcb in decision
        ]

    def _sweep_score(self, terms, params):
        # Pass the raw triple through un-normalised: the three features are
        # min-max normalised ACROSS the queue, which is set-relative and can
        # only happen in _sweep_rank. Mirrors NoiseRouter returning its raw
        # pair here.
        return (terms["width"], terms["shots"], terms["seq"])

    def _sweep_rank(self, scored, params):
        '''
        The across-queue step: three independent min-max normalisations, then
        the weighted combine, ascending (lowest wins — no sign flip; the
        paper's negation is cosmetic). scored is [(key, terms, raw_triple),
        ...]; returns [(key, final_score, enriched_terms), ...].
        '''
        widths = [raw[0] for _, _, raw in scored]
        shotss = [raw[1] for _, _, raw in scored]
        seqs   = [raw[2] for _, _, raw in scored]

        def minmax(vals):
            lo, hi = min(vals), max(vals)
            span = hi - lo
            return {v: (0.0 if span == 0 else (v - lo) / span) for v in set(vals)}

        nw, ns, nq = minmax(widths), minmax(shotss), minmax(seqs)
        a = params[WIDTH_WEIGHT_KEY]
        b = params[SHOTS_WEIGHT_KEY]
        g = params[SEQ_WEIGHT_KEY]

        out = []
        for key, terms, raw in scored:
            w_n, s_n, q_n = nw[raw[0]], ns[raw[1]], nq[raw[2]]
            final = a * w_n + b * s_n + g * q_n
            enriched = dict(terms)
            enriched.update(width_norm=w_n, shots_norm=s_n, seq_norm=q_n)
            out.append((key, final, enriched))
        return out