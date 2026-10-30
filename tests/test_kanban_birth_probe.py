"""Birth-probe execution (P12b): runnable-but-failing, unrunnable, timeout, bounded capture.

The distinction under test: ``_probe_runnable_at_birth`` answers "can this run at
all at birth", NOT "does it pass" - a probe that runs and exits non-zero is
still runnable, and only a probe that never ran is reported unrunnable.
"""
import pytest

from hermes_cli import kanban_birth_probe as bp


@pytest.fixture(autouse=True)
def _opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bp.BIRTH_PROBE_ENV, "1")


def _script(tmp_path, body: str) -> str:
    path = tmp_path / "probe.sh"
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def test_runnable_but_failing_probe_runs_and_keeps_exit_code(tmp_path):
    script = _script(tmp_path, "#!/bin/sh\necho from-probe\necho boom >&2\nexit 3\n")
    cmd = "bash " + script

    runnable, why = bp._probe_runnable_at_birth(cmd)
    assert runnable is True, why

    rc, out, err = bp._run_birth_probe(cmd, cwd=str(tmp_path))
    assert rc == 3
    assert out == "from-probe"
    assert err == "boom"


def test_missing_interpreter_is_unrunnable_and_never_runs(tmp_path):
    cmd = "definitely-not-installed-xyz " + str(tmp_path / "probe.sh")

    runnable, why = bp._probe_runnable_at_birth(cmd)
    assert runnable is False
    assert "not birth-runnable" in why

    rc, out, err = bp._run_birth_probe(cmd)
    assert rc is None
    assert out == ""
    assert "binary not found" in err


def test_execution_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv(bp.BIRTH_PROBE_ENV, raising=False)
    runnable, why = bp._probe_runnable_at_birth(
        "bash " + _script(tmp_path, "#!/bin/sh\nexit 0\n"))
    assert runnable is False
    assert "disabled at birth" in why


def test_timeout_is_recorded_as_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "_BIRTH_PROBE_TIMEOUT", 1)
    script = _script(tmp_path, "#!/bin/sh\nsleep 5\n")

    rc, out, err = bp._run_birth_probe("bash " + script)
    assert rc is None
    assert out == ""
    assert err.startswith("timed out after")


def test_chatty_probe_output_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "_BIRTH_PROBE_OUTPUT_CAP", 1000)
    script = _script(tmp_path, "#!/bin/sh\nyes x | head -c 200000\n")

    rc, out, _ = bp._run_birth_probe("bash " + script)
    assert rc == 0
    assert "truncated" in out
    assert len(out) < 1100