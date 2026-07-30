"""Tests for crash-window preservation of session_reset entries.

When the gateway is killed mid-drain (SIGTERM/fsfreeze), some sessions
receive ``end_reason = session_reset`` in the DB before
``suspend_recently_active()`` can mark them ``crash_interrupted``.  On the
next boot ``_prune_stale_sessions_locked()`` would normally prune them,
causing real conversation history to vanish from the ``/resume`` numbered
list.

The crash-window check preserves ``session_reset`` entries whose ``ended_at``
falls within 5 minutes of gateway boot, treating them as crash victims.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import MagicMock

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform=Platform.TELEGRAM, chat_id="123", user_id="u1"):
    return SessionSource(platform=platform, chat_id=chat_id, user_id=user_id)


def _make_store_with_db(tmp_path, db_rows):
    """Build a SessionStore with a mock DB that returns the given rows.

    db_rows: dict mapping session_id -> row dict (as SessionDB.get_session would return).
    """
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    db = MagicMock()
    db.get_session.side_effect = lambda sid: db_rows.get(sid)
    # No recovery — _recover_session_from_db depends on db methods we don't mock;
    # set origin=None on entries to skip that path.
    store._db = db
    return store


def _timestamp(seconds_ago: float) -> float:
    """Unix timestamp N seconds before now."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).timestamp()


def _seed_session(store, session_id):
    """Create a session in the store with the given session_id and no origin
    (avoids triggering _recover_session_from_db during pruning)."""
    source = _make_source()
    entry = store.get_or_create_session(source)
    entry.session_id = session_id
    entry.origin = None  # skip DB recovery lookup path
    return entry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSessionResetCrashWindowPreservation:
    """Verify session_reset entries within the crash window are preserved."""

    def test_session_reset_within_crash_window_preserved(self, tmp_path):
        """A session_reset ended <5 min before boot should be preserved."""
        store = _make_store_with_db(tmp_path, {
            "sid_recent": {
                "id": "sid_recent",
                "end_reason": "session_reset",
                "ended_at": _timestamp(90),  # 90 seconds ago
            }
        })
        entry = _seed_session(store, "sid_recent")

        store._prune_stale_sessions_locked()

        found = any(e.session_id == "sid_recent" for e in store._entries.values())
        assert found, "session_reset within 5-min crash window should be preserved"

    def test_session_reset_outside_crash_window_pruned(self, tmp_path):
        """A session_reset ended >5 min before boot should be pruned."""
        store = _make_store_with_db(tmp_path, {
            "sid_old": {
                "id": "sid_old",
                "end_reason": "session_reset",
                "ended_at": _timestamp(1800),  # 30 minutes ago
            }
        })
        _seed_session(store, "sid_old")

        store._prune_stale_sessions_locked()

        found = any(e.session_id == "sid_old" for e in store._entries.values())
        assert not found, "session_reset outside crash window should be pruned"

    def test_other_end_reasons_always_pruned(self, tmp_path):
        """Non-session_reset end_reasons are not subject to crash-window grace."""
        store = _make_store_with_db(tmp_path, {
            "sid_idle": {
                "id": "sid_idle",
                "end_reason": "idle_timeout",
                "ended_at": _timestamp(30),  # 30 seconds ago — well within window
            }
        })
        _seed_session(store, "sid_idle")

        store._prune_stale_sessions_locked()

        found = any(e.session_id == "sid_idle" for e in store._entries.values())
        assert not found, "idle_timeout should be pruned even within crash window"

    def test_null_end_reason_always_preserved(self, tmp_path):
        """Sessions with no end_reason (alive) are never pruned."""
        store = _make_store_with_db(tmp_path, {
            "sid_alive": {
                "id": "sid_alive",
                "end_reason": None,
                "ended_at": None,
            }
        })
        _seed_session(store, "sid_alive")

        store._prune_stale_sessions_locked()

        found = any(e.session_id == "sid_alive" for e in store._entries.values())
        assert found, "alive session (end_reason=None) should always be preserved"

    def test_boundary_just_outside_window_pruned(self, tmp_path):
        """A session_reset ended just over 5 min ago is pruned."""
        store = _make_store_with_db(tmp_path, {
            "sid_edge": {
                "id": "sid_edge",
                "end_reason": "session_reset",
                "ended_at": _timestamp(301),  # 5 min 1 sec ago
            }
        })
        _seed_session(store, "sid_edge")

        store._prune_stale_sessions_locked()

        found = any(e.session_id == "sid_edge" for e in store._entries.values())
        assert not found, "session_reset at 5m01s should be pruned (outside window)"

    def test_missing_ended_at_falls_through_to_prune(self, tmp_path):
        """If ended_at is absent, the entry is pruned (no crash)."""
        store = _make_store_with_db(tmp_path, {
            "sid_no_ts": {
                "id": "sid_no_ts",
                "end_reason": "session_reset",
                "ended_at": None,
            }
        })
        _seed_session(store, "sid_no_ts")

        store._prune_stale_sessions_locked()

        found = any(e.session_id == "sid_no_ts" for e in store._entries.values())
        assert not found, "session_reset without ended_at should be pruned"
