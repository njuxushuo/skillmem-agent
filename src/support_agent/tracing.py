"""Local, opt-in observation. Never changes tool results or retries business actions."""

from __future__ import annotations

import json
import os
import re
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage, message_to_dict
from langgraph.errors import GraphInterrupt


def private_directory(path: Path) -> Path:
    """Keep repository output under ignored .local; allow external temporary directories."""
    path = path.resolve()
    root = Path(__file__).resolve().parents[2]
    if path.is_relative_to(root) and not path.is_relative_to(root / ".local"):
        raise ValueError("Raw evaluation output inside this repository must be under .local/")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def usage_of(message) -> dict:
    """Prefer normalized usage, but do not accept SDK zero defaults for absent wire fields."""
    metadata = message.response_metadata
    raw = metadata.get("token_usage")
    normalized = message.usage_metadata or {}
    result = {}
    for name, wire in [
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ]:
        value = normalized.get(name)
        if "token_usage" in metadata and (not isinstance(raw, dict) or raw.get(wire) is None):
            value = None
        if value is None and isinstance(raw, dict):
            value = raw.get(wire)
        result[name] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return {**result, "raw": raw, "normalized": normalized}


class TraceRecorder(BaseCallbackHandler):
    """One recorder per task. Callbacks cover logical model calls, not SDK HTTP retries."""

    run_inline = True

    def __init__(self, path: Path, *, run_id: str, thread_id: str, source: str, secrets=()) -> None:
        private_directory(path.parent)
        self.path, self.run_id, self.thread_id, self.source = path, run_id, thread_id, source
        self.secrets = tuple(s for s in secrets if s)
        self.events: list[dict] = []
        self.write_errors: list[str] = []
        self._lock = threading.RLock()
        self._starts: dict[str, float] = {}
        self._tool_context = ContextVar(f"trace_tool_{run_id}", default=None)
        self._observed: set[str] = set()
        self._file = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")

    def clean(self, value):
        if isinstance(value, dict):
            return {
                str(k): "[REDACTED]"
                if re.search(r"api[_-]?key|authorization|password|secret|access_token", str(k), re.I)
                else self.clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
            return re.sub(r"Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value, flags=re.I)
        return value

    def emit(self, event: str, **fields) -> None:
        with self._lock:
            row = self.clean(
                {
                    "run_id": self.run_id,
                    "thread_id": self.thread_id,
                    "step": len(self.events) + 1,
                    "active_skill": None,
                    "time": datetime.now(UTC).isoformat(),
                    "event": event,
                    **fields,
                }
            )
            self.events.append(row)
            try:
                self._file.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
                self._file.flush()
            except (OSError, ValueError) as exc:
                # Observation failure must not replay or cancel a business mutation.
                self.write_errors.append(type(exc).__name__)

    def close(self) -> None:
        self._file.close()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._starts[str(run_id)] = perf_counter()
        self.emit(
            "model.request",
            call_id=str(run_id),
            parameters={
                k: v
                for k, v in kwargs.get("invocation_params", {}).items()
                if k
                in {
                    "tools",
                    "tool_choice",
                    "model",
                    "temperature",
                    "max_tokens",
                    "max_completion_tokens",
                    "extra_body",
                    "stream",
                    "stop",
                }
            },
            messages=[[message_to_dict(m) for m in group] for group in messages],
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = response.generations[0][0].message
        self.emit(
            "model.response",
            call_id=str(run_id),
            message=message_to_dict(message),
            usage=usage_of(message),
            usage_source=self.source,
            latency_ms=self._elapsed(run_id),
        )
        for call in [*message.tool_calls, *message.invalid_tool_calls]:
            self.emit(
                "tool.proposed",
                tool_call_id=call["id"],
                tool_name=call["name"],
                args=call["args"],
                protocol_valid=call["type"] != "invalid_tool_call",
            )

    def on_llm_error(self, error, *, run_id, **kwargs):
        category = "api_error"
        cause, seen = error, set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            category = getattr(cause, "failure_category", category)
            cause = cause.__cause__
        self.emit(
            "model.error",
            failure_category=category,
            http_status=getattr(error, "status_code", None),
            call_id=str(run_id),
            error=type(error).__name__,
            detail=str(error),
            usage=None,
            usage_source=self.source,
            latency_ms=self._elapsed(run_id),
        )

    def on_tool_start(self, serialized, input_str, *, run_id, tool_call_id=None, **kwargs):
        context = {"invocation_id": str(run_id), "tool_call_id": tool_call_id, "tool_name": serialized.get("name")}
        self._tool_context.set(context)
        self._starts[str(run_id)] = perf_counter()
        self.emit("tool.attempt", **context)

    def instrument_tools(self, tools):
        """Count entry to actual tool functions AFTER schema validation. Keep schemas intact."""

        def wrap(func):
            @wraps(func)
            def observed(*args, **kwargs):
                context = self._tool_context.get() or {}
                self.emit("tool.execution_start", **context)
                try:
                    result = func(*args, **kwargs)
                except GraphInterrupt:
                    self.emit("tool.paused", **context)
                    raise
                except Exception as exc:
                    self.emit("tool.execution_error", **context, error=type(exc).__name__, detail=str(exc))
                    raise
                try:
                    payload = json.loads(result)
                except (ValueError, TypeError):
                    payload = {}
                payload = payload if isinstance(payload, dict) else {}
                rejected = (
                    bool(payload.get("error"))
                    or payload.get("status") in {"rejected", "denied"}
                    or payload.get("eligible") is False
                )
                self.emit("tool.execution_end", **context, result=result, outcome="rejected" if rejected else "success")
                return result

            return observed

        return [tool.model_copy(update={"func": wrap(tool.func)}) for tool in tools]

    def on_tool_end(self, output, *, run_id, **kwargs):
        self.emit(
            "tool.return",
            invocation_id=str(run_id),
            tool_call_id=getattr(output, "tool_call_id", None),
            latency_ms=self._elapsed(run_id),
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.emit(
            "tool.interrupt" if isinstance(error, GraphInterrupt) else "tool.error",
            invocation_id=str(run_id),
            error=type(error).__name__,
            detail=str(error),
            latency_ms=self._elapsed(run_id),
        )

    def on_chain_end(self, outputs, **kwargs):
        # Also capture ToolNode-generated validation/unknown-tool errors (no function execution).
        messages = outputs.get("messages", []) if isinstance(outputs, dict) else []
        if not isinstance(messages, list):
            return
        with self._lock:
            for m in messages:
                if isinstance(m, ToolMessage) and m.tool_call_id not in self._observed:
                    self._observed.add(m.tool_call_id)
                    self.emit(
                        "tool.observation",
                        tool_call_id=m.tool_call_id,
                        tool_name=m.name,
                        status=m.status,
                        result=m.content,
                    )

    def on_chain_error(self, error, *, parent_run_id=None, **kwargs):
        if parent_run_id is None:
            self.emit("graph.error", error=type(error).__name__, detail=str(error))

    def _elapsed(self, run_id) -> float | None:
        start = self._starts.pop(str(run_id), None)
        return (perf_counter() - start) * 1000 if start is not None else None

    def statistics(self) -> dict:
        def count(event):
            return sum(e["event"] == event for e in self.events)

        responses = [e for e in self.events if e["event"] == "model.response"]
        requests = count("model.request")
        stats = {
            "model_calls": requests,
            "model_response_count": len(responses),
            "model_error_count": count("model.error"),
            "tool_proposed_count": count("tool.proposed"),
            "tool_executed_count": count("tool.execution_start"),
            "tool_success_count": sum(
                e["event"] == "tool.execution_end" and e["outcome"] == "success" for e in self.events
            ),
            "tool_rejected_count": sum(
                e["event"] == "tool.execution_end" and e["outcome"] == "rejected" for e in self.events
            ),
            "tool_error_count": count("tool.error"),
            "tool_execution_error_count": count("tool.execution_error"),
            "tool_protocol_error_count": sum(
                e["event"] == "tool.observation" and e["status"] == "error" for e in self.events
            ),
            "tool_reentry_count": count("tool.execution_start")
            - len({e.get("tool_call_id") for e in self.events if e["event"] == "tool.execution_start"}),
            "tool_paused_count": count("tool.paused"),
            "skill_switch_count": 0,
            "usage_source": self.source,
            "cost": None,
        }
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            values = [e["usage"][field] for e in responses if e["usage"][field] is not None]
            stats[field] = sum(values) if len(values) == requests and requests else None
            stats[f"known_{field}"] = sum(values)
            stats[f"unknown_{field}_calls"] = requests - len(values)
        return stats
