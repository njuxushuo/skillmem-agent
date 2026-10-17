"""Serializable task data; identity/authorization remain in trusted RunnableConfig."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import MessagesState


class TaskMemory(TypedDict):
    task_id: str
    goal: str | None
    active_order_id: str | None
    focus_source: str | None
    constraints: list[dict]
    verified_orders: dict[str, dict]
    eligibility: dict | None
    last_tool_outcomes: list[dict]
    pending_action: dict | None
    status: str


class SupportState(MessagesState):
    """New fields default at read time, so completed B1 checkpoints need no migration."""

    schema_version: int
    stop_reason: str | None
    active_skill: str | None
    task: TaskMemory
    context_summary: str | None
    observed_call_ids: list[str]
    observed_user_ids: list[str]
    observed_result_hashes: dict[str, str]
    context_diagnostics: dict


def empty_task(task_id: str = "session") -> TaskMemory:
    return {
        "task_id": task_id,
        "goal": None,
        "active_order_id": None,
        "focus_source": None,
        "constraints": [],
        "verified_orders": {},
        "eligibility": None,
        "last_tool_outcomes": [],
        "pending_action": None,
        "status": "in_progress",
    }
