"""Tests for the deadness scoring policy.

This is the one part of Custodian where a quiet mistake is expensive: these
functions decide which production tables get staged for retirement. The agent
sanity-checks the shortlist, but it only ever sees assets that cleared these
rules — anything wrongly dropped here is never reviewed by anyone.

So the rules that must not drift are pinned directly: the downstream veto, the
threshold boundary, and the retention hold.
"""

from __future__ import annotations

import pytest

from custodian.sweep import (
    DEFAULT_THRESHOLD,
    WEIGHTS,
    Signals,
    explain,
    is_candidate,
    protected_reason,
    score,
)

# An orphaned, unowned, undocumented table: the canonical dead asset.
DEAD = Signals(no_downstream=True, no_owner=True, no_description=True)


class TestScore:
    def test_no_signals_scores_zero(self):
        assert score(Signals()) == 0.0

    def test_every_signal_scores_one(self):
        assert score(Signals(**{key: True for key in WEIGHTS})) == 1.0

    def test_weights_sum_to_one(self):
        # Keeps `score` a true 0.0-1.0 fraction; the README documents it as one.
        assert sum(WEIGHTS.values()) == pytest.approx(1.0)

    def test_score_is_additive(self):
        assert score(DEAD) == pytest.approx(
            WEIGHTS["no_downstream"] + WEIGHTS["no_owner"] + WEIGHTS["no_description"]
        )


class TestDownstreamVeto:
    """A live dependent blocks retirement regardless of everything else."""

    # Every signal except the lineage one, i.e. the worst an asset with live
    # dependents can possibly look.
    WORST_WITH_DEPENDENTS = Signals(
        no_downstream=False,
        no_owner=True,
        no_description=True,
        stale=True,
        no_queries=True,
    )

    def test_asset_with_downstream_is_never_a_candidate(self):
        assert not is_candidate(self.WORST_WITH_DEPENDENTS)

    def test_veto_holds_independently_of_the_threshold(self):
        # At the default weights the threshold alone would also exclude this
        # asset (0.60 < 0.65), so passing `is_candidate` proves little on its
        # own. Drop the threshold to zero to isolate the veto: retirement must
        # still be blocked purely because something depends on it.
        assert not is_candidate(self.WORST_WITH_DEPENDENTS, threshold=0.0)

    def test_non_lineage_signals_cannot_reach_the_threshold_alone(self):
        # Documents why the veto is belt-and-braces today. If a future weight
        # change makes this sum reach the bar, the veto becomes the only thing
        # standing between a busy table and a retirement proposal.
        assert score(self.WORST_WITH_DEPENDENTS) < DEFAULT_THRESHOLD

    def test_orphan_alone_is_not_enough(self):
        # 0.40 < 0.65: no dependents, but no other evidence of abandonment.
        assert not is_candidate(Signals(no_downstream=True))

    def test_orphan_with_corroboration_qualifies(self):
        assert is_candidate(DEAD)


class TestThreshold:
    def test_exactly_at_threshold_qualifies(self):
        at_bar = Signals(no_downstream=True, no_owner=True)
        assert score(at_bar) == pytest.approx(DEFAULT_THRESHOLD)
        assert is_candidate(at_bar)

    def test_just_below_threshold_does_not(self):
        below = Signals(no_downstream=True, no_description=True)
        assert score(below) < DEFAULT_THRESHOLD
        assert not is_candidate(below)

    def test_threshold_is_overridable(self):
        lenient = Signals(no_downstream=True, no_description=True)
        assert is_candidate(lenient, threshold=0.5)


class TestProtectedReason:
    """Retention and legal holds veto retirement the way lineage does."""

    def test_no_properties_is_unprotected(self):
        assert protected_reason(None) is None
        assert protected_reason({}) is None

    def test_ordinary_properties_do_not_protect(self):
        assert protected_reason({"owner_team": "growth", "rows": "12"}) is None

    @pytest.mark.parametrize(
        "key",
        ["retentionPeriod", "retention_period", "legal_hold", "legalHold",
         "compliance_class", "data-retention"],
    )
    def test_retention_and_legal_keys_protect(self, key):
        assert protected_reason({key: "7 years"}) is not None

    def test_reason_names_the_property_and_value(self):
        reason = protected_reason({"retentionPeriod": "7 years"})
        assert "retentionPeriod" in reason
        assert "7 years" in reason

    def test_falsy_value_still_protects(self):
        # Whether `legal_hold: false` really means "safe to delete" is a
        # judgment call with legal consequences. Custodian surfaces it to the
        # human instead of deciding.
        assert protected_reason({"legal_hold": "false"}) is not None


class TestExplain:
    def test_lists_every_firing_signal(self):
        reason = explain(DEAD)
        assert "nothing downstream depends on it" in reason
        assert "no owner assigned" in reason
        assert "no description" in reason

    def test_says_so_when_nothing_fires(self):
        assert explain(Signals()) == "no death signals"

    def test_reason_is_written_into_the_graph_so_must_stay_readable(self):
        # This string lands in `custodian_reason` and in the deprecation note a
        # human reads in the DataHub UI, so it must not leak field names.
        reason = explain(DEAD)
        assert "no_downstream" not in reason
        assert ";" in reason
