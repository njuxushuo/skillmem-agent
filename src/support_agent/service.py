"""Runs conversations, enforces conversation ownership and manages refund approvals."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from support_agent.context import STRATEGIES, ContextBudget
from support_agent.execution import ExecutionControl, ExecutionLimits, database_lock
from support_agent.graph import build_graph
from support_agent.models import get_model
from support_agent.policies_kb import PolicyBase
from support_agent.policy import PolicyConfig
from support_agent.skills import load_skills
from support_agent.store import Store
from support_agent.tools import make_tools
from support_agent.tracing import TraceRecorder

MAX_STEPS_PER_TURN = 16  # model + tool steps; stops a confused model from looping forever


def default_data_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "store.json").is_file():
            return parent / "data"
    raise FileNotFoundError("data/store.json not found")


class ConversationRegistry:
    """Which customer owns each conversation, and which refunds are waiting for approval.

    Stored in SQLite next to LangGraph's checkpoints, so a pending approval survives a restart.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS conversations (thread_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS pending_approvals (thread_id TEXT PRIMARY KEY, details TEXT NOT NULL)"
            )

    def claim(self, thread_id: str, customer_id: str) -> str:
        """Records the owner of a new conversation and returns the owner (which may be someone else)."""
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO conversations VALUES (?, ?)", (thread_id, customer_id))
            return self._db.execute(
                "SELECT customer_id FROM conversations WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]

    def owner(self, thread_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT customer_id FROM conversations WHERE thread_id = ?", (thread_id,)).fetchone()
        return row[0] if row else None

    def add_pending(self, thread_id: str, details: dict) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO pending_approvals VALUES (?, ?)", (thread_id, json.dumps(details)))

    def is_pending(self, thread_id: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM pending_approvals WHERE thread_id = ?", (thread_id,)).fetchone()
                is not None
            )

    def take_pending(self, thread_id: str) -> dict | None:
        """Removes and returns a pending approval, so two reviewers can't both resolve it."""
        with self._lock, self._db:
            row = self._db.execute("SELECT details FROM pending_approvals WHERE thread_id = ?", (thread_id,)).fetchone()
            if row:
                self._db.execute("DELETE FROM pending_approvals WHERE thread_id = ?", (thread_id,))
        return json.loads(row[0]) if row else None

    def all_pending(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT thread_id, details FROM pending_approvals ORDER BY rowid").fetchall()
        return [{"thread_id": t, **json.loads(d)} for t, d in rows]

    def setup_journal(self):
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS approval_journal "
                "(approval_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
                "details TEXT NOT NULL, state TEXT NOT NULL, decision TEXT, result TEXT)"
            )

    def journal_pending(self, thread_id, approval_id, details):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO approval_journal VALUES (?, ?, ?, 'pending', NULL, NULL)",
                (approval_id, thread_id, json.dumps(details)),
            )

    def approval(self, thread_id, approval_id=None):
        with self._lock:
            row = self._db.execute(
                "SELECT approval_id, details, state, decision, result FROM approval_journal "
                "WHERE thread_id=? AND (? IS NULL OR approval_id=?) ORDER BY rowid DESC LIMIT 1",
                (thread_id, approval_id, approval_id),
            ).fetchone()
        if not row:
            return None
        return dict(
            approval_id=row[0],
            details=json.loads(row[1]),
            state=row[2],
            decision=json.loads(row[3]) if row[3] else None,
            result=json.loads(row[4]) if row[4] else None,
        )

    def approval_count(self, thread_id):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM approval_journal WHERE thread_id=?", (thread_id,)).fetchone()[
                0
            ]

    def decide(self, approval_id, decision):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE approval_journal SET state='resolving', decision=? WHERE approval_id=?",
                (json.dumps(decision), approval_id),
            )

    def finish(self, approval_id, result=None):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE approval_journal SET state=?, result=? WHERE approval_id=?",
                ("resolved" if result else "retryable", json.dumps(result) if result else None, approval_id),
            )

    def conversations(self):
        with self._lock:
            return self._db.execute("SELECT thread_id,customer_id FROM conversations").fetchall()


class ConversationError(Exception):
    """The request doesn't fit the conversation (wrong customer, waiting for approval, unknown thread)."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ChatResult:
    thread_id: str
    status: str  # answered | awaiting_approval | failed
    reply: str
    tools_used: list[str] = field(default_factory=list)
    approval: dict | None = None


class SupportService:
    def __init__(
        self,
        model: BaseChatModel | None = None,
        store: Store | None = None,
        policy: PolicyConfig | None = None,
        data_dir: Path | None = None,
        db_path: str | None = None,
        trace: TraceRecorder | None = None,
        variant: str | None = None,
        limits: ExecutionLimits | None = None,
        context_strategy: str | None = None,
        context_budget: ContextBudget | None = None,
        full_sop: bool = False,
    ) -> None:
        """B1 db_path persists business, receipts, budgets, approvals and checkpoints; None is ephemeral."""
        with database_lock(db_path, "initialize"):
            self.variant = variant or os.getenv("SKILLMEM_VARIANT", "B1")
            if self.variant not in {"B0-compatible", "B1", "M", "MS"}:
                raise ValueError("Only B0-compatible, B1, M and MS are implemented")
            if type(full_sop) is not bool or (full_sop and self.variant not in {"B1", "M"}):
                raise ValueError("full_sop is a boolean control for B1 or M only")
            self.full_sop = full_sop
            self.skill_enabled = self.variant == "MS"
            self.memory_enabled = self.variant in {"M", "MS"}
            self.context_strategy = None
            self.context_budget = None
            if db_path:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            data_dir = data_dir or default_data_dir()
            skills = load_skills(data_dir) if self.skill_enabled or self.full_sop else None
            self.store = store or Store.load(data_dir / "store.json")
            self.policy = policy or PolicyConfig()
            self.control = None
            if self.variant != "B0-compatible":
                self.store.persist(db_path)
                self.control = ExecutionControl(db_path, limits or ExecutionLimits())
            tools = make_tools(
                self.store,
                PolicyBase(data_dir / "policies.md", chinese_aliases=self.variant != "B0-compatible"),
                self.policy,
                variant="B1" if self.control else "B0-compatible",
            )

            self.trace = trace
            if trace is not None:
                tools = trace.instrument_tools(tools)

            connection = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
            checkpointer: BaseCheckpointSaver = SqliteSaver(connection) if db_path else InMemorySaver()
            # Separate registry and saver connections: their transactions must not commit each other.
            self._checkpoint_connection = connection
            registry_connection = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
            self.registry = ConversationRegistry(registry_connection)
            if self.control:
                self.registry.setup_journal()
            elif connection.execute("SELECT 1 FROM sqlite_master WHERE name='business_state'").fetchone():
                registry_connection.close()
                connection.close()
                raise ValueError("B0 must not open a B1 database")
            model = model or get_model()
            if self.control and getattr(model, "max_retries", 0) != 0:
                self.close()
                raise ValueError("B1 requires model max_retries=0 so request budgets count all attempts")
            if self.memory_enabled:
                self.context_strategy = context_strategy or os.getenv("SKILLMEM_CONTEXT_STRATEGY", "structured_memory")
                output_limit = getattr(model, "max_tokens", None) or getattr(model, "max_completion_tokens", None)
                self.context_budget = context_budget or ContextBudget(
                    total_tokens=int(os.getenv("SKILLMEM_CONTEXT_BUDGET", "16384")),
                    output_tokens=output_limit or 1024,
                )
                if self.context_strategy not in STRATEGIES or (
                    output_limit and self.context_budget.output_tokens < output_limit
                ):
                    self.close()
                    raise ValueError("Invalid context strategy or output reserve below model output limit")
            elif context_strategy is not None or context_budget is not None:
                self.close()
                raise ValueError("Context settings require the M or MS variant")
            self.graph = build_graph(
                model,
                tools,
                self.store.customers,
                checkpointer,
                control=self.control,
                trace=trace,
                memory_enabled=self.memory_enabled,
                context_strategy=self.context_strategy,
                context_budget=self.context_budget,
                skills=skills if self.skill_enabled else None,
                full_sop=(
                    "All skills and their business tools are already available in this comparison mode. "
                    "Use business tools directly; ignore SOP instructions to select or switch skills. "
                    "There is no select_skill tool and no selection is required.\n\n"
                    + "\n\n".join(skill.sop for skill in skills.values())
                )
                if self.full_sop
                else "",
            )

            if self.control and db_path:
                # Force SqliteSaver schema/WAL initialization before accepting concurrent requests.
                checkpointer.get_tuple({"configurable": {"thread_id": "__initialize__"}})

    # ---------- customer side ----------

    def chat(self, customer_id: str, message: str, thread_id: str | None = None) -> ChatResult:
        thread_id = thread_id or uuid.uuid4().hex
        with self.control.session(thread_id) if self.control else nullcontext():
            if customer_id not in self.store.customers:
                raise ConversationError("Unknown customer.", 401)
            if self.registry.claim(thread_id, customer_id) != customer_id:
                raise ConversationError("Conversation not found.", 404)
            if self.control:
                self._reconcile(thread_id, customer_id)
                approval = self.registry.approval(thread_id)
                if approval and approval["state"] != "resolved":
                    raise ConversationError("A refund is waiting for approval or recovery.", 409)
                state = self.graph.get_state(self._config(customer_id, thread_id))
                if state.next:
                    raise ConversationError("Previous execution is incomplete; retry_turn before a new message.", 409)
                if state.values.get("stop_reason") == "invalid_tool_call_id":
                    raise ConversationError("Invalid tool protocol; start a new conversation.", 409)
                self.control.start(thread_id)
                return self._run({"messages": [HumanMessage(message)], "stop_reason": None}, customer_id, thread_id)
            if self.registry.is_pending(thread_id):
                raise ConversationError("A refund in this conversation is waiting for approval.", 409)
            return self._run({"messages": [HumanMessage(message)]}, customer_id, thread_id)

    def history(self, customer_id: str, thread_id: str) -> list[dict]:
        if self.registry.owner(thread_id) != customer_id:
            raise ConversationError("Conversation not found.", 404)
        messages = self.graph.get_state(self._config(customer_id, thread_id)).values.get("messages", [])
        return [
            {"role": "customer" if isinstance(m, HumanMessage) else "assistant", "text": str(m.content)}
            for m in messages
            if isinstance(m, HumanMessage | AIMessage) and m.content
        ]

    # ---------- staff side ----------

    def pending_approvals(self) -> list[dict]:
        if not self.control:
            return self.registry.all_pending()
        pending = []
        for thread_id, owner in self.registry.conversations():
            with self.control.session(thread_id):
                self._reconcile(thread_id, owner)
                row = self.registry.approval(thread_id)
                if row and row["state"] != "resolved":
                    pending.append(
                        {
                            "thread_id": thread_id,
                            **row["details"],
                            "state": row["state"],
                            "approval_id": row["approval_id"],
                        }
                    )
        return pending

    def resolve_approval(
        self, thread_id: str, approved: bool, reviewer: str, note: str | None = None, *, approval_id: str | None = None
    ) -> ChatResult:
        if self.control:
            return self._resolve_b1(thread_id, approved, reviewer, note, approval_id)
        details = self.registry.take_pending(thread_id)
        if details is None:
            raise ConversationError("No refund is waiting for approval in this conversation.", 404)
        decision = {"approved": approved, "reviewer": reviewer, "note": note}
        return self._run(Command(resume=decision), details["customer_id"], thread_id)

    # ---------- internals ----------

    def _config(self, customer_id: str, thread_id: str) -> dict:
        return {
            "configurable": {"thread_id": thread_id, "customer_id": customer_id},
            "recursion_limit": (3 if self.memory_enabled else 2) * self.control.limits.model_calls + 4
            if self.control
            else MAX_STEPS_PER_TURN,
        }

    def _run(self, graph_input, customer_id: str, thread_id: str) -> ChatResult:
        config = self._config(customer_id, thread_id)
        if self.control:
            config["configurable"]["deadline"] = time() + self.control.limits.turn_seconds
        if self.trace is not None:
            config["callbacks"] = [self.trace]
        initial_state = self.graph.get_state(config)
        if self.memory_enabled:
            # Trusted projection only; do not update_state on an interrupt, which changes resume scheduling.
            row = self.registry.approval(thread_id)
            if row and row["state"] != "resolved":
                config["configurable"]["pending_approval"] = {
                    **row["details"],
                    "state": row["state"],
                    "approval_id": row["approval_id"],
                }
            elif initial_state.interrupts:
                config["configurable"]["pending_approval"] = {
                    **initial_state.interrupts[0].value,
                    "state": "pending",
                    "approval_id": initial_state.interrupts[0].id,
                }
            if config["configurable"].get("pending_approval"):
                latest_call = next(
                    (
                        (message.id, call)
                        for message in reversed(initial_state.values.get("messages", []))
                        if isinstance(message, AIMessage)
                        for call in message.tool_calls
                        if call["name"] == "request_refund"
                    ),
                    None,
                )
                if latest_call:
                    config["configurable"]["pending_approval"].update(
                        source_message_id=latest_call[0], source_tool_call_id=latest_call[1]["id"]
                    )
        before = len(initial_state.values.get("messages", []))
        try:
            self.graph.invoke(graph_input, config)
        except GraphRecursionError:
            if self.control:
                if self.trace:
                    self.trace.emit("execution.stopped", reason="recursion_limit")
                return ChatResult(thread_id, "failed", "Execution stopped. Please contact a colleague.")
            ticket = self.store.open_ticket(customer_id, f"Agent loop limit reached in conversation {thread_id}")
            return ChatResult(
                thread_id,
                "answered",
                f"Sorry, I'm having trouble with this one. I've passed it to a colleague (ticket {ticket}).",
            )

        state = self.graph.get_state(config)
        new_messages = state.values.get("messages", [])[before:]
        tools_used = [call["name"] for m in new_messages if isinstance(m, AIMessage) for call in m.tool_calls]

        if state.interrupts:
            details = state.interrupts[0].value
            if self.control:
                self.registry.journal_pending(thread_id, state.interrupts[0].id, details)
                details = {**details, "approval_id": state.interrupts[0].id}
            else:
                self.registry.add_pending(thread_id, details)
            reply = (
                f"I've asked a colleague to approve a refund of {details['amount']} {details['currency']} "
                f"for order {details['order_id']}. You'll get an answer here shortly."
            )
            return ChatResult(thread_id, "awaiting_approval", reply, tools_used, details)

        if self.control and state.values.get("stop_reason"):
            return ChatResult(
                thread_id,
                "failed",
                f"Execution stopped: {state.values['stop_reason']}. Please contact a colleague before retrying.",
                tools_used,
            )
        last_ai = next(
            (
                m
                for m in reversed(new_messages or state.values.get("messages", []))
                if isinstance(m, AIMessage) and not m.tool_calls
            ),
            None,
        )
        return ChatResult(thread_id, "answered", str(last_ai.content) if last_ai else "", tools_used)

    def _reconcile(self, thread_id, customer_id):
        """Recover a committed graph interrupt even if saving the registry row failed/crashed."""
        state = self.graph.get_state(self._config(customer_id, thread_id))
        if state.interrupts:
            self.registry.journal_pending(thread_id, state.interrupts[0].id, state.interrupts[0].value)

    def _resolve_b1(self, thread_id, approved, reviewer, note, approval_id):
        if type(approved) is not bool or not reviewer.strip():
            raise ConversationError("A boolean decision and reviewer are required.", 400)
        with self.control.session(thread_id):
            owner = self.registry.owner(thread_id)
            if not owner:
                raise ConversationError("Conversation not found.", 404)
            self._reconcile(thread_id, owner)
            if approval_id is None and self.registry.approval_count(thread_id) > 1:
                raise ConversationError("approval_id is required when a conversation has multiple approvals.", 409)
            row = self.registry.approval(thread_id, approval_id)
            if not row:
                raise ConversationError("No refund is waiting for approval.", 404)
            decision = dict(approved=approved, reviewer=reviewer, note=note)
            if row["decision"] is not None and row["decision"] != decision:
                raise ConversationError("Approval already has a different decision; it cannot be overwritten.", 409)
            if row["state"] == "resolved":
                if self.trace:
                    self.trace.emit("approval.duplicate", approval_id=row["approval_id"])
                return ChatResult(**row["result"])
            self.registry.decide(row["approval_id"], decision)
            if self.trace:
                self.trace.emit("approval.resolving", approval_id=row["approval_id"], decision=decision)
            try:
                state = self.graph.get_state(self._config(owner, thread_id))
                graph_input = Command(resume=decision) if state.interrupts else None
                answer = self._run(graph_input, owner, thread_id)
                if answer.status == "awaiting_approval":
                    raise RuntimeError("approval_resume_did_not_progress")
                self.registry.finish(row["approval_id"], asdict(answer))
                if self.trace:
                    self.trace.emit("approval.resolved", approval_id=row["approval_id"], status=answer.status)
                return answer
            except Exception:
                self.registry.finish(row["approval_id"])
                if self.trace:
                    self.trace.emit("approval.retryable", approval_id=row["approval_id"])
                raise

    def retry_turn(self, customer_id: str, thread_id: str) -> ChatResult:
        """Explicit checkpoint recovery after an infrastructure failure; never restart user input."""
        if not self.control:
            raise ConversationError("Recovery requires B1.", 400)
        with self.control.session(thread_id):
            if self.registry.owner(thread_id) != customer_id:
                raise ConversationError("Conversation not found.", 404)
            self._reconcile(thread_id, customer_id)
            row = self.registry.approval(thread_id)
            if row and row["state"] != "resolved":
                raise ConversationError("Use the saved approval decision to resume.", 409)
            return self._run(None, customer_id, thread_id)

    def close(self):
        self.registry._db.close()
        self._checkpoint_connection.close()
        if self.control:
            self.control.close()
            self.store.close()
