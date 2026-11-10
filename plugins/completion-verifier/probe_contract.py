"""Shared probe contract for the Hermes kanban completion gate.

This module is the SINGLE validator that all acceptance-probe paths funnel
through: block parsing, probe execution, outcome classification, and verdict
persistence.  It is stdlib-only and imports nothing from the plugin, so the
plugin can import it without circularity.

Three properties hold by construction:

1. ONE SCHEMA, TWO ERAS.  A frozen ``## ACCEPTANCE`` block either declares a
   machine field (``expect_exit`` / ``expect_regex``) — schema v2 — or it does
   not, in which case it is grandfathered as schema v1 and judged by its
   narrative ``expected`` token.  Legacy blocks are NEVER rejected: a card
   written before the machine schema existed must not be blocked by a parser
   that post-dates it.  Conversely a v2 field that is declared but unparseable
   raises :class:`AcceptanceSchemaError` immediately, because a bound the runner
   cannot compare is a card defect, not a probe result.

2. MACHINERY FAILURE IS NOT EVIDENCE OF BAD WORK.  A probe that times out
   (124), is not executable (126), is not found (127), or dies on ENOENT-class
   stderr is classified ``error`` — never ``false``.  The runner does not keep
   a lead-binary whitelist and does no realpath/basename pre-flight: it runs
   the command through ``bash -c`` and judges the exit code.  Because nothing
   is vetoed before execution, the old ``sh -> dash`` denial class cannot exist
   here.  An ``error`` verdict routes the card to the ``probe_repair`` lane
   (see below) rather than failing the work.

3. ``probe_repair`` IS A BOUNDED STATE, NOT A GRAVEYARD.  A card whose probe
   could not be executed is tagged ``probe_repair`` with an SLA of
   ``PROBE_REPAIR_SLA_MINUTES`` (60) minutes.  Its verdicts are appended as
   JSONL under ``VERDICT_DIR`` so the state survives restarts, and every repair
   event is mirrored to ``VERDICT_LOG`` for the severity channel.  Verdicts are
   bound to a block hash (see :func:`block_hash`) so a re-written ACCEPTANCE
   block cannot silently inherit an old pass.

Nothing in this module raises for legacy input, network trouble, a missing
verdict directory, or a missing git binary; all such paths degrade to ``None``
or an ``error`` verdict and are logged best-effort.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone, UTC

SCHEMA_VERSION = 2
PROBE_REPAIR_STATUS = "probe_repair"
PROBE_REPAIR_SLA_MINUTES = 60

# Tokens that assert a NEGATIVE verdict. A v1 (legacy) block is exit-0-only, so
# `expected: false` next to a probe that truthfully exits 1 can never be
# satisfied — the freeze gate refuses it (Rule 4b) and Gate 6 names the v2
# remedy. ONE definition, consumed by both gates (t_9861fcaa ruling).
NEGATIVE_VERDICT_TOKENS = frozenset({
    "false", "fail", "failed", "failure", "no", "denied", "deny", "stuck",
    "broken", "none", "notfound", "not_found",
})

# Budget for one probe. Frozen contracts routinely allow <90s and the
# browser-driven live-parity legs alone take ~60s, so a 30s cap turned correct
# probes into error/124 (t_9b8cdd19). Matches the legacy VERIFY_SCRIPT_TIMEOUT.
# Ceiling: 120s, no per-card override — raise only with evidence a contract needs more.
PROBE_TIMEOUT_SECONDS = 120
ERROR_EXIT_CODES = frozenset({124, 126, 127})
MACHINE_FIELDS = ("expect_exit", "expect_regex")
CMD_FIELDS = ("cmd", "evidence_cmd", "probe_cmd")
VERDICT_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "state", "completion-runner")
VERDICT_LOG = os.path.join(os.path.expanduser("~"), ".hermes", "logs", "probe-repair-sev.log")

# Environment keys that would let a caller's shell state leak into a probe and
# make a hermetic command behave differently (or hijack git's repo selection).
_ENV_SCRUB = frozenset({
    "BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "PYTHONSTARTUP",
})

_BLOCK_SCALARS = frozenset({"|", "|-", "|+", ">", ">-", ">+"})

# stderr shapes that mean "the command never really ran" rather than "the
# command ran and rejected the work".
_ENOENT_STDERR = re.compile(
    r"(?i)(no such file or directory|command not found|not found|cannot access|permission denied)"
)

_EXPECT_EXIT_SHAPE = re.compile(r"-?\d{1,3}")

# A FAIL leg prints one ``FAIL: <leg>: <detail>`` line and still exits 0 by the
# probe convention, so an exit code is not evidence of a pass.  Receipts that
# read exit_code alone are fail-open (t_d2c33545).
FAIL_LINE = re.compile(r"^[ \t]*FAIL:", re.MULTILINE)

# Receipt keys that may carry the probe's recorded output, most explicit first.
RECEIPT_OUTPUT_FIELDS = ("stdout", "output", "out", "probe_output")


class AcceptanceSchemaError(ValueError):
    """A frozen ACCEPTANCE block declared a bound the runner cannot compare."""


# ── hashing ────────────────────────────────────────────────────────────────


def block_hash(raw: str) -> str:
    """sha256 hex of the block text, normalised for cosmetic-only differences.

    Normalisation: strip the whole text, rstrip every line, join with ``\\n``.
    Trailing whitespace and blank lines at the edges therefore do not change
    the hash, while any change to content (including internal blank lines) does.
    Used for tamper detection and to bind a verdict to the exact block it was
    produced against.
    """
    if not raw:
        return hashlib.sha256(b"").hexdigest()
    lines = [ln.rstrip() for ln in raw.strip().splitlines()]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ── parsing ────────────────────────────────────────────────────────────────


def _strip_quotes(val: str) -> str:
    """Strip exactly ONE matching leading/trailing quote pair, if present.

    Backticks count as a quote pair: a markdown-idiomatic ``evidence_cmd: `bash
    x.sh` `` would otherwise reach ``bash -c`` as a command substitution and run
    the probe's OUTPUT as a command (t_47b9f548).
    """
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"', "`"):
        return val[1:-1]
    return val


def _locate(raw_lines: list[str], name: str) -> tuple[int, str] | None:
    """Return (line_index, inline_value) for the first ``name:`` line, or None."""
    pat = re.compile(r"^" + re.escape(name) + r":(.*)$")
    for idx, ln in enumerate(raw_lines):
        m = pat.match(ln)
        if m:
            return idx, m.group(1)
    return None


def _scalar(raw_lines: list[str], idx: int, inline: str) -> str:
    """Resolve a YAML-ish scalar: quoted inline value, or 2-space block scalar."""
    val = inline.strip()
    if val in _BLOCK_SCALARS:
        cont: list[str] = []
        for ln in raw_lines[idx + 1:]:
            if ln.startswith("  ") or ln.strip() == "":
                cont.append(ln)
            else:
                break
        while cont and cont[-1].strip() == "":
            cont.pop()
        return "\n".join(ln.removeprefix("  ") for ln in cont)
    return _strip_quotes(val)


def _field(raw_lines: list[str], name: str) -> tuple[bool, str | None]:
    """Return (present, value).  present is True even when the value is empty."""
    hit = _locate(raw_lines, name)
    if hit is None:
        return False, None
    idx, inline = hit
    return True, _scalar(raw_lines, idx, inline)


def _parse_expect_exit(raw_lines: list[str]) -> int | None:
    present, value = _field(raw_lines, "expect_exit")
    if not present:
        return None
    text = (value or "").strip()
    if not text:
        raise AcceptanceSchemaError(
            "expect_exit: field is declared but empty. The runner compares exit "
            "codes, so an empty bound cannot be evaluated; set expect_exit to an "
            "integer 0..255 or remove the field."
        )
    if not _EXPECT_EXIT_SHAPE.fullmatch(text):
        raise AcceptanceSchemaError(
            f"expect_exit: {text!r} is not an integer exit code. The runner "
            "compares exit codes numerically, so expect_exit must be 0..255."
        )
    code = int(text)
    if not 0 <= code <= 255:
        raise AcceptanceSchemaError(
            f"expect_exit: {code} is out of range. The runner compares exit "
            "codes, which are 0..255."
        )
    return code


def _parse_expect_regex(raw_lines: list[str]) -> str | None:
    present, value = _field(raw_lines, "expect_regex")
    if not present:
        return None
    text = (value or "").strip()
    if not text:
        # An empty expect_regex declares nothing; treat as absent so a legacy
        # card with a stray empty key is still grandfathered.
        return None
    if re.search(r"\s", text):
        raise AcceptanceSchemaError(
            f"expect_regex: {text!r} contains whitespace. expect_regex must be "
            "ONE whitespace-free token so the probe line cannot drift."
        )
    try:
        re.compile(text)
    except re.error as exc:
        raise AcceptanceSchemaError(
            f"expect_regex: {text!r} does not compile as a regex ({exc})."
        ) from exc
    return text


def parse_block(raw: str) -> dict:
    """Parse the body of a frozen ``## ACCEPTANCE`` block.

    Returns a dict with exactly these keys::

        present, schema_version, cmd, expect_exit, expect_regex, expected,
        scope_paths, frozen_at, block_hash, legacy

    ``schema_version`` is 2 when the block declares a machine field
    (``expect_exit`` or ``expect_regex``), else 1.  ``legacy`` is True exactly
    when no machine field is present — such a block is grandfathered and is
    judged by its narrative ``expected`` token.  A blank or missing block yields
    the all-None shape with ``present=False``.

    Raises :class:`AcceptanceSchemaError` only for a *declared* machine field
    that cannot be compared (non-integer or out-of-range ``expect_exit``,
    whitespace-bearing or non-compiling ``expect_regex``).  Legacy narrative
    blocks never raise.
    """
    if not raw or not raw.strip():
        return {
            "present": False,
            "schema_version": 1,
            "cmd": None,
            "expect_exit": None,
            "expect_regex": None,
            "expected": None,
            "scope_paths": [],
            "frozen_at": None,
            "block_hash": None,
            "legacy": True,
        }

    raw_lines = raw.splitlines()

    cmd = None
    for name in CMD_FIELDS:
        _present, value = _field(raw_lines, name)
        if value is not None and value.strip():
            cmd = value
            break

    expect_exit = _parse_expect_exit(raw_lines)
    expect_regex = _parse_expect_regex(raw_lines)

    _p, expected = _field(raw_lines, "expected")
    _p, frozen_at = _field(raw_lines, "frozen_at")
    _p, declared_hash = _field(raw_lines, "block_hash")
    if declared_hash is not None:
        declared_hash = declared_hash.strip() or None

    scope_paths: list[str] = []
    in_scope = False
    for ln in raw_lines:
        if ln.startswith("scope_paths:"):
            in_scope = True
            continue
        if in_scope:
            if ln.startswith("  - "):
                scope_paths.append(_strip_quotes(ln.strip()[2:].strip()))
            elif ln and not ln.startswith(" "):
                break

    machine = expect_exit is not None or expect_regex is not None
    return {
        "present": True,
        "schema_version": SCHEMA_VERSION if machine else 1,
        "cmd": cmd,
        "expect_exit": expect_exit,
        "expect_regex": expect_regex,
        "expected": expected,
        "scope_paths": scope_paths,
        "frozen_at": frozen_at,
        "block_hash": declared_hash,
        "legacy": not machine,
    }


# ── classification ─────────────────────────────────────────────────────────


def is_error_outcome(rc: int | None, stderr: str = "") -> bool:
    """True when the probe did not produce a usable exit code at all.

    124 (timeout), 126 (not executable), 127 (not found), and a missing rc are
    machinery failures.  An ENOENT-class stderr on a non-zero exit means the
    command never really ran either.
    """
    if rc is None or rc in ERROR_EXIT_CODES:
        return True
    if rc != 0 and _ENOENT_STDERR.search(stderr or ""):
        return True
    return False


def _result(verdict: str, rc: int | None, reason: str) -> dict:
    return {"verdict": verdict, "exit_code": rc, "reason": reason}


def classify(rc: int | None, stdout: str, stderr: str, block: dict | None = None) -> dict:
    """Turn a probe result into ``pass`` / ``false`` / ``error``.

    ``error`` means the machinery failed and says nothing about the work;
    ``false`` means the probe ran and the work did not satisfy the bound.
    Rules are applied in order and documented in the module docstring contract:

    1. rc None or in ERROR_EXIT_CODES -> ``error``, naming the exit code.
    2. non-zero rc with ENOENT-class stderr -> ``error``.
    3. no block -> ``pass`` on rc 0, else ``false``.
    4. rc must equal ``expect_exit``; **legacy blocks default ``expect_exit`` to
       0 and a non-zero rc is ``false`` before the token is consulted** — v1 is
       exit-0-only by decision (t_9861fcaa).
    5. ``expect_regex`` must match stdout when declared.
    6. machine-schema block without a regex -> exit-code match IS the verdict.
    7. legacy block -> ``expected`` token must equal a whole stripped stdout
       line (or the whole stripped stdout).  Never substring containment;
       an empty token passes.
    """
    stdout = stdout or ""
    stderr = stderr or ""

    if rc is None:
        return _result("error", rc, "runner produced no exit code (machinery failure, not evidence)")
    if rc in ERROR_EXIT_CODES:
        return _result(
            "error",
            rc,
            f"runner machinery failure: exit {rc} "
            "(124 timeout / 126 not executable / 127 not found); not evidence about the work",
        )
    if rc != 0 and _ENOENT_STDERR.search(stderr):
        return _result(
            "error",
            rc,
            f"probe machinery failure: exit {rc} with ENOENT/PATH-class stderr; not evidence about the work",
        )

    if block is None:
        if rc == 0:
            return _result("pass", rc, "probe exited 0")
        return _result("false", rc, f"probe ran and exited {rc}")

    expect_exit = block.get("expect_exit")
    if expect_exit is None:
        expect_exit = 0
    if rc != expect_exit:
        return _result(
            "false",
            rc,
            f"probe RAN and exited {rc}, expect_exit is {expect_exit}",
        )

    expect_regex = block.get("expect_regex")
    if expect_regex:
        if re.search(expect_regex, stdout):
            return _result("pass", rc, f"stdout matched expect_regex {expect_regex!r}")
        return _result(
            "false",
            rc,
            f"probe exited {expect_exit} but stdout did not match expect_regex {expect_regex!r}",
        )

    if not block.get("legacy", True):
        # Machine schema with no regex: the exit code IS the verdict.
        return _result("pass", rc, f"probe exited expect_exit {expect_exit}")

    token = (block.get("expected") or "").strip()
    if not token:
        return _result("pass", rc, "legacy block has no expected token; exit code only")
    lowered = token.lower()
    if lowered == stdout.strip().lower():
        return _result("pass", rc, f"stdout equals expected token {token!r}")
    for line in stdout.splitlines():
        if line.strip().lower() == lowered:
            return _result("pass", rc, f"stdout line equals expected token {token!r}")
    return _result(
        "false",
        rc,
        f"stdout has no whole line equal to expected token {token!r} "
        "(substring containment is not accepted)",
    )


# ── receipt judgement (L3-1 evidence bundle) ───────────────────────────────


def _receipt_output(receipt: dict) -> str:
    """The probe output a receipt recorded, or "" when it recorded none."""
    for key in RECEIPT_OUTPUT_FIELDS:
        value = receipt.get(key)
        if isinstance(value, str):
            return value
    return ""


def receipt_verdict(receipt: dict, block: dict | None = None) -> dict:
    """Judge ONE L3-1 evidence receipt token-aware and fail-closed.

    Gate 6 (the frozen-ACCEPTANCE path) has always consulted the token; the
    receipt shape ``{claim, file, evidence_cmd, exit_code}`` did not, and read
    "exit_code (0 = pass)".  That is fail-open: 634 probes deliberately print
    one ``FAIL: <leg>: <detail>`` line and exit 0, so a receipt for a FAILED
    leg read as a pass (t_d2c33545).

    Order of precedence, most-fatal first:

    1. malformed receipt (not an object / non-integer exit_code) -> ``false``.
    2. ``pass: false`` self-declared on the receipt -> ``false``.
    3. a ``FAIL:`` line anywhere in the recorded output -> ``false``,
       *regardless of exit_code* (this is the bug being fixed).
    4. otherwise the exit code and output are judged by :func:`classify` —
       machinery failures (124/126/127) become ``error`` (-> ``probe_repair``),
       a v2 ``expect_exit`` bound is honoured, a v1 block is judged by its
       ``expected`` token as a whole line.

    A receipt that recorded NO output has nothing to consult, so it is
    grandfathered to its exit code (block=None -> rc 0 passes).  shortcut:
    receipts without recorded output are judged by exit code alone; upgrade
    them to a v2 ``expect_exit`` block or record ``stdout`` when the command is
    a probe (one that may print FAIL: and still exit 0).
    """
    if not isinstance(receipt, dict):
        return _result("false", None, "receipt is not an object")
    raw_exit = receipt.get("exit_code")
    if isinstance(raw_exit, bool) or not isinstance(raw_exit, (int, str)):
        return _result("false", None, f"receipt exit_code is not an integer ({raw_exit!r})")
    try:
        rc = int(raw_exit)
    except (TypeError, ValueError):
        return _result("false", None, f"receipt exit_code is not an integer ({raw_exit!r})")

    if receipt.get("pass") is False:
        return _result("false", rc, "receipt self-declares pass: false")

    out = _receipt_output(receipt)
    fail_match = FAIL_LINE.search(out)
    if fail_match:
        line = fail_match.group(0).strip()
        return _result(
            "false",
            rc,
            f"receipt output carries a {line!r} line; exit {rc} is NOT a pass "
            "(a FAIL leg may still exit 0)",
        )

    return classify(rc, out, "", block)


# ── execution ──────────────────────────────────────────────────────────────


def structured_env(sha: str | None = None, cwd: str | None = None) -> dict:
    """Build a scrubbed, hermetic environment for a probe.

    Copies the current environment minus the scrub list (shell startup hooks and
    git's repo-selection overrides), then pins the runner identity and disables
    prompts, optional locks, bytecode writes, and pagers so output is stable.
    """
    env = {k: v for k, v in os.environ.items() if k not in _ENV_SCRUB}
    env["HERMES_PROBE_RUNNER"] = "completion-runner/v2"
    env["HERMES_PROBE_SCHEMA_VERSION"] = "2"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    if sha is not None:
        env["HERMES_PROBE_SHA"] = sha
    if cwd is not None:
        env["HERMES_PROBE_CWD"] = cwd
    return env


def run_probe(
    cmd: str,
    cwd: str,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    sha: str | None = None,
) -> dict:
    """Run ``cmd`` through ``bash -c`` in ``cwd`` and report the outcome.

    ALWAYS ``subprocess.run(["bash", "-c", cmd], ...)``.  No lead-binary
    whitelist, no realpath/basename pre-flight, no shell-name veto: the runner
    runs the command and judges the exit code, which is why the old
    ``sh -> dash`` denial class cannot exist here.

    Returns keys: exit_code, stdout, stderr, timed_out, cmd, cwd, seconds.
    Timeout -> 124, missing bash -> 127, any other exception -> 127; those are
    machinery failures and classify() reports them as ``error``.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            check=False, capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=structured_env(sha=sha, cwd=cwd),
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "timed_out": False,
            "cmd": cmd,
            "cwd": cwd,
            "seconds": time.monotonic() - started,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "stdout": _coerce_text(exc.stdout),
            "stderr": f"runner timeout after {timeout}s",
            "timed_out": True,
            "cmd": cmd,
            "cwd": cwd,
            "seconds": time.monotonic() - started,
        }
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"runner failure: {exc}",
            "timed_out": False,
            "cmd": cmd,
            "cwd": cwd,
            "seconds": time.monotonic() - started,
        }
    except Exception as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"runner failure: {exc}",
            "timed_out": False,
            "cmd": cmd,
            "cwd": cwd,
            "seconds": time.monotonic() - started,
        }


def _coerce_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def readonly_checkout(sha: str, repo_dir: str, dest_parent: str | None = None) -> str | None:
    """Create a detached, read-only worktree of ``sha`` for a probe to run in.

    Returns the worktree path, or None on ANY failure (never raises).  The
    chmod is best-effort: a probe that needs to write will simply fail its own
    exit code rather than taking the runner down.
    """
    try:
        parent = tempfile.mkdtemp(prefix=f"probe-sha-{sha[:8]}-", dir=dest_parent)
        dest = os.path.join(parent, "wt")
        proc = subprocess.run(
            ["git", "-C", repo_dir, "worktree", "add", "--detach", dest, sha],
            check=False, capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=structured_env(sha=sha, cwd=repo_dir),
        )
        if proc.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            return None
        try:
            subprocess.run(
                ["chmod", "-R", "a-w", dest],
                check=False, capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        return dest
    except Exception:
        return None


# ── verdict persistence ────────────────────────────────────────────────────


def _safe_id(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_id or "")
    return safe or "unknown"


def _verdict_path(task_id: str) -> str:
    return os.path.join(VERDICT_DIR, f"{_safe_id(task_id)}.jsonl")


def write_verdict(task_id: str, **fields) -> str | None:
    """Append one JSON line to the card's verdict ledger.  Best-effort."""
    try:
        path = _verdict_path(task_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception:
        return None


def read_verdicts(task_id: str) -> list[dict]:
    """Read the card's verdict ledger, skipping malformed lines.  [] if absent."""
    out: list[dict] = []
    try:
        path = _verdict_path(task_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except Exception:
        return out
    return out


def latest_verdict(task_id: str) -> dict | None:
    """Most recent verdict record for the card, or None."""
    records = read_verdicts(task_id)
    return records[-1] if records else None


def append_sev_line(line: str) -> None:
    """Append one severity-channel line: UTC ISO8601Z timestamp + one-liner."""
    try:
        flat = " ".join((line or "").split())[:400]
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        os.makedirs(os.path.dirname(VERDICT_LOG), exist_ok=True)
        with open(VERDICT_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} {flat}\n")
    except Exception:
        pass
