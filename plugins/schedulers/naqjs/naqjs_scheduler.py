'''
Tags: Plugin

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

from plugin_bases.base_scheduler import BaseScheduler
from kernel.process.lifecycle import JobStates
from registry.keyspec import KeySpec, non_negative, unit_interval


# Namespaced config keys. Every plugin key is dotted "<prefix>.<key>" — the
# registry rejects un-namespaced plugin keys (they are reserved for DevQ
# core), and the dot keeps qconfig readable and the plugin boundary visible
# in published artifacts. These strings are the CASCADE / live_params / sweep
# identity of each knob; the ctor stores them under plain-identifier
# attributes (a dotted string is not a valid Python name).
WIDTH_WEIGHT_KEY   = "naqjs.width_weight"
SHOTS_WEIGHT_KEY   = "naqjs.shots_weight"
SEQ_WEIGHT_KEY     = "naqjs.seq_weight"
ETA_KEY            = "naqjs.eta"
DEFAULT_SHOTS_KEY  = "naqjs.default_shots"


def _positive_int_or_none(value):
    '''Accept None (defer) or a positive integer shot count. None means
    "no plugin-level default — a job that specifies no shots contributes a
    neutral (tied) value to the shots axis". A supplied value must be a
    positive integer, since it stands in for a real shot count.'''
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return "expected a positive integer or null"
    if value <= 0:
        return "expected a positive integer or null"
    return None


class NAQJSScheduler(BaseScheduler):

    LABEL = "NAQJS (noise-aware priority scheduler)"

    # Plugin-contributed config keys. The registry merges these into the
    # cascade at register_scheduler() time, validates each user-supplied
    # value, and surfaces them in qconfig. scope="device": a scheduler is
    # per-device policy (one instance per DeviceContext), so each device
    # resolves its own NAQJS knobs through the full four-level cascade.
    # dq.build() reads these keys off this class, rewrites the "naqjs."
    # prefix dot to "___", and passes the resolved values as ctor kwargs
    # (naqjs___width_weight=..., etc. — see the __init__ signature).
    CONFIG_SCHEMA = {
        WIDTH_WEIGHT_KEY: KeySpec(
            scope="device", default=1.0, validate=non_negative,
            label="NAQJS circuit-width weight (α)"),
        SHOTS_WEIGHT_KEY: KeySpec(
            scope="device", default=1.0, validate=non_negative,
            label="NAQJS shot-count weight (β)"),
        SEQ_WEIGHT_KEY: KeySpec(
            scope="device", default=1.0, validate=non_negative,
            label="NAQJS submission-order weight (γ)"),
        ETA_KEY: KeySpec(
            scope="device", default=1.0, validate=unit_interval,
            label="NAQJS packing cap η (fraction of device qubits)"),
        DEFAULT_SHOTS_KEY: KeySpec(
            scope="device", default=None, validate=_positive_int_or_none,
            label="NAQJS assumed shots when a job specifies none"),
    }

    def __init__(self, memory_manager, process_table,
                 naqjs___width_weight=1.0, naqjs___shots_weight=1.0,
                 naqjs___seq_weight=1.0, naqjs___eta=1.0,
                 naqjs___default_shots=None):
        # Parameter names mirror the dotted CONFIG_SCHEMA keys with the
        # namespace dot rewritten to "___" (naqjs.eta -> naqjs___eta), the
        # form DevQ's generic schema-to-ctor injection passes. Preserving
        # the prefix keeps a plugin key that reuses a core name distinct
        # from the core parameter; see registry.keyspec.flatten_key.
        super().__init__(memory_manager, process_table)
        self.naqjs_width_weight  = naqjs___width_weight
        self.naqjs_shots_weight  = naqjs___shots_weight
        self.naqjs_seq_weight    = naqjs___seq_weight
        self.naqjs_eta           = naqjs___eta
        self.naqjs_default_shots = naqjs___default_shots

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

    # Sentinel shot value for a job that specifies no shots when no plugin
    # default is set either. It is a CONSTANT, so every such job takes the
    # same value and they tie on the shots axis after min-max — which is the
    # correct behaviour: a job that does not distinguish itself on shots
    # should not be ordered by shots. (Reaching into the device's resolved
    # `shots` config to recover the real number would give the identical
    # ranking in the all-unspecified case, and would couple the scheduler to
    # DeviceContext.config — a layer the scheduler does not and should not
    # hold. See docs/COST_MODEL.md.)
    _NEUTRAL_SHOTS = 0

    def _resolve_shots(self, qcb):
        '''
        The shots feature for ranking. A job's own shot count wins; failing
        that, the plugin-level naqjs.default_shots (if the researcher set one
        — e.g. to make the shots axis live on a workload whose jobs omit
        shots); failing that, a neutral constant so all such jobs tie.

        Deliberately does NOT consult the device-resolved shots config: that
        lives on DeviceContext.config, which the scheduler cannot reach, and
        the kernel already resolves job-vs-device shots at dispatch. Ranking
        is a queue-relative ordering, so a per-plugin assumed value (or a tie)
        is the faithful, layer-clean choice.
        '''
        if qcb.shots is not None:
            return qcb.shots
        if self.naqjs_default_shots is not None:
            return self.naqjs_default_shots
        return self._NEUTRAL_SHOTS

    def _sweep_terms(self, decision):
        '''
        Per-job RAW, weight-free terms for the current queue. `decision` is
        the list of QCBs to rank. Keyed by job_id (already JSON-friendly).
        width = circuit qubits, shots = resolved shot feature (see
        _resolve_shots), seq = deterministic submission order.
        '''
        return [
            (qcb.job_id, {
                "width": qcb.circuit.num_qubits,
                "shots": self._resolve_shots(qcb),
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
            enriched.update({
                "width_norm": w_n, "shots_norm": s_n, "seq_norm": q_n,
                # Log the weights that produced this ranking into the terms,
                # exactly as NoiseRouter does: the sweep's faithfulness anchor
                # recovers the run's weights from the recorded terms by
                # matching live_params() keys, so these MUST be the same
                # dotted keys live_params() reports, or the anchor replays at
                # empty params and fails.
                WIDTH_WEIGHT_KEY: a, SHOTS_WEIGHT_KEY: b, SEQ_WEIGHT_KEY: g,
            })
            out.append((key, final, enriched))
        return out