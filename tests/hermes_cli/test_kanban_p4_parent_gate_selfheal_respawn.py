"""P4 subsets ported onto v0.22.5: GOV-4 parent-card gate, retired-assignee
self-heal in the dispatcher, and the changes_requested respawn-guard exemption.

Three independent behaviours, three tests (plus the boundary cases the same
code path has to get right: a leaf card is unaffected, and a guard episode
writes ONE ``respawn_guarded`` event, not one per tick).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _profile(home: Path, name: str, config: str) -> None:
    """Materialise a real on-disk profile so ``profile_exists``/retired read it."""
    d = home / "profiles" / name
    d.mkdir(parents=True)
    (d / "config.yaml").write_text(config, encoding="utf-8")


def _kinds(conn, tid: str) -> list[str]:
    return [e.kind for e in kb.list_events(conn, tid)]


# ---------------------------------------------------------------------------
# P4d - GOV-4 parent-card gate: a parent cannot complete with an open child
# ---------------------------------------------------------------------------


def test_parent_with_queued_child_cannot_complete(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        run = kb.claim_task(conn, parent)
        assert run is not None and run.status == "running"
        # A child that is dispatchable NOW: the parent would declare done work
        # that is still queued.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))

        with pytest.raises(kb.ChildrenNotTerminalError) as exc:
            kb.complete_task(conn, parent, result="done", expected_run_id=run.current_run_id)
        # The rejection names the child so the worker knows what to finish.
        assert child in str(exc.value)
        assert kb.get_task(conn, parent).status == "running"


def test_parked_child_does_not_block_the_parent(tmp_path, monkeypatch):
    """A child parked for human ops (``initial_status="blocked"``) is upstream's
    intended state, not work in flight: the parent must still be able to
    complete over it."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        kb.create_task(
            conn, title="parked child", assignee="worker", parents=[parent],
            initial_status="blocked",
        )
        run = kb.claim_task(conn, parent)
        assert kb.complete_task(
            conn, parent, result="done", expected_run_id=run.current_run_id,
        )
        assert kb.get_task(conn, parent).status == "done"


def test_dependency_waiting_child_does_not_block_the_parent(tmp_path, monkeypatch):
    """A child still in ``todo`` is waiting on THIS parent: blocking its
    completion would deadlock the pair (the negative control for the gate)."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        run = kb.claim_task(conn, parent)
        assert kb.complete_task(
            conn, parent, result="done", expected_run_id=run.current_run_id,
        )
        assert kb.get_task(conn, parent).status == "done"
        # Finishing the parent is what releases the child.
        assert kb.get_task(conn, child).status == "ready"


def test_parent_completes_once_children_are_terminal(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        run = kb.claim_task(conn, parent)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(time.time()), child),
            )
        assert kb.complete_task(
            conn, parent, result="done", expected_run_id=run.current_run_id,
        )
        assert kb.get_task(conn, parent).status == "done"


def test_leaf_card_completes_with_no_children(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        leaf = kb.create_task(conn, title="leaf", assignee="worker")
        run = kb.claim_task(conn, leaf)
        assert kb.complete_task(conn, leaf, result="done", expected_run_id=run.current_run_id)
        assert kb.get_task(conn, leaf).status == "done"


# ---------------------------------------------------------------------------
# P4e - a blocked card behind a retired / deleted profile is reassigned
# ---------------------------------------------------------------------------


def test_blocked_card_with_retired_assignee_is_reassigned(tmp_path, monkeypatch):
    """The block is addressed to a worker that can never come back: reassign it
    to a live profile (the creator) instead of leaving it deadlocked."""
    home = _isolated_home(tmp_path, monkeypatch)
    _profile(home, "ghost", "retired: true\n")
    _profile(home, "pm", "name: pm\n")
    with kbc.connect() as conn:
        retired = kb.create_task(conn, title="retired", assignee="ghost", created_by="pm")
        missing = kb.create_task(conn, title="missing", assignee="gone", created_by="pm")
        live = kb.create_task(conn, title="live", assignee="pm", created_by="pm")
        for tid in (retired, missing, live):
            kb.block_task(conn, tid, reason="waiting on the worker", kind="dependency")

        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)

        for tid in (retired, missing):
            assert kb.get_task(conn, tid).assignee == "pm"
            # Never auto-complete: it stays blocked, just no longer ownerless.
            assert kb.get_task(conn, tid).status == "blocked"
            assert "self_healed" in _kinds(conn, tid)
        # A live assignee is left exactly as the operator left it.
        assert kb.get_task(conn, live).assignee == "pm"
        assert "self_healed" not in _kinds(conn, live)


def test_retired_assignee_heal_is_idempotent(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    _profile(home, "ghost", "retired: true\n")
    _profile(home, "pm", "name: pm\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="retired", assignee="ghost", created_by="pm")
        kb.block_task(conn, tid, reason="waiting", kind="dependency")
        for _ in range(3):
            kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert _kinds(conn, tid).count("self_healed") == 1


def test_retired_assignee_with_no_live_creator_is_unassigned(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    _profile(home, "ghost", "retired: true\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="orphan", assignee="ghost", created_by="ghost")
        kb.block_task(conn, tid, reason="waiting", kind="dependency")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        # Surfaced as unassigned work rather than parked behind a dead profile.
        assert kb.get_task(conn, tid).assignee is None


# ---------------------------------------------------------------------------
# P4g - the changes_requested requeue must not respawn-loop
# ---------------------------------------------------------------------------


def test_changes_requested_lifts_the_recent_success_guard(tmp_path, monkeypatch):
    """request_changes requeues a card right after its completed run; the ready
    lane must treat that as a deliberate re-run, not as a respawn to guard."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix-me", assignee="worker")
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'worker', 'completed', 'completed', ?, ?)",
                (tid, now - 1000, now - 100),
            )
        assert kbd.check_respawn_guard(conn, tid) == "recent_success"

        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'changes_requested', '{\"status\": \"ready\"}', ?)",
                (tid, now - 50),
            )
        assert kbd.check_respawn_guard(conn, tid) is None


def test_guarded_respawn_writes_one_event_per_episode(tmp_path, monkeypatch):
    """Live evidence: respawn_guarded is the board's most-fired event (115,129
    rows). One row per guarded tick is a spiral; one per episode is the record."""
    home = _isolated_home(tmp_path, monkeypatch)
    _profile(home, "worker", "name: worker\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="quota-walled", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                ("429 quota exceeded", tid),
            )
        spawned: list[str] = []
        for _ in range(3):
            res = kbd.dispatch_once(conn, spawn_fn=lambda t, *a, **k: spawned.append(t.id))
            assert [r for r in res.respawn_guarded if r[0] == tid] == [(tid, "blocker_auth")]
        assert spawned == []
        assert _kinds(conn, tid).count("respawn_guarded") == 1
