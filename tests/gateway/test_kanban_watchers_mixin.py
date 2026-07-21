"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import hashlib
import inspect
import json

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _active_watch_runtime_manifests,
)

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


def test_active_watch_runtime_manifest_is_read_only_and_current(tmp_path, monkeypatch):
    """Watcher manifests observe the active route without loading runtime state."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path().resolve()))
    with kb.connect() as conn:
        def receipt(receipt_id, kind, payload):
            return {
                "schema_version": "strict-route/v1", "receipt_id": receipt_id,
                "kind": kind, "payload": payload,
                "digest": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "current": True, "route_revision": "1",
            }

        result = kb.reconcile_strict_route(conn, {
            "schema_version": "strict-route/v1", "request_id": "watcher-manifest",
            "route": {"governing_board": "default", "governing_source_id": "watcher", "root_task_id": "root", "route_revision": "1", "requirements_digest": "requirements", "risk": {"external": False, "credentials": False, "payment": False, "production_risk": False}},
            "stage": {"key": "developer.0", "kind": "developer", "cycle": 0, "idempotency_key": "watcher/developer/0"},
            "receipts": [receipt("detector", "detector_source", {"source": "watcher"}), receipt("risk", "risk_classification", {"external": False, "credentials": False, "payment": False, "production_risk": False}), receipt("plan", "planning_materialization", {"plan": "watcher"})],
        })
        assert result.ok is True
        assert result.route is not None
        before = conn.total_changes
        manifests = _active_watch_runtime_manifests(conn)

        assert manifests == [{
            "schema_version": "strict-route/v1", "route_id": result.route["route_id"],
            "route_revision": "1", "task_id": result.candidates["developer.0"]["task_id"],
            "stage_kind": "developer", "requirements_digest": "requirements",
        }]
        assert conn.total_changes == before
