"""Explicit offline browser fixture. Never loads .env or calls a provider API.

Run: .venv/bin/python -m tests.studio_fixture_server
Storage: /tmp/queryforge_phase4_fixture/backend; listens only on 127.0.0.1:8000.
"""
import time
from pathlib import Path

from queryforge.application import AgentService
from queryforge.core.config import Config
from queryforge.interfaces.api.app import create_app
from scripts.benchmark_runners import ScriptedModel


class BrowserFixtureModel(ScriptedModel):
    calls = 0

    def generate_json(self, prompt):
        type(self).calls += 1
        question = prompt.partition("User question:\n")[2].split("\n\n", 1)[0].lower()
        if "generate one" in prompt.lower() or "alternative" in prompt.lower():
            if "slow" in question:
                time.sleep(20)
            self.sql = "DELETE FROM uploaded_record" if "delete" in question else (
                "SELECT SUM(amount) AS amount FROM uploaded_record" if "uploaded_amount_sum" in question
                else "SELECT COUNT(DISTINCT record_id) AS n FROM uploaded_record")
        return super().generate_json(prompt)


class FixtureService(AgentService):
    def health(self):
        return {**super().health(), "offline_fixture": True, "offline_model_calls": BrowserFixtureModel.calls}


def fixture_service(root: Path):
    config = Config("scripted", None, "offline-browser-fixture", None,
                    "sample_data/anime_streaming/anime_streaming.sqlite",
                    orchestration_state_root=str(root / "runs"),
                    domain_registry_path=str(root / "registry.json"),
                    history_db_path=str(root / "history.db"), report_output_dir=str(root / "reports"),
                    allowed_database_paths=(str(root), str(Path("sample_data").resolve())),)
    return FixtureService(config_loader=lambda **_: config,
                        llm_factory=lambda _: BrowserFixtureModel("SELECT COUNT(*) AS n FROM uploaded_record"))


if __name__ == "__main__":
    import uvicorn
    root = Path("/tmp/queryforge_phase4_fixture/backend")
    root.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(fixture_service(root)), host="127.0.0.1", port=8000)
