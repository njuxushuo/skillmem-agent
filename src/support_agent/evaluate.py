"""Runs the conversation scenarios in data/scenarios.json and checks tools, outcome and refunds.

python -m support_agent.evaluate [--provider openai]

Each scenario runs in a fresh service, so one scenario's refund can't affect another.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from support_agent.models import get_model
from support_agent.service import SupportService, default_data_dir


def run_scenarios(provider: str | None = None) -> list[dict]:
    scenarios = json.loads((default_data_dir() / "scenarios.json").read_text(encoding="utf-8"))
    results = []
    for scenario in scenarios:
        service = SupportService(model=get_model(provider))
        result = service.chat(scenario["customer"], scenario["message"])
        checks = {
            "tools": result.tools_used == scenario["expect_tools"],
            "status": result.status == scenario["expect_status"],
            "reply": scenario.get("expect_reply", "").lower() in result.reply.lower(),
            "no_refund": not (scenario.get("expect_no_refund") and service.store.refunds),
        }
        results.append(
            {
                "name": scenario["name"],
                "passed": all(checks.values()),
                "failed_checks": [k for k, ok in checks.items() if not ok],
                "tools": result.tools_used,
                "status": result.status,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the support agent on conversation scenarios.")
    parser.add_argument("--provider", help="offline | deepseek | openai | azure")
    parser.add_argument("--tasks", type=Path, help="Versioned multi-turn evaluation JSON")
    parser.add_argument("--output", type=Path, default=Path(".local/runs"))
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.tasks:
        from support_agent.evaluation import run_cases

        summary = run_cases(
            json.loads(args.tasks.read_text()), args.output, provider=args.provider or "offline", repeat=args.repeat
        )
        print(f"{summary['passed']}/{summary['tasks']} tasks passed; {summary['run_dir']}/summary.json")
        raise SystemExit(0 if summary["passed"] == summary["tasks"] else 1)
    results = run_scenarios(args.provider)
    for r in results:
        mark = "PASS" if r["passed"] else f"FAIL ({', '.join(r['failed_checks'])})"
        print(f"{mark:<22} {r['name']:<36} {r['status']:<18} {', '.join(r['tools'])}")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} scenarios passed")


if __name__ == "__main__":
    main()
