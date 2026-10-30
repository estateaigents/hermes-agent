"""Frozen `## ACCEPTANCE` blocks: parse, DRAFT detection, verdict, freeze events (P12a).

One loss mode only: a block EDITED after it was frozen, whose probe still passes
against the rewritten test. The verdict must fail that and still allow a
well-formed frozen card.
"""
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


FROZEN = """## ACCEPTANCE
criterion: the freeze event lands
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


def test_extract_and_draft_detection():
    assert len(kb._extract_acceptance_blocks(FROZEN)) == 1
    assert kb.acceptance_block_is_draft(FROZEN) is False
    draft = "## ACCEPTANCE\ncriterion: <REQUIRED: what>\nfrozen_at: 2026-10-30T00:00Z\n"
    assert kb.acceptance_block_is_draft(draft) is True
    # A block-less body is a different gate class, never a draft.
    assert kb.acceptance_block_is_draft("just prose") is False
    # percent-0A bodies must be decoded before parsing.
    encoded = FROZEN.replace("\n", "%0A")
    assert kb.acceptance_block_is_draft(encoded) is False
    assert kb.acceptance_gate_verdict(encoded)[0] is None


def test_verdict_fails_a_block_edited_after_freeze_even_though_the_probe_passes():
    edited = FROZEN.replace("expected: true", "expected: false")
    defect, note = kb.acceptance_gate_verdict(edited, frozen_body=FROZEN)
    assert note == ""
    assert "EDITED after it was frozen" in defect


def test_verdict_allows_a_well_formed_frozen_card():
    assert kb.acceptance_gate_verdict(FROZEN, frozen_body=FROZEN) == (None, "")
    # No reference snapshot -> nothing to contradict; not a fail.
    assert kb.acceptance_gate_verdict(FROZEN) == (None, "")


def test_verdict_notes_when_nothing_is_frozen():
    defect, note = kb.acceptance_gate_verdict("no block here")
    assert (defect, bool(note)) == (None, True)


def test_create_emits_acceptance_frozen(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="frozen at birth", assignee="programmer", body=FROZEN)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("acceptance_frozen") == 1
        assert "acceptance_refrozen" not in kinds
        payload = [e.payload for e in kb.list_events(conn, tid) if e.kind == "acceptance_frozen"][0]
        assert payload["expected"] == "true"
        assert payload["frozen_at"] == "2026-10-30T14:34:36Z"
        assert len(payload["sha256"]) == 64


def test_specify_emits_frozen_then_refrozen(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="triage card", assignee="programmer", triage=True)
        assert kb.specify_triage_task(conn, tid, body=FROZEN) is True
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert (kinds.count("acceptance_frozen"), kinds.count("acceptance_refrozen")) == (1, 0)
        # Re-specifying the SAME body is not a freeze event.
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        assert kb.specify_triage_task(conn, tid, body=FROZEN) is True
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("acceptance_refrozen") == 0
        # A real edit is a refreeze, and the event names the prior digest.
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        changed = FROZEN.replace("expected: true", "expected: FALSE")
        assert kb.specify_triage_task(conn, tid, body=changed) is True
        refrozen = [e.payload for e in kb.list_events(conn, tid) if e.kind == "acceptance_refrozen"]
        assert len(refrozen) == 1 and refrozen[0]["expected"] == "FALSE"
        assert refrozen[0]["previous_sha256"] != refrozen[0]["sha256"]


def test_birth_denial_is_one_row_per_event_and_the_count_agrees(kanban_home):
    with kbc.connect() as conn:
        before = kb.birth_denial_count(conn)
        kb.record_birth_denial(conn, title="bad card", assignee="programmer",
                               reason="missing ## ACCEPTANCE block")
        rows = kb.read_birth_denials(conn)
        assert len(rows) == before + 1
        entry = rows[-1]
        assert entry["defect_class"] == "missing_acceptance_block"
        assert (entry["kind"], entry["title"]) == ("acceptance", "bad card")
        assert kb.birth_denial_count(conn) == len(rows)


def test_birth_denial_since_filter(kanban_home):
    with kbc.connect() as conn:
        old_id = kb.record_birth_denial(conn, title="old", assignee="programmer",
                                        reason="unparseable shell")
        kb.record_birth_denial(conn, title="new", assignee="programmer",
                               reason="unfilled placeholder")
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 86400 WHERE id = ?", (old_id,))
        cutoff = int(time.time()) - 3600
        assert [r["title"] for r in kb.read_birth_denials(conn, since=cutoff)] == ["new"]
        assert kb.birth_denial_count(conn, since=cutoff) == 1
        assert kb.birth_denial_count(conn) == 2
        assert kb.birth_denial_count(conn, since="2000-01-01T00:00:00Z") == 2


def test_stamp_birth_verified_is_idempotent():
    once = kb._stamp_birth_verified(FROZEN, "2026-10-30T14:33Z - both directions")
    assert once.count("birth-verified:") == 1
    assert kb._stamp_birth_verified(once, "second note") == once
    encoded = kb._stamp_birth_verified(FROZEN.replace("\n", "%0A"), "note")
    assert "birth-verified: note" in encoded
    plain = kb._stamp_birth_verified("just prose", "n")
    assert plain.count("birth-verified:") == 1
    assert kb._stamp_birth_verified(plain, "n") == plain
