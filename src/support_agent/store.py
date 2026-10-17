"""The shop's data: customers, orders, refunds, cancellations and escalation tickets.

B0 uses memory. B1 persists a small simulated shop snapshot and operation journal in SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from support_agent.execution import check_deadline


@dataclass
class Order:
    id: str
    customer_id: str
    item: str
    total: Decimal
    currency: str
    status: str  # processing | shipped | delivered | cancelled
    ordered: date
    delivered: date | None = None
    tracking: str | None = None
    eta: date | None = None
    refunded: Decimal = Decimal("0")


@dataclass(frozen=True)
class Refund:
    id: str
    order_id: str
    amount: Decimal
    reason: str
    approved_by: str  # "policy" for automatic refunds, otherwise the reviewer


@dataclass
class Store:
    today: date
    customers: dict[str, dict]
    orders: dict[str, Order]
    refunds: list[Refund] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _db: sqlite3.Connection | None = field(default=None, repr=False)
    _depth: int = field(default=0, repr=False)

    @classmethod
    def load(cls, path: str | Path, today: date | None = None) -> Store:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))

        def as_date(value: str | None) -> date | None:
            return date.fromisoformat(value) if value else None

        orders = {
            o["id"]: Order(
                id=o["id"],
                customer_id=o["customer_id"],
                item=o["item"],
                total=Decimal(o["total"]),
                currency=o["currency"],
                status=o["status"],
                ordered=as_date(o["ordered"]),
                delivered=as_date(o.get("delivered")),
                tracking=o.get("tracking"),
                eta=as_date(o.get("eta")),
            )
            for o in raw["orders"]
        }
        return cls(today=today or date.fromisoformat(raw["demo_today"]), customers=raw["customers"], orders=orders)

    def order_for(self, customer_id: str, order_id: str) -> Order | None:
        """Only returns the order if it belongs to this customer. Other customers' orders don't 'exist'."""
        self.refresh()
        order = self.orders.get(str(order_id).strip().lstrip("#"))
        return order if order and order.customer_id == customer_id else None

    def orders_for(self, customer_id: str) -> list[Order]:
        self.refresh()
        return sorted((o for o in self.orders.values() if o.customer_id == customer_id), key=lambda o: o.ordered)

    def record_refund(self, order: Order, amount: Decimal, reason: str, approved_by: str) -> Refund:
        if self._db is not None and not self._depth:
            with self.transaction():
                return self.record_refund(self.orders[order.id], amount, reason, approved_by)
        with self._lock:
            if order.refunded > 0:
                raise ValueError(f"Order {order.id} has already been refunded.")
            refund = Refund(f"R-{len(self.refunds) + 1:04d}", order.id, amount, reason, approved_by)
            order.refunded = amount
            self.refunds.append(refund)
            return refund

    def cancel(self, order: Order) -> None:
        if self._db is not None and not self._depth:
            with self.transaction():
                return self.cancel(self.orders[order.id])
        with self._lock:
            order.status = "cancelled"

    def open_ticket(self, customer_id: str, summary: str) -> str:
        if self._db is not None and not self._depth:
            with self.transaction():
                return self.open_ticket(customer_id, summary)
        with self._lock:
            ticket_id = f"T-{len(self.tickets) + 1:04d}"
            self.tickets.append({"id": ticket_id, "customer_id": customer_id, "summary": summary})
            return ticket_id

    def _dump(self) -> str:
        return json.dumps(
            {
                "today": self.today,
                "customers": self.customers,
                "orders": {k: asdict(v) for k, v in self.orders.items()},
                "refunds": [asdict(r) for r in self.refunds],
                "tickets": self.tickets,
            },
            default=str,
        )

    def _restore(self, payload: str):
        raw = json.loads(payload)
        self.today = date.fromisoformat(raw["today"])
        self.customers = raw["customers"]
        self.orders = {}
        for key, value in raw["orders"].items():
            for name in ("ordered", "delivered", "eta"):
                value[name] = date.fromisoformat(value[name]) if value[name] else None
            for name in ("total", "refunded"):
                value[name] = Decimal(value[name])
            self.orders[key] = Order(**value)
        self.refunds = [Refund(**{**r, "amount": Decimal(r["amount"])}) for r in raw["refunds"]]
        self.tickets = raw["tickets"]

    def persist(self, db_path: str | None):
        """Seed once. Never silently upgrade a B0 checkpoint without its lost business data."""
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False, timeout=5)
        self._db.execute("PRAGMA synchronous=FULL")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (
                "business_state" not in tables
                and "conversations" in tables
                and self._db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            ):
                raise ValueError("B0 database requires explicit reconciliation; use a new B1 database")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS business_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS business_operations "
                "(operation_key TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL)"
            )
            self._db.execute("INSERT OR IGNORE INTO business_state VALUES (1, ?)", (self._dump(),))
        self.refresh()

    def refresh(self):
        with self._lock:
            if self._db is not None and not self._depth:
                self._restore(self._db.execute("SELECT payload FROM business_state WHERE id=1").fetchone()[0])

    @contextmanager
    def transaction(self, deadline: float | None = None):
        """Policy check + mutation + operation receipt commit together, or all roll back.

        No graph/model/interrupt may run inside this transaction. Reload under the write lock
        so another process cannot invalidate a policy check between reading and committing.
        """
        with self._lock:
            if self._db is None:
                raise RuntimeError("B1 requires a transactional store")
            if self._depth:
                yield
                return
            check_deadline(deadline)
            self._db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            before = None
            try:
                before = self._db.execute("SELECT payload FROM business_state WHERE id=1").fetchone()[0]
                self._restore(before)
                yield
                check_deadline(deadline)
                self._db.execute("UPDATE business_state SET payload=? WHERE id=1", (self._dump(),))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                if before is not None:
                    self._restore(before)
                raise
            finally:
                self._depth = 0

    def operation(self, key: str, request: dict) -> dict | None:
        if not self._depth:
            raise RuntimeError("Operation lookup requires a business transaction")
        row = self._db.execute(
            "SELECT request,result FROM business_operations WHERE operation_key=?", (key,)
        ).fetchone()
        if not row:
            return None
        if json.loads(row[0]) != request:
            raise ValueError("idempotency_conflict: operation already completed with different parameters")
        return {**json.loads(row[1]), "idempotent_replay": True}

    def remember(self, key: str, request: dict, result: dict):
        if not self._depth:
            raise RuntimeError("Operation receipt requires a business transaction")
        self._db.execute(
            "INSERT INTO business_operations VALUES (?, ?, ?)",
            (key, json.dumps(request, sort_keys=True), json.dumps(result)),
        )

    def close(self):
        if self._db is not None:
            self._db.close()
