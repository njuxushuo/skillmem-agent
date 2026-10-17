"""Deterministic model-input views; never mutate the canonical checkpoint history."""

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from support_agent.memory import ORDER_FIELDS, OUTCOME_FIELDS, render_task_context
from support_agent.state import empty_task

STRATEGIES = {"full_history", "legacy_trim", "structured_memory"}


@dataclass(frozen=True)
class ContextBudget:
    """Estimated total window, including output reserve and safety margin (not actual usage)."""

    total_tokens: int = 16384
    output_tokens: int = 1024
    safety_margin: int = 512

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (self.total_tokens, self.output_tokens, self.safety_margin)):
            raise ValueError("Context budget values must be nonnegative integers")
        if not self.output_tokens or self.total_tokens <= self.output_tokens + self.safety_margin:
            raise ValueError("Context budget must leave room for input and positive output")


class ContextError(ValueError):
    def __init__(self, reason: str, diagnostics: dict | None = None):
        super().__init__(reason)
        self.reason, self.diagnostics = reason, diagnostics or {}


def estimate_tokens(value) -> int:
    """Conservative UTF-8 byte estimate, not a DeepSeek tokenizer or billing measurement."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return len(text.encode("utf-8"))


def _message_cost(message: BaseMessage) -> int:
    data = {"role": message.type, "content": message.content}
    if isinstance(message, AIMessage) and (message.tool_calls or message.invalid_tool_calls):
        data["tool_calls"] = [*message.tool_calls, *message.invalid_tool_calls]
    if isinstance(message, ToolMessage):
        data.update(tool_call_id=message.tool_call_id, name=message.name)
    return estimate_tokens(data) + 12  # role/framing allowance


def message_groups(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    """Keep every assistant call and all of its tool replies indivisible, or reject malformed history."""
    groups, index = [], 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, ToolMessage):
            raise ContextError("context_tool_protocol_invalid")
        group = [message]
        index += 1
        if isinstance(message, AIMessage) and (message.tool_calls or message.invalid_tool_calls):
            calls = [*message.tool_calls, *message.invalid_tool_calls]
            expected = {call.get("id") for call in calls}
            if None in expected or "" in expected or len(expected) != len(calls):
                raise ContextError("context_tool_protocol_invalid")
            received = set()
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                reply = messages[index]
                if reply.tool_call_id not in expected or reply.tool_call_id in received:
                    raise ContextError("context_tool_protocol_invalid")
                received.add(reply.tool_call_id)
                group.append(reply)
                index += 1
            if received != expected:
                raise ContextError("context_tool_protocol_invalid")
        groups.append(group)
    return groups


def _compact_result(message: BaseMessage) -> BaseMessage:
    if not isinstance(message, ToolMessage) or estimate_tokens(message.content) <= 1024:
        return message
    try:
        data = json.loads(message.content)
    except (ValueError, TypeError):
        return message  # Never fabricate a safe summary of an opaque error/result.
    if not isinstance(data, dict):
        return message
    allowed = ORDER_FIELDS | OUTCOME_FIELDS | {"arrives", "retryable", "section", "text"}
    compact = {k: v for k, v in data.items() if k in allowed}
    if isinstance(data.get("orders"), list):
        compact["orders"] = [
            {k: v for k, v in order.items() if k in allowed} for order in data["orders"] if isinstance(order, dict)
        ]
    if isinstance(data.get("results"), list):
        compact["results"] = data["results"]  # policy text is evidence, not disposable filler
    if not compact:
        return message
    compact["_context_compressed"] = True
    content = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    return message.model_copy(update={"content": content}) if len(content) < len(str(message.content)) else message


def _pending_approval_view(messages: list[BaseMessage], pending: dict | None) -> dict | None:
    """Reconcile the invocation's registry snapshot with durable completed tool evidence.

    The full checkpoint, not the bounded recent-outcomes projection, is authoritative here.
    Proposal message identity disambiguates provider IDs reused in later requests.
    """
    if not pending or not pending.get("source_tool_call_id"):
        return pending
    matching_proposal = False
    for message in messages:
        if isinstance(message, AIMessage):
            matching_proposal = message.id == pending.get("source_message_id") and any(
                call["id"] == pending["source_tool_call_id"]
                and call["name"] == "request_refund"
                and str(call["args"].get("order_id", "")).strip().lstrip("#") == pending.get("order_id")
                for call in message.tool_calls
            )
        elif isinstance(message, HumanMessage):
            matching_proposal = False
        elif (
            matching_proposal
            and isinstance(message, ToolMessage)
            and message.tool_call_id == pending["source_tool_call_id"]
            and message.name in {None, "request_refund"}
            and message.status != "error"
        ):
            try:
                result = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            if (
                isinstance(result, dict)
                and result.get("status") in ("refunded", "denied", "rejected")
                and result.get("order_id", pending["order_id"]) == pending["order_id"]
            ):
                return None
    return pending


def build_model_context(
    state: dict,
    *,
    system_policy: str,
    tool_schemas: list[dict],
    budget: ContextBudget,
    skill_index: str = "",
    active_sop: str = "",
    pending_approval: dict | None = None,
    strategy: str = "structured_memory",
) -> tuple[list[BaseMessage], dict]:
    """Build an estimated-budget view, pinning safety, latest request and newest result group.

    full_history and legacy_trim are controls: neither uses task projection or extra budget trimming.
    They fail explicitly if their selected history exceeds the same configured total window.
    """
    if strategy not in STRATEGIES:
        raise ValueError("Unknown context strategy")
    history = list(state.get("messages", []))
    groups = message_groups(history)
    task = (
        render_task_context(state.get("task") or empty_task(), _pending_approval_view(history, pending_approval))
        if strategy == "structured_memory"
        else ""
    )
    pieces = [system_policy, skill_index, active_sop, task]
    system = SystemMessage("\n\n".join(piece for piece in pieces if piece))
    components = {
        "system": estimate_tokens(system_policy),
        "skill": estimate_tokens(skill_index) + estimate_tokens(active_sop),
        "task": estimate_tokens(task),
        "tools": estimate_tokens(tool_schemas) + 16,
    }
    # Includes delimiters, role wrapper and conservative chat framing, not just text.
    components["framing"] = _message_cost(system) - sum(components[k] for k in ("system", "skill", "task"))
    fixed = sum(components.values())
    available = budget.total_tokens - budget.output_tokens - budget.safety_margin
    selected = list(range(len(groups)))
    compressed = 0
    if strategy == "legacy_trim":
        count = 0
        selected = []
        for i in reversed(range(len(groups))):
            if count + len(groups[i]) > 40:
                break
            selected.insert(0, i)
            count += len(groups[i])
        while selected and not isinstance(groups[selected[0]][0], HumanMessage):
            selected.pop(0)
    elif strategy == "structured_memory":
        projected = []
        for group in groups:
            projected_group = [_compact_result(message) for message in group]
            compressed += sum(a is not b for a, b in zip(group, projected_group, strict=True))
            projected.append(projected_group)
        groups = projected
    costs = [sum(_message_cost(message) for message in group) for group in groups]
    pinned = {len(groups) - 1} if groups else set()
    last_user = next((i for i in reversed(range(len(groups))) if isinstance(groups[i][0], HumanMessage)), None)
    if last_user is not None:
        pinned.add(last_user)
    if strategy == "structured_memory":
        for i in list(selected):
            if fixed + sum(costs[j] for j in selected) <= available:
                break
            if i not in pinned:
                selected.remove(i)
    history_cost = sum(costs[i] for i in selected)
    diagnostics = {
        "version": 1,
        "strategy": strategy,
        "estimator": "utf8_bytes_plus_framing_v1",
        "estimated_input_tokens": fixed + history_cost,
        "actual_input_tokens": None,
        "components": {**components, "history": history_cost},
        "output_reserve": budget.output_tokens,
        "safety_margin": budget.safety_margin,
        "total_budget": budget.total_tokens,
        "dropped_message_groups": len(groups) - len(selected),
        "compressed_tool_results": compressed,
        "original_message_count": len(history),
        "selected_message_count": sum(len(groups[i]) for i in selected),
        "active_skill": state.get("active_skill"),
        "compression_reason": "estimated_budget"
        if len(groups) != len(selected)
        else "long_tool_result"
        if compressed
        else None,
    }
    if fixed + history_cost > available or (last_user is not None and last_user not in selected):
        raise ContextError("context_budget_exceeded", diagnostics)
    return [system, *(message for i in selected for message in groups[i])], diagnostics
