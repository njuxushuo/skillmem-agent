"""Four fixed business skills; selecting one changes context, never business authorization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool

PUBLIC_TOOLS = frozenset({"select_skill", "escalate_to_human"})


@dataclass(frozen=True)
class SkillSpec:
    id: str
    name: str
    description: str
    tool_names: tuple[str, ...]
    sop: str
    sop_path: Path


_DEFINITIONS = (
    (
        "order_shipping",
        "Orders and shipping",
        "Find an order, its status and delivery tracking.",
        ("list_my_orders", "get_order", "track_shipment"),
    ),
    (
        "refund",
        "Refunds",
        "Check eligibility, agree amount and request a refund with human approval when required.",
        ("list_my_orders", "get_order", "check_refund_eligibility", "request_refund"),
    ),
    (
        "cancel",
        "Cancellation",
        "Check an unshipped order and cancel it when the customer requests this.",
        ("list_my_orders", "get_order", "cancel_order"),
    ),
    (
        "policy_support",
        "Policies and human support",
        "Look up shop policies or ask a human colleague for help.",
        ("search_policies", "escalate_to_human"),
    ),
)


def load_skills(data_dir: Path) -> dict[str, SkillSpec]:
    """Only fixed repository-owned paths are read; model text never becomes a filename."""
    registry = {}
    for identity, name, description, names in _DEFINITIONS:
        path = data_dir / "skills" / f"{identity}.md"
        sop = path.read_text(encoding="utf-8")
        if not sop.strip():
            raise ValueError(f"Skill SOP is empty: {identity}")
        registry[identity] = SkillSpec(identity, name, description, names, sop, path)
    return registry


def allowed_tool_names(active_skill: str | None, skills: dict[str, SkillSpec]) -> frozenset[str]:
    """Fail closed to public controls for absent or unrecognized state."""
    skill = skills.get(active_skill)
    return PUBLIC_TOOLS | frozenset(skill.tool_names if skill else ())


def render_skill_index(skills: dict[str, SkillSpec]) -> str:
    return (
        "Available skills (choose with select_skill before using their business tools):\n"
        + "\n".join(f"- {skill.id}: {skill.description}" for skill in skills.values())
        + "\nUse only tools exposed for the current skill. Select another skill when the task changes. "
        "If the user's intention or order is unclear, ask a concise clarification instead of guessing. "
        "A skill selection is not customer consent or staff approval. Select at most one tool per response."
    )


@tool
def select_skill(skill_id: str) -> str:
    """Select one skill: order_shipping, refund, cancel, or policy_support. Does not execute a business action."""
    return json.dumps({"status": "selected", "skill_id": skill_id})
