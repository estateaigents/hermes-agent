"""Fail-closed guard: the 7 retired carried patches stay retired.

Retired 2026-11-10 under the owner-approved contraction policy; audit:
``03_technology/architecture/briefs/patch-dependency-audit-2026-11-10.md``.

P02 (``9027b59f57``) carried no tree content — its tree is byte-identical to
P01 (``37663d594a``) — so it has no symbol or file to assert. It survives only
as merge topology in history, which cannot be dropped without a history
rewrite; the other six patches are asserted absent here.
"""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# patch -> (module the patch exported from, symbols it added)
RETIRED = {
    "P05": (
        "hermes_cli.kanban_db",
        [
            "birth_defect_class",
            "record_birth_denial",
            "read_birth_denials",
            "birth_denial_count",
            "_denial_since_epoch",
        ],
    ),
    "P06": ("hermes_cli.kanban_db", ["_run_birth_probe"]),
    "P07": ("hermes_cli.kanban_db", ["record_birth_denial"]),
    "P08": (
        "hermes_cli.kanban_db_dispatch",
        ["probe_repair_pass_receipt", "probe_repair_exit_ready", "resolve_probe_repair_lane"],
    ),
    "P12": (
        "hermes_cli.kanban_db_graph",
        ["clear_block_loop", "repair_creator_gated_fanout", "held_lineage_root"],
    ),
}

# P02 is merge topology only (tree byte-identical to P01) and P14 was one test
# marker on the P06 timeout test, so neither leaves a symbol or file to assert.
RETIRED_WITHOUT_TREE_CONTENT = ["P02", "P14"]

RETIRED_FILES = [
    "hermes_cli/kanban_birth_probe.py",
    "tests/test_kanban_birth_probe.py",
]

# Scanned under hermes_cli/ and tools/ only: tests/hermes_state keeps its own
# unrelated _probe_repair_lock_from_child state-DB repair helper.
RETIRED_TOKENS = [
    "birth_probe",
    "birth_denial",
    "probe_repair_pass_receipt",
    "probe_repair_exit_ready",
    "resolve_probe_repair_lane",
    "creator_gated",
    "held_lineage_root",
    "clear_block_loop",
]
TOKEN_SCAN_ROOTS = ["hermes_cli", "tools"]


def test_retired_patch_manifest_complete():
    expected = {"P02", "P05", "P06", "P07", "P08", "P12", "P14"}
    covered = set(RETIRED) | set(RETIRED_WITHOUT_TREE_CONTENT)
    assert covered == expected, f"Manifest mismatch: {expected ^ covered}"


@pytest.mark.parametrize("patch", sorted(RETIRED))
def test_retired_patch_symbols_are_gone(patch):
    module_name, symbols = RETIRED[patch]
    module = importlib.import_module(module_name)
    reappeared = [name for name in symbols if hasattr(module, name)]
    assert not reappeared, f"{patch} re-grafted: {module_name} defines {reappeared}"


@pytest.mark.parametrize("relative_path", RETIRED_FILES)
def test_retired_patch_files_are_gone(relative_path):
    assert not (REPO_ROOT / relative_path).exists(), f"retired patch file reappeared: {relative_path}"


def test_no_retired_machinery_in_source():
    hits = []
    for root in TOKEN_SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            hits += [f"{path.relative_to(REPO_ROOT)}:{token}" for token in RETIRED_TOKENS if token in text]
    assert not hits, f"retired patch machinery reappeared in: {hits}"
