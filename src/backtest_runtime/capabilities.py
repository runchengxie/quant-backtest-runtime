"""The bounded public operation catalogue; discovery does not inspect storage."""

from __future__ import annotations

from typing import Any


def capabilities() -> dict[str, Any]:
    rows = [
        ("capabilities", "Describe available contracts", [], "read", [], "object"),
        (
            "plan",
            "Validate immutable inputs and declare unchecked conditions",
            [],
            "read",
            ["manifest"],
            "quant.backtest_plan.v1",
        ),
        (
            "submit",
            "Submit once with an idempotency key; optionally bind a plan",
            ["recovery", "job_insert", "worker_execution"],
            "submit",
            ["manifest"],
            "object",
        ),
        (
            "inspect",
            "Read current state without recovery",
            [],
            "read",
            ["job_id"],
            "object",
        ),
        (
            "lookup",
            "Resolve a lost receipt by idempotency key without recovery",
            [],
            "read",
            ["idempotency_key"],
            "object",
        ),
        (
            "status",
            "Read state after recovering expired leases",
            ["recovery"],
            "recover",
            ["job_id"],
            "object",
        ),
        (
            "cancel",
            "Request cancellation and signal a matching worker",
            ["cancel_request", "process_signal"],
            "cancel",
            ["job_id"],
            "object",
        ),
        (
            "result",
            "Verify manifest and result-file integrity; no promotion",
            ["storage_initialization"],
            "read",
            ["job_id"],
            "object",
        ),
        (
            "recover",
            "Expire leases and remove unpublished output",
            ["recovery", "process_signal", "output_cleanup"],
            "recover",
            [],
            "object",
        ),
    ]
    suffixes = {
        "plan": " /tmp/synthetic/request.json",
        "submit": " /tmp/synthetic/request.json --expected-plan-sha256 <plan-sha256>",
        "lookup": " --idempotency-key synthetic-v2",
        "inspect": " <job-id>",
        "status": " <job-id>",
        "cancel": " <job-id>",
        "result": " <job-id>",
    }
    return {
        "schema": "quant.agent_capabilities.v1",
        "owner": "quant-backtest-runtime",
        "request_versions": [1, 2, 3, 4, 5, 6],
        "permission_grant": False,
        "operations": [
            {
                "id": name,
                "description": description,
                "owner": "quant-backtest-runtime",
                "contract_version": 1,
                "side_effects": effects,
                "permission": permission,
                "input_schema": {
                    "type": "object",
                    "properties": {key: {"type": "string"} for key in required},
                    "required": required,
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object"},
                "output_contract": output,
                "invocation": "backtest-job [--database DB --artifact-root INPUTS --result-root RESULTS] "
                + name,
                "preconditions": []
                if name == "capabilities"
                else ["explicit storage paths", "authorized local account"],
                "retry": "same_request_and_key_after_lookup"
                if name == "submit"
                else ("read_again" if not effects else "inspect_state_first"),
                "examples": [
                    f"backtest-job --database /tmp/synthetic/jobs.sqlite --artifact-root /tmp/synthetic/artifacts --result-root /tmp/synthetic/results {name}{suffixes.get(name, chr(32)).rstrip()}"
                ],
                "limitations": [
                    "Discovery grants no permission",
                    "Success does not approve research",
                ],
            }
            for name, description, effects, permission, required, output in rows
        ],
    }
