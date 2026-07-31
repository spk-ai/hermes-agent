"""Behavioral coverage for the native opt-in strict-route authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _request() -> dict:
    def receipt(receipt_id: str, kind: str, payload: dict) -> dict:
        return {
            "schema_version": "strict-route/v1",
            "receipt_id": receipt_id,
            "kind": kind,
            "digest": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "current": True,
            "route_revision": "v1",
            "payload": payload,
        }

    return {
        "schema_version": "strict-route/v1",
        "request_id": "strict-route-native-authority",
        "route": {
            "governing_board": "default",
            "governing_source_id": "detector-source",
            "root_task_id": "root-task",
            "route_revision": "v1",
            "requirements_digest": "requirements-digest",
            "risk": {
                "external": False,
                "credentials": False,
                "payment": False,
                "production_risk": False,
            },
        },
        "stage": {
            "key": "developer.0",
            "kind": "developer",
            "cycle": 0,
            "idempotency_key": "route/developer/0",
        },
        "receipts": [
            receipt("detector", "detector_source", {"source": "detector-source"}),
            receipt(
                "risk",
                "risk_classification",
                {
                    "external": False,
                    "credentials": False,
                    "payment": False,
                    "production_risk": False,
                },
            ),
            receipt("plan", "planning_materialization", {"plan": "immutable"}),
        ],
    }


def test_native_strict_route_reconciliation_is_idempotent_and_uses_only_scheduling_links(kanban_home):
    with kb.connect() as conn:
        first = kb.reconcile_strict_route(conn, _request())
        replay = kb.reconcile_strict_route(conn, _request())

        assert first.ok is True
        assert replay.ok is True
        assert replay.replayed is True
        assert set(first.candidates) == {"developer.0", "qa.0", "rollout.0"}
        assert first.execution_links == [
            (first.candidates["developer.0"]["task_id"], first.candidates["qa.0"]["task_id"])
        ]
        assert first.active_watch["task_id"] == first.candidates["developer.0"]["task_id"]
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 1
