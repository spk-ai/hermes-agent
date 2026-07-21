"""Versioned, opt-in native strict-route/v1 Kanban contract.

This module owns no lifecycle state itself: it is invoked by kanban_db and uses
that module's transaction/event helpers at call time to avoid a second authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = "strict-route/v1"
ADMISSION_RECEIPTS = frozenset({"detector_source", "risk_classification", "planning_materialization"})
IMPLEMENTATION_RECEIPTS = (
    "implementation_commit",
    "baseline_ancestry",
    "changed_paths",
    "focused_tests",
    "full_tests",
)
_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_EXTERNAL_RECEIPT_PERMISSIONS = {
    "developer": {kind: "completion" for kind in IMPLEMENTATION_RECEIPTS},
    "qa": {
        "qa_verdict": "qa_verdict",
        "fresh_hermes_home": "qa_verdict",
        "implementation_commit": "qa_verdict",
    },
    "rollout": {
        "rollout_authority": "rollout_authority",
        "runtime_manifest": "runtime_manifest",
    },
}


@dataclass(frozen=True)
class StrictRouteRefusal:
    code: str
    message: str
    request_id: Optional[str] = None
    route_id: Optional[str] = None


@dataclass(frozen=True)
class StrictEligibilityResult:
    allowed: bool
    operation: str
    strict: bool
    route_id: Optional[str] = None
    candidate_id: Optional[str] = None
    task_id: Optional[str] = None
    reason_code: Optional[str] = None
    missing_receipt_kinds: tuple[str, ...] = ()
    active_task_id: Optional[str] = None
    expected_route_revision: Optional[str] = None


@dataclass(frozen=True)
class StrictRouteReconcileResult:
    ok: bool
    replayed: bool = False
    route: Optional[dict] = None
    candidates: dict[str, dict] = field(default_factory=dict)
    execution_links: list[tuple[str, str]] = field(default_factory=list)
    active_watch: Optional[dict] = None
    cardinality: dict[str, int] = field(default_factory=dict)
    refusal: Optional[StrictRouteRefusal] = None


def _refuse(code: str, message: str, request: Optional[dict] = None, route_id: Optional[str] = None) -> StrictRouteReconcileResult:
    return StrictRouteReconcileResult(False, refusal=StrictRouteRefusal(code, message, (request or {}).get("request_id"), route_id))


def _risk_flags(route: dict) -> dict[str, bool]:
    """Return the immutable risk classification in its v1 canonical shape.

    Early callers used flattened flags.  Keep that additive compatibility,
    but validate and digest the nested public ``route.risk`` form when it is
    supplied so a mismatched receipt cannot silently qualify a route.
    """
    supplied = route.get("risk")
    if supplied is not None and not isinstance(supplied, dict):
        raise ValueError("risk must be an object")
    source = supplied if supplied is not None else route
    return {
        name: bool(source.get(name, False))
        for name in ("external", "credentials", "payment", "production_risk")
    }


def _risk_digest(route: dict) -> str:
    flags = _risk_flags(route)
    return hashlib.sha256(json.dumps(flags, sort_keys=True).encode()).hexdigest()


def _readback(conn, revision_id: str):
    candidates = {row["stage_key"]: dict(row) for row in conn.execute(
        "SELECT * FROM strict_route_candidates WHERE revision_id=? ORDER BY stage_key", (revision_id,)
    )}
    links = [tuple(row) for row in conn.execute(
        "SELECT l.parent_id, l.child_id FROM task_links l JOIN strict_route_candidates c ON c.task_id=l.child_id WHERE c.revision_id=?", (revision_id,)
    )]
    watch = conn.execute("SELECT * FROM strict_route_watches WHERE revision_id=? AND active=1", (revision_id,)).fetchone()
    return candidates, links, dict(watch) if watch else None


def _receipt_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_admission_receipt(receipt: Any, route_revision: Any) -> Optional[str]:
    """Return a typed refusal code for an invalid strict-route/v1 receipt.

    Receipt admission is deliberately stronger than a truthiness check: the
    currentness witness is boolean, the receipt is bound to this revision, and
    its digest must authenticate its canonical payload.
    """
    if not isinstance(receipt, dict) or any(
        not receipt.get(key) for key in ("schema_version", "receipt_id", "kind", "digest")
    ) or "payload" not in receipt:
        return "MISSING_RECEIPT"
    if receipt["schema_version"] != SCHEMA_VERSION:
        return "UNSUPPORTED_SCHEMA_VERSION"
    if receipt.get("current") is not True:
        return "RECEIPT_NOT_CURRENT"
    if str(receipt.get("route_revision")) != str(route_revision):
        return "STALE_OR_SUPERSEDED"
    if receipt["digest"] != _receipt_digest(receipt["payload"]):
        return "RECEIPT_MISMATCH"
    return None


def _candidate_row(conn, task_id: str):
    return conn.execute(
        "SELECT c.*, r.route_id, r.route_revision, r.state AS revision_state FROM strict_route_candidates c "
        "JOIN strict_route_revisions r ON r.revision_id=c.revision_id "
        "WHERE c.task_id=?",
        (task_id,),
    ).fetchone()


def _validate_commit_identity(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return "RECEIPT_SEMANTICS_INVALID"
    sha, remote_ref = payload.get("sha"), payload.get("remote_ref")
    if not isinstance(sha, str) or not _GIT_COMMIT_RE.fullmatch(sha):
        return "RECEIPT_SEMANTICS_INVALID"
    if not isinstance(remote_ref, str) or not remote_ref.strip():
        return "RECEIPT_SEMANTICS_INVALID"
    return None


def _validate_completion_payload(kind: str, payload: Any, *, implementation_commit: Any = None) -> Optional[str]:
    """Validate the canonical, replayable evidence shape before it is bound."""
    if kind == "implementation_commit":
        return _validate_commit_identity(payload)
    if kind == "baseline_ancestry":
        if not isinstance(payload, dict):
            return "RECEIPT_SEMANTICS_INVALID"
        baseline_sha, descendant_sha = payload.get("baseline_sha"), payload.get("descendant_sha")
        if (
            not isinstance(baseline_sha, str)
            or not _GIT_COMMIT_RE.fullmatch(baseline_sha)
            or not isinstance(descendant_sha, str)
            or not _GIT_COMMIT_RE.fullmatch(descendant_sha)
            or payload.get("is_ancestor") is not True
            or not isinstance(implementation_commit, dict)
            or descendant_sha != implementation_commit.get("sha")
        ):
            return "RECEIPT_SEMANTICS_INVALID"
        return None
    if kind == "changed_paths":
        paths = payload.get("paths") if isinstance(payload, dict) else None
        if (
            not isinstance(paths, list)
            or not paths
            or paths != sorted(set(paths))
            or any(
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                for path in paths
            )
        ):
            return "RECEIPT_SEMANTICS_INVALID"
        return None
    if kind in {"focused_tests", "full_tests"}:
        if not isinstance(payload, dict):
            return "RECEIPT_SEMANTICS_INVALID"
        if (
            not isinstance(payload.get("command"), str)
            or not payload["command"].strip()
            or not isinstance(payload.get("output"), str)
            or not payload["output"].strip()
            or payload.get("exit_code") != 0
        ):
            return "RECEIPT_SEMANTICS_INVALID"
        return None
    return None


def _active_candidate_authorization(conn, candidate, task_id: str, operation: str) -> Optional[StrictEligibilityResult]:
    watch = conn.execute(
        "SELECT task_id FROM strict_route_watches WHERE revision_id=? AND active=1",
        (candidate["revision_id"],),
    ).fetchone()
    if (
        candidate["state"] != "active"
        or candidate["revision_state"] != "active"
        or watch is None
        or watch["task_id"] != task_id
    ):
        return StrictEligibilityResult(
            False, operation, True, candidate["route_id"], candidate["candidate_id"],
            task_id, "NOT_CURRENT_CANDIDATE",
            active_task_id=watch["task_id"] if watch else None,
            expected_route_revision=candidate["route_revision"],
        )
    return None


def _bind_receipt(conn, candidate, kind: str, payload: Any, *, purpose: str) -> None:
    """Persist immutable, non-secret evidence for one strict candidate."""
    digest = _receipt_digest(payload)
    receipt_id = f"{candidate['candidate_id']}:{kind}"
    conn.execute(
        "INSERT OR IGNORE INTO strict_route_receipts "
        "(revision_id, receipt_id, kind, digest, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            candidate["revision_id"], receipt_id, kind, digest,
            json.dumps(payload, sort_keys=True), int(time.time()),
        ),
    )
    receipt = conn.execute(
        "SELECT receipt_pk FROM strict_route_receipts "
        "WHERE revision_id=? AND receipt_id=? AND kind=? AND digest=?",
        (candidate["revision_id"], receipt_id, kind, digest),
    ).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO strict_route_candidate_receipts "
        "(candidate_id, receipt_pk, purpose) VALUES (?, ?, ?)",
        (candidate["candidate_id"], receipt["receipt_pk"], purpose),
    )


def _activate_watch(conn, candidate) -> None:
    now = int(time.time())
    conn.execute(
        "UPDATE strict_route_watches SET active=0, deactivated_at=? "
        "WHERE revision_id=? AND active=1",
        (now, candidate["revision_id"]),
    )
    conn.execute(
        "INSERT INTO strict_route_watches "
        "(watch_id, revision_id, candidate_id, task_id, role, route_digest, active, activated_at) "
        "SELECT ?, ?, ?, ?, ?, requirements_digest, 1, ? "
        "FROM strict_route_revisions WHERE revision_id=?",
        (
            "srw_" + secrets.token_hex(10), candidate["revision_id"],
            candidate["candidate_id"], candidate["task_id"],
            candidate["stage_kind"], now, candidate["revision_id"],
        ),
    )


def _candidate_receipt_payload(conn, candidate, kind: str):
    row = conn.execute(
        "SELECT r.payload FROM strict_route_receipts r "
        "JOIN strict_route_candidate_receipts b ON b.receipt_pk=r.receipt_pk "
        "WHERE b.candidate_id=? AND r.kind=? ORDER BY r.receipt_pk DESC LIMIT 1",
        (candidate["candidate_id"], kind),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None


def _candidate_receipt_kinds(conn, candidate) -> set[str]:
    return {
        row["kind"] for row in conn.execute(
            "SELECT r.kind FROM strict_route_receipts r "
            "JOIN strict_route_candidate_receipts b ON b.receipt_pk=r.receipt_pk "
            "WHERE b.candidate_id=?",
            (candidate["candidate_id"],),
        )
    }


def _validate_immutable_binding(conn, candidate, kind: str, payload: Any, *, purpose: str) -> Optional[str]:
    """Reject attempts to replace evidence already bound for this purpose."""
    digests = {
        row["digest"] for row in conn.execute(
            "SELECT r.digest FROM strict_route_receipts r "
            "JOIN strict_route_candidate_receipts b ON b.receipt_pk=r.receipt_pk "
            "WHERE b.candidate_id=? AND b.purpose=? AND r.kind=?",
            (candidate["candidate_id"], purpose, kind),
        )
    }
    if digests and _receipt_digest(payload) not in digests:
        return "RECEIPT_IMMUTABLE_CONFLICT"
    return None


def _insert_candidate(conn, kb, revision_id: str, route_id: str, *, key: str, kind: str, cycle: int, status: str, assignee: str):
    """Create one canonical strict stage inside the caller's transaction."""
    now = int(time.time())
    task_id, candidate_id = kb._new_task_id(), "src_" + secrets.token_hex(10)
    idempotency_key = f"{route_id}/{key}"
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,created_at,workspace_kind,idempotency_key) "
        "VALUES (?, ?, ?, ?, ?, 'scratch', ?)",
        (task_id, f"Strict route {key}", assignee, status, now, idempotency_key),
    )
    conn.execute(
        "INSERT INTO strict_route_candidates "
        "(candidate_id,revision_id,task_id,stage_key,stage_kind,cycle,idempotency_key,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, revision_id, task_id, key, kind, cycle, idempotency_key, now),
    )
    kb._append_event(conn, task_id, "strict_route_admitted", {
        "route_id": route_id, "stage_key": key,
    })
    return conn.execute(
        "SELECT * FROM strict_route_candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()


def record_completion_receipt(conn, task_id: str, metadata: Optional[dict]) -> StrictEligibilityResult:
    """Bind a stage's receipt before its guarded terminal transition.

    The caller owns the surrounding native transaction; a failed status CAS
    rolls back both evidence and the transition together.
    """
    candidate = _candidate_row(conn, task_id)
    if candidate is None:
        return StrictEligibilityResult(True, "complete", False, task_id=task_id)
    authorization = _active_candidate_authorization(conn, candidate, task_id, "complete")
    if authorization is not None:
        return authorization
    metadata = metadata if isinstance(metadata, dict) else {}
    if candidate["stage_kind"] == "developer":
        fields = {
            "implementation_commit": metadata.get("commit_sha"),
            "baseline_ancestry": metadata.get("baseline_sha"),
            "changed_paths": metadata.get("changed_files"),
            "focused_tests": metadata.get("focused_test_output"),
            "full_tests": metadata.get("full_test_output"),
        }
        missing = tuple(kind for kind, value in fields.items() if not value)
        if missing:
            return StrictEligibilityResult(
                False, "complete", True, candidate["route_id"],
                candidate["candidate_id"], task_id, "MISSING_RECEIPT", missing,
            )
        for kind, value in fields.items():
            refusal_code = _validate_completion_payload(
                kind, value, implementation_commit=fields["implementation_commit"],
            )
            if refusal_code:
                return StrictEligibilityResult(
                    False, "complete", True, candidate["route_id"],
                    candidate["candidate_id"], task_id, refusal_code, (kind,),
                )
            refusal_code = _validate_immutable_binding(
                conn, candidate, kind, value, purpose="completion",
            )
            if refusal_code:
                return StrictEligibilityResult(
                    False, "complete", True, candidate["route_id"],
                    candidate["candidate_id"], task_id, refusal_code, (kind,),
                )
        for kind, value in fields.items():
            _bind_receipt(conn, candidate, kind, value, purpose="completion")
    elif candidate["stage_kind"] == "qa":
        verdict = metadata.get("verdict")
        fresh_home = metadata.get("fresh_hermes_home")
        implementation_commit = metadata.get("implementation_commit")
        if verdict not in {"PASS", "FAIL"} or not fresh_home or not implementation_commit:
            return StrictEligibilityResult(
                False, "complete", True, candidate["route_id"],
                candidate["candidate_id"], task_id, "MISSING_RECEIPT",
                ("qa_verdict", "fresh_hermes_home", "implementation_commit"),
            )
        developer = conn.execute(
            "SELECT * FROM strict_route_candidates WHERE revision_id=? AND stage_key=?",
            (candidate["revision_id"], f"developer.{candidate['cycle']}"),
        ).fetchone()
        expected_commit = (
            _candidate_receipt_payload(conn, developer, "implementation_commit")
            if developer is not None else None
        )
        if implementation_commit != expected_commit:
            return StrictEligibilityResult(
                False, "complete", True, candidate["route_id"],
                candidate["candidate_id"], task_id, "RECEIPT_MISMATCH",
                ("implementation_commit",),
            )
        _bind_receipt(conn, candidate, "qa_verdict", verdict, purpose="qa_verdict")
        _bind_receipt(conn, candidate, "fresh_hermes_home", fresh_home, purpose="qa_verdict")
        _bind_receipt(conn, candidate, "implementation_commit", implementation_commit, purpose="qa_verdict")
    elif candidate["stage_kind"] == "rollout":
        authority = metadata.get("rollout_authority")
        manifest = metadata.get("runtime_manifest")
        if not authority or not manifest:
            return StrictEligibilityResult(
                False, "complete", True, candidate["route_id"],
                candidate["candidate_id"], task_id, "ROLLOUT_AUTHORITY_REQUIRED",
                ("rollout_authority", "runtime_manifest"),
            )
        _bind_receipt(conn, candidate, "rollout_authority", authority, purpose="rollout_authority")
        _bind_receipt(conn, candidate, "runtime_manifest", manifest, purpose="runtime_manifest")
    return StrictEligibilityResult(True, "complete", True, candidate["route_id"], candidate["candidate_id"], task_id)


def record_strict_route_receipt(conn, request: dict, *, board: Optional[str] = None) -> StrictEligibilityResult:
    """Persist one externally supplied immutable receipt without a status transition."""
    from hermes_cli import kanban_db as kb
    required = (
        "schema_version", "request_id", "board", "task_id", "receipt_kind",
        "receipt_purpose", "immutable_digest", "issuer_witness",
    )
    if not isinstance(request, dict) or any(not request.get(key) for key in required):
        return StrictEligibilityResult(False, "record_receipt", True, reason_code="MISSING_FIELD")
    if request["schema_version"] != SCHEMA_VERSION:
        return StrictEligibilityResult(False, "record_receipt", True, reason_code="UNSUPPORTED_SCHEMA_VERSION")
    if request["board"] != (board or kb.get_current_board()):
        return StrictEligibilityResult(False, "record_receipt", True, task_id=request["task_id"], reason_code="WRONG_BOARD")
    if _receipt_digest(request.get("payload")) != request["immutable_digest"]:
        return StrictEligibilityResult(False, "record_receipt", True, task_id=request["task_id"], reason_code="RECEIPT_MISMATCH")
    with kb.write_txn(conn):
        candidate = _candidate_row(conn, request["task_id"])
        if candidate is None:
            return StrictEligibilityResult(False, "record_receipt", False, task_id=request["task_id"], reason_code="OPERATION_NOT_ALLOWED")
        authorization = _active_candidate_authorization(
            conn, candidate, request["task_id"], "record_receipt",
        )
        if authorization is not None:
            return authorization
        stage_permissions = _EXTERNAL_RECEIPT_PERMISSIONS.get(candidate["stage_kind"], {})
        permitted_purpose = stage_permissions.get(request["receipt_kind"])
        if permitted_purpose is None:
            return StrictEligibilityResult(
                False, "record_receipt", True, candidate["route_id"],
                candidate["candidate_id"], request["task_id"], "RECEIPT_KIND_NOT_PERMITTED",
            )
        if request["receipt_purpose"] != permitted_purpose:
            return StrictEligibilityResult(
                False, "record_receipt", True, candidate["route_id"],
                candidate["candidate_id"], request["task_id"], "RECEIPT_PURPOSE_NOT_PERMITTED",
            )
        witness_code = _validate_admission_receipt(
            request["issuer_witness"], candidate["route_revision"],
        )
        if witness_code:
            return StrictEligibilityResult(
                False, "record_receipt", True, candidate["route_id"],
                candidate["candidate_id"], request["task_id"], witness_code,
            )
        witness = request["issuer_witness"]
        if (
            witness.get("kind") != request["receipt_kind"]
            or witness.get("payload") != request.get("payload")
        ):
            return StrictEligibilityResult(
                False, "record_receipt", True, candidate["route_id"],
                candidate["candidate_id"], request["task_id"], "RECEIPT_MISMATCH",
            )
        refusal_code = _validate_completion_payload(
            request["receipt_kind"], request.get("payload"),
            implementation_commit=(
                request.get("payload")
                if request["receipt_kind"] == "implementation_commit"
                else _candidate_receipt_payload(conn, candidate, "implementation_commit")
            ),
        )
        if refusal_code:
            return StrictEligibilityResult(
                False, "record_receipt", True, candidate["route_id"],
                candidate["candidate_id"], request["task_id"], refusal_code,
            )
        refusal_code = _validate_immutable_binding(
            conn, candidate, request["receipt_kind"], request.get("payload"),
            purpose=request["receipt_purpose"],
        )
        if refusal_code:
            return StrictEligibilityResult(
                False, "record_receipt", True, candidate["route_id"],
                candidate["candidate_id"], request["task_id"], refusal_code,
            )
        _bind_receipt(
            conn, candidate, request["receipt_kind"], request.get("payload"),
            purpose=request["receipt_purpose"],
        )
        kb._append_event(conn, request["task_id"], "strict_route_receipt_recorded", {
            "request_id": request["request_id"], "route_id": candidate["route_id"],
            "candidate_id": candidate["candidate_id"], "receipt_kind": request["receipt_kind"],
            "immutable_digest": request["immutable_digest"],
        })
        return StrictEligibilityResult(True, "record_receipt", True, candidate["route_id"], candidate["candidate_id"], request["task_id"])


def advance_after_completion(conn, task_id: str) -> None:
    """Move the single active watch to the permitted next strict stage."""
    candidate = _candidate_row(conn, task_id)
    if candidate is None:
        return
    if candidate["stage_kind"] == "developer":
        next_stage = f"qa.{candidate['cycle']}"
    elif candidate["stage_kind"] == "qa":
        verdict = _candidate_receipt_payload(conn, candidate, "qa_verdict")
        if verdict == "FAIL":
            # Strict-route/v1 permits one bounded repair cycle.  It is
            # materialized here, atomically with the accepted QA result,
            # rather than through generic task creation or a recovery path.
            if candidate["cycle"] >= 1:
                return
            from hermes_cli import kanban_db as kb
            repair_cycle = candidate["cycle"] + 1
            developer = _insert_candidate(
                conn, kb, candidate["revision_id"], candidate["route_id"],
                key=f"developer.{repair_cycle}", kind="developer", cycle=repair_cycle,
                status="ready", assignee="developer",
            )
            qa = _insert_candidate(
                conn, kb, candidate["revision_id"], candidate["route_id"],
                key=f"qa.{repair_cycle}", kind="qa", cycle=repair_cycle,
                status="todo", assignee="qa",
            )
            original_developer = conn.execute(
                "SELECT * FROM strict_route_candidates WHERE revision_id=? AND stage_key='developer.0'",
                (candidate["revision_id"],),
            ).fetchone()
            if original_developer is not None:
                for receipt_pk in conn.execute(
                    "SELECT receipt_pk FROM strict_route_candidate_receipts "
                    "WHERE candidate_id=? AND purpose='admission'",
                    (original_developer["candidate_id"],),
                ).fetchall():
                    conn.execute(
                        "INSERT INTO strict_route_candidate_receipts (candidate_id, receipt_pk, purpose) "
                        "VALUES (?, ?, 'admission')",
                        (developer["candidate_id"], receipt_pk["receipt_pk"]),
                    )
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (developer["task_id"], qa["task_id"]),
            )
            kb._append_event(conn, developer["task_id"], "strict_route_repair_admitted", {
                "route_id": candidate["route_id"], "from_candidate_id": candidate["candidate_id"],
                "cycle": repair_cycle,
            })
            _activate_watch(conn, developer)
            return
        if verdict != "PASS":
            return
        next_stage = f"rollout.{candidate['cycle']}"
    else:
        return
    next_candidate = conn.execute(
        "SELECT * FROM strict_route_candidates WHERE revision_id=? AND stage_key=? AND state='active'",
        (candidate["revision_id"], next_stage),
    ).fetchone()
    if next_candidate is not None:
        _activate_watch(conn, next_candidate)


def reconcile_strict_route(conn, request: dict, *, board: Optional[str] = None) -> StrictRouteReconcileResult:
    """Atomically admit/read back a normal-scope developer → QA → rollout route."""
    from hermes_cli import kanban_db as kb

    if not isinstance(request, dict):
        return _refuse("MISSING_FIELD", "strict route request must be an object")
    if request.get("schema_version") != SCHEMA_VERSION:
        return _refuse("UNSUPPORTED_SCHEMA_VERSION", "strict route schema version is not supported", request)
    route, stage, receipts = request.get("route"), request.get("stage"), request.get("receipts", [])
    required_route = ("governing_board", "governing_source_id", "root_task_id", "route_revision", "requirements_digest")
    required_stage = ("key", "kind", "cycle", "idempotency_key")
    if not isinstance(route, dict) or not isinstance(stage, dict) or not isinstance(receipts, list):
        return _refuse("MISSING_FIELD", "route, stage, and receipts are required", request)
    if any(not route.get(key) for key in required_route) or any(stage.get(key) in (None, "") for key in required_stage):
        return _refuse("MISSING_FIELD", "strict route identity fields are required", request)
    resolved_board = board or kb.get_current_board()
    if route["governing_board"] != resolved_board:
        return _refuse("WRONG_BOARD", "governing board does not match active board", request)
    try:
        risk_flags = _risk_flags(route)
    except ValueError:
        return _refuse("INVALID_RISK_CLASSIFICATION", "risk must be an object", request)
    normal_scope = not any(risk_flags.values())
    if normal_scope and stage["kind"] in {"human", "needs_input", "review", "digest_approval"}:
        return _refuse("UNSUPPORTED_INTERNAL_APPROVAL", "normal-scope routes cannot materialize approval work", request)
    if stage["kind"] != "developer" or stage["key"] != f"developer.{int(stage['cycle'])}":
        return _refuse("INVALID_STAGE", "initial admission must reconcile developer.<cycle>", request)
    supplied_kinds = {item.get("kind") for item in receipts if isinstance(item, dict)}
    missing = ADMISSION_RECEIPTS - supplied_kinds
    if missing:
        return _refuse("MISSING_RECEIPT", f"missing admission receipts: {', '.join(sorted(missing))}", request)

    for receipt in receipts:
        refusal_code = _validate_admission_receipt(receipt, route["route_revision"])
        if refusal_code:
            return _refuse(
                refusal_code,
                "strict-route/v1 receipt is incomplete, stale, or does not match its payload",
                request,
            )
    risk_receipts = [item for item in receipts if item.get("kind") == "risk_classification"]
    if len(risk_receipts) != 1:
        return _refuse("RECEIPT_CARDINALITY_INVALID", "exactly one risk classification receipt is required", request)
    risk_payload = risk_receipts[0].get("payload")
    if isinstance(risk_payload, dict):
        if any(bool(risk_payload.get(key, False)) != value for key, value in risk_flags.items()):
            return _refuse("RISK_CLASSIFICATION_MISMATCH", "risk receipt does not match route risk", request)

    now, risk_digest = int(time.time()), _risk_digest(route)
    with kb.write_txn(conn):
        route_row = conn.execute("SELECT * FROM strict_routes WHERE board_slug=? AND governing_source_id=? AND root_task_id=?", (resolved_board, route["governing_source_id"], route["root_task_id"])).fetchone()
        if route_row is None:
            route_id = "sr_" + secrets.token_hex(10)
            conn.execute("INSERT INTO strict_routes VALUES (?, ?, ?, ?, ?)", (route_id, resolved_board, route["governing_source_id"], route["root_task_id"], now))
        else:
            route_id = route_row["route_id"]
        revision = conn.execute("SELECT * FROM strict_route_revisions WHERE route_id=? AND route_revision=?", (route_id, str(route["route_revision"]))).fetchone()
        if revision:
            if revision["requirements_digest"] != route["requirements_digest"] or revision["risk_digest"] != risk_digest:
                return _refuse("DIGEST_MISMATCH", "route revision immutable fields differ", request, route_id)
            candidates, links, watch = _readback(conn, revision["revision_id"])
            kb._append_event(conn, candidates["developer.0"]["task_id"], "strict_route_replayed", {"request_id": request.get("request_id"), "route_id": route_id})
            return StrictRouteReconcileResult(True, True, dict(revision), candidates, links, watch, {"candidates": len(candidates), "links": len(links), "watches": int(watch is not None)})
        if conn.execute("SELECT 1 FROM strict_route_revisions WHERE route_id=? AND state='active'", (route_id,)).fetchone():
            return _refuse("STALE_OR_SUPERSEDED", "route already has an active revision", request, route_id)
        revision_id = "srv_" + secrets.token_hex(10)
        conn.execute("INSERT INTO strict_route_revisions VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)", (revision_id, route_id, str(route["route_revision"]), SCHEMA_VERSION, route["requirements_digest"], risk_digest, now))
        for receipt in receipts:
            conn.execute("INSERT INTO strict_route_receipts (revision_id, receipt_id, kind, digest, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)", (revision_id, receipt["receipt_id"], receipt["kind"], receipt["digest"], json.dumps(receipt.get("payload"), sort_keys=True), now))
        candidates = {}
        for key, kind, status, assignee in (("developer.0", "developer", "ready", "developer"), ("qa.0", "qa", "todo", "qa"), ("rollout.0", "rollout", "blocked", "developer")):
            task_id, candidate_id = kb._new_task_id(), "src_" + secrets.token_hex(10)
            conn.execute("INSERT INTO tasks (id,title,assignee,status,created_at,workspace_kind,idempotency_key) VALUES (?, ?, ?, ?, ?, 'scratch', ?)", (task_id, f"Strict route {key}", assignee, status, now, f"{route_id}/{key}"))
            conn.execute("INSERT INTO strict_route_candidates (candidate_id,revision_id,task_id,stage_key,stage_kind,cycle,idempotency_key,created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (candidate_id, revision_id, task_id, key, kind, f"{route_id}/{key}", now))
            kb._append_event(conn, task_id, "strict_route_admitted", {"request_id": request.get("request_id"), "route_id": route_id, "stage_key": key})
            candidates[key] = {"candidate_id": candidate_id, "task_id": task_id, "stage_key": key, "stage_kind": kind}
        developer_candidate = conn.execute(
            "SELECT * FROM strict_route_candidates WHERE candidate_id=?",
            (candidates["developer.0"]["candidate_id"],),
        ).fetchone()
        for receipt in receipts:
            if receipt["kind"] in ADMISSION_RECEIPTS:
                receipt_row = conn.execute(
                    "SELECT receipt_pk FROM strict_route_receipts "
                    "WHERE revision_id=? AND receipt_id=? AND kind=? AND digest=?",
                    (revision_id, receipt["receipt_id"], receipt["kind"], receipt["digest"]),
                ).fetchone()
                conn.execute(
                    "INSERT INTO strict_route_candidate_receipts (candidate_id, receipt_pk, purpose) "
                    "VALUES (?, ?, 'admission')",
                    (developer_candidate["candidate_id"], receipt_row["receipt_pk"]),
                )
        link = (candidates["developer.0"]["task_id"], candidates["qa.0"]["task_id"])
        conn.execute("INSERT INTO task_links (parent_id,child_id) VALUES (?, ?)", link)
        watch_id = "srw_" + secrets.token_hex(10)
        conn.execute("INSERT INTO strict_route_watches VALUES (?, ?, ?, ?, 'developer', ?, 1, ?, NULL)", (watch_id, revision_id, candidates["developer.0"]["candidate_id"], candidates["developer.0"]["task_id"], route["requirements_digest"], now))
        return StrictRouteReconcileResult(True, False, {"route_id": route_id, "revision_id": revision_id}, candidates, [link], {"watch_id": watch_id, "task_id": link[0]}, {"candidates": 3, "links": 1, "watches": 1})


def is_current_eligible(conn, task_id: str, operation: str) -> StrictEligibilityResult:
    row = conn.execute("SELECT c.*, r.route_id, r.route_revision, r.state AS revision_state FROM strict_route_candidates c JOIN strict_route_revisions r ON r.revision_id=c.revision_id WHERE c.task_id=?", (task_id,)).fetchone()
    if row is None:
        return StrictEligibilityResult(True, operation, False, task_id=task_id)
    watch = conn.execute("SELECT task_id FROM strict_route_watches WHERE revision_id=? AND active=1", (row["revision_id"],)).fetchone()
    if operation == "claim_review":
        return StrictEligibilityResult(False, operation, True, row["route_id"], row["candidate_id"], task_id, "OPERATION_NOT_ALLOWED")
    if row["stage_kind"] == "rollout" and operation in {"unblock", "claim", "dispatch_enumerate", "complete", "promote"}:
        return StrictEligibilityResult(False, operation, True, row["route_id"], row["candidate_id"], task_id, "ROLLOUT_AUTHORITY_REQUIRED")
    if row["stage_kind"] == "qa" and operation in {"recompute_ready", "claim", "promote", "dispatch_enumerate"}:
        developer = conn.execute(
            "SELECT * FROM strict_route_candidates WHERE revision_id=? AND stage_key=?",
            (row["revision_id"], f"developer.{row['cycle']}"),
        ).fetchone()
        present = _candidate_receipt_kinds(conn, developer) if developer else set()
        missing = tuple(kind for kind in IMPLEMENTATION_RECEIPTS if kind not in present)
        if missing:
            return StrictEligibilityResult(False, operation, True, row["route_id"], row["candidate_id"], task_id, "MISSING_RECEIPT", missing)
    active_operations = {"complete", "recompute_ready", "claim", "claim_review", "promote", "unblock", "block", "release_stale", "reclaim", "crash_recovery", "timeout_recovery", "dispatch_enumerate", "dispatch_claim", "dispatch_spawn", "dashboard_status", "dashboard_link"}
    if row["state"] != "active" or row["revision_state"] != "active" or watch is None or (operation in active_operations and watch["task_id"] != task_id):
        return StrictEligibilityResult(False, operation, True, row["route_id"], row["candidate_id"], task_id, "NOT_CURRENT_CANDIDATE", active_task_id=watch["task_id"] if watch else None, expected_route_revision=row["route_revision"])
    if row["stage_kind"] == "developer" and operation == "complete":
        present = _candidate_receipt_kinds(conn, row)
        missing = tuple(kind for kind in IMPLEMENTATION_RECEIPTS if kind not in present)
        if missing:
            return StrictEligibilityResult(False, operation, True, row["route_id"], row["candidate_id"], task_id, "MISSING_RECEIPT", missing)
    if row["stage_kind"] == "developer" and operation in {"recompute_ready", "claim", "dispatch_enumerate"}:
        missing = tuple(kind for kind in ADMISSION_RECEIPTS if kind not in _candidate_receipt_kinds(conn, row))
        if missing:
            return StrictEligibilityResult(False, operation, True, row["route_id"], row["candidate_id"], task_id, "MISSING_RECEIPT", missing)
    return StrictEligibilityResult(True, operation, True, row["route_id"], row["candidate_id"], task_id, active_task_id=watch["task_id"], expected_route_revision=row["route_revision"])
