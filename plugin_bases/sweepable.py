'''
Tags: Main

Sweepable — the shared scoring/explain/sweep contract for any DevQ
component whose decision is a parameterised choice among candidates.

WHY THIS EXISTS. explain() and a weight sweep are the SAME operation seen
from two angles. explain() reports the raw terms behind the decision a
component just made at its live parameters; a sweep replays that same
decision from those same raw terms under DIFFERENT parameters. They share
the terms surface and the selection logic entirely — which is exactly why
keeping them as two hand-written methods invites drift (the reported best
must equal the selected one; the swept best at live weights must equal the
recorded one). Unifying them makes that impossible to get wrong: a
component supplies its scoring ONCE, and both explain() and the sweep are
derived from it here.

WHAT A COMPONENT SUPPLIES. Three small pieces, none of which know about
logging, sweeping, or the event schema:

  _sweep_terms(decision)  -> list[(key, terms_dict)]
      The per-candidate RAW inputs to the score for one live decision —
      the α/β-free (or param-free) quantities the score is built from,
      one dict per candidate, tagged by a stable candidate key (a device
      index, a block id, a job id). Computed once, live. This is the
      quantity that gets logged, so a sweep can re-score from it without
      re-executing anything.

  _sweep_score(terms, params) -> float
      One candidate's PRE-selection score from its recorded terms and a
      parameter assignment. This is where the component's own formula
      lives — α·Σq + β·Σe for a noise router/allocator, whatever an
      external component computes. `params` is opaque: the component
      reads the keys it cares about and ignores the rest, so a component
      that introduces its own scoring parameters sweeps over them with no
      change here. Returns float('inf') for a candidate with no valid
      decision (e.g. no mapping) — inf is weight-invariant, there is
      nothing to re-weight.

  _sweep_rank(scored, params) -> [(key, final_score, enriched_terms), ...]
      The winner-agnostic ACROSS-CANDIDATE step: given every candidate's
      (key, terms, raw_score) for one decision and the parameters, return
      each candidate's FINAL comparable score (lowest wins) plus enriched
      terms (the raw inputs plus any set-relative quantities worth logging,
      e.g. normalised forms). This is where set-relative logic lives —
      min-max normalisation across the candidate set, weight combination,
      whatever the component does — and the base is blind to it. Receives
      the full set at once precisely because set-relative normalisation
      cannot be done per candidate, and may read `params` (combination
      weights often live there). Selection is the base's argmin over
      (final_score, key); explain() is the base reporting the same ranked
      detail — so both derive from this one method and cannot drift.

WHAT THE BASE DERIVES. explain() (live terms + live scores, for the log)
and the sweep driver both call the three above. A component that
implements them is sweepable and explainable for free and consistently; a
component that does not is neither (the default), which is the honest
outcome for a policy with no scores to report or no parameter to sweep
(round-robin routing, FCFS scheduling, a cost-oblivious allocator).

DETERMINISM IS THE PRECONDITION. The sweep is valid only if a decision is
a PURE function of (recorded terms, params). A component whose choice
depends on anything not captured in its terms — a sampled action, hidden
state, an ML policy that is stochastic at decision time — is not
faithfully sweepable, and MUST NOT implement these callbacks (leave the
default, and it is skipped honestly). The sweep DRIVER guards this with a
faithfulness anchor: re-scoring the recorded terms at the recorded params
must reproduce the recorded decision, or the session is refused with a
reason rather than emitting fiction. This is the same decision-determinism
contract the rest of the benchmark layer already requires (seed the
providers, or nothing is comparable).
'''

# Sentinel a component returns from _sweep_terms when it has no scores to
# expose for a decision (a non-scoring policy). Distinct from "an empty
# decision" (a decision with zero candidates), which is a real, scorable
# thing that simply has no candidates.
NOT_SCORED = None


class Sweepable:
    '''
    Mixin providing derived explain() and sweep support from a
    component's three scoring hooks. Inheriting it does NOT make a
    component sweepable — the default hooks report "not scored", so a
    component is sweepable exactly when it overrides them. This keeps the
    common case (a non-scoring policy) zero-effort and correct.
    '''

    # ── Hooks a scoring component overrides ───────────────────────────────────

    def _sweep_terms(self, decision):
        '''
        Per-candidate raw terms for one live decision, as a list of
        (key, terms_dict), or NOT_SCORED if this component does not score.

        `decision` is whatever the component's live decision path already
        has in hand (for a router, the candidate contexts + the job); the
        component shapes it. Default: not scored.
        '''
        return NOT_SCORED

    def _sweep_score(self, terms, params):
        '''
        One candidate's pre-selection score from its recorded terms and a
        parameter assignment. Override in a scoring component. Must be a
        pure function of (terms, params) — the sweep's validity rests on
        it. inf for a candidate with no valid decision.
        '''
        raise NotImplementedError(
            "a scoring component that overrides _sweep_terms must also "
            "override _sweep_score"
        )

    def _sweep_rank(self, scored, params):
        '''
        The across-candidate step: given [(key, terms, raw_score), ...] for
        one whole decision and the parameter assignment, return the FINAL
        per-candidate report

            [(key, final_score, enriched_terms), ...]

        where final_score is the comparable scalar the component ranks on
        (lowest wins) and enriched_terms is the recorded terms plus any
        across-candidate quantities worth logging (e.g. normalised forms).

        This is the single across-candidate path. Both selection (argmin
        over final_score, key) and explain() are derived from it here, so
        the reported best is always the selected one and neither can drift
        from the other or from a sweep. Owns all set-relative logic —
        min-max normalisation, weight combination, tie-break shape — none
        of which the base sees. May read `params` (combination weights
        often live there). Override in a scoring component.
        '''
        raise NotImplementedError(
            "a scoring component that overrides _sweep_terms must also "
            "override _sweep_rank"
        )

    # ── Derived: what the component uses live ─────────────────────────────────

    def live_params(self):
        '''
        The parameter assignment this component is currently configured
        with — the point in sweep space its live decisions correspond to.
        A scoring component overrides this to report its own parameters
        (the sweep uses it as the faithfulness anchor). Default None,
        matching a component that does not score.
        '''
        return None

    def explain_decision(self, decision):
        '''
        Per-candidate scoring detail for a live decision, for the event
        log: [{"key", "score", "terms"}, ...], or None if the component
        does not score. Derived from the same _sweep_terms/_sweep_score/
        _sweep_rank a weight sweep uses, so the reported best equals the
        selected one by construction.

        Reports each candidate's FINAL score at the component's live
        params — the decision actually being made — through the identical
        ranking a sweep re-runs under other params, with the enriched
        terms (raw inputs plus any across-candidate quantities the
        component chose to expose).
        '''
        tagged = self._sweep_terms(decision)
        if tagged is NOT_SCORED:
            return None
        return self.explain_recorded(tagged)

    def explain_recorded(self, recorded_terms):
        '''
        The same report as explain_decision, but from ALREADY-RECORDED
        terms rather than a live decision — for a component that computed
        and stashed its candidates during its live decision and must not
        re-enumerate (e.g. an allocator whose pool state has since changed
        because it reserved the winning block). `recorded_terms` is
        [(key, terms_dict), ...]; scoring and ranking run at live params,
        identical to explain_decision, so the two agree by construction.
        '''
        params = self.live_params()
        scored = [(key, terms, self._sweep_score(terms, params))
                  for key, terms in recorded_terms]
        ranked = self._sweep_rank(scored, params)
        return [
            {"key": key, "score": final, "terms": terms}
            for key, final, terms in ranked
        ]

    # ── Derived: what a sweep calls ───────────────────────────────────────────

    def sweep_decision(self, recorded_terms, params):
        '''
        Re-decide one recorded decision under `params`, from the raw terms
        the log captured. `recorded_terms` is [(key, terms_dict), ...] as
        recorded by explain_decision (the "terms" field per candidate,
        with its key). Returns the winning key.

        This is a pure replay: it re-scores each candidate's recorded
        terms under params via _sweep_score, ranks via _sweep_rank, and
        takes the argmin (final_score, key). Runs NOTHING from the live
        decision path — no allocation, no device access — which is what
        makes a sweep answerable from one recorded run. The (score, key)
        tie-break gives lower keys precedence, matching every scoring
        component's deterministic lower-index rule.
        '''
        scored = [
            (key, terms, self._sweep_score(terms, params))
            for key, terms in recorded_terms
        ]
        ranked = self._sweep_rank(scored, params)
        return min(ranked, key=lambda r: (r[1], r[0]))[0]

    def is_sweepable(self):
        '''
        Whether this component exposes scores at all. True once it
        overrides the hooks (detected by live_params being defined, which
        a scoring component sets). A non-scoring component is not
        sweepable and its sessions are skipped with that reason.
        '''
        return self.live_params() is not None