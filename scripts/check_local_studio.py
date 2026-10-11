"""Exercise production Studio routes against an explicitly offline local fixture.

Start tests.studio_fixture_server and Studio with isolated STUDIO_STATE_DIR first.
This script refuses a real provider backend, even when credentials are configured.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from io import BytesIO
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, ProxyHandler, build_opener


CSV = (Path(__file__).resolve().parents[1] / "tests/fixtures/studio_records.csv").read_bytes()
CONTRACT = {
    "version": 1, "entity": "uploaded_record", "description": "Disposable offline records",
    "owner": "Local acceptance", "grain": "record_id", "primaryKey": "record_id",
    "sensitivity": "internal", "dimensions": ["record_id", "entity_id", "recorded_at"],
    "metrics": [
        {"name": "uploaded_record_count", "description": "Number of records", "aggregation": "count", "expression": "COUNT(DISTINCT uploaded_record.record_id)"},
        {"name": "uploaded_amount_sum", "description": "Total amount", "aggregation": "sum", "expression": "SUM(uploaded_record.amount)"},
    ],
}


class StudioCheck:
    def __init__(self, base: str):
        parsed = urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username:
            raise ValueError("Acceptance must use a loopback HTTP address.")
        self.base = base.rstrip("/")
        self.opener = build_opener(ProxyHandler({}))
        self.checks: list[str] = []

    def request(self, path, payload=None, *, headers=None, expected=200, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        request_headers = {"content-type": "application/json"} if payload is not None else {}
        request_headers.update(headers or {})
        request = Request(self.base + path, data=body, headers=request_headers)
        try:
            response = self.opener.open(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            raw_response = response.read()
            try:
                data = json.loads(raw_response)
            except json.JSONDecodeError:
                data = {"detail": raw_response.decode(errors="replace")[:500]}
            assert response.status == expected, (path, response.status, data)
        return data

    def check(self, name, condition):
        assert condition, name
        self.checks.append(name)

    def events(self, options, *, disconnect=False):
        request = Request(self.base + "/api/queryforge/ask/stream", data=json.dumps(options).encode(),
                          headers={"content-type": "application/json"})
        events = []
        with self.opener.open(request, timeout=35) as response:
            assert response.headers.get("x-queryforge-event-protocol") == "1"
            for line in response:
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if disconnect and event["event_type"] == "node_started" and event["node_name"] == "gen_sql":
                    break
        return events

    def upload(self, domain, *, reviewed=True, csv=CSV, filename="records.csv", expected=201):
        boundary = "queryforge-" + uuid.uuid4().hex
        fields = {"domain_id": domain, "reviewed": str(reviewed).lower(), "reviewed_by": "local-acceptance", "semantic_contract": json.dumps(CONTRACT)}
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + csv + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("/api/studio/upload", raw=b"".join(parts), headers={"content-type": f"multipart/form-data; boundary={boundary}"}, expected=expected)

    def run(self):
        health = self.request("/api/queryforge/health")
        self.check("explicit_offline_fixture", health.get("offline_fixture") is True)
        # Only after this guard is it safe to start a workflow.
        domain = self.request("/api/studio/domains", {"name": "Local acceptance " + uuid.uuid4().hex[:8], "description": "Disposable offline acceptance"}, expected=201)["domain"]["id"]
        other = self.request("/api/studio/domains", {"name": "Unpublished " + uuid.uuid4().hex[:8]}, expected=201)["domain"]["id"]
        self.upload(domain, reviewed=False, expected=400)
        self.check("unreviewed_upload_denied", True)
        rejected = self.upload(other, csv=CSV.replace(b"record_id,", b"missing_id,"), expected=422)
        self.check("invalid_contract_not_published", rejected["source"]["status"] == "publish_failed")
        invalid_binary = self.upload(other, csv=b"invalid parquet", filename="invalid.parquet", expected=422)
        self.check("invalid_parquet_physically_rejected", invalid_binary["source"]["pythonPublish"]["status"] == "failed")
        import pyarrow as pa
        import pyarrow.parquet as pq
        binary = BytesIO()
        pq.write_table(pa.table({"record_id": [1, 2, 3], "entity_id": [11, 12, 13], "recorded_at": ["2024-01-01", "2024-01-02", "2024-01-03"], "amount": [10, 20, 30]}), binary)
        parquet_domain = self.request("/api/studio/domains", {"name": "Parquet acceptance " + uuid.uuid4().hex[:8]}, expected=201)["domain"]["id"]
        binary_result = self.upload(parquet_domain, csv=binary.getvalue(), filename="records.parquet")["source"]
        self.check("parquet_physical_publication", binary_result["status"] == "ready" and binary_result["pythonPublish"]["status"] == "published" and binary_result["semanticModel"]["validation"]["files"][0]["validation_status"] == "not_verified_server_side")
        published = self.upload(domain)["source"]
        version = published["pythonPublish"]["data_version"]
        self.check("physical_publication", published["status"] == "ready" and bool(version))
        domains = self.request("/api/studio/domains")["domains"]
        self.check("ready_domain_after_publication", next(item for item in domains if item["id"] == domain)["status"] == "ready")
        options = {"domain_id": domain, "complexity_mode": "simple", "report": True}
        count = self.request("/api/queryforge/ask", {**options, "question": "How many uploaded records? Return a single column named n."})
        self.check("physical_count_oracle", count["status"] == "success" and count["rows"] == [[3]])
        total = self.request("/api/queryforge/ask", {**options, "question": "What is uploaded_amount_sum? Return only amount."})
        self.check("physical_sum_oracle", total["status"] == "success" and total["rows"] == [[60]])
        self.request("/api/queryforge/ask", {"domain_id": other, "question": "Count rows", "complexity_mode": "simple"}, expected=400)
        self.check("unpublished_domain_denied", True)
        self.request("/api/queryforge/ask", {**options, "question": "Count rows", "database": "sample_data/anime_streaming/anime_streaming.sqlite"}, expected=400)
        self.check("domain_path_override_denied", True)
        blocked = self.request("/api/queryforge/ask", {**options, "question": "Delete uploaded records. Return only n."}, expected=422)
        self.check("write_sql_denied", "SQL_SECURITY_ERROR" in blocked["detail"] and "query_executed': False" in blocked["detail"])
        count_after = self.request("/api/queryforge/ask", {**options, "question": "How many uploaded records? Return a single column named n."})
        self.check("write_rejection_keeps_data", count_after["rows"] == [[3]])
        execution = count
        snapshot = {"runId": count["run_id"], "status": count["status"], "provenance": "live", "mode": "live-request",
                    "columns": execution["columns"], "rows": execution["rows"], "rowCount": execution["row_count"],
                    "sql": count["sql"], "explanation": "Offline fixture with real governed execution", "planText": "", "detail": "", "output": count}
        saved = {"id": count["run_id"], "domainId": domain, "question": "How many uploaded records? Return a single column named n.", "status": "success", "model": "offline-browser-fixture", "rowCount": 1, "duration": "offline", "snapshot": snapshot, "dataVersion": version}
        self.request("/api/studio/runs", saved, expected=201)
        calls_before = self.request("/api/queryforge/health")["offline_model_calls"]
        replay_path = "/api/studio/runs?" + urlencode({"run_id": count["run_id"], "domain_id": domain})
        replay = self.request(replay_path)
        self.check("snapshot_replay", replay["run"] == snapshot and replay["data_version"] == version)
        self.check("replay_zero_model_calls", calls_before == self.request("/api/queryforge/health")["offline_model_calls"])
        self.request("/api/studio/runs?" + urlencode({"run_id": count["run_id"], "domain_id": other}), expected=404)
        self.request("/api/studio/runs", {**saved, "domainId": other}, expected=409)
        self.check("cross_domain_replay_and_id_conflict_denied", True)
        self.request("/api/studio/domains", headers={"Origin": "https://untrusted.example"}, expected=403)
        self.check("cross_origin_denied", True)
        streamed = self.events({**options, "question": "How many uploaded records? Return a single column named n."})
        terminal = [event for event in streamed if event["event_type"] == "final_result"]
        self.check("sse_terminal_protocol_and_oracle", len(terminal) == 1 and terminal[0]["outcome"] == "success" and terminal[0]["result"]["rows"] == [[3]])
        self.check("sse_gapless_sequence", [event["sequence"] for event in streamed] == list(range(1, len(streamed) + 1)))
        cancelled = self.events({**options, "question": "Slow count of uploaded records. Return a single column named n."}, disconnect=True)
        cancelled_id = cancelled[0]["run_id"]
        fixture_root = Path("/tmp/queryforge_phase4_fixture/backend")
        state_path = fixture_root / "runs" / cancelled_id / "state.json"
        deadline = time.monotonic() + 30
        state = {}
        while time.monotonic() < deadline:
            if state_path.is_file():
                state = json.loads(state_path.read_text())
                if state.get("status") in {"cancelled", "completed", "failed", "blocked"}:
                    break
            time.sleep(0.2)
        self.check("sse_disconnect_persisted_cancelled", state.get("status") == "cancelled")
        self.check("cancelled_run_has_no_execution_artifact", not any(item.get("artifact_type") == "execution_result" for item in state.get("artifacts", [])))
        # Only disposable reports under the explicitly offline fixture are aged.
        report_id = "local_expiry_" + uuid.uuid4().hex
        report_root = fixture_root / "reports"
        report_root.mkdir(parents=True, exist_ok=True)
        html = report_root / (report_id + ".html")
        manifest = report_root / (report_id + ".manifest.json")
        html.write_text("Disposable offline expiry fixture")
        manifest.write_text('{"evidence":"preserved"}')
        old = time.time() - 31 * 86400
        os.utime(html, (old, old))
        backend = StudioCheck("http://127.0.0.1:8000")
        self.check("backend_offline_guard", backend.request("/health").get("offline_fixture") is True)
        backend.request("/report/" + report_id, expected=404)
        preview = backend.request("/reports/expire", {"dry_run": True})
        self.check("expired_report_denied_dry_run_preserves", report_id in preview["expired"] and html.exists() and manifest.exists())
        cleanup = backend.request("/reports/expire", {"dry_run": False})
        self.check("expired_report_reversibly_quarantined", report_id in cleanup["expired"] and not html.exists() and len(list((report_root / ".expired").glob(report_id + "*/*"))) == 2)
        return {"mode": "offline_model_real_studio_and_database", "checks": self.checks, "passed": len(self.checks),
                "domain_id": domain, "unpublished_domain_id": other, "run_id": count["run_id"], "data_version": version,
                "parquet_domain_id": parquet_domain,
                "cancelled_run_id": cancelled_id, "cancelled_state": str(state_path),
                "fixture_sha256": hashlib.sha256(CSV).hexdigest(), "oracle": {"count": 3, "sum": 60}, "real_provider_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio", default="http://localhost:3000")
    parser.add_argument("--report", default="/tmp/queryforge_phase4_studio_acceptance.json")
    arguments = parser.parse_args()
    started = time.monotonic()
    result = StudioCheck(arguments.studio).run()
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    Path(arguments.report).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
