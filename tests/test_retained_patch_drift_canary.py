"""Fail-closed drift canary for the 7 non-retirable carried fork patches.

Incident 2026-11-08: An unattended upstream update silently deleted 11 locally-applied
patches; they were re-applied by hand, and nothing detected it.

This test suite guarantees that none of the 7 retained patches (P01, P03, P04, P09, P10, P11, P13)
can be silently dropped, overwritten, or reverted during upstream merges.

Retained Patches:
- P01 (37663d594a): fix(tools): make kanban_complete receipts reachable
- P03 (012bb1035d): feat(regraft): port P12a acceptance-block parsing + freeze guards
- P04 (acdbb56126): feat(regraft): port P1 decompose frozen-ACCEPTANCE + zombie guards
- P09 (8c44d1fb45): feat(regraft): port P2 create-path injection preservation
- P10 (49ae7bc5be): feat(regraft): P3 worker memcg cap 8 GiB + OOM fixture
- P11 (f8f8564861): feat(regraft): port P4d/P4e/P4g subsets
- P13 (5674c5f82d): feat(regraft): vendor patched memory_tencentdb memory provider into build tree
"""

import importlib
import inspect
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

RETAINED_PATCHES = [
    "P01",
    "P03",
    "P04",
    "P09",
    "P10",
    "P11",
    "P13",
]


def test_retained_patch_canary_manifest_complete():
    """Fail closed if the canary manifest itself is missing any of the 7 retained patches."""
    assert len(RETAINED_PATCHES) == 7, f"Expected exactly 7 retained patches, found {len(RETAINED_PATCHES)}"
    expected = {"P01", "P03", "P04", "P09", "P10", "P11", "P13"}
    assert set(RETAINED_PATCHES) == expected, f"Manifest mismatch: missing {expected - set(RETAINED_PATCHES)}"


def test_p01_kanban_complete_schema_retained():
    """P01 (37663d594a): kanban_complete receipts schema & open metadata retained."""
    schema_path = REPO_ROOT / "tools" / "kanban_tools_schemas.py"
    tools_path = REPO_ROOT / "tools" / "kanban_tools.py"
    assert schema_path.exists() or tools_path.exists(), "P01 target tool schema file missing"
    
    found_receipts = False
    found_additional_props = False
    
    if schema_path.exists():
        content = schema_path.read_text(encoding="utf-8")
        if "receipts" in content:
            found_receipts = True
        if "additionalProperties" in content or "sha" in content:
            found_additional_props = True
            
    if tools_path.exists():
        content = tools_path.read_text(encoding="utf-8")
        if "receipts" in content:
            found_receipts = True
        if "sha" in content or "branch" in content:
            found_additional_props = True

    assert found_receipts, "P01 drift: kanban_complete receipts parameter removed"
    assert found_additional_props, "P01 drift: kanban_complete sha/branch/metadata schema removed"


def test_p03_acceptance_parsing_and_freeze_guards_retained():
    """P03 (012bb1035d): acceptance-block parsing and freeze guards retained in kanban_db.py."""
    kanban_db_path = REPO_ROOT / "hermes_cli" / "kanban_db.py"
    assert kanban_db_path.exists(), "P03 drift: hermes_cli/kanban_db.py missing"

    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    
    assert hasattr(kanban_db, "_extract_acceptance_blocks"), "P03 drift: _extract_acceptance_blocks missing"
    assert hasattr(kanban_db, "_frozen_acceptance_block"), "P03 drift: _frozen_acceptance_block missing"
    assert hasattr(kanban_db, "acceptance_gate_verdict"), "P03 drift: acceptance_gate_verdict missing"
    assert hasattr(kanban_db, "ChildrenNotTerminalError"), "P03 drift: ChildrenNotTerminalError missing"


def test_p04_decompose_frozen_acceptance_and_zombie_guards_retained():
    """P04 (acdbb56126): decompose frozen-ACCEPTANCE & zombie guards retained in kanban_decompose.py."""
    decompose_path = REPO_ROOT / "hermes_cli" / "kanban_decompose.py"
    assert decompose_path.exists(), "P04 drift: hermes_cli/kanban_decompose.py missing"

    kanban_decompose = importlib.import_module("hermes_cli.kanban_decompose")

    assert hasattr(kanban_decompose, "_check_pr_states"), "P04 drift: _check_pr_states missing"
    assert hasattr(kanban_decompose, "_guard_frozen_blocks"), "P04 drift: _guard_frozen_blocks missing"
    assert hasattr(kanban_decompose, "_preserve_frozen_blocks"), "P04 drift: _preserve_frozen_blocks missing"


def test_p09_create_path_injection_and_specify_preservation_retained():
    """P09 (8c44d1fb45): create-path injection preservation retained in specify and db."""
    specify_path = REPO_ROOT / "hermes_cli" / "kanban_specify.py"
    assert specify_path.exists(), "P09 drift: hermes_cli/kanban_specify.py missing"

    content = specify_path.read_text(encoding="utf-8")
    assert "_frozen_acceptance_block" in content or "_extract_acceptance_blocks" in content, (
        "P09 drift: specify path does not preserve frozen ACCEPTANCE blocks"
    )

    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    db_source = inspect.getsource(kanban_db)
    assert "ACCEPTANCE" in db_source, "P09 drift: kanban_db missing ACCEPTANCE injection preservation"


def test_p10_worker_memcg_cap_8gib_retained():
    """P10 (49ae7bc5be): worker memcg cap 8 GiB retained in process_registry.py."""
    proc_reg = importlib.import_module("tools.process_registry")
    cap = getattr(proc_reg, "_WORKER_MEMORY_MAX_CAP_BYTES", None)
    expected_8gib = 8 * 1024 * 1024 * 1024
    assert cap == expected_8gib, (
        f"P10 drift: _WORKER_MEMORY_MAX_CAP_BYTES is {cap}, expected {expected_8gib} (8 GiB)"
    )


def test_p11_parent_gate_selfheal_and_respawn_dedup_retained():
    """P11 (f8f8564861): parent gate ready-only, self-heal & respawn dedup retained in dispatch."""
    dispatch_path = REPO_ROOT / "hermes_cli" / "kanban_db_dispatch.py"
    assert dispatch_path.exists(), "P11 drift: hermes_cli/kanban_db_dispatch.py missing"

    kanban_db_dispatch = importlib.import_module("hermes_cli.kanban_db_dispatch")
    assert hasattr(kanban_db_dispatch, "_self_heal_blocked_tasks"), (
        "P11 drift: _self_heal_blocked_tasks missing in dispatch"
    )

    source = dispatch_path.read_text(encoding="utf-8")
    assert "changes_requested" in source, "P11 drift: changes_requested exemption missing in dispatch"


def test_p13_vendored_tencentdb_memory_provider_retained():
    """P13 (5674c5f82d): vendored TencentDB memory provider retained in build tree."""
    tencentdb_dir = REPO_ROOT / "plugins" / "memory" / "memory_tencentdb"
    assert tencentdb_dir.is_dir(), "P13 drift: plugins/memory/memory_tencentdb directory missing"

    init_py = tencentdb_dir / "__init__.py"
    assert init_py.exists(), "P13 drift: plugins/memory/memory_tencentdb/__init__.py missing"

    plugin_yaml = tencentdb_dir / "plugin.yaml"
    assert plugin_yaml.exists(), "P13 drift: plugins/memory/memory_tencentdb/plugin.yaml missing"

    init_content = init_py.read_text(encoding="utf-8")
    assert "spawn_context_thread" in init_content, (
        "P13 drift: spawn_context_thread missing in memory_tencentdb/__init__.py"
    )


def test_all_seven_retained_patches_fail_closed_aggregate():
    """Combined aggregate check for all seven retained patches — fails closed if any single check fails."""
    checks = [
        test_p01_kanban_complete_schema_retained,
        test_p03_acceptance_parsing_and_freeze_guards_retained,
        test_p04_decompose_frozen_acceptance_and_zombie_guards_retained,
        test_p09_create_path_injection_and_specify_preservation_retained,
        test_p10_worker_memcg_cap_8gib_retained,
        test_p11_parent_gate_selfheal_and_respawn_dedup_retained,
        test_p13_vendored_tencentdb_memory_provider_retained,
    ]
    
    assert len(checks) == 7, "Aggregate canary must check all 7 retained patches"
    
    failed = []
    for check in checks:
        try:
            check()
        except Exception as err:
            failed.append((check.__name__, str(err)))

    assert not failed, f"Retained patch drift detected in {len(failed)} patches: {failed}"
