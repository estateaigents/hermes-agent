"""Receipt leg 3: a run is bound to exactly the artifact it wrote (issue #848).

The write is exercised on the real run path (``_run_one_job_body`` with a ``no_agent`` script job)
in an isolated ``HERMES_HOME``: no model, no network, no send.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes"
    (h / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    # The build tree lives under the real home, so PM's payload-manifest probe (dependency venv
    # selection, hermes bin dir on PATH) is a real-home read the test guard refuses. Neither is
    # needed for a no_agent script run.
    monkeypatch.setattr("hermes_cli._launchers.resolve_store_python", lambda _repo: None)
    monkeypatch.setattr("tools.environments.local._resolve_hermes_bin_dir", lambda: None)
    return h


def _job(home, sentinel="run"):
    (home / "scripts" / "run.sh").write_text(f'#!/usr/bin/env bash\necho "{sentinel}"\n')
    from cron.jobs import create_job

    return create_job(prompt=None, schedule="every 5m", script="run.sh", no_agent=True, deliver="local")


def _run(job):
    from cron.scheduler import _run_one_job_body

    return _run_one_job_body(dict(job))


def _rows(home):
    db = sqlite3.connect(home / "cron" / "executions.db")
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute("select * from executions order by id")]


def test_real_run_records_its_own_artifact(home):
    job = _job(home)
    assert _run(job) is True
    (row,) = _rows(home)
    assert row["status"] == "completed", (row["status"], row["error"])
    path = Path(row["output_path"])
    assert path.parent.name == job["id"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert f"**Job ID:** {job['id']}" in path.read_text(encoding="utf-8")
    assert row["id"] in path.name  # execution-unique filename, not a shared per-second stem


def test_second_write_never_repoints_the_receipt(home):
    from cron.executions import set_execution_output_path

    job = _job(home)
    assert _run(job) is True
    (row,) = _rows(home)
    other = home / "cron" / "output" / job["id"] / "other.md"
    other.write_text("other run", encoding="utf-8")
    assert set_execution_output_path(row["id"], str(other)) is False
    assert _rows(home)[0]["output_path"] == row["output_path"]
    assert Path(row["output_path"]).exists()


def test_recording_failure_leaves_run_and_delivery_untouched(home, monkeypatch, caplog):
    from cron import scheduler

    def boom(*_a, **_kw):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(scheduler, "set_execution_output_path", boom)
    job = _job(home, "sentinel-payload")
    with caplog.at_level(logging.WARNING):
        assert _run(job) is True
    (row,) = _rows(home)
    assert (row["status"], row["delivery_outcome"], row["output_path"]) == ("completed", "suppressed", None)
    (artifact,) = list((home / "cron" / "output" / job["id"]).glob("*.md"))
    assert "sentinel-payload" in artifact.read_text(encoding="utf-8")
    assert any("could not record output_path" in r.message for r in caplog.records)

    # Control: the same job, recording intact, produces the same outcome and delivery state.
    from cron.executions import set_execution_output_path

    monkeypatch.setattr(scheduler, "set_execution_output_path", set_execution_output_path)
    assert _run(job) is True
    control = next(r for r in _rows(home) if r["id"] != row["id"])
    assert (control["status"], control["delivery_outcome"]) == (row["status"], row["delivery_outcome"])
    assert control["output_path"] is not None


def test_terminalisation_redaction_unchanged_and_binding_resolves(home, monkeypatch):
    from cron import delivery_queue

    job = _job(home)
    assert _run(job) is True
    (row,) = _rows(home)
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
    delivery_queue.enqueue(row["id"], dict(job), "payload text")
    claimed = delivery_queue.claim_next()
    assert claimed and claimed["execution_id"] == row["id"]
    assert delivery_queue._finish(row["id"], error=None, suppressed=False)
    assert sqlite3.connect(delivery_queue.DELIVERY_DB).execute(
        "select status, job_json, content from deliveries where execution_id=?", (row["id"],)
    ).fetchone() == ("delivered", "{}", "")
    resolved = Path(_rows(home)[0]["output_path"])
    assert resolved.exists()
    assert f"**Job ID:** {job['id']}" in resolved.read_text(encoding="utf-8")


def test_legacy_callers_keep_todays_filename_and_listing_parses_both(home):
    from cron.jobs import save_job_output
    from hermes_cli.web_routers.cron import _cron_output_run_timestamp

    legacy = save_job_output("legacy-job", "no execution id")
    assert "__" not in legacy.stem

    suffixed = save_job_output("legacy-job", "with execution id", execution_id="a" * 32)
    assert suffixed.name.endswith(f"__{'a' * 32}.md")
    assert _cron_output_run_timestamp(legacy) is not None
    assert _cron_output_run_timestamp(suffixed) is not None
    # Same wall-clock second parsed, so the cron output listing keeps its timestamps.
    assert int(_cron_output_run_timestamp(legacy)) == int(_cron_output_run_timestamp(suffixed))

