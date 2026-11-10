"""A protected-instruction write that does not persist must fail loudly, never read as success.

2026-11-10 (reproduced twice by the PA session): an OWNER-APPROVED paragraph added to
`/root/projects/raia/CLAUDE.md` returned success with ``verified: true``, and the file was
back to its previous bytes inside a minute — same size, clean ``git status``, mtime changed
(written, then restored). The revert was NOT the protected-write path: the checkout is
pinned to ``origin/main`` and `/usr/local/bin/raia-repo-owner.sh` (`pin_main`) runs
``git reset --hard origin/main`` + ``git checkout -- .`` on any dirty tracked file every
minute, after copying it to ``/var/lib/raia/orphan/<ts>/`` (three such backups hold the lost
paragraph). So ``verified: true`` was true at call time — what was missing is that the write
was never DURABLE, and nothing said so. A worker cards itself done on that.

``tools/file_tools_write_guards._protected_write_durability_warning`` now reports
``durable: false`` for a protected-instruction write that only lives as an uncommitted
change in a shared main checkout. The post-write verification (hash for write_file,
re-read for patch) is what refuses a write whose bytes are gone — asserted here too.
"""

import json
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _owner_approved(monkeypatch):
    """Stand in for the owner's approval — the gate's own behaviour is covered by
    tests/tools/test_file_write_safety.py and test_protected_instruction_cli_channel.py."""
    import tools.file_tools_write_guards as guards
    monkeypatch.setattr(guards, "_protected_instruction_config", lambda: (True, []))
    monkeypatch.setattr(guards, "_request_protected_instruction_approval",
                        lambda reasons, task_id="default": None)


def _git(root, *args):
    return subprocess.run(("git", "-C", str(root), *args), check=True, capture_output=True, text=True)


def _repo(tmp_path, branch="main"):
    """A checkout with a committed CLAUDE.md, on *branch* — main = the pinned shared case."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "CLAUDE.md").write_text("# Rules\n\n1. old rule\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


NEW_TEXT = "# Rules\n\n1. old rule\n2. Harness budget rule\n"


def test_pinned_checkout_write_is_flagged_not_durable(tmp_path):
    """The exact 2026-11-10 shape: write lands, a main-pin reset discards it silently.
    The call must say the change is not durable instead of reading as plain success."""
    from tools.file_tools import read_file_tool, write_file_tool

    root = _repo(tmp_path)
    target = root / "CLAUDE.md"
    read_file_tool(str(target))

    res = json.loads(write_file_tool(str(target), NEW_TEXT))

    assert "error" not in res, res
    assert res["verified"] is True  # the bytes DID land — that was never the lie
    assert res["durable"] is False
    assert "NOT DURABLE" in res["_warning"]
    assert "git reset --hard" in res["_warning"]
    assert "Harness budget rule" in target.read_text()

    # The pass that ate the paragraph: uncommitted tracked file reset to HEAD.
    _git(root, "reset", "--hard", "-q", "HEAD")
    assert "Harness budget rule" not in target.read_text()


def test_patch_into_pinned_checkout_is_flagged_not_durable(tmp_path):
    """Same for the `patch` tool, which is what the PA used to reproduce it."""
    from tools.file_tools import read_file_tool, patch_tool

    root = _repo(tmp_path)
    target = root / "CLAUDE.md"
    read_file_tool(str(target))

    res = json.loads(patch_tool(path=str(target), old_string="1. old rule",
                                new_string="1. old rule\n2. Harness budget rule"))

    assert "error" not in res, res
    assert res["durable"] is False
    assert "NOT DURABLE" in res["_warning"]


def test_committed_branch_write_is_not_flagged(tmp_path):
    """A task branch (linked worktree or not) is not pinned, so no false alarm."""
    from tools.file_tools import read_file_tool, write_file_tool

    root = _repo(tmp_path, branch="fix/harness-budget")
    target = root / "CLAUDE.md"
    read_file_tool(str(target))

    res = json.loads(write_file_tool(str(target), NEW_TEXT))

    assert "error" not in res, res
    assert res["verified"] is True
    assert "durable" not in res


def _drop_the_bytes(monkeypatch):
    """Make the atomic write land the OLD content: the write does not persist."""
    from tools.file_operations import ShellFileOperations
    real = ShellFileOperations._atomic_write
    monkeypatch.setattr(ShellFileOperations, "_atomic_write",
                        lambda self, path, content: real(self, path, "# Rules\n\n1. old rule\n"))


def test_non_persisting_write_returns_explicit_failure(tmp_path, monkeypatch):
    from tools.file_tools import read_file_tool, write_file_tool

    target = _repo(tmp_path) / "CLAUDE.md"
    read_file_tool(str(target))
    _drop_the_bytes(monkeypatch)

    res = json.loads(write_file_tool(str(target), NEW_TEXT))

    assert "error" in res, res
    assert res.get("verified") is not True
    assert "did not persist" in res["error"]


def test_non_persisting_patch_returns_explicit_failure(tmp_path, monkeypatch):
    from tools.file_tools import read_file_tool, patch_tool

    target = _repo(tmp_path) / "CLAUDE.md"
    read_file_tool(str(target))
    _drop_the_bytes(monkeypatch)

    res = json.loads(patch_tool(path=str(target), old_string="1. old rule",
                                new_string="1. old rule\n2. Harness budget rule"))

    assert "error" in res, res
    assert res.get("verified") is not True
    assert "did not persist" in res["error"] or "Post-write verification failed" in res["error"]
