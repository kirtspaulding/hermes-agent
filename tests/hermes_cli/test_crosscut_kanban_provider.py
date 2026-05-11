from __future__ import annotations

from types import SimpleNamespace

from hermes_cli import kanban_provider as kp


class FakeCrossCutRepo:
    def __init__(self):
        self.backends = {}
        self.items = {}
        self.mappings = {}
        self.links = []
        self.comments = []
        self.events = []
        self.runs = {}
        self.migrated = False

    def migrate(self):
        self.migrated = True

    def create_execution_backend(self, request):
        backend = SimpleNamespace(
            id=request.id,
            backend_type=request.backend_type,
            name=request.name,
            enabled=request.enabled,
            config=request.config,
        )
        self.backends[backend.id] = backend
        return backend

    def create_item(self, request):
        item = SimpleNamespace(
            id=request.id,
            title=request.title,
            body=request.body,
            status=request.status,
            priority=request.priority,
            assignee_hint=request.assignee_hint,
            executable=request.executable,
        )
        self.items[item.id] = item
        return item

    def create_execution_mapping(self, request):
        mapping = SimpleNamespace(
            id=request.id,
            work_item_id=request.work_item_id,
            backend_id=request.backend_id,
            external_board=request.external_board,
            external_task_id=request.external_task_id,
            external_status=request.external_status,
            sync_state=request.sync_state,
            claim_token=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
        )
        self.mappings[mapping.id] = mapping
        return mapping

    def list_execution_task_projections(self, backend_id=None, external_board=None, status=None, assignee_hint=None, include_archived=False):
        out = []
        for mapping in self.mappings.values():
            item = self.items[mapping.work_item_id]
            if backend_id and mapping.backend_id != backend_id:
                continue
            if external_board is not None and mapping.external_board != external_board:
                continue
            if status and item.status != status:
                continue
            if assignee_hint and item.assignee_hint != assignee_hint:
                continue
            if not include_archived and (item.status == "archived" or mapping.sync_state == "archived"):
                continue
            out.append(self._projection(mapping, item))
        return out

    def show_execution_task_projection(self, mapping_id):
        mapping = self.mappings[mapping_id]
        return self._projection(mapping, self.items[mapping.work_item_id])

    def show_execution_task_details(self, mapping_id):
        projection = self.show_execution_task_projection(mapping_id)
        return SimpleNamespace(
            task=projection,
            comments=[c for c in self.comments if c.work_item_id == projection.work_item_id],
            events=[e for e in self.events if e.work_item_id == projection.work_item_id],
            runs=[r for r in self.runs.values() if r.mapping_id == mapping_id],
            parents=[p for p, c in self.links if c == projection.work_item_id],
            children=[c for p, c in self.links if p == projection.work_item_id],
        )

    def create_item_link(self, request):
        self.links.append((request.parent_id, request.child_id))
        return SimpleNamespace(parent_id=request.parent_id, child_id=request.child_id, link_type=request.link_type)

    def create_comment(self, request):
        comment = SimpleNamespace(id=len(self.comments) + 1, work_item_id=request.work_item_id, author=request.author, body=request.body, source=request.source)
        self.comments.append(comment)
        return comment

    def create_event(self, request):
        event = SimpleNamespace(id=len(self.events) + 1, work_item_id=request.work_item_id, event_type=request.event_type, source=request.source, payload=request.payload or {})
        self.events.append(event)
        return event

    def create_or_update_execution_run(self, request):
        run = SimpleNamespace(
            id=request.id,
            mapping_id=request.mapping_id,
            external_run_id=request.external_run_id,
            profile=request.profile,
            status=request.status,
            outcome=request.outcome,
            summary=request.summary,
            error=request.error,
            started_at=request.started_at,
            ended_at=request.ended_at,
        )
        self.runs[run.id] = run
        return run

    def update_work_item_status(self, work_item_id, status):
        item = self.items[work_item_id]
        item.status = status
        return item

    def update_execution_mapping_status(self, mapping_id, external_status, sync_state="active", last_external_event_id=None):
        mapping = self.mappings[mapping_id]
        mapping.external_status = external_status
        mapping.sync_state = sync_state
        return mapping

    def claim_work_item(self, request):
        mapping = next(m for m in self.mappings.values() if m.work_item_id == request.work_item_id and m.external_task_id == request.external_task_id)
        if self.items[request.work_item_id].status != "ready":
            raise RuntimeError("work item must be ready to claim")
        mapping.claim_token = "lease-token"
        mapping.lease_expires_at = 1234567890 + request.lease_seconds
        mapping.last_heartbeat_at = 1234567890
        mapping.sync_state = "active"
        return SimpleNamespace(mapping_id=mapping.id, work_item_id=mapping.work_item_id, backend_id=mapping.backend_id, claim_token=mapping.claim_token, lease_expires_at=mapping.lease_expires_at)

    def heartbeat_work_item_claim(self, mapping_id, claim_token, lease_seconds=3600, note=None):
        mapping = self.mappings[mapping_id]
        assert claim_token == mapping.claim_token
        mapping.last_heartbeat_at = 1234567999
        mapping.lease_expires_at = 1234567999 + lease_seconds
        self.create_event(SimpleNamespace(work_item_id=mapping.work_item_id, event_type="work_item_claim_heartbeat", source="crosscut", payload={"note": note}))
        return SimpleNamespace(mapping_id=mapping.id, work_item_id=mapping.work_item_id, backend_id=mapping.backend_id, claim_token=mapping.claim_token, lease_expires_at=mapping.lease_expires_at)

    def release_work_item_claim(self, mapping_id, claim_token, reason):
        mapping = self.mappings[mapping_id]
        assert claim_token == mapping.claim_token
        mapping.claim_token = None
        mapping.sync_state = "released"
        self.items[mapping.work_item_id].status = "ready"

    def reclaim_expired_work_items(self):
        reclaimed = 0
        for mapping in self.mappings.values():
            if mapping.sync_state == "active" and mapping.lease_expires_at and mapping.lease_expires_at < 1000:
                mapping.sync_state = "expired"
                mapping.claim_token = None
                self.items[mapping.work_item_id].status = "ready"
                reclaimed += 1
        return reclaimed

    def archive_work_item(self, work_item_id, reason=None):
        return self.update_work_item_status(work_item_id, "archived")

    def list_execution_board_projections(self, backend_id=None, include_archived=False):
        task_count = len([m for m in self.mappings.values() if not backend_id or m.backend_id == backend_id])
        return [SimpleNamespace(backend_id=backend_id or "backend", backend_type="hermes_kanban_crosscut_provider", backend_name="CrossCut", external_board="proto", enabled=True, task_count=task_count, active_count=task_count, archived=False)]

    def _projection(self, mapping, item):
        return SimpleNamespace(
            mapping_id=mapping.id,
            work_item_id=mapping.work_item_id,
            backend_id=mapping.backend_id,
            external_board=mapping.external_board,
            external_task_id=mapping.external_task_id,
            external_status=mapping.external_status,
            sync_state=mapping.sync_state,
            title=item.title,
            body=item.body,
            status=item.status,
            priority=item.priority,
            assignee_hint=item.assignee_hint,
            executable=item.executable,
            claim_token=mapping.claim_token,
            lease_expires_at=mapping.lease_expires_at,
            last_heartbeat_at=mapping.last_heartbeat_at,
        )


def test_crosscut_provider_create_list_show_and_lifecycle():
    repo = FakeCrossCutRepo()
    provider = kp.CrossCutKanbanCliProvider(repository=repo, backend_id="backend", board="proto")

    provider.init_db()
    task = provider.create_task(
        title="crosscut backed",
        body="body",
        assignee="worker",
        created_by="tester",
        workspace_kind="scratch",
        workspace_path=None,
        tenant=None,
        priority=5,
        parents=(),
        triage=False,
        idempotency_key="idem-1",
        max_runtime_seconds=None,
        skills=None,
        max_retries=None,
    )

    assert repo.migrated is True
    assert task.id.startswith("t_")
    assert task.status == "ready"
    assert provider.list_tasks(assignee="worker", status="ready", tenant=None, include_archived=False)[0].id == task.id

    claim = provider.claim_task(task.id, ttl_seconds=60)
    assert claim is not None
    assert claim.task.status == "running"
    projection = provider._projection_for_task_id(task.id)
    assert projection.claim_token == "lease-token"
    assert projection.lease_expires_at is not None
    assert provider.heartbeat_task(task.id, note="alive", expected_run_id=None) is True
    assert provider._projection_for_task_id(task.id).last_heartbeat_at == 1234567999
    provider.add_comment(task.id, "tester", "hello")
    assert provider.block_task(task.id, reason="waiting", expected_run_id=None) is True
    assert provider.get_task_details(task.id).task.status == "blocked"
    assert provider.unblock_task(task.id) is True
    assert provider.complete_task(task.id, result=None, summary="done", metadata={"ok": True}, expected_run_id=None) is True

    details = provider.get_task_details(task.id)
    assert details.latest_summary == "done"
    assert details.comments[0].body == "hello"
    assert any(event.kind == "hermes_task_completed" for event in details.events)
    assert details.runs[-1].outcome == "done"


def test_crosscut_provider_links_parent_child_by_task_ids():
    repo = FakeCrossCutRepo()
    provider = kp.CrossCutKanbanCliProvider(repository=repo, backend_id="backend", board="proto")
    parent = provider.create_task(title="parent", body=None, assignee="worker", created_by="tester", workspace_kind="scratch", workspace_path=None, tenant=None, priority=0, parents=(), triage=False, idempotency_key="parent", max_runtime_seconds=None, skills=None, max_retries=None)
    child = provider.create_task(title="child", body=None, assignee="worker", created_by="tester", workspace_kind="scratch", workspace_path=None, tenant=None, priority=0, parents=(parent.id,), triage=False, idempotency_key="child", max_runtime_seconds=None, skills=None, max_retries=None)

    details = provider.get_task_details(child.id)
    assert details.parents == [parent.id]


def test_crosscut_provider_reclaims_running_task_through_repository_claim_release():
    repo = FakeCrossCutRepo()
    provider = kp.CrossCutKanbanCliProvider(repository=repo, backend_id="backend", board="proto")
    task = provider.create_task(title="lease", body=None, assignee="worker", created_by="tester", workspace_kind="scratch", workspace_path=None, tenant=None, priority=0, parents=(), triage=False, idempotency_key="lease", max_runtime_seconds=None, skills=None, max_retries=None)
    assert provider.claim_task(task.id, ttl_seconds=60) is not None

    assert provider.reclaim_task(task.id, reason="operator reclaim") is True

    projection = provider._projection_for_task_id(task.id)
    assert projection.status == "ready"
    assert projection.claim_token is None
    assert any(event.kind == "hermes_task_reclaimed" for event in provider.get_task_details(task.id).events)


def test_crosscut_provider_board_stats_uses_projection_shape():
    repo = FakeCrossCutRepo()
    provider = kp.CrossCutKanbanCliProvider(repository=repo, backend_id="backend", board="proto")
    provider.create_task(title="a", body=None, assignee="worker", created_by="tester", workspace_kind="scratch", workspace_path=None, tenant=None, priority=0, parents=(), triage=False, idempotency_key="a", max_runtime_seconds=None, skills=None, max_retries=None)
    provider.create_task(title="b", body=None, assignee="reviewer", created_by="tester", workspace_kind="scratch", workspace_path=None, tenant=None, priority=0, parents=(), triage=True, idempotency_key="b", max_runtime_seconds=None, skills=None, max_retries=None)

    stats = provider.board_stats()

    assert stats["by_status"]["ready"] == 1
    assert stats["by_status"]["todo"] == 1
    assert stats["by_assignee"]["worker"]["ready"] == 1
