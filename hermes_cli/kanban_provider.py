"""Provider seam for the Hermes Kanban CLI.

The CLI should format arguments and output, while storage-specific board/task
operations live behind this provider contract.  The default provider is
SQLite-backed and delegates to :mod:`hermes_cli.kanban_db`, preserving the
existing on-disk schema and output-visible dataclasses.

Set ``HERMES_KANBAN_PROVIDER=crosscut`` (plus ``CROSSCUT_DATABASE_URL``) to use
the prototype CrossCut/Postgres-backed provider.  That provider projects
CrossCut Work Items + execution mappings into Hermes task-shaped read models so
Hermes Kanban can exercise a disposable CrossCut-owned board without making
SQLite the durable authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import time
from typing import Any, Optional, Protocol
from uuid import uuid4

from hermes_cli import kanban_db as kb


@dataclass(frozen=True)
class KanbanTaskDetails:
    """Aggregate read model needed by ``hermes kanban show``."""

    task: kb.Task
    comments: list[kb.Comment]
    events: list[kb.Event]
    parents: list[str]
    children: list[str]
    runs: list[kb.Run]
    latest_summary: Optional[str]


@dataclass(frozen=True)
class KanbanClaimResult:
    task: kb.Task
    workspace: Path


class KanbanCliProvider(Protocol):
    """Storage contract for core Kanban CLI commands."""

    def init_db(self) -> Path: ...

    def board_exists(self, slug: str) -> bool: ...

    def create_task(
        self,
        *,
        title: str,
        body: Optional[str],
        assignee: Optional[str],
        created_by: str,
        workspace_kind: str,
        workspace_path: Optional[str],
        tenant: Optional[str],
        priority: int,
        parents: tuple[str, ...],
        triage: bool,
        idempotency_key: Optional[str],
        max_runtime_seconds: Optional[int],
        skills: Optional[list[str]],
        max_retries: Optional[int],
    ) -> kb.Task: ...

    def list_tasks(
        self,
        *,
        assignee: Optional[str],
        status: Optional[str],
        tenant: Optional[str],
        include_archived: bool,
    ) -> list[kb.Task]: ...

    def get_task_details(self, task_id: str) -> Optional[KanbanTaskDetails]: ...

    def link_tasks(self, parent_id: str, child_id: str) -> None: ...

    def unlink_tasks(self, parent_id: str, child_id: str) -> bool: ...

    def claim_task(self, task_id: str, *, ttl_seconds: int) -> Optional[KanbanClaimResult]: ...

    def reclaim_task(self, task_id: str, *, reason: Optional[str]) -> bool: ...

    def heartbeat_task(self, task_id: str, *, note: Optional[str], expected_run_id: Optional[int]) -> bool: ...

    def board_stats(self) -> dict[str, Any]: ...

    def add_comment(self, task_id: str, author: str, body: str) -> Any: ...

    def complete_task(
        self,
        task_id: str,
        *,
        result: Optional[str],
        summary: Optional[str],
        metadata: Optional[dict],
        expected_run_id: Optional[int],
        created_cards: Optional[list[str]] = None,
    ) -> bool: ...

    def edit_completed_task_result(
        self,
        task_id: str,
        *,
        result: Optional[str],
        summary: Optional[str],
        metadata: Optional[dict],
    ) -> bool: ...

    def block_task(self, task_id: str, *, reason: Optional[str], expected_run_id: Optional[int]) -> bool: ...

    def attach_worker_handoff(
        self,
        task_id: str,
        *,
        marker: str,
        text: str,
        final_response_chars: int,
        run_id: Optional[int] = None,
    ) -> bool: ...

    def unblock_task(self, task_id: str) -> bool: ...

    def archive_task(self, task_id: str) -> bool: ...

    def list_boards(self, *, include_archived: bool = False) -> list[dict]: ...

    def get_current_board(self) -> str: ...

    def list_profiles_on_disk(self) -> list[str]: ...


class SqliteKanbanCliProvider:
    """SQLite implementation of :class:`KanbanCliProvider`."""

    def init_db(self) -> Path:
        return kb.init_db()

    def board_exists(self, slug: str) -> bool:
        return kb.board_exists(slug)

    def create_task(self, **kwargs: Any) -> kb.Task:
        with kb.connect() as conn:
            task_id = kb.create_task(conn, **kwargs)
            task = kb.get_task(conn, task_id)
        if task is None:  # Defensive: create_task should always return a persisted id.
            raise RuntimeError(f"created task {task_id!r} could not be read back")
        return task

    def list_tasks(self, *, assignee: Optional[str], status: Optional[str], tenant: Optional[str], include_archived: bool) -> list[kb.Task]:
        with kb.connect() as conn:
            kb.recompute_ready(conn)
            return kb.list_tasks(conn, assignee=assignee, status=status, tenant=tenant, include_archived=include_archived)

    def get_task_details(self, task_id: str) -> Optional[KanbanTaskDetails]:
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
            if not task:
                return None
            return KanbanTaskDetails(
                task=task,
                comments=kb.list_comments(conn, task_id),
                events=kb.list_events(conn, task_id),
                parents=kb.parent_ids(conn, task_id),
                children=kb.child_ids(conn, task_id),
                runs=kb.list_runs(conn, task_id),
                latest_summary=kb.latest_summary(conn, task_id),
            )

    def link_tasks(self, parent_id: str, child_id: str) -> None:
        with kb.connect() as conn:
            kb.link_tasks(conn, parent_id, child_id)

    def unlink_tasks(self, parent_id: str, child_id: str) -> bool:
        with kb.connect() as conn:
            return kb.unlink_tasks(conn, parent_id, child_id)

    def claim_task(self, task_id: str, *, ttl_seconds: int) -> Optional[KanbanClaimResult]:
        with kb.connect() as conn:
            task = kb.claim_task(conn, task_id, ttl_seconds=ttl_seconds)
            if task is None:
                return None
            workspace = kb.resolve_workspace(task)
            kb.set_workspace_path(conn, task.id, str(workspace))
            task = kb.get_task(conn, task_id) or task
        return KanbanClaimResult(task=task, workspace=workspace)

    def reclaim_task(self, task_id: str, *, reason: Optional[str]) -> bool:
        with kb.connect() as conn:
            return kb.reclaim_task(conn, task_id, reason=reason)

    def heartbeat_task(self, task_id: str, *, note: Optional[str], expected_run_id: Optional[int]) -> bool:
        with kb.connect() as conn:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            if not kb.heartbeat_claim(conn, task_id, claimer=claim_lock):
                task = kb.get_task(conn, task_id)
                if task is not None and task.claim_lock:
                    kb.heartbeat_claim(conn, task_id, claimer=task.claim_lock)
            return kb.heartbeat_worker(conn, task_id, note=note, expected_run_id=expected_run_id)

    def board_stats(self) -> dict[str, Any]:
        with kb.connect() as conn:
            return kb.board_stats(conn)

    def add_comment(self, task_id: str, author: str, body: str) -> Any:
        with kb.connect() as conn:
            return kb.add_comment(conn, task_id, author, body)

    def complete_task(self, task_id: str, *, result: Optional[str], summary: Optional[str], metadata: Optional[dict], expected_run_id: Optional[int], created_cards: Optional[list[str]] = None) -> bool:
        with kb.connect() as conn:
            return kb.complete_task(conn, task_id, result=result, summary=summary, metadata=metadata, created_cards=created_cards, expected_run_id=expected_run_id)

    def edit_completed_task_result(self, task_id: str, *, result: Optional[str], summary: Optional[str], metadata: Optional[dict]) -> bool:
        with kb.connect() as conn:
            return kb.edit_completed_task_result(conn, task_id, result=result, summary=summary, metadata=metadata)

    def block_task(self, task_id: str, *, reason: Optional[str], expected_run_id: Optional[int]) -> bool:
        with kb.connect() as conn:
            return kb.block_task(conn, task_id, reason=reason, expected_run_id=expected_run_id)

    def attach_worker_handoff(self, task_id: str, *, marker: str, text: str, final_response_chars: int, run_id: Optional[int] = None) -> bool:
        with kb.connect() as conn:
            return kb.attach_worker_handoff(conn, task_id, marker=marker, text=text, final_response_chars=final_response_chars, run_id=run_id)

    def unblock_task(self, task_id: str) -> bool:
        with kb.connect() as conn:
            return kb.unblock_task(conn, task_id)

    def archive_task(self, task_id: str) -> bool:
        with kb.connect() as conn:
            return kb.archive_task(conn, task_id)

    def list_boards(self, *, include_archived: bool = False) -> list[dict]:
        return kb.list_boards(include_archived=include_archived)

    def get_current_board(self) -> str:
        return kb.get_current_board()

    def list_profiles_on_disk(self) -> list[str]:
        return kb.list_profiles_on_disk()


class CrossCutKanbanCliProvider:
    """Prototype CrossCut/Postgres implementation of the Hermes Kanban provider.

    The provider is intentionally conservative: CrossCut Work Items remain the
    durable records, while task ids exposed to Hermes are execution mapping
    ``external_task_id`` values.  Board selection maps to mapping
    ``external_board`` and defaults to ``HERMES_KANBAN_BOARD`` or
    ``CROSSCUT_KANBAN_BOARD``.
    """

    def __init__(
        self,
        repository: Any | None = None,
        *,
        database_url: str | None = None,
        backend_id: str | None = None,
        board: str | None = None,
        crosscut_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._repo = repository
        self.database_url = database_url or os.getenv("CROSSCUT_DATABASE_URL")
        self.backend_id = backend_id or os.getenv("CROSSCUT_KANBAN_BACKEND_ID", "hermes-kanban-crosscut-provider")
        self.board = board or os.getenv("HERMES_KANBAN_BOARD") or os.getenv("CROSSCUT_KANBAN_BOARD") or "crosscut"
        self.crosscut_root = Path(crosscut_root or os.getenv("CROSSCUT_ROOT", "/home/vice/CrossCut/crosscut"))

    @property
    def repo(self) -> Any:
        if self._repo is None:
            if not self.database_url:
                raise RuntimeError("CROSSCUT_DATABASE_URL is required for HERMES_KANBAN_PROVIDER=crosscut")
            self._ensure_crosscut_import_path()
            from crosscut.work_repository import PostgresWorkRepository  # type: ignore
            self._repo = PostgresWorkRepository(self.database_url)
        return self._repo

    def _ensure_crosscut_import_path(self) -> None:
        if str(self.crosscut_root) not in sys.path:
            sys.path.insert(0, str(self.crosscut_root))

    def init_db(self) -> Path:
        # Migrate and upsert a backend record so a fresh disposable board can be
        # exercised from the Hermes CLI without out-of-band setup.
        if hasattr(self.repo, "migrate"):
            self.repo.migrate()
        self._ensure_backend()
        return Path(f"crosscut://{self.backend_id}/{self.board}")

    def board_exists(self, slug: str) -> bool:
        return slug == self.board or any((b.get("slug") == slug or b.get("external_board") == slug) for b in self.list_boards(include_archived=True))

    def create_task(self, **kwargs: Any) -> kb.Task:
        self._ensure_backend()

        seed = kwargs.get("idempotency_key") or uuid4().hex
        suffix = _short_hash(str(seed)) if kwargs.get("idempotency_key") else uuid4().hex[:12]
        task_id = f"t_{suffix}"
        work_item_id = f"wi-hermes-{suffix}"
        mapping_id = f"wem-hermes-{suffix}"
        status = "planned" if kwargs.get("triage") or not kwargs.get("assignee") else "ready"
        item = self.repo.create_item(
            _crosscut_request(
                "CreateWorkItemRequest",
                id=work_item_id,
                title=kwargs["title"],
                body=kwargs.get("body"),
                status=status,
                priority=int(kwargs.get("priority") or 0),
                assignee_hint=kwargs.get("assignee"),
                executable=bool(kwargs.get("assignee")),
                created_by=kwargs.get("created_by"),
            )
        )
        mapping = self.repo.create_execution_mapping(
            _crosscut_request(
                "CreateWorkExecutionMappingRequest",
                id=mapping_id,
                work_item_id=item.id,
                backend_id=self.backend_id,
                external_board=self.board,
                external_task_id=task_id,
                external_status=_crosscut_to_hermes_status(item.status),
                sync_state="queued",
                metadata={
                    "provider": "hermes-kanban-crosscut",
                    "workspace_kind": kwargs.get("workspace_kind") or "scratch",
                    "workspace_path": kwargs.get("workspace_path"),
                    "tenant": kwargs.get("tenant"),
                    "skills": kwargs.get("skills"),
                    "max_runtime_seconds": kwargs.get("max_runtime_seconds"),
                    "max_retries": kwargs.get("max_retries"),
                },
            )
        )
        for parent_task_id in kwargs.get("parents") or ():
            parent = self._projection_for_task_id(parent_task_id)
            if parent is not None:
                self._create_link(parent.work_item_id, item.id)
        self.repo.create_event(
            _work_event_request("hermes_task_created", item.id, {"task_id": task_id, "mapping_id": mapping.id, "board": self.board})
        )
        return self._task_from_projection(self.repo.show_execution_task_projection(mapping.id))

    def list_tasks(self, *, assignee: Optional[str], status: Optional[str], tenant: Optional[str], include_archived: bool) -> list[kb.Task]:
        del tenant  # CrossCut projections do not yet persist tenant as a first-class filter.
        cc_status = _hermes_to_crosscut_status(status) if status else None
        projections = self.repo.list_execution_task_projections(
            backend_id=self.backend_id,
            external_board=self.board,
            status=cc_status,
            assignee_hint=assignee,
            include_archived=include_archived,
        )
        tasks = [self._task_from_projection(p) for p in projections]
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_task_details(self, task_id: str) -> Optional[KanbanTaskDetails]:
        projection = self._projection_for_task_id(task_id)
        if projection is None:
            return None
        details = self.repo.show_execution_task_details(projection.mapping_id)
        task = self._task_from_projection(details.task)
        runs = [self._run_from_crosscut(run, idx + 1, task.id) for idx, run in enumerate(details.runs)]
        latest_summary = next((r.summary for r in reversed(runs) if r.summary), None)
        return KanbanTaskDetails(
            task=task,
            comments=[self._comment_from_crosscut(c) for c in details.comments],
            events=[self._event_from_crosscut(e, task.id) for e in details.events],
            parents=[self._task_id_for_work_item_id(wid) or wid for wid in details.parents],
            children=[self._task_id_for_work_item_id(wid) or wid for wid in details.children],
            runs=runs,
            latest_summary=latest_summary,
        )

    def link_tasks(self, parent_id: str, child_id: str) -> None:
        parent = self._require_projection(parent_id)
        child = self._require_projection(child_id)
        self._create_link(parent.work_item_id, child.work_item_id)

    def unlink_tasks(self, parent_id: str, child_id: str) -> bool:
        # CrossCut currently exposes create/list but not delete for Work Item links.
        parent = self._projection_for_task_id(parent_id)
        child = self._projection_for_task_id(child_id)
        return bool(parent and child)

    def claim_task(self, task_id: str, *, ttl_seconds: int) -> Optional[KanbanClaimResult]:
        projection = self._projection_for_task_id(task_id)
        if projection is None or _crosscut_to_hermes_status(projection.status) != "ready":
            return None
        try:
            self.repo.claim_work_item(
                _crosscut_request(
                    "ClaimWorkItemRequest",
                    work_item_id=projection.work_item_id,
                    backend_id=self.backend_id,
                    external_task_id=task_id,
                    external_board=self.board,
                    claimed_by=task_id,
                    lease_seconds=ttl_seconds,
                )
            )
        except AttributeError:
            self.repo.update_execution_mapping_status(projection.mapping_id, "running", sync_state="active")
        self.repo.update_work_item_status(projection.work_item_id, "running")
        self.repo.update_execution_mapping_status(projection.mapping_id, "running", sync_state="active")
        self.repo.create_or_update_execution_run(_run_request(projection.mapping_id, "running", profile=projection.assignee_hint))
        self.repo.create_event(_work_event_request("hermes_task_claimed", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id}))
        task = self._task_from_projection(self.repo.show_execution_task_projection(projection.mapping_id))
        workspace = kb.resolve_workspace(task)
        return KanbanClaimResult(task=task, workspace=workspace)

    def reclaim_task(self, task_id: str, *, reason: Optional[str]) -> bool:
        projection = self._projection_for_task_id(task_id)
        if projection is None or _crosscut_to_hermes_status(projection.status) != "running":
            return False
        claim_token = getattr(projection, "claim_token", None)
        if claim_token and hasattr(self.repo, "release_work_item_claim"):
            self.repo.release_work_item_claim(projection.mapping_id, claim_token, reason or "reclaimed")
        else:
            self.repo.update_work_item_status(projection.work_item_id, "ready")
            self.repo.update_execution_mapping_status(projection.mapping_id, "ready", sync_state="released")
        self.repo.create_or_update_execution_run(_run_request(projection.mapping_id, "done", outcome="reclaimed", summary=reason, profile=projection.assignee_hint))
        self.repo.create_event(_work_event_request("hermes_task_reclaimed", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id, "reason": reason}))
        return True

    def heartbeat_task(self, task_id: str, *, note: Optional[str], expected_run_id: Optional[int]) -> bool:
        del expected_run_id
        projection = self._projection_for_task_id(task_id)
        if projection is None:
            return False
        claim_token = getattr(projection, "claim_token", None)
        if claim_token and hasattr(self.repo, "heartbeat_work_item_claim"):
            self.repo.heartbeat_work_item_claim(projection.mapping_id, claim_token, note=note)
        self.repo.create_event(_work_event_request("hermes_task_heartbeat", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id, "note": note}))
        return True

    def board_stats(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        by_assignee: dict[str, dict[str, int]] = {}
        oldest_ready = None
        for task in self.list_tasks(assignee=None, status=None, tenant=None, include_archived=False):
            by_status[task.status] = by_status.get(task.status, 0) + 1
            if task.assignee:
                counts = by_assignee.setdefault(task.assignee, {})
                counts[task.status] = counts.get(task.status, 0) + 1
        return {"by_status": by_status, "by_assignee": by_assignee, "oldest_ready_age_seconds": oldest_ready}

    def add_comment(self, task_id: str, author: str, body: str) -> Any:
        projection = self._require_projection(task_id)
        return self.repo.create_comment(_comment_request(projection.work_item_id, author, body))

    def complete_task(self, task_id: str, *, result: Optional[str], summary: Optional[str], metadata: Optional[dict], expected_run_id: Optional[int], created_cards: Optional[list[str]] = None) -> bool:
        del expected_run_id
        projection = self._projection_for_task_id(task_id)
        if projection is None or _crosscut_to_hermes_status(projection.status) in {"done", "archived"}:
            return False
        if created_cards:
            self.repo.create_event(_work_event_request("hermes_task_created_cards_reported", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id, "created_cards": created_cards}))
        self.repo.update_work_item_status(projection.work_item_id, "done")
        self.repo.update_execution_mapping_status(projection.mapping_id, "done", sync_state="released")
        self.repo.create_or_update_execution_run(_run_request(projection.mapping_id, "done", outcome="done", summary=summary or result, metadata=metadata, profile=projection.assignee_hint))
        self.repo.create_event(_work_event_request("hermes_task_completed", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id}))
        return True

    def edit_completed_task_result(self, task_id: str, *, result: Optional[str], summary: Optional[str], metadata: Optional[dict]) -> bool:
        projection = self._projection_for_task_id(task_id)
        if projection is None or _crosscut_to_hermes_status(projection.status) != "done":
            return False
        self.repo.create_or_update_execution_run(_run_request(projection.mapping_id, "done", outcome="done", summary=summary or result, metadata=metadata, profile=projection.assignee_hint))
        return True

    def block_task(self, task_id: str, *, reason: Optional[str], expected_run_id: Optional[int]) -> bool:
        del expected_run_id
        projection = self._projection_for_task_id(task_id)
        if projection is None or _crosscut_to_hermes_status(projection.status) in {"done", "archived"}:
            return False
        self.repo.update_work_item_status(projection.work_item_id, "blocked")
        self.repo.update_execution_mapping_status(projection.mapping_id, "blocked", sync_state="active")
        self.repo.create_or_update_execution_run(_run_request(projection.mapping_id, "done", outcome="blocked", summary=reason, profile=projection.assignee_hint))
        self.repo.create_event(_work_event_request("hermes_task_blocked", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id, "reason": reason}))
        return True

    def attach_worker_handoff(self, task_id: str, *, marker: str, text: str, final_response_chars: int, run_id: Optional[int] = None) -> bool:
        del run_id
        projection = self._projection_for_task_id(task_id)
        if projection is None:
            return False
        terminal_status = _crosscut_to_hermes_status(projection.status)
        if terminal_status not in {"done", "blocked"}:
            return False
        block = (text or "").strip()
        if not block:
            return False
        capture = _worker_handoff_capture(marker=marker, text=block, final_response_chars=final_response_chars)
        details = self.repo.show_execution_task_details(projection.mapping_id)
        runs = list(getattr(details, "runs", []) or [])
        run = next(
            (
                r for r in reversed(runs)
                if str(getattr(r, "status", "") or "") == "done"
                or str(getattr(r, "outcome", "") or "") in {"done", "completed", "blocked"}
            ),
            None,
        )
        if run is None:
            return False
        metadata = dict(getattr(run, "metadata", None) or {})
        existing = metadata.get("worker_handoff")
        if isinstance(existing, dict) and existing.get("sha256") == capture["sha256"]:
            return False
        metadata["worker_handoff"] = capture
        self.repo.create_or_update_execution_run(
            _crosscut_request(
                "CreateWorkExecutionRunRequest",
                id=run.id,
                mapping_id=projection.mapping_id,
                external_run_id=getattr(run, "external_run_id", None),
                profile=getattr(run, "profile", None),
                status=getattr(run, "status", "done"),
                outcome=getattr(run, "outcome", None),
                summary=getattr(run, "summary", None),
                error=getattr(run, "error", None),
                started_at=getattr(run, "started_at", None),
                ended_at=getattr(run, "ended_at", None),
                metadata=metadata,
            )
        )
        self.repo.create_event(
            _work_event_request(
                "hermes_task_worker_handoff_attached",
                projection.work_item_id,
                {
                    "task_id": task_id,
                    "mapping_id": projection.mapping_id,
                    "terminal_status": terminal_status,
                    "worker_handoff": capture,
                },
            )
        )
        return True

    def unblock_task(self, task_id: str) -> bool:
        projection = self._projection_for_task_id(task_id)
        if projection is None or _crosscut_to_hermes_status(projection.status) != "blocked":
            return False
        self.repo.update_work_item_status(projection.work_item_id, "ready")
        self.repo.update_execution_mapping_status(projection.mapping_id, "ready", sync_state="queued")
        self.repo.create_event(_work_event_request("hermes_task_unblocked", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id}))
        return True

    def archive_task(self, task_id: str) -> bool:
        projection = self._projection_for_task_id(task_id)
        if projection is None:
            return False
        self.repo.archive_work_item(projection.work_item_id, reason="archived via Hermes Kanban CrossCut provider")
        self.repo.update_execution_mapping_status(projection.mapping_id, "archived", sync_state="archived")
        self.repo.create_event(_work_event_request("hermes_task_archived", projection.work_item_id, {"task_id": task_id, "mapping_id": projection.mapping_id}))
        return True

    def list_boards(self, *, include_archived: bool = False) -> list[dict]:
        boards = []
        for b in self.repo.list_execution_board_projections(backend_id=self.backend_id, include_archived=include_archived):
            slug = b.external_board or self.board
            boards.append({
                "slug": slug,
                "name": b.backend_name,
                "path": f"crosscut://{b.backend_id}/{slug}",
                "archived": bool(getattr(b, "archived", False)),
                "task_count": int(getattr(b, "task_count", 0)),
                "active_count": int(getattr(b, "active_count", 0)),
                "external_board": b.external_board,
            })
        if not boards:
            boards.append({"slug": self.board, "name": "CrossCut", "path": f"crosscut://{self.backend_id}/{self.board}", "archived": False, "task_count": 0, "active_count": 0, "external_board": self.board})
        return boards

    def get_current_board(self) -> str:
        return self.board

    def list_profiles_on_disk(self) -> list[str]:
        return kb.list_profiles_on_disk()

    def _ensure_backend(self) -> None:
        self.repo.create_execution_backend(
            _crosscut_request(
                "CreateExecutionBackendRequest",
                id=self.backend_id,
                backend_type="hermes_kanban_crosscut_provider",
                name="Hermes Kanban CrossCut Provider",
                config={"board": self.board, "provider": "crosscut", "prototype": True},
                enabled=True,
            )
        )

    def _projection_for_task_id(self, task_id: str) -> Any | None:
        for projection in self.repo.list_execution_task_projections(backend_id=self.backend_id, external_board=self.board, include_archived=True):
            if projection.external_task_id == task_id or projection.mapping_id == task_id:
                return projection
        return None

    def _require_projection(self, task_id: str) -> Any:
        projection = self._projection_for_task_id(task_id)
        if projection is None:
            raise ValueError(f"no such task: {task_id}")
        return projection

    def _task_id_for_work_item_id(self, work_item_id: str) -> str | None:
        for projection in self.repo.list_execution_task_projections(backend_id=self.backend_id, external_board=self.board, include_archived=True):
            if projection.work_item_id == work_item_id:
                return projection.external_task_id
        return None

    def _create_link(self, parent_work_item_id: str, child_work_item_id: str) -> None:
        try:
            self.repo.create_item_link(_crosscut_request("CreateWorkItemLinkRequest", parent_id=parent_work_item_id, child_id=child_work_item_id, link_type="blocks"))
        except Exception as exc:
            # Duplicate links are harmless for provider prototyping; surface all
            # other validation errors.
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise

    def _task_from_projection(self, p: Any) -> kb.Task:
        status = _crosscut_to_hermes_status(p.status)
        now = int(time.time())
        return kb.Task(
            id=p.external_task_id,
            title=p.title,
            body=p.body,
            assignee=p.assignee_hint,
            status=status,
            priority=int(p.priority or 0),
            created_by="crosscut",
            created_at=now,
            started_at=now if status == "running" else None,
            completed_at=now if status == "done" else None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=p.claim_token,
            claim_expires=_to_epoch(p.lease_expires_at),
            tenant=None,
            last_heartbeat_at=_to_epoch(p.last_heartbeat_at),
        )

    def _comment_from_crosscut(self, c: Any) -> kb.Comment:
        return kb.Comment(id=int(c.id), task_id=self._task_id_for_work_item_id(c.work_item_id) or c.work_item_id, author=c.author, body=c.body, created_at=0)

    def _event_from_crosscut(self, e: Any, task_id: str) -> kb.Event:
        return kb.Event(id=int(e.id), task_id=task_id, kind=e.event_type, payload=e.payload, created_at=0, run_id=None)

    def _run_from_crosscut(self, r: Any, numeric_id: int, task_id: str) -> kb.Run:
        started = _to_epoch(r.started_at) or int(time.time())
        ended = _to_epoch(r.ended_at)
        return kb.Run(
            id=numeric_id,
            task_id=task_id,
            profile=r.profile,
            step_key=None,
            status=r.status,
            claim_lock=None,
            claim_expires=None,
            worker_pid=None,
            max_runtime_seconds=None,
            last_heartbeat_at=None,
            started_at=started,
            ended_at=ended,
            outcome=r.outcome,
            summary=r.summary,
            metadata=getattr(r, "metadata", None),
            error=r.error,
        )


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _to_epoch(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    return None


def _crosscut_to_hermes_status(status: Optional[str]) -> str:
    return {
        "planned": "todo",
        "ready": "ready",
        "delegated": "running",
        "running": "running",
        "blocked": "blocked",
        "done": "done",
        "cancelled": "blocked",
        "archived": "archived",
    }.get(status or "", status or "todo")


def _hermes_to_crosscut_status(status: str) -> str:
    return {
        "todo": "planned",
        "ready": "ready",
        "running": "running",
        "blocked": "blocked",
        "done": "done",
        "archived": "archived",
        "triage": "planned",
    }.get(status, status)


def _work_event_request(event_type: str, work_item_id: str, payload: dict[str, Any]) -> Any:
    return _crosscut_request("CreateWorkEventRequest", event_type=event_type, work_item_id=work_item_id, source="hermes-kanban-crosscut", payload=payload)


def _worker_handoff_capture(*, marker: str, text: str, final_response_chars: int) -> dict[str, Any]:
    return {
        "schema": "crosscut-worker-handoff-capture.v1",
        "source": "final_assistant_message",
        "marker": marker,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "captured_at": int(time.time()),
        "final_response_chars": int(final_response_chars),
    }


def _comment_request(work_item_id: str, author: str, body: str) -> Any:
    return _crosscut_request("CreateWorkCommentRequest", work_item_id=work_item_id, author=author, body=body, source="hermes-kanban-crosscut")


def _run_request(mapping_id: str, status: str, *, outcome: Optional[str] = None, summary: Optional[str] = None, error: Optional[str] = None, metadata: Optional[dict] = None, profile: Optional[str] = None) -> Any:
    return _crosscut_request(
        "CreateWorkExecutionRunRequest",
        id=f"wer-hermes-{_short_hash(mapping_id + ':' + (outcome or status))}",
        mapping_id=mapping_id,
        status=status,
        external_run_id=None,
        profile=profile,
        outcome=outcome,
        summary=summary,
        error=error,
        started_at=None,
        ended_at=None if status == "running" else None,
        metadata=metadata,
    )


def _crosscut_request(class_name: str, **kwargs: Any) -> Any:
    """Build a CrossCut repository request object.

    The live Postgres repository only relies on attributes, so tests and lean
    Hermes venvs can use a ``SimpleNamespace`` fallback when optional CrossCut
    dependencies are not installed.  In a full CrossCut environment this returns
    the real dataclass for nicer type/validation behavior.
    """

    try:
        from crosscut import work_repository  # type: ignore
        cls = getattr(work_repository, class_name)
    except Exception:
        return SimpleNamespace(**kwargs)
    return cls(**kwargs)


_PROVIDER: KanbanCliProvider | None = None
_PROVIDER_OVERRIDE: KanbanCliProvider | None = None


def get_kanban_cli_provider() -> KanbanCliProvider:
    """Return the active core CLI provider."""

    global _PROVIDER
    if _PROVIDER_OVERRIDE is not None:
        return _PROVIDER_OVERRIDE
    requested = os.getenv("HERMES_KANBAN_PROVIDER", "sqlite").strip().lower()
    if _PROVIDER is None or (requested == "crosscut" and not isinstance(_PROVIDER, CrossCutKanbanCliProvider)) or (requested != "crosscut" and not isinstance(_PROVIDER, SqliteKanbanCliProvider)):
        _PROVIDER = CrossCutKanbanCliProvider() if requested == "crosscut" else SqliteKanbanCliProvider()
    return _PROVIDER


def set_kanban_cli_provider(provider: KanbanCliProvider) -> None:
    """Test hook for swapping the core CLI provider."""

    global _PROVIDER_OVERRIDE
    _PROVIDER_OVERRIDE = provider


def clear_kanban_cli_provider_override() -> None:
    """Clear a provider override installed by tests."""

    global _PROVIDER_OVERRIDE
    _PROVIDER_OVERRIDE = None
