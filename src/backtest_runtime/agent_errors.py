"""Opt-in machine-readable failures; legacy exception behavior is retained."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Never

from .agent_access import AccessError
from .store import RegistryConflict


class ArgumentFailure(ValueError):
    """An argparse error in JSON mode."""


class AgentParser(argparse.ArgumentParser):
    json_errors = False

    def error(self, message: str) -> Never:
        if self.json_errors:
            raise ArgumentFailure(message)
        super().error(message)


def emit_error(error: Exception, args: Any = None) -> int:
    code, message = classify(error)
    payload = {
        "schema": "quant.agent_error.v1",
        "code": code,
        "message": message,
        "retryable": False,
        "suggested_action": "lookup_before_resubmit"
        if getattr(args, "command", None) == "submit"
        else "inspect_inputs_and_state",
        "job_id": getattr(args, "job_id", None),
        "idempotency_key": getattr(args, "idempotency_key", None),
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    return 2 if isinstance(error, ArgumentFailure) else 1


def classify(error: Exception) -> tuple[str, str]:
    if isinstance(error, AccessError):
        return error.code, str(error)
    if isinstance(error, ArgumentFailure):
        return "INVALID_ARGUMENTS", "Invalid command arguments; consult --help"
    if isinstance(error, KeyError):
        return "JOB_NOT_FOUND", "No matching job exists"
    if isinstance(error, RegistryConflict):
        if "idempotency key" in str(error):
            return "IDEMPOTENCY_CONFLICT", "Key is bound to different request content"
        return "RESULT_UNAVAILABLE", "Job has no available successful result"
    if isinstance(error, sqlite3.Error):
        return "STORAGE_FAILURE", "Runtime storage could not be accessed"
    if isinstance(error, FileNotFoundError):
        return "INPUT_UNAVAILABLE", "Requested file is unavailable"
    if isinstance(error, (ValueError, UnicodeError)):
        return "INVALID_REQUEST", "Request or result validation failed"
    if isinstance(error, OSError):
        return "STORAGE_FAILURE", "Runtime file or process operation failed"
    return "INTERNAL_ERROR", "Runtime operation failed; inspect local diagnostics"
