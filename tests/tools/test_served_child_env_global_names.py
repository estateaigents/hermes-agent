"""A profile ``.env``'s global lines must not reach a served child (t_960abc57).

``agent.secret_scope.load_env_file`` is a read-only tokenizer that keeps ``${PATH}`` raw, so the profile
secret scope carried ``PATH=<home>/bin:${PATH}`` verbatim and every consumer that overlays the scope
onto a child env published the literal token. The child then started with no system bin dir in PATH and
its own ``#!/usr/bin/env bash`` children died ``/usr/bin/env: 'bash': No such file or directory`` (127).
"""

import os

from agent.secret_scope import build_profile_secret_scope
from tools.environments.local import served_profile_child_env

_BASE_PATH = "/usr/local/bin:/usr/bin:/bin"


def _homes(tmp_path):
    a = tmp_path / ".hermes"
    b = a / "profiles" / "b"
    b.mkdir(parents=True)
    (a / ".env").write_text("A_MARKER=a\n", encoding="utf-8")
    (b / ".env").write_text("export PATH=/x:${PATH}\nB_MARKER=b\n", encoding="utf-8")
    return a, b


def test_profile_scope_drops_globals_from_the_dotenv(tmp_path):
    """The scope is documented to hold profile secrets only — the raw global line must not be in it."""
    scope = build_profile_secret_scope(_homes(tmp_path)[1])
    assert "PATH" not in scope
    assert scope["B_MARKER"] == "b"


def test_served_child_env_publishes_the_parent_path_not_the_raw_dotenv_line(tmp_path, monkeypatch):
    """The child gets a usable PATH (the parent's) and still its non-global profile values."""
    a, b = _homes(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(a))

    env = served_profile_child_env(
        base={"PATH": _BASE_PATH, "HOME": "/root"}, target_home=b, inherit_credentials=True)

    assert "${PATH}" not in env["PATH"]
    assert "/usr/bin" in env["PATH"].split(os.pathsep)
    assert env["PATH"] == _BASE_PATH
    assert env["B_MARKER"] == "b"
