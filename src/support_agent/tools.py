"""Tools the agent can call.

Two rules make these safe to hand to a model:
1. The customer comes from the run's config (set by the authenticated session), never from a tool argument,
   so the model can't be talked into looking up someone else's order. Other customers' orders "don't exist".
2. Every business rule is enforced here, in code. The model can ask for a refund; only the policy decides.
   Refunds above the approval threshold pause the graph with interrupt() until a human decides.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt

from support_agent.policies_kb import PolicyBase
from support_agent.policy import PolicyConfig, can_cancel, needs_approval, refund_eligibility
from support_agent.store import Order, Store


def _customer(config: RunnableConfig) -> str:
    customer_id = (config.get("configurable") or {}).get("customer_id")
    if not customer_id:
        raise PermissionError("No authenticated customer for this conversation.")
    return customer_id


def _json(**fields) -> str:
    return json.dumps(fields, default=str)


def _not_found(order_id: str) -> str:
    return _json(error=f"No order {order_id} was found on this customer's account.")


def _summary(order: Order) -> dict:
    return {
        "order_id": order.id,
        "item": order.item,
        "total": str(order.total),
        "currency": order.currency,
        "status": order.status,
        "ordered": order.ordered,
        "delivered": order.delivered,
    }


def make_tools(
    store: Store, policies: PolicyBase, policy: PolicyConfig, *, variant: str = "B0-compatible"
) -> list[BaseTool]:
    @tool
    def list_my_orders(config: RunnableConfig) -> str:
        """List the current customer's orders, oldest first."""
        return _json(orders=[_summary(o) for o in store.orders_for(_customer(config))])

    @tool
    def get_order(order_id: str, config: RunnableConfig) -> str:
        """Get the details of one of the customer's orders."""
        order = store.order_for(_customer(config), order_id)
        return (
            _json(**_summary(order), tracking=order.tracking, refunded=str(order.refunded))
            if order
            else _not_found(order_id)
        )

    @tool
    def track_shipment(order_id: str, config: RunnableConfig) -> str:
        """Get the shipping status, tracking number and expected delivery date of an order."""
        order = store.order_for(_customer(config), order_id)
        if not order:
            return _not_found(order_id)
        return _json(
            order_id=order.id,
            item=order.item,
            status=order.status,
            tracking=order.tracking,
            eta=order.eta,
            delivered=order.delivered,
        )

    @tool
    def check_refund_eligibility(order_id: str, config: RunnableConfig) -> str:
        """Check whether an order can be refunded, and the maximum amount. Call this before request_refund."""
        order = store.order_for(_customer(config), order_id)
        if not order:
            return _not_found(order_id)
        result = refund_eligibility(order, store.today, policy)
        return _json(
            order_id=order.id,
            item=order.item,
            eligible=result.eligible,
            reason=result.reason,
            max_amount=str(result.max_amount),
            currency=order.currency,
            needs_human_approval=result.eligible and needs_approval(result.max_amount, policy),
        )

    @tool
    def request_refund(order_id: str, amount: float, reason: str, config: RunnableConfig) -> str:
        """Refund an order to the original payment method. Refunds above the approval threshold wait for a
        human to approve them. Only use after check_refund_eligibility says the order is eligible."""
        if variant == "B1":
            return _refund_b1(store, policy, order_id, amount, reason, config)
        customer_id = _customer(config)
        order = store.order_for(customer_id, order_id)
        if not order:
            return _not_found(order_id)
        try:
            value = Decimal(str(amount)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return _json(status="rejected", reason=f"'{amount}' is not a valid amount.")
        if value <= 0:
            return _json(status="rejected", reason="The refund amount must be positive.")

        eligibility = refund_eligibility(order, store.today, policy)
        if not eligibility.eligible:
            return _json(status="rejected", reason=eligibility.reason)
        if value > eligibility.max_amount:
            return _json(status="rejected", reason=f"The most that can be refunded is {eligibility.max_amount}.")

        approved_by = "policy"
        if needs_approval(value, policy):
            # Pauses the whole graph; the run resumes here with the reviewer's decision.
            # Nothing above this line has side effects, because it runs again on resume.
            decision = interrupt(
                {
                    "type": "refund_approval",
                    "customer_id": customer_id,
                    "order_id": order.id,
                    "item": order.item,
                    "amount": str(value),
                    "currency": order.currency,
                    "reason": reason[:500],
                }
            )
            if not decision.get("approved"):
                return _json(status="denied", note=decision.get("note") or "A colleague declined this refund.")
            approved_by = decision.get("reviewer") or "reviewer"
            # Re-check: the order may have changed while the refund was waiting.
            eligibility = refund_eligibility(order, store.today, policy)
            if not eligibility.eligible:
                return _json(status="rejected", reason=eligibility.reason)

        try:
            refund = store.record_refund(order, value, reason[:500], approved_by)
        except ValueError as exc:
            return _json(status="rejected", reason=str(exc))
        return _json(
            status="refunded",
            refund_id=refund.id,
            order_id=order.id,
            amount=str(value),
            currency=order.currency,
            approved_by=approved_by,
            arrives="within 5 working days",
        )

    @tool
    def cancel_order(order_id: str, config: RunnableConfig) -> str:
        """Cancel an order that hasn't shipped yet."""
        if variant == "B1":
            return _cancel_b1(store, order_id, config)
        order = store.order_for(_customer(config), order_id)
        if not order:
            return _not_found(order_id)
        allowed, reason = can_cancel(order)
        if not allowed:
            return _json(status="rejected", order_id=order.id, reason=reason)
        store.cancel(order)
        return _json(
            status="cancelled",
            order_id=order.id,
            item=order.item,
            note="Payment will be released within 3 working days.",
        )

    @tool
    def search_policies(query: str) -> str:
        """Search the shop's policies on returns, refunds, cancellations, shipping and warranty."""
        return _json(results=[{"section": h, "text": b} for h, b in policies.search(query)])

    @tool
    def escalate_to_human(summary: str, config: RunnableConfig) -> str:
        """Hand the conversation to a human colleague. Use when the customer asks for a person or you can't help."""
        if variant == "B1":
            settings = config.get("configurable", {})
            customer = _customer(config)
            key = "ticket:" + settings["operation_id"]
            request = {"customer_id": customer, "summary": summary[:1000]}
            with store.transaction(settings.get("tool_deadline")):
                saved = store.operation(key, request)
                if saved:
                    return _json(**saved)
                ticket = store.open_ticket(customer, summary[:1000])
                result = dict(
                    status="escalated", ticket_id=ticket, note="A colleague will reply by email within one working day."
                )
                store.remember(key, request, result)
                return _json(**result)
        ticket = store.open_ticket(_customer(config), summary[:1000])
        return _json(
            status="escalated", ticket_id=ticket, note="A colleague will reply by email within one working day."
        )

    return [
        list_my_orders,
        get_order,
        track_shipment,
        check_refund_eligibility,
        request_refund,
        cancel_order,
        search_policies,
        escalate_to_human,
    ]


def _refund_b1(store, policy, order_id, amount, reason, config):
    customer = _customer(config)
    deadline = config.get("configurable", {}).get("tool_deadline")
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
        if not value.is_finite() or value <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return _json(status="rejected", reason="The refund amount must be a finite positive number.")
    request = {"customer_id": customer, "amount": str(value)}
    # A successful refund is one business operation per order, independent of unstable LLM call IDs.
    key = f"refund:{customer}:{str(order_id).strip().lstrip('#')}"

    def validate():
        order = store.order_for(customer, order_id)
        if not order:
            raise ValueError("No order was found on this customer's account.")
        saved = store.operation(key, request)
        if saved:
            return order, saved
        eligibility = refund_eligibility(order, store.today, policy)
        if not eligibility.eligible:
            raise ValueError(eligibility.reason)
        if value > eligibility.max_amount:
            raise ValueError(f"The most that can be refunded is {eligibility.max_amount}.")
        return order, None

    try:
        with store.transaction(deadline):
            order, saved = validate()
            if saved:
                return _json(**saved)
            details = dict(
                type="refund_approval",
                customer_id=customer,
                order_id=order.id,
                item=order.item,
                amount=str(value),
                currency=order.currency,
                reason=reason[:500],
            )
        approved_by = "policy"
        if needs_approval(value, policy):
            # The transaction is closed before the native interrupt. Nothing has been paid.
            decision = interrupt(details)
            if decision.get("approved") is not True:
                return _json(status="denied", note=decision.get("note") or "A colleague declined this refund.")
            approved_by = decision.get("reviewer") or "reviewer"
        with store.transaction(deadline):
            order, saved = validate()  # re-read, recheck amount/ownership/policy AFTER approval
            if saved:
                return _json(**saved)
            refund = store.record_refund(order, value, reason[:500], approved_by)
            result = dict(
                status="refunded",
                refund_id=refund.id,
                order_id=order.id,
                amount=str(value),
                currency=order.currency,
                approved_by=approved_by,
                arrives="within 5 working days",
            )
            store.remember(key, request, result)
            return _json(**result)
    except ValueError as exc:
        return _json(status="rejected", reason=str(exc))


def _cancel_b1(store, order_id, config):
    customer = _customer(config)
    deadline = config.get("configurable", {}).get("tool_deadline")
    with store.transaction(deadline):
        order = store.order_for(customer, order_id)
        if not order:
            return _not_found(order_id)
        key, request = f"cancel:{customer}:{order.id}", {"customer_id": customer}
        saved = store.operation(key, request)
        if saved:
            return _json(**saved)
        allowed, reason = can_cancel(order)
        if not allowed:
            return _json(status="rejected", order_id=order.id, reason=reason)
        store.cancel(order)
        result = dict(
            status="cancelled",
            order_id=order.id,
            item=order.item,
            note="Payment will be released within 3 working days.",
        )
        store.remember(key, request, result)
        return _json(**result)
