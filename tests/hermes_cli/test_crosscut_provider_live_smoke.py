"""Guarded live smoke for the CrossCut-backed Hermes Kanban provider.

This test is intentionally opt-in because it touches the operator's local
CrossCut/Postgres services and may spawn a bounded worker wave. Run with:

    HERMES_CROSSCUT_PROVIDER_LIVE_SMOKE=1 \
    CROSSCUT_DATABASE_URL=postgresql://... \
    PYTHONPATH=/home/vice/.hermes/hermes-agent:/home/vice/CrossCut/crosscut \
    venv/bin/python -m pytest tests/hermes_cli/test_crosscut_provider_live_smoke.py -q -o 'addopts='

It uses a disposable backend/board and archives created records when the provider
surface reaches the cleanup step.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.getenv("HERMES_CROSSCUT_PROVIDER_LIVE_SMOKE") != "1",
    reason="opt-in live CrossCut/Postgres smoke; set HERMES_CROSSCUT_PROVIDER_LIVE_SMOKE=1",
)
def test_live_crosscut_provider_worker_lifecycle_smoke(monkeypatch):
    from hermes_cli import kanban_provider as kp
    from tools import kanban_tools as kt

    if not os.getenv("CROSSCUT_DATABASE_URL"):
        pytest.skip("CROSSCUT_DATABASE_URL is required for live smoke")

    suffix = uuid4().hex[:10]
    provider = kp.CrossCutKanbanCliProvider(
        backend_id=f"hermes-kanban-crosscut-provider-smoke-{suffix}",
        board=f"disposable-provider-smoke-{suffix}",
    )
    old_provider = kp.get_kanban_cli_provider()
    kp.set_kanban_cli_provider(provider)
    created: list[str] = []
    try:
        provider.init_db()
        task = provider.create_task(
            title="live CrossCut provider smoke",
            body="disposable live-smoke record; archive after verification",
            assignee="crosscut-coder",
            created_by="live-smoke",
            workspace_kind="scratch",
            workspace_path=None,
            tenant=None,
            priority=0,
            parents=(),
            triage=False,
            idempotency_key=f"live-smoke:{suffix}",
            max_runtime_seconds=60,
            skills=["kanban-worker"],
            max_retries=1,
        )
        created.append(task.id)
        assert task.assignee == "crosscut-coder"
        claim = provider.claim_task(task.id, ttl_seconds=120)
        assert claim is not None

        monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
        monkeypatch.setenv("HERMES_PROFILE", "crosscut-coder")
        assert kt._handle_heartbeat({"note": "live smoke heartbeat"})
        assert kt._handle_complete({"summary": "live smoke complete", "metadata": {"smoke": True}})

        details = provider.get_task_details(task.id)
        assert details is not None
        assert details.task.status == "done"
        assert any(event.kind == "hermes_task_heartbeat" for event in details.events)
        assert details.latest_summary == "live smoke complete"
    finally:
        for task_id in created:
            try:
                provider.archive_task(task_id)
            except Exception:
                pass
        kp.set_kanban_cli_provider(old_provider)
