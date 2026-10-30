"""probe_repair lane exit (P12d): a PASS receipt is required, everything else holds.

Two loss modes: the lane must never open for a card that was not routed into it,
and a card must never leave without a post-routing passing verdict.
"""
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kd


class FakeContract:
    def __init__(self, verdicts):
        self._verdicts = verdicts

    def read_verdicts(self, task_id):
        return list(self._verdicts)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _routed_into_lane(conn, tid: str) -> str:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (kd.PROBE_REPAIR_STATUS, tid))
    kb._append_event(conn, tid, kd.PROBE_REPAIR_STATUS, {"to": kd.PROBE_REPAIR_STATUS})
    return tid


def _pass_verdict(**over):
    rec = {
        "verdict": "pass", "status_ok": True, "runner": "completion-runner",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exit_code": 0, "probe_cmd": "true",
    }
    rec.update(over)
    return rec


def test_lane_opens_only_for_the_repair_class(kanban_home, monkeypatch):
    monkeypatch.setattr(kd, "_load_probe_contract", lambda: FakeContract([_pass_verdict()]))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="not in the lane", assignee="engineer")
        assert kd.probe_repair_exit_ready(conn, tid) is False
        assert kd.resolve_probe_repair_lane(conn) == {
            "exited": [], "held": [], "to": {}, "dry_run": False,
        }
        assert kb._task_status(conn, tid) != kd.PROBE_REPAIR_STATUS
        assert "probe_repair_exited" not in [e.kind for e in kb.list_events(conn, tid)]


def test_missing_receipt_keeps_the_lane_closed(kanban_home, monkeypatch):
    monkeypatch.setattr(kd, "_load_probe_contract", lambda: FakeContract([]))
    with kbc.connect() as conn:
        tid = _routed_into_lane(
            conn, kb.create_task(conn, title="repair me", assignee="engineer", body="prose only"))
        assert kd.probe_repair_pass_receipt(conn, tid) is None
        assert kd.probe_repair_exit_ready(conn, tid) is False
        out = kd.resolve_probe_repair_lane(conn)
        assert (out["exited"], out["held"]) == ([], [tid])
        assert kb._task_status(conn, tid) == kd.PROBE_REPAIR_STATUS


def test_latest_verdict_contradicting_the_pass_still_holds(kanban_home, monkeypatch):
    verdicts = [_pass_verdict(), _pass_verdict(verdict="fail", status_ok=False)]
    monkeypatch.setattr(kd, "_load_probe_contract", lambda: FakeContract(verdicts))
    with kbc.connect() as conn:
        tid = _routed_into_lane(
            conn, kb.create_task(conn, title="repair me", assignee="engineer", body="prose only"))
        assert kd.probe_repair_exit_ready(conn, tid) is False


def test_pass_receipt_exits_to_review_and_emits_the_event(kanban_home, monkeypatch):
    monkeypatch.setattr(kd, "_load_probe_contract", lambda: FakeContract([_pass_verdict()]))
    with kbc.connect() as conn:
        tid = _routed_into_lane(
            conn, kb.create_task(conn, title="repair me", assignee="engineer", body="prose only"))
        assert kd.probe_repair_exit_ready(conn, tid) is True
        out = kd.resolve_probe_repair_lane(conn)
        assert (out["exited"], out["held"], out["to"]) == ([tid], [], {tid: "review"})
        assert kb._task_status(conn, tid) == "review"
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert kd.PROBE_REPAIR_STATUS in kinds  # routing row
        assert kinds.count(kd.PROBE_REPAIR_EXIT_EVENT) == 1
        payload = [e.payload for e in events if e.kind == kd.PROBE_REPAIR_EXIT_EVENT][0]
        assert (payload["to"], payload["via"], payload["exit_code"]) == (
            "review", "probe_repair_pass_receipt", 0)
        # Idempotent: a second sweep finds nothing in the lane.
        assert kd.resolve_probe_repair_lane(conn)["exited"] == []
