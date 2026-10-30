"""Governance guards in the decomposer: frozen-``## ACCEPTANCE`` preservation
and the zombie guards (P1 port).

Regression for two loss modes: "someone changed the acceptance test after it
was frozen" (the decomposer paraphrasing a PA-frozen block) and "decomposed
hours after PA stood the card down" (t_bc15576c).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp

FROZEN = """## ACCEPTANCE
criterion: probe prints true
evidence_cmd: |
  bash /root/.hermes/scripts/verify/probes/p.sh
expected: true
frozen_at: 2026-10-30T14:34:36Z"""


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _kinds(tid: str) -> list[str]:
    with kbc.connect() as conn:
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall()
    return [r["kind"] if isinstance(r, dict) else r[0] for r in rows]


def _routing() -> decomp._Routing:
    return decomp._Routing(
        orchestrator="orchestrator", default_assignee="programmer",
        auto_promote=True, roster=[], valid_names={"programmer", "orchestrator"},
    )


def test_guard_frozen_blocks_reports_found_even_for_identical_body():
    """Sentinel bool, not string equality: a frozen block is found even when
    the incoming body is byte-identical (the arm-idempotency defect)."""
    body = "spec\n\n" + FROZEN + "\n"
    out, found = decomp._guard_frozen_blocks(body, body)
    assert found is True
    assert FROZEN in out


def test_guard_frozen_blocks_passes_through_a_draft_block():
    draft = FROZEN.replace(
        "frozen_at: 2026-10-30T14:34:36Z", "frozen_at: PENDING_PA_FREEZE",
    )
    out, found = decomp._guard_frozen_blocks(draft, "llm body")
    assert found is False
    assert out == "llm body"


def test_clean_children_preserves_frozen_block_over_llm_paraphrase():
    parent = "parent spec\n\n" + FROZEN + "\n"
    raw = [{
        "title": "child", "assignee": "programmer", "parents": [],
        "body": "new spec\n\n## ACCEPTANCE\ncriterion: paraphrased\nexpected: 0",
    }]
    children, reason = decomp._clean_children(
        "t1", raw, _routing(), parent_body=parent,
    )
    assert reason == ""
    body = children[0]["body"]
    assert FROZEN in body
    assert "paraphrased" not in body


def test_inject_acceptance_block_is_idempotent_across_two_runs():
    once = decomp._inject_acceptance_block("spec", "programmer")
    twice = decomp._inject_acceptance_block(once, "programmer")
    assert once == twice
    assert twice.count("## ACCEPTANCE") == 1


def test_ensure_arm_block_is_idempotent_across_two_runs():
    spec = (
        "do it\n\n## ACCEPTANCE\ncriterion: c\nexpected: 0\n"
        "scope_paths:\n  - /root/.hermes/scripts/verify\n"
        "frozen_at: 2026-10-30T14:34:36Z\n"
    )
    once = decomp._ensure_arm_block(spec)
    twice = decomp._ensure_arm_block(once)
    assert "\narm:" in once
    assert once == twice
    assert twice.count("\narm:") == 1


def test_non_tooling_scope_gets_no_arm_block():
    spec = "do it\nscope_paths:\n  - /root/projects/raia\n"
    assert decomp._ensure_arm_block(spec) == spec


def test_stale_pr_target_is_skipped_with_a_skip_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="zombie", body="iterate on PR #2245", triage=True)
    states = [{
        "repo": "estateaigents/raia", "number": 2245, "state": "MERGED",
        "closed": True, "merged_at": "2026-10-01T00:00:00Z",
    }]
    with patch.object(decomp, "_check_pr_states", return_value=(True, states)):
        reason = decomp._check_stale_pr_targets(tid, "iterate on PR #2245")
    assert reason is not None and reason.startswith("stale-pr-targets")
    assert "decompose-skipped: stale-pr-targets" in _kinds(tid)


def test_open_pr_target_is_not_skipped(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="live", body="iterate on PR #2246", triage=True)
    states = [{
        "repo": "estateaigents/raia", "number": 2246, "state": "OPEN",
        "closed": False, "merged_at": None,
    }]
    with patch.object(decomp, "_check_pr_states", return_value=(True, states)):
        assert decomp._check_stale_pr_targets(tid, "iterate on PR #2246") is None
    # create_task itself records a "created" event in this tree; only guard events matter.
    assert not [k for k in _kinds(tid) if k.startswith("decompose-")]


def test_superseded_disposition_skips_with_a_skip_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="zombie", triage=True)
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tid, "pa", "STAND DOWN — superseded by #900", 0),
        )
        conn.commit()
    reason = decomp._check_superseded_disposition(tid)
    assert reason is not None and reason.startswith("superseded-disposition")
    assert "decompose-skipped: superseded-disposition" in _kinds(tid)
