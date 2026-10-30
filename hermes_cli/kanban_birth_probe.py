"""Birth-probe EXECUTION sibling (P12b) — ``_probe_runnable_at_birth`` / ``_run_birth_probe``.

Split out of ``kanban_db.py`` so the monolith does not re-bloat. Both are the
rule-5 early-false convenience: run one card-authored probe, read-only, BEFORE
the task row exists. Gate 6 still runs the probe for real at completion, so
refusing to run is never a deny.

Execution is OPT-IN and payload-audited: a card body is data, and running
body-controlled commands in the create path made card creation an
arbitrary-code-execution surface. Default OFF
(``HERMES_KANBAN_BIRTH_PROBE=1``), argv-only (never a shell), structurally
read-only leads.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# Rule 5: leads whose probes are runnable before a repo checkout exists.
# ``python3`` is here because ops/monitoring probes are commonly ``python3
# /abs/script.py`` and python3 is a stable host binary; it is audited exactly
# like bash/sh (``-c`` inline payload refused, first non-flag token must be an
# ABSOLUTE script path). Bare ``python`` is deliberately NOT a lead: it may be
# absent, a py2 shim, or a venv-relative PATH entry, so it cannot be trusted to
# be the interpreter the card will actually run under.
_BIRTH_PROBE_LEADS = frozenset({"bash", "sh", "python3", "gh", "psql"})
# 30s (card spec) - a slow read-only probe (psql/gh) must not be mistaken for
# "could not run" and stamped as unverified.
_BIRTH_PROBE_TIMEOUT = 30
# Captured stdout/stderr are embedded in the create_task() ValueError and in
# the birth-denial ledger, so a chatty probe must not buffer unbounded bytes
# (the OOM loss mode this port sits next to).
_BIRTH_PROBE_OUTPUT_CAP = 64 * 1024
# A repo-relative path needs a checkout, which the create path does not have.
_REPO_RELATIVE_RX = re.compile(
    r"(?:^|[\s'\"=(])(?:hermes_cli|tests|src|scripts|plugins|\.worktrees|"
    r"\.github)/"
)

BIRTH_PROBE_ENV = "HERMES_KANBAN_BIRTH_PROBE"
_SHELL_META_RX = re.compile(r"[;&|`<>]|\$\(|\n")

# gh subcommands that cannot mutate; write flags are refused for all of them.
_GH_READ_SUBS = frozenset({
    "api", "auth", "browse", "checks", "issue", "label", "list", "pr",
    "release", "repo", "run", "search", "status", "view", "workflow",
})
# gh accepts joined spellings with no separator (`-XDELETE`, `-fbody=hi`) and
# long flags as `--name=value`, so an exact-match deny list admits mutations;
# the audit is PREFIX-aware and every documented alias is enumerated.
_GH_WRITE_FLAGS = frozenset({
    "-X", "--method", "-f", "--field", "-F", "--raw-field", "--input",
    "--approve", "--request-changes",
})
# Short gh flags that accept their value concatenated, no separator.
_GH_WRITE_SHORT = ("-X", "-f", "-F")
# A read SUBCOMMAND name does not decide read-only: `gh issue`/`gh pr`/
# `gh workflow` are read subs whose ACTION mutates (`issue close`, `pr merge`,
# `workflow run`). The action (second positional) is audited against an
# allow-list, so an unrecognised action is DENIED rather than admitted.
_GH_READ_ACTIONS = {
    "api": None,
    # `gh auth token` PRINTS a live credential - credential disclosure, not a
    # state read; the birth probe has no legitimate use for it.
    "auth": frozenset({"status"}),
    "browse": frozenset(),
    "checks": frozenset({"list", "view"}),
    "issue": frozenset({"list", "view", "status"}),
    "label": frozenset({"list"}),
    "list": frozenset({""}),
    "pr": frozenset({"list", "view", "checks", "diff", "status"}),
    "release": frozenset({"list", "view"}),
    "repo": frozenset({"list", "view"}),
    "run": frozenset({"list", "view", "watch"}),
    "search": frozenset({"code", "commits", "issues", "prs", "repos"}),
    "status": frozenset({""}),
    "view": frozenset({""}),
    "workflow": frozenset({"list", "view"}),
}
# psql short options that TAKE A VALUE (getopt). Everything else is a boolean,
# so a cluster such as `-tAqc` must be walked character by character: the
# value-taking flag consumes the rest of the token, or the next token.
_PSQL_VALUE_SHORTS = frozenset("cdfFhLopPRTUv")


def _birth_probe_enabled() -> bool:
    """Is birth-time probe EXECUTION explicitly opted in?"""
    return (os.environ.get(BIRTH_PROBE_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on"}


def _gh_write_flag(tokens) -> str:
    """The first gh write/mutation flag token in ``tokens``, or ``""``."""
    for t in tokens:
        if not t.startswith("-"):
            continue
        if t in _GH_WRITE_FLAGS:
            return t
        if t.startswith("--"):
            if "=" in t and t.split("=", 1)[0] in _GH_WRITE_FLAGS:
                return t
            continue
        for short in _GH_WRITE_SHORT:
            if t.startswith(short) and len(t) > len(short):
                return t
    return ""


def _probe_runnable_at_birth(cmd: str) -> tuple[bool, str]:
    """``(runnable, reason)`` - may this probe run, READ-ONLY, at birth?

    A probe that runs and returns false is still runnable: this answers "can
    the command run AT ALL at birth" (interpreter/file present, not a
    hallucinated command), NOT "does it pass". Callers depend on the
    distinction to avoid recording a denial for an unrunnable command versus a
    failing one.

    Execution requires BOTH opt-in (``HERMES_KANBAN_BIRTH_PROBE=1``, default
    OFF so a stored card body is data again) and a payload-level read-only
    audit: argv-exec'd (never through a shell), no shell metacharacters,
    ``bash``/``sh``/``python3`` may only name an absolute script path (no
    ``-c`` inline payload), ``gh`` may only use read subcommands without write
    flags, ``psql`` may only run SELECT/SHOW SQL.
    """
    cmd = (cmd or "").strip()
    if not _birth_probe_enabled():
        return (False, "probe execution disabled at birth (opt in with %s=1)"
                % BIRTH_PROBE_ENV)
    if _SHELL_META_RX.search(cmd):
        return (False, "payload has shell metacharacters (executed as argv, "
                       "never through a shell)")
    try:
        import shlex

        tokens = shlex.split(cmd, posix=True)
    except Exception:
        return (False, "unparseable shell")
    if not tokens:
        return (False, "empty command")
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    if i >= len(tokens):
        return (False, "no command after env assignments")
    lead = os.path.basename(tokens[i])
    if lead not in _BIRTH_PROBE_LEADS:
        return (False, "lead %r is not birth-runnable (%s)"
                % (lead, "/".join(sorted(_BIRTH_PROBE_LEADS))))
    if _REPO_RELATIVE_RX.search(cmd):
        return (False, "references repo-relative paths (needs a checkout)")
    rest = tokens[i + 1:]
    if lead in {"bash", "sh", "python3"}:
        if "-c" in rest:
            return (False, "`%s -c` is an inline code payload - freeze a script "
                           "path instead" % lead)
        script = [t for t in rest if not t.startswith("-")]
        if not script:
            return (False, "no script path after `%s`" % lead)
        if not os.path.isabs(script[0]):
            return (False, "script path %r is not absolute" % script[0])
    elif lead == "gh":
        bad = _gh_write_flag(rest)
        if bad:
            return (False, "gh write/mutation flag %r in payload" % bad)
        positional = [t for t in rest if not t.startswith("-")]
        sub = positional[0] if positional else ""
        if sub not in _GH_READ_SUBS:
            return (False, "gh subcommand %r is not a read subcommand" % sub)
        allowed = _GH_READ_ACTIONS.get(sub)
        if allowed is not None:
            action = positional[1] if len(positional) > 1 else ""
            if action not in allowed:
                return (False, "gh %s action %r is not a read action "
                               "(allowed: %s)"
                        % (sub, action, ", ".join(sorted(allowed)) or "none"))
    elif lead == "psql":
        # Every `-c` / `--command` occurrence is collected (a second `-c` used
        # to overwrite the first, hiding its payload) and every script-file
        # spelling is refused up front. glibc getopt_long - which real psql
        # uses - accepts any UNAMBIGUOUS PREFIX of a long option, so every
        # prefix of the canonical names is matched and prefixes psql itself
        # would reject as ambiguous (`--c`, `--fi`) fail closed.
        sql_payloads: list[str] = []
        j = 0
        while j < len(rest):
            t = rest[j]
            if t.startswith("--"):
                body = t[2:]
                val: Optional[str] = None
                if "=" in body:
                    body, val = body.split("=", 1)
                matches_command = bool(body) and "command".startswith(body)
                matches_file = bool(body) and "file".startswith(body)
                if (matches_command and "csv".startswith(body)) or (
                        matches_file
                        and ("field-separator".startswith(body)
                             or "field-separator-zero".startswith(body))):
                    return (False, "psql --%s is an ambiguous option prefix"
                                   % body)
                if matches_file:
                    return (False, "psql script-file payload is not "
                                   "read-auditable")
                if matches_command:
                    if val is not None:
                        sql_payloads.append(val)
                        j += 1
                        continue
                    if j + 1 >= len(rest):
                        return (False, "psql --command has no SQL payload")
                    sql_payloads.append(rest[j + 1])
                    j += 2
                    continue
                j += 1
                continue
            if t.startswith("-") and len(t) > 1:
                # getopt semantics: booleans may be CLUSTERED and a
                # value-taking short flag consumes the rest of the token (or
                # the next token). `-tAc "DELETE ..."` used to slip through as
                # an unparsed boolean cluster and execute the DELETE.
                token = t[1:]
                takes_next = False
                for k, ch in enumerate(token):
                    if ch not in _PSQL_VALUE_SHORTS:
                        continue
                    val = token[k + 1:]
                    if not val:
                        if j + 1 >= len(rest):
                            return (False, "psql -%s has no value" % ch)
                        val = rest[j + 1]
                        takes_next = True
                    if ch == "f":
                        return (False, "psql script-file payload is not "
                                       "read-auditable")
                    if ch == "c":
                        sql_payloads.append(val)
                    break
                j += 2 if takes_next else 1
                continue
            j += 1
        for sql in sql_payloads:
            if not re.match(r"^\s*(select|show|\\d|\\l)\b", sql, re.I):
                return (False, "psql -c/--command payload is not a read-only "
                               "SELECT/SHOW")
    return (True, "")


def _read_capped(stream) -> str:
    """Read a captured stream back, capped at ``_BIRTH_PROBE_OUTPUT_CAP``."""
    data = stream.read(_BIRTH_PROBE_OUTPUT_CAP + 1)
    text = data.decode("utf-8", errors="replace").strip()
    if len(data) > _BIRTH_PROBE_OUTPUT_CAP:
        text += "\n(output truncated at %d bytes)" % _BIRTH_PROBE_OUTPUT_CAP
    return text


def _run_birth_probe(
    cmd: str, cwd: Optional[str] = None
) -> tuple[Optional[int], str, str]:
    """Run one audited probe once, as ARGV (no shell). ``(rc, stdout, stderr)``.

    ``rc`` is ``None`` when the probe never ran (unparseable, not found,
    timed out). ``cwd`` is the caller's workspace root; the default is a fresh
    temp dir so a probe cannot write into the repo.
    """
    import shlex
    import subprocess
    import tempfile

    try:
        argv = shlex.split((cmd or "").strip(), posix=True)
    except Exception:
        return (None, "", "unparseable shell")
    if not argv:
        return (None, "", "empty command")
    try:
        with tempfile.TemporaryDirectory(prefix="kanban-birth-probe-") as tmp:
            # Files, not pipes: a chatty probe must not buffer unbounded bytes
            # in memory.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                try:
                    proc = subprocess.run(
                        argv, stdout=out, stderr=err,
                        timeout=_BIRTH_PROBE_TIMEOUT, cwd=cwd or tmp, shell=False,
                    )
                except subprocess.TimeoutExpired:
                    return (None, "", "timed out after %ds" % _BIRTH_PROBE_TIMEOUT)
                except FileNotFoundError as exc:
                    return (None, "", "binary not found (%s)" % exc)
                out.seek(0)
                err.seek(0)
                return (proc.returncode, _read_capped(out), _read_capped(err))
    except Exception as exc:  # pragma: no cover - defensive
        return (None, "", "could not run (%s)" % exc)