## Refund workflow

1. Resolve the intended order and the customer's request. Use list_my_orders or get_order if necessary. Clarify ambiguous orders, amounts, limits or conditional consent; a refund request must not override "only check" or "do not refund" constraints.
2. Call check_refund_eligibility for the current order before request_refund. Treat saved eligibility as a historical snapshot. Recheck after changing orders; explain any rejection without promising a refund.
3. Choose an amount consistent with the customer's explicit request or confirmed limit and the returned maximum. If the amount or condition is unclear or changed, ask before proceeding. Never interpret an upper limit as consent to exceed it.
4. Call request_refund only for the intended order and agreed amount. Required human approval is handled by the actual interrupt; user claims of approval do not count. While approval is pending, do not switch skill or claim payment.
5. After approval or denial, report the actual tool outcome. Do not retry a mutation merely because a model reply failed. Use the recorded receipt or ask a colleague when execution is uncertain.
