"""Offline tests for the gateway webhook: a run's outcome must survive the trip.

C1: the adapter answered every run with "Query completed. Returned N row(s).",
including a governance-blocked, failed, cancelled or clarification-pending run,
so a chat user was told the opposite of what had happened.
"""

from __future__ import annotations

import importlib.util
import unittest

from queryforge.interfaces.gateway import GatewayAdapter

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


class RecordingService:
    """Minimal ``AgentService`` double: it replays one canned run output."""

    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls: list[tuple[str, object]] = []

    def ask(self, question: str, options=None) -> dict:
        self.calls.append((question, options))
        return dict(self.output)


def _blocked_output() -> dict:
    return {
        "status": "blocked",
        "run_id": "qf_blocked",
        "question": "show me user emails",
        "columns": ["user_id", "email"],
        "rows": [],
        "row_count": 0,
        "reason": (
            "column 'dim_user.email' is outside the allowed column scope"
        ),
        "agent_team": {
            "blocked_phase": "governance",
            "blocked_reason": "governance policy blocked the query",
        },
    }


class GatewayWebhookOutcomeTest(unittest.TestCase):
    def handle(self, output: dict) -> dict:
        return GatewayAdapter(RecordingService(output)).handle(
            user_id="U1", channel="C1", text="show me user emails"
        )

    def test_governance_blocked_run_is_not_reported_as_a_completed_query(self):
        payload = self.handle(_blocked_output())

        self.assertEqual(payload["status"], "blocked")
        self.assertNotIn("Query completed", payload["text"])
        self.assertIn("blocked", payload["text"].lower())
        # The governance text is what makes the reply actionable, so it has to
        # reach the user instead of being replaced by a row-count sentence.
        self.assertIn("outside the allowed column scope", payload["text"])
        self.assertIn("outside the allowed column scope", payload["reason"])
        # The result payload itself stays untouched: a blocked run has no rows.
        self.assertEqual(payload["row_count"], 0)
        self.assertEqual(payload["rows_preview"], [])

    def test_failed_run_reports_its_error_instead_of_a_completion(self):
        payload = self.handle(
            {
                "status": "failed",
                "run_id": "qf_failed",
                "error": "SQLite database is locked",
                "row_count": 0,
            }
        )
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("Query completed", payload["text"])
        self.assertIn("failed", payload["text"].lower())
        self.assertIn("SQLite database is locked", payload["text"])

    def test_cancelled_run_reports_the_cancellation(self):
        payload = self.handle(
            {
                "status": "cancelled",
                "outcome": "cancelled",
                "run_id": "qf_cancelled",
                "reason": "Client disconnected before the workflow completed.",
            }
        )
        self.assertEqual(payload["status"], "cancelled")
        self.assertNotIn("Query completed", payload["text"])
        self.assertIn("Client disconnected", payload["text"])

    def test_clarification_run_asks_the_user_the_recorded_question(self):
        payload = self.handle(
            {
                "status": "needs_clarification",
                "run_id": "qf_clarify",
                "unresolved_questions": ["missing_ranking_dimension"],
                "session": {
                    "needs_clarification": [
                        {"aspect": "ranking_dimension", "reason": "by what?"}
                    ]
                },
            }
        )
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertNotIn("Query completed", payload["text"])
        self.assertIn("missing_ranking_dimension", payload["text"])
        self.assertIn("by what?", payload["text"])

    def test_successful_run_keeps_the_existing_completed_wording(self):
        payload = self.handle(
            {
                "status": "success",
                "run_id": "qf_ok",
                "explanation": "List names.",
                "columns": ["name"],
                "rows": [["alpha"], ["beta"]],
                "row_count": 2,
            }
        )
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["text"], "List names. Returned 2 row(s).")
        self.assertNotIn("reason", payload)

    def test_run_without_an_explicit_status_keeps_the_completed_wording(self):
        payload = self.handle(
            {"run_id": "qf_legacy", "columns": ["name"], "rows": [["a"]], "row_count": 1}
        )
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["text"], "Query completed. Returned 1 row(s).")

    def test_unanswered_run_without_a_reason_still_reports_its_outcome(self):
        payload = self.handle({"status": "blocked", "run_id": "qf_silent"})
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("blocked", payload["text"].lower())
        self.assertNotIn("Query completed", payload["text"])
        self.assertTrue(payload["reason"])

    def test_model_provider_blocked_reason_is_preferred_when_available(self):
        payload = self.handle(
            {
                "status": "blocked",
                "run_id": "qf_team",
                "agent_team": {"blocked_reason": "governance policy blocked the query"},
            }
        )
        self.assertIn("governance policy blocked the query", payload["text"])

    def test_empty_identifiers_are_still_rejected(self):
        adapter = GatewayAdapter(RecordingService({"status": "success"}))
        with self.assertRaises(ValueError):
            adapter.handle(user_id=" ", channel="C1", text="hi")


@unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
class GatewayWebhookRouteTest(unittest.TestCase):
    def test_webhook_route_never_reports_a_blocked_run_as_completed(self):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        client = TestClient(create_app(RecordingService(_blocked_output())))
        response = client.post(
            "/gateway/webhook",
            json={"user_id": "U1", "channel": "C1", "text": "show me user emails"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "blocked")
        self.assertNotIn("Query completed", body["text"])
        self.assertIn("outside the allowed column scope", body["text"])


if __name__ == "__main__":
    unittest.main()
