"""The terminal-outcome vocabulary and derivation (feat-008).

Before ``queryforge.core.outcomes`` existed, the terminal status of a run was
derived in at least two places with three overlapping vocabularies. That is how
defect E-02 happened: the QA gate recorded a block, ``OrchestratorAgent`` then
re-derived the status from the workflow's earlier result dict and overwrote it to
``completed``, so ``state.json`` and the caller disagreed and both were locally
consistent.

These tests pin the single derivation, the normalisation of every historical
spelling, and the rule that a recorded block outranks a stale result status.
"""

from __future__ import annotations

import unittest

from queryforge.core.outcomes import (
    EVENT_OUTCOMES,
    NON_REPLACEABLE,
    TERMINAL_OUTCOMES,
    derive_outcome,
    is_terminal,
    normalize_outcome,
    to_event_outcome,
)


class OutcomeVocabularyTest(unittest.TestCase):
    def test_every_canonical_outcome_maps_to_an_event_outcome(self):
        for outcome in TERMINAL_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertIn(outcome, EVENT_OUTCOMES)
                self.assertIn(to_event_outcome(outcome), {
                    "success", "partial", "blocked", "failed", "cancelled",
                })

    def test_a_clarification_is_reported_as_blocked_to_a_streaming_client(self):
        """The transport protocol distinguishes fewer outcomes than the engine.

        A clarification is not a success and not a failure; a streaming client sees
        a blocked run carrying the reason.
        """
        self.assertEqual(to_event_outcome("needs_clarification"), "blocked")

    def test_historical_spellings_normalise_onto_the_canonical_set(self):
        expected = {
            "success": "succeeded",
            "completed": "succeeded",
            "planned": "succeeded",
            "ok": "succeeded",
            "degraded": "partial",
            "error": "failed",
            "canceled": "cancelled",
            "cancelled": "cancelled",
            "needs_clarification": "needs_clarification",
            "blocked": "blocked",
            # Case and whitespace must not matter.
            "  COMPLETED  ": "succeeded",
        }
        for raw, want in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_outcome(raw), want)

    def test_non_terminal_and_unknown_values_are_not_terminal(self):
        for raw in ("running", "created", "routing", "", None, "  "):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_outcome(raw))
                self.assertFalse(is_terminal(raw))

    def test_a_cancellation_is_non_replaceable(self):
        """First-writer-wins covers every terminal outcome, not just some."""
        for outcome in TERMINAL_OUTCOMES:
            self.assertIn(outcome, NON_REPLACEABLE)


class OutcomeDerivationTest(unittest.TestCase):
    def test_a_recorded_block_outranks_a_stale_result_status(self):
        """The E-02 rule: a gate that recorded why it blocked is not overwritten.

        The workflow's result dict is captured before the completion hook runs, so
        it can still say "success" while a gate has already blocked the run.
        """
        outcome = derive_outcome(
            result_status="success",
            blocked_reason="qa_report found severe data quality issue",
        )
        self.assertEqual(outcome, "blocked")

    def test_cancellation_outranks_everything(self):
        outcome = derive_outcome(
            result_status="success",
            blocked_reason="some gate",
            cancelled=True,
        )
        self.assertEqual(outcome, "cancelled")

    def test_result_status_is_normalised_not_passed_through(self):
        self.assertEqual(derive_outcome(result_status="planned"), "succeeded")
        self.assertEqual(derive_outcome(result_status="degraded"), "partial")
        self.assertEqual(
            derive_outcome(result_status="needs_clarification"), "needs_clarification"
        )

    def test_absent_or_unknown_status_defaults_to_succeeded(self):
        """Explicit and testable, rather than an implicit ``or "success"``."""
        self.assertEqual(derive_outcome(), "succeeded")
        self.assertEqual(derive_outcome(result_status=None), "succeeded")
        self.assertEqual(derive_outcome(result_status="something-new"), "succeeded")


if __name__ == "__main__":
    unittest.main()
