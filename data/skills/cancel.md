## Cancellation workflow

1. Resolve the intended order and confirm that cancellation is what the customer wants. A shipping inquiry is not cancellation consent. If several orders are mentioned, clarify.
2. Use get_order to inspect the order. Call cancel_order only for the intended order after the customer requests cancellation; the tool checks whether shipping already prevents cancellation.
3. Explain the actual cancelled or rejected result. Do not promise cancellation for a shipped order or invent a payment release date.
4. If a refund is requested instead, select the refund skill and check that order's eligibility. For a policy question, select policy_support. Escalate unresolved or explicitly human-directed requests.
