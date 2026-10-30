"""Create path (P2): a frozen `## ACCEPTANCE` block is never rewritten, and the
DRAFT scaffold goes in only when the body carries no block at all."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


FROZEN = """## ACCEPTANCE
criterion: the frozen contract survives a rewrite
    evidence_cmd: true
    expected: true
    scope_paths:
      - hermes_cli/kanban_db.py
    frozen_at: 2026-10-30T14:34:36Z
"""


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_create_keeps_a_frozen_block_verbatim(kanban_home):
    body = "**Goal**\nsome spec\n\n" + FROZEN
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="frozen card", assignee="programmer", body=body)
        task = kb.get_task(conn, tid)
    assert task.body.count("## ACCEPTANCE") == 1
    assert kb._frozen_acceptance_block(task.body) == FROZEN.rstrip()


def test_create_injects_exactly_one_draft_when_the_body_has_no_block(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="draft card", assignee="programmer", body="**Goal**\nprose only")
        task = kb.get_task(conn, tid)
    assert task.body.count("## ACCEPTANCE") == 1
    assert kb.acceptance_block_is_draft(task.body) is True


def test_second_create_run_adds_no_second_arm_block(kanban_home):
    tooling = "**Goal**\nx\n\n## ACCEPTANCE\ncriterion: c\nevidence_cmd: true\nexpected: true\nscope_paths:\n  - /root/.hermes/scripts/verify\nfrozen_at: 2026-10-30T14:34:36Z"
    with kbc.connect() as conn:
        first = kb.get_task(conn, kb.create_task(conn, title="a", assignee="programmer", body=tooling)).body
        second = kb.get_task(conn, kb.create_task(conn, title="b", assignee="programmer", body=first)).body
    assert first.count("\narm:") == 1
    assert second.count("\narm:") == 1
    assert second == first


def test_bodyless_programmer_card_is_left_alone(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no body", assignee="programmer", triage=True)
        assert kb.get_task(conn, tid).body in (None, "")


def test_specify_preserves_the_frozen_block_over_an_llm_rewrite(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="triage frozen", assignee="programmer", triage=True)
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (FROZEN, tid))
        conn.commit()
    paraphrase = json.dumps({"title": "t", "body": "**Goal**\nHIJACKED rewrite, no block"})

    from hermes_cli import kanban_specify as spec

    monkeypatch.setattr(spec, "_call_aux", lambda *a, **k: (paraphrase, ""))
    outcome = spec.specify_task(tid, author="ace")
    assert outcome.ok is True
    with kbc.connect() as conn:
        body = kb.get_task(conn, tid).body
    assert "HIJACKED" not in body
    assert kb._frozen_acceptance_block(body) == FROZEN.rstrip()
