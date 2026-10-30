"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the gateway
dispatcher's auto-decompose path. Reads the profile roster (with
descriptions), asks the auxiliary LLM for a task graph in JSON, then
atomically creates the children, links them under the root, and flips the
root ``triage -> todo``. The root stays alive as parent of every leaf child so
it wakes back up when the graph completes and its assignee (the orchestrator
profile) can judge completion and add more work.

Mirrors ``kanban_specify`` (lazy aux import, lenient parse, never raises on
expected failures). ``fanout=false`` collapses to the ``specify`` behaviour
(tighten + promote, no children), making ``decompose`` a strict superset.
Unknown assignees are rewritten to ``default_assignee`` — a child NEVER ends
up with ``assignee=None``.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import profiles as profiles_mod
from hermes_cli.kanban_specify import (
    _call_aux, _extract_json_blob, _load_triage_task, _task_prompt_fields, _title_body,
)
from hermes_cli.kanban_specify import _profile_author as _specify_author

logger = logging.getLogger(__name__)

_ACCEPTANCE_BLOCK_TEMPLATE = """

## ACCEPTANCE
criterion: Auto-decomposed child — implement per specification in body
evidence_cmd: |
  npx tsc --noEmit
expected: 0
scope_paths:
  - /root/projects/raia
frozen_at: {frozen_at}"""

_DRAFT_ACCEPTANCE_TEMPLATE = """

## ACCEPTANCE
criterion: <REQUIRED: describe the acceptance criterion>
evidence_cmd: |
  <REQUIRED: command that proves this task is complete>
expected: <expected output>
scope_paths:
  - <REQUIRED: scope path>
# DRAFT-ACCEPTANCE: PA/author must fill before dispatch
frozen_at: {frozen_at}"""

_ACCEPTANCE_CMD_TOKENS = re.compile(
    r'curl|psql|pytest|playwright|npx|npm run|bash |grep |jq |gh run|tsc|SELECT'
)


def _extract_arm_block(body: str) -> Optional[str]:
    """The top-level ``arm:`` YAML block (key + indented continuation lines)."""
    if not body:
        return None
    lines = body.replace("\r\n", "\n").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("arm:"):
            block = [line]
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt)
            return "\n".join(block).rstrip()
    return None


def _guard_frozen_blocks(original_body: str, new_body: str) -> tuple[str, bool]:
    """Fail-closed: a frozen ``## ACCEPTANCE`` block is NEVER rewritten by the
    decomposer — the card's acceptance test is the published contract.

    Returns ``(body, found)``. ``found=True`` means the frozen block was the
    sole source, so callers branch on ``if _found`` and must NOT fall through
    to ``_preserve_frozen_blocks`` (deciding that by comparing bodies was the
    arm-idempotency defect, ce491846f7).
    """
    if not original_body or not new_body:
        return new_body, False
    frozen = kb._frozen_acceptance_block(original_body)
    if frozen is None:
        return new_body, False
    cleaned = new_body.rstrip()
    for block in kb._extract_acceptance_blocks(cleaned):
        cleaned = cleaned.replace(block, "").rstrip()
    logger.info(
        "decompose: preserved frozen ACCEPTANCE block from original body "
        "(card already has PA-frozen evidence)"
    )
    return ((cleaned + "\n\n" if cleaned else "") + frozen + "\n"), True


def _preserve_frozen_blocks(original_body: str, new_body: str) -> str:
    """Re-attach the original ACCEPTANCE and ``arm:`` blocks verbatim when the
    incoming body carries no frozen block (non-frozen template / tooling scope)."""
    if not original_body or not new_body:
        return new_body
    blocks = kb._extract_acceptance_blocks(original_body)
    acceptance = blocks[0] if blocks else None
    arm = _extract_arm_block(original_body)
    if not acceptance and not arm:
        return new_body

    cleaned = new_body.rstrip()
    for block in kb._extract_acceptance_blocks(cleaned):
        cleaned = cleaned.replace(block, "").rstrip()
    rem = _extract_arm_block(cleaned)
    while rem:
        remaining = cleaned.replace(rem, "").rstrip()
        if remaining == cleaned:
            break
        cleaned = remaining
        rem = _extract_arm_block(cleaned)

    result = cleaned
    if acceptance:
        result += "\n\n" + acceptance
    if arm:
        result += "\n\n" + arm
    return result + "\n"


def _inject_acceptance_block(body: str, assignee: str, parent_body: Optional[str] = None,
                             template: Optional[str] = None) -> str:
    """Ensure a programmer-class child carries a ``## ACCEPTANCE`` block.

    An existing block is ALWAYS preserved verbatim, so a second decompose run
    never re-injects. Otherwise a DRAFT scaffold is appended, inheriting the
    parent's ``evidence_cmd`` when there is one — a derivable-but-imperfect
    block beats a guaranteed 28-bounce loop (GOV P2, t_d230046b).
    """
    if assignee != "programmer":
        return body
    if not isinstance(body, str):
        body = ""
    if kb._extract_acceptance_blocks(body):
        return body
    frozen_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parent_evidence = None
    if parent_body:
        m = re.search(
            r'^evidence_cmd:\s*(\|.*?)(?=^\S|\Z)',
            parent_body, re.MULTILINE | re.DOTALL,
        )
        if m:
            parent_evidence = m.group(0).strip()
    if parent_evidence:
        block = f"""

## ACCEPTANCE
criterion: Auto-decomposed child — implement per specification in body
evidence_cmd: |
  # Inherited from parent triage card — tighten per child spec
  {parent_evidence}
expected: 0
scope_paths:
  - /root/projects/raia
frozen_at: {frozen_at}"""
    else:
        block = (template or _DRAFT_ACCEPTANCE_TEMPLATE).format(frozen_at=frozen_at)
    return body + block


_TOOLING_SCOPE_RX = re.compile(
    r'(^|/)(\.hermes/scripts|/usr/local/bin|/etc/cron|/etc/systemd|\.githooks|plugins/|/root/\.hermes/)'
)


def _ensure_arm_block(body: str) -> str:
    """Tooling-scope children need an ``arm:`` block. Idempotent: an existing
    arm block is never duplicated (same class as ``_stamp_birth_verified``)."""
    if not body:
        return body
    scope_section = re.search(
        r'^scope_paths:$(.*?)(?=^arm:|\n\n## |\Z)',
        body, re.MULTILINE | re.DOTALL,
    )
    if not scope_section or not _TOOLING_SCOPE_RX.search(scope_section.group(0)):
        return body
    if _extract_arm_block(body):
        return body
    frozen_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return body + f"""

arm:
  probe_cmd: test -d /root/projects/raia
  expected: 0
  frozen_at: {frozen_at}"""


# ---------------------------------------------------------------------------
# Zombie guards — refuse to decompose a card that was already stood down.
# Incident t_bc15576c (2026-10-02): decomposed hours after PA stood it down,
# resurrecting a REJECTED spec into PR #2245.
# ---------------------------------------------------------------------------
_PR_REF_RE = re.compile(r'(?:PR\s*#?|pull/)(\d+)', re.IGNORECASE)
_GH_URL_RE = re.compile(
    r'github\.com/([\w-]+/[\w-]+)/(?:pull|issues)/(\d+)', re.IGNORECASE
)
_OWNER_REPO_PR_RE = re.compile(r'(?<!/)([\w-]+/[\w-]+)#(\d+)', re.IGNORECASE)
_STAND_DOWN_KW_RE = re.compile(
    r'\b(SUPERSEDED|STAND\s*DOWN|zombie|do not push)\b', re.IGNORECASE
)
_PA_AUTHORS = frozenset({'default', 'pa'})
_DEFAULT_REPO = "estateaigents/raia"


def _emit_skip_event(task_id: str, kind: str, payload: dict, *, skipped: bool = True) -> None:
    """Record a zombie-guard decision in ``task_events`` + an SEV log line.
    ``skipped=False`` records an advisory that did NOT stop the run."""
    prefix = "decompose-skipped" if skipped else "decompose-advisory"
    try:
        with kbc.connect_closing() as conn:
            now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, None, f"{prefix}: {kind}",
                 json.dumps(payload, default=str), now),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("decompose: failed to emit %s event for %s: %s", prefix, task_id, exc)
    logger.warning("SEV|decomposer-guard|%s|kind=%s|skipped=%s", task_id, kind, skipped)


def _extract_pr_refs(body: str) -> list[dict]:
    """``{repo, number}`` PR references: full GitHub URLs, ``owner/repo#123``,
    and bare ``PR #123`` / ``pull/123`` (assumed ``_DEFAULT_REPO``)."""
    refs: list[dict] = []
    seen: set[tuple[str, int]] = set()
    if not body:
        return refs

    for rxt, repo_from_group in (
        (_GH_URL_RE, True), (_OWNER_REPO_PR_RE, True), (_PR_REF_RE, False),
    ):
        for m in rxt.finditer(body):
            repo = m.group(1).strip() if repo_from_group else _DEFAULT_REPO
            num = int(m.group(2) if repo_from_group else m.group(1))
            key = (repo.lower(), num)
            if key not in seen:
                seen.add(key)
                refs.append({"repo": repo, "number": num})
    return refs


def _check_pr_states(pr_refs: list[dict]) -> tuple[bool, list[dict]]:
    """Query GitHub PR states via ``gh``. ``ok=False`` when nothing could be
    queried (gh missing, network) so the caller proceeds fail-open."""
    states: list[dict] = []
    for ref in pr_refs:
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(ref["number"]),
                 "--repo", ref["repo"],
                 "--json", "state,closed,mergedAt",
                 ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                logger.warning(
                    "decompose: gh pr view %s#%d failed: %s",
                    ref["repo"], ref["number"],
                    (result.stderr or result.stdout or "").strip(),
                )
                continue
            data = json.loads(result.stdout)
            states.append({
                "repo": ref["repo"],
                "number": ref["number"],
                "state": data["state"],
                "closed": data.get("closed", False),
                "merged_at": data.get("mergedAt"),
            })
        except subprocess.TimeoutExpired:
            logger.warning("decompose: gh pr view timed out for %s#%d",
                           ref["repo"], ref["number"])
        except Exception as exc:
            logger.warning("decompose: gh pr view error for %s#%d: %s",
                           ref["repo"], ref["number"], exc)
    return bool(states), states


def _check_stale_pr_targets(task_id: str, body: str) -> Optional[str]:
    """Guard 1: skip when every referenced PR is merged/closed."""
    pr_refs = _extract_pr_refs(body)
    if not pr_refs:
        return None

    ok, states = _check_pr_states(pr_refs)
    if not ok or not states:
        return None  # fail-open

    def _done(s: dict) -> bool:
        return s["state"] in ("MERGED", "CLOSED") or bool(s.get("closed", False))

    if not all(_done(s) for s in states):
        closed = [f"{s['repo']}#{s['number']}({s['state']})" for s in states if _done(s)]
        if closed:
            logger.info("decompose: card %s has merged/closed PRs: %s — proceeding",
                        task_id, ", ".join(closed))
        return None

    _emit_skip_event(task_id, "stale-pr-targets", {"pr_states": states})
    return "stale-pr-targets: all referenced PRs merged/closed"


def _check_superseded_disposition(task_id: str) -> Optional[str]:
    """Guard 2: skip when a PA comment carries stand-down language."""
    try:
        with kbc.connect_closing() as conn:
            cursor = conn.execute(
                "SELECT id, author, body FROM task_comments "
                "WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            )
            for row in cursor.fetchall():
                cid = row["id"] if isinstance(row, dict) else row[0]
                author = row["author"] if isinstance(row, dict) else row[1]
                cbody = row["body"] if isinstance(row, dict) else row[2]
                if author.strip().lower() in _PA_AUTHORS and _STAND_DOWN_KW_RE.search(cbody):
                    _emit_skip_event(task_id, "superseded-disposition", {
                        "comment_id": cid,
                        "comment_author": author,
                    })
                    return f"superseded-disposition: PA stand-down found (comment #{cid})"
    except Exception as exc:
        logger.warning("decompose: error checking superseded for %s: %s", task_id, exc)
    return None


def _check_freshness_floor(task) -> None:
    """Guard 3: advisory only — a card citing a SHA on a known branch is
    recorded, never blocked (too fuzzy for a hard gate)."""
    body = task.body or ""
    sha_match = re.search(r'\b([a-f0-9]{7,40})\b', body)
    if sha_match and task.branch_name:
        _emit_skip_event(
            task.id, "freshness-floor",
            {"sha": sha_match.group(1), "branch": task.branch_name},
            skipped=False,
        )


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return _specify_author("decomposer")


def _resolve_profile_from_cfg(cfg: dict, key: str, *, fallback: Optional[str] = None) -> str:
    """``kanban.<key>`` if it names an existing profile, else ``fallback``
    (the root task's own assignee) if that does, else the active default
    profile — so a task is never stranded for lack of an owner.
    ``orchestrator_profile`` owns the root after fan-out; ``default_assignee``
    catches children the decomposer can't route.

    The root's assignee sits before the active profile because the decomposer
    runs inside whatever profile hosts the dispatcher — an operator's
    credential-less incognito profile, say — and that profile must never
    silently become the owner of work the card was assigned away from (#114294).
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get(key) or "").strip()
    for candidate in (explicit, (fallback or "").strip()):
        if candidate:
            try:
                if profiles_mod.profile_exists(candidate):
                    return candidate
            except Exception:
                pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """``(roster_for_prompt, valid_assignee_names)``; entries are
    ``{name, description, has_description}``."""
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return [], set()
    roster = []
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
    return roster, {p.name for p in all_profiles}


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    return "\n".join(
        f"  - {entry['name']}{'' if entry['has_description'] else ' ⚠ undescribed'}: {entry['description']}"
        for entry in roster
    )


def _normalize_assignee_choice(assignee: object, *, default_assignee: str, valid_names: set[str]) -> str:
    """A valid assignee, else ``default_assignee`` — promoted work is never
    left unassigned."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    return chosen if chosen in valid_names else default_assignee


@dataclass
class _Routing:
    """Config-derived routing context for one decomposition."""

    orchestrator: str
    default_assignee: str
    auto_promote: bool
    roster: list[dict]
    valid_names: set[str]


def _load_routing(*, root_assignee: Optional[str] = None) -> _Routing:
    from hermes_cli.config import load_config_readonly
    try:
        cfg = load_config_readonly()
    except Exception:  # decompose_task promises ok=False, never a raise, on config trouble
        cfg = {}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    roster, valid_names = _build_roster()
    return _Routing(
        orchestrator=_resolve_profile_from_cfg(cfg, "orchestrator_profile", fallback=root_assignee),
        default_assignee=_resolve_profile_from_cfg(cfg, "default_assignee", fallback=root_assignee),
        auto_promote=bool(kanban_cfg.get("auto_promote_children", True)),
        roster=roster,
        valid_names=valid_names,
    )


def _apply_single(task: kb.Task, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    """``fanout=false``: single-task spec promotion (same effect as specify)."""
    title_val, body_val = _title_body(parsed)
    assignee_val = None
    if not task.assignee:
        assignee_val = _normalize_assignee_choice(
            parsed.get("assignee"), default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
    if title_val is None and body_val is None:
        return DecomposeOutcome(task.id, False, "decomposer returned fanout=false with no title/body")
    # A frozen ACCEPTANCE block is the card's published contract — never rewritten
    # (t_98d708bc). _found decides the sole path, never body equality.
    resolved_assignee = assignee_val if assignee_val is not None else task.assignee
    if body_val:
        _guarded, _found = _guard_frozen_blocks(task.body or "", body_val)
        body_val = _guarded if _found else _preserve_frozen_blocks(task.body or "", body_val)
    if body_val and resolved_assignee == "programmer":
        body_val = _inject_acceptance_block(body_val, "programmer")
    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn, task.id, title=title_val, body=body_val, assignee=assignee_val, author=author,
        )
    if not ok:
        return DecomposeOutcome(task.id, False, "task moved out of triage before promotion")
    return DecomposeOutcome(task.id, True, "single task (no fanout)", fanout=False, new_title=title_val)


def _clean_children(task_id: str, raw_tasks: list, routing: _Routing,
                    parent_body: Optional[str] = None) -> tuple[list[dict], str]:
    """Validate/normalise the LLM's ``tasks`` list; ``(children, "")`` or ``([], reason)``.
    Unknown assignees route to the default; never assignee=None."""
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return [], f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee, default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
        if isinstance(assignee, str) and assignee.strip() and assignee.strip() not in routing.valid_names:
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, routing.default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        raw_body = body.strip() if isinstance(body, str) else ""
        if raw_body and parent_body:
            _guarded, _found = _guard_frozen_blocks(parent_body, raw_body)
            raw_body = _guarded if _found else _preserve_frozen_blocks(parent_body, raw_body)
        fixed_body = _inject_acceptance_block(raw_body, chosen, parent_body=parent_body)
        children.append({
            "title": title.strip()[:200],
            "body": fixed_body,
            "assignee": chosen,
            # Drop non-int, out-of-range and self parent indices.
            "parents": [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx],
        })
    return children, ""


def _apply_fanout(task_id: str, parsed: dict, routing: _Routing, author: str,
                  parent_body: Optional[str] = None) -> DecomposeOutcome:
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(task_id, False, "decomposer returned fanout=true with empty tasks list")
    children, reason = _clean_children(task_id, raw_tasks, routing, parent_body=parent_body)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    # A programmer child that lost its ACCEPTANCE block, or a tooling-scope
    # card without its arm block, is a dispatch-time 28-bounce.
    for idx, child in enumerate(children):
        if child.get("assignee") != "programmer":
            continue
        cbody = child.get("body") or ""
        if not kb._extract_acceptance_blocks(cbody):
            logger.warning("decompose: programmer child %d body lost ACCEPTANCE block — re-injecting", idx)
            child["body"] = _inject_acceptance_block(cbody, "programmer")
            cbody = child["body"]
        if not _ACCEPTANCE_CMD_TOKENS.search(cbody):
            logger.warning("decompose: programmer child %d evidence_cmd has no runnable "
                           "command token — may fail acceptance gate", idx)
        child["body"] = _ensure_arm_block(child["body"])
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn,
                task_id,
                root_assignee=routing.orchestrator,
                children=children,
                author=author,
                auto_promote=routing.auto_promote,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return DecomposeOutcome(task_id, False, "task already decomposed or moved out of triage")
    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children", fanout=True, child_ids=child_ids,
    )


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, no aux client, API error, malformed/empty reply) surface
    as ``ok=False``."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, reason)

    # Zombie guards: a card PA already stood down must never be resurrected
    # (t_bc15576c — decomposed hours after stand-down, PR #2245).
    skip_reason = _check_stale_pr_targets(task_id, task.body or "")
    if skip_reason:
        return DecomposeOutcome(task_id, False, skip_reason)
    skip_reason = _check_superseded_disposition(task_id)
    if skip_reason:
        return DecomposeOutcome(task_id, False, skip_reason)
    _check_freshness_floor(task)

    routing = _load_routing(root_assignee=task.assignee)
    raw, reason = _call_aux(
        "decompose", task_id, aux_task="kanban_decomposer", system=_SYSTEM_PROMPT,
        user=_USER_TEMPLATE.format(
            **_task_prompt_fields(task),
            roster=_format_roster(routing.roster),
            default_assignee=routing.default_assignee,
        ),
        max_tokens=4000, timeout=timeout or 180, log=logger,
    )
    if raw is None:
        return DecomposeOutcome(task_id, False, reason)

    parsed = _extract_json_blob(raw, _FENCE_RE)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    audit_author = author or _profile_author()
    if not parsed.get("fanout"):
        return _apply_single(task, parsed, routing, audit_author)
    return _apply_fanout(task_id, parsed, routing, audit_author, parent_body=task.body)


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
    return [row.id for row in rows]
