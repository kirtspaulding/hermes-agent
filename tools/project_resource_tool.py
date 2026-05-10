"""Project resource tool for family-chat Discord project threads."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error

_CROSSCUT_ROOT = Path("/home/vice/CrossCut/crosscut")
_PROJECTS_ROOT = Path("/home/vice/CrossCut/family-chat-projects")


def _ensure_crosscut_import_path() -> None:
    root = str(_CROSSCUT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def check_project_resource_requirements() -> bool:
    if os.getenv("HERMES_PROFILE", "") != "family-chat":
        return False
    return (_CROSSCUT_ROOT / "crosscut" / "family_chat_projects.py").exists() and (_PROJECTS_ROOT / "index.json").exists()


def project_resource_tool(args: dict[str, Any], **_kw: Any) -> str:
    action = str(args.get("action") or "list").strip().lower()
    query = _string_or_none(args.get("query"))
    project = _string_or_none(args.get("project")) or _string_or_none(_session_value("HERMES_SESSION_PROJECT_SLUG"))
    resource_ids = args.get("resource_ids") or []
    if isinstance(resource_ids, str):
        resource_ids = [resource_ids]
    resource_ids = [str(item).strip() for item in resource_ids if str(item).strip()]

    _ensure_crosscut_import_path()
    try:
        from crosscut.family_chat_projects import (
            list_project_resources,
            load_project_registry,
            plan_project_resource_send,
        )
    except Exception as exc:
        return tool_error(f"CrossCut project registry unavailable: {exc}")

    if action == "list":
        try:
            return json.dumps(list_project_resources(query, project=project, root=_PROJECTS_ROOT), ensure_ascii=False)
        except Exception as exc:
            return tool_error(f"Project resource list failed: {exc}")

    if action != "send":
        return tool_error("action must be 'list' or 'send'")

    if not query and not resource_ids:
        return tool_error("project_resource send requires query or resource_ids")

    try:
        plan = plan_project_resource_send(
            query=query,
            project=project,
            resource_ids=resource_ids,
            root=_PROJECTS_ROOT,
        )
        registry = load_project_registry(_PROJECTS_ROOT)
    except Exception as exc:
        return tool_error(f"Project resource send planning failed: {exc}")

    if not plan.should_send:
        return json.dumps(
            {
                "status": plan.status,
                "reason": plan.reason,
                "total_size_bytes": plan.total_size_bytes,
                "resources": [resource.to_discord_dict() for resource in plan.resources[:12]],
            },
            ensure_ascii=False,
        )

    target = _resolve_target(args.get("target"))
    if not target:
        return tool_error("project_resource send can only auto-target the current Discord surface")

    source_links = _unique(
        resource.source_message_url for resource in plan.resources if resource.source_message_url
    )
    labels = [resource.display_title for resource in plan.resources]
    media_lines = []
    for resource in plan.resources:
        path = resource.absolute_path(registry.projects_root)
        if path is not None:
            media_lines.append(f"MEDIA:{path}")
    text_lines = [f"Project resources: {', '.join(labels)}"]
    if source_links:
        text_lines.extend(f"Source: {url}" for url in source_links[:5])
    message = "\n".join(text_lines + media_lines)

    try:
        from tools.send_message_tool import _handle_send
        send_result = json.loads(_handle_send({"target": target, "message": message}))
    except Exception as exc:
        return tool_error(f"Project resource delivery failed: {exc}")

    return json.dumps(
        {
            "status": "sent" if send_result.get("success") else "error",
            "target": "current",
            "resource_count": len(plan.resources),
            "resources": [resource.to_discord_dict() for resource in plan.resources],
            "source_message_urls": source_links,
            "delivery": send_result,
        },
        ensure_ascii=False,
    )


def _resolve_target(value: Any) -> str | None:
    raw = _string_or_none(value)
    if raw and raw != "current":
        return raw
    platform = _session_value("HERMES_SESSION_PLATFORM")
    if platform != "discord":
        return None
    chat_id = _session_value("HERMES_SESSION_CHAT_ID")
    parent_chat_id = _session_value("HERMES_SESSION_PARENT_CHAT_ID")
    thread_id = _session_value("HERMES_SESSION_THREAD_ID")
    if thread_id and parent_chat_id:
        return f"discord:{parent_chat_id}:{thread_id}"
    if chat_id:
        return f"discord:{chat_id}"
    return None


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "")
    except Exception:
        return os.getenv(name, "")


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


PROJECT_RESOURCE_SCHEMA = {
    "name": "project_resource",
    "description": (
        "List or send reviewed family-chat project resources. Use this for questions like "
        "'where is the tractor wiring diagram?' or 'send the Third Coast packets'. "
        "Outputs Discord-facing resource labels and source-message links, not local paths."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "send"],
                "description": "Use list to inspect matching project resources; use send to attach reviewed sendable files.",
            },
            "query": {
                "type": "string",
                "description": "Resource or project query, for example 'wiring diagram' or 'Third Coast packets'.",
            },
            "resource_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact resource IDs to send after a list call.",
            },
            "project": {
                "type": "string",
                "description": "Optional project slug or alias. Defaults to the current registered project thread when available.",
            },
            "target": {
                "type": "string",
                "description": "Delivery target. Omit or use 'current' to send back to the current Discord surface.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="project_resource",
    toolset="project_resource",
    schema=PROJECT_RESOURCE_SCHEMA,
    handler=lambda args, **kw: project_resource_tool(args, **kw),
    check_fn=check_project_resource_requirements,
    description="List or send family-chat project resources.",
)
