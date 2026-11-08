"""kanban_complete.metadata must be an OPEN object schema on the wire.

A bare ``_prop("object", ...)`` sanitizes to ``{"properties": {}, "required": []}``,
which constrained decoders (provider strict structured outputs) read as a CLOSED
object: the model can only emit ``metadata: {}``, so completion-verifier Gate 4
sees a prose-only completion and rejects every attempt (t_8cf30d34 / t_eceb3c0c).
"""
from __future__ import annotations

from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA, KANBAN_REQUEST_REVIEW_SCHEMA
from tools.schema_sanitizer import sanitize_tool_schemas


def _metadata(schema: dict, *, sanitized: bool = False) -> dict:
    if sanitized:
        schema = sanitize_tool_schemas([{"name": schema["name"], "parameters": schema["parameters"]}])[0]
    return schema["parameters"]["properties"]["metadata"]


def test_complete_metadata_declares_gate_consumed_keys() -> None:
    meta = _metadata(KANBAN_COMPLETE_SCHEMA)
    assert meta["type"] == "object"
    assert meta["additionalProperties"] is True
    # Keys completion-verifier Gate 4 / merge-gate read; undeclared keys are
    # unreachable from a grammar-constrained decoder.
    for key in ("sha", "branch", "receipts", "changed_files", "REVIEW_CLASS_RECEIPT"):
        assert key in meta["properties"], key
    receipt = meta["properties"]["receipts"]["items"]
    assert set(receipt["properties"]) == {"claim", "file", "evidence_cmd", "exit_code"}
    assert receipt["additionalProperties"] is True


def test_complete_metadata_survives_sanitizing_open() -> None:
    meta = _metadata(KANBAN_COMPLETE_SCHEMA, sanitized=True)
    assert meta["additionalProperties"] is True
    assert "sha" in meta["properties"] and "receipts" in meta["properties"]


def test_request_review_metadata_is_open_too() -> None:
    meta = _metadata(KANBAN_REQUEST_REVIEW_SCHEMA, sanitized=True)
    assert meta["additionalProperties"] is True
