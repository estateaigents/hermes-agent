"""P12e port: creator-gated fanout repair, held lineage root, block-loop clear.

* ``repair_creator_gated_fanout`` only touches edges the child's own creator
  made (deny direction: a non-creator edge is left alone), and ``dry_run``
  reports the same violations without writing anything.
* ``held_lineage_root`` walks the ``from_decompose_of`` created-event chain and
  returns None (never raises) on a cycle or a missing ancestor.
* ``clear_block_loop`` zeroes a latched card's recurrence counter, records a
  ``block_loop_cleared`` event, and refuses (not raises) a second clear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_graph as kbg


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _stalled_fanout(conn, *, creator_made: bool):
    """Blocked creator + one ``todo`` child linked under it, fan-out recorded.

    ``creator_made`` decides whether the child's ``created`` payload claims this
    parent as ``creator_task_id`` (a real creator fan-out) or not (an ordinary
    dependency link that repairs must leave alone).
    """
    creator = kb.create_task(conn, title="root", assignee="engineer")
    child = kb.create_task(
        conn, title="child", assignee="engineer",
        parents=(creator,), creator_task_id=creator if creator_made else None,
    )
    assert conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child,)
    ).fetchone()["status"] == "todo"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' WHERE id = ?",
            (creator,),
        )
        # 6a: the creator's decompose event names this child, so only the creator
        # gate itself can exclude the deny case.
        kb._append_event(
            conn, creator, "decomposed", {"child_ids": [child], "root_assignee": "engineer"},
        )
    return creator, child


def test_repair_inverts_creator_made_edge(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        creator, child = _stalled_fanout(conn, creator_made=True)
        report = kbg.repair_creator_gated_fanout(conn)

        assert report["violations"] == [(creator, child)]
        assert report["removed"] == [(creator, child)]
        assert report["added"] == [(child, creator)]
        assert kb.parent_ids(conn, creator) == [child]
        assert kb.parent_ids(conn, child) == []
        # recompute_ready ran: the now-unparented child promotes out of todo.
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child,)
        ).fetchone()["status"] == "ready"
        assert [e.kind for e in kb.list_events(conn, child) if e.kind == "unlinked"]
        assert [e.kind for e in kb.list_events(conn, creator) if e.kind == "linked"]


def test_repair_denies_non_creator_edge(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        creator, child = _stalled_fanout(conn, creator_made=False)
        report = kbg.repair_creator_gated_fanout(conn)

        assert report == {"violations": [], "removed": [], "added": [], "skipped": []}
        # Nothing written: the edge survives and no repair events appear.
        assert kb.parent_ids(conn, child) == [creator]
        assert kb.parent_ids(conn, creator) == []
        assert [e.kind for e in kb.list_events(conn, child) if e.kind == "unlinked"] == []


def test_repair_dry_run_plans_without_writing(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        creator, child = _stalled_fanout(conn, creator_made=True)
        report = kbg.repair_creator_gated_fanout(conn, dry_run=True)

        assert report["violations"] == [(creator, child)]
        assert report["removed"] == [] and report["added"] == []
        assert kb.parent_ids(conn, child) == [creator]
        assert kb.parent_ids(conn, creator) == []
        assert [e.kind for e in kb.list_events(conn, child) if e.kind == "unlinked"] == []


def test_held_lineage_root_names_the_held_root(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="root", assignee="engineer")
        child = kb.create_task(conn, title="child", assignee="engineer")
        with kb.write_txn(conn):
            kb._append_event(conn, child, "created", {"from_decompose_of": root})
        assert kbg.held_lineage_root(conn, child) is None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' WHERE id = ?",
                (root,),
            )
        assert kbg.held_lineage_root(conn, child) == (root, "needs_input")


def test_held_lineage_root_terminates_on_a_cycle(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="engineer")
        b = kb.create_task(conn, title="b", assignee="engineer")
        with kb.write_txn(conn):
            kb._append_event(conn, a, "created", {"from_decompose_of": b})
            kb._append_event(conn, b, "created", {"from_decompose_of": a})
        assert kbg.held_lineage_root(conn, a) is None


def test_held_lineage_root_ignores_a_missing_ancestor(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="child", assignee="engineer")
        with kb.write_txn(conn):
            kb._append_event(conn, child, "created", {"from_decompose_of": "t_gone"})
        assert kbg.held_lineage_root(conn, child) is None


# ---------------------------------------------------------------------------
# clear_block_loop
# ---------------------------------------------------------------------------


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="engineer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _latched_task(conn, title="t"):
    """Block -> unblock -> re-block the same kind until the breaker latches."""
    tid = _running_task(conn, title=title)
    kb.block_task(conn, tid, reason="x", kind="capability")
    assert kb.unblock_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    kb.block_task(conn, tid, reason="x", kind="capability")
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "triage"
    return tid


def test_clear_block_loop_clears_and_records_an_event(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _latched_task(conn)
        ok, err = kb.clear_block_loop(conn, tid, actor="owner", reason="owner reviewed the block")
        assert (ok, err) == (True, None)
        row = conn.execute(
            "SELECT block_kind, block_recurrences FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["block_recurrences"] == 0
        assert row["block_kind"] == "capability", "a cleared latch re-arms on the same cause"
        cleared = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_cleared"]
        assert len(cleared) == 1
        payload = cleared[0].payload or {}
        assert payload["actor"] == "owner"
        assert payload["reason"] == "owner reviewed the block"
        assert payload["previous_recurrences"] == kb.BLOCK_RECURRENCE_LIMIT


def test_clear_block_loop_is_idempotent(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _latched_task(conn)
        assert kb.clear_block_loop(conn, tid, actor="owner", reason="first")[0] is True
        ok, err = kb.clear_block_loop(conn, tid, actor="owner", reason="second")
        assert ok is False
        assert "nothing to clear" in (err or "")
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "block_loop_cleared"]) == 1


def test_clear_block_loop_refuses_a_blank_reason(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _latched_task(conn)
        ok, err = kb.clear_block_loop(conn, tid, actor="owner", reason="  ")
        assert ok is False and "reason is required" in (err or "")
