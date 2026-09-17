"""Isolated real-workflow runners. Scripted SQL is a control-flow test, not model accuracy."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
import time
from uuid import uuid4

from queryforge.core.config import Config, load_config
from queryforge.core.schemas.models import SqlTask
from queryforge.workflow.workflow_runner import WorkflowRunner


class ScriptedModel:
    def __init__(self, sql: str):
        self.sql = sql

    def generate_json(self, prompt: str) -> dict:
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "deterministic fixture"}
        if "Evaluate whether the SQL and result" in prompt:
            return {"success": True, "strategy": "SUCCESS", "reason": "fixture; independently scored"}
        return {"sql": self.sql, "explanation": "Scripted fixture; not a model prediction", "tables_used": []}


def isolated_config(dataset: dict, root: Path, *, provider=None, model=None) -> Config:
    root.mkdir(parents=True, exist_ok=True)
    base = load_config(provider_override=provider, model_override=model) if provider else Config("scripted", None, "fixture", None, dataset["database"])
    return replace(base, llm_provider=provider or "scripted", llm_model=model or "fixture",
                   database_path=dataset["database"], semantic_model_path=dataset["semantic_model"],
                   sql_policy_path=dataset["sql_policy"], history_db_path=str(root / "history.db"),
                   vector_kb_path=str(root / "vector"), orchestration_state_root=str(root / "runs"),
                   report_output_dir=str(root / "reports"), require_semantic_model=True)


def run_workflow_task(spec, dataset: dict, context) -> dict:
    config = isolated_config(dataset, context.state_root, provider=context.provider, model=context.model)
    kwargs = dict(history_top_k=0, enable_vector_kb=False, vector_top_k=0,
                  semantic_model_path=dataset["semantic_model"], sql_policy_path=dataset["sql_policy"],
                  today_provider=lambda: date(2024, 12, 31), show_run_summary=True,
                  max_retries=0 if "fix_loop" in context.ablations else 2,
                  parallel_candidates=1 if "multi_candidate" in context.ablations else 2 if context.provider else 1)
    if not context.provider:
        kwargs["llm_factory"] = lambda _: ScriptedModel(spec.reference_sql)
    started = time.perf_counter()
    run_id = f"eval_{uuid4().hex}"
    from queryforge.core.observability import get_span_recorder
    error = None
    payload = {}
    try:
        if getattr(spec, "follow_up_context", None):
            from queryforge.application import AgentService, AgentOptions
            options = AgentOptions(database=dataset["database"], semantic_model_path=dataset["semantic_model"],
                                   sql_policy_path=dataset["sql_policy"], session_id="eval-session", complexity_mode="simple",
                                   history_top_k=0, show_run_summary=True, orchestration_state_root=str(context.state_root/'runs'))
            service_kwargs = {"config_loader": lambda **_: config}
            if not context.provider:
                service_kwargs["llm_factory"] = lambda _: ScriptedModel(spec.reference_sql)
            service = AgentService(**service_kwargs)
            turns=[]
            for question in [*spec.follow_up_context, spec.question]:
                run_id=f"eval_{uuid4().hex}"
                options.run_id=run_id
                turns.append(service.ask(question,options))
            payload=turns[-1]
            payload["previous_turns"] = turns[:-1]
        else:
            payload = WorkflowRunner(config, run_id_factory=lambda: run_id, **kwargs).run(SqlTask(question=spec.question, database_path=dataset["database"]))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    summary = payload.get("run_summary") or {}
    recorder = get_span_recorder(run_id)
    usage = recorder.usage_summary() if recorder else summary.get("usage")
    raw_spans = [s.to_dict() for s in recorder.spans] if recorder else []
    # Include previous turns once; never add both a summary and its child spans.
    if usage and payload.get("previous_turns"):
        usage=dict(usage)
        for turn in payload["previous_turns"]:
            prior=(turn.get("run_summary") or {}).get("usage")
            if not prior:
                usage["estimated"]=True
                continue
            for key in ("prompt_tokens","completion_tokens","total_tokens","model_calls","measured_calls"):
                usage[key]=usage.get(key,0)+prior.get(key,0)
            usage["estimated"]=usage.get("estimated",False) or prior.get("estimated",False)
    nodes = summary.get("workflow_nodes") or []
    calls = [{"tool": "execute_sql", "action": "execute_sql", "ok": n["success"],
              "duration_ms": n.get("duration_ms"), "error": n.get("error")}
             for n in nodes if n["name"] == "execute_sql"]
    return dict(task_id=spec.task_id, payload=payload, tool_calls=calls, usage=usage,
                cost_usd=None, wall_ms=(time.perf_counter()-started)*1000, error=error,
                provider=context.provider, model=context.model,
                model_spans=raw_spans,
                runner="model_e2e" if context.provider else "scripted_workflow")
