#!/usr/bin/env bash
# Asserts the EXPORTED kanban_complete schema keeps structured receipts reachable:
# metadata is an open object (additionalProperties true) declaring sha/branch/receipts,
# and sha/branch/receipts also exist as optional TOP-LEVEL params. Prints one token:
# PASS, or FAIL with the reasons on stderr (t_bef91904).
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
exec python3 - "$root" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA
from tools.schema_sanitizer import sanitize_tool_schemas

schema = KANBAN_COMPLETE_SCHEMA
# The wire form: what the provider actually receives after schema sanitizing.
exported = sanitize_tool_schemas([{
    "type": "function",
    "function": {"name": schema["name"], "parameters": schema["parameters"]},
}])[0]["function"]["parameters"]
props = exported["properties"]
meta = props.get("metadata") or {}

problems = []
if meta.get("type") != "object":
    problems.append("metadata.type != object")
if meta.get("additionalProperties") is not True:
    problems.append("metadata.additionalProperties != true")
declared = meta.get("properties") or {}
for key in ("sha", "branch", "receipts"):
    if key not in declared:
        problems.append(f"metadata.properties.{key} missing")
    if key not in props:
        problems.append(f"top-level {key} missing")
    if key in (exported.get("required") or []):
        problems.append(f"{key} must stay optional")
receipt = (declared.get("receipts") or {}).get("items") or {}
for field in ("claim", "file", "evidence_cmd", "exit_code"):
    if field not in (receipt.get("properties") or {}):
        problems.append(f"receipts.items.properties.{field} missing")

if problems:
    print("kanban_metadata_schema_probe: " + "; ".join(problems), file=sys.stderr)
    print("FAIL", file=sys.stderr)
    sys.exit(1)
print("PASS")
PY
