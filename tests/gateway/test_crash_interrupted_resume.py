"""Tests for crash-interrupted session recovery (#71916).

Crash-interrupted sessions must remain recoverable via /resume after an unclean
gateway shutdown. This is distinct from intentional resets (/new, idle-timeout)
which should NOT become recoverable.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionSource, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from typing import Optional

def _make_entry(key: str, session_id: str, updated_at: Optional[datetime] = None) -> SessionEntry:
    now = datetime.now()
    return SessionEntry(
        session_key=key,
        session_id=session_id,
        created_at=now - timedelta(hours=2),
        updated_at=updated_at or now - timedelta(hours=1),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )


def _make_entry_with_origin(key: str, session_id: str) -> SessionEntry:
    entry = _make_entry(key, session_id)
    entry.origin = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5140768830",
        chat_type="dm",
        user_id="5140768830",
        user_name="João",
    )
    return entry


def _make_store_with_db(tmp_path, db_mock) -> SessionStore:
    """Build a SessionStore with a mock SessionDB, bypassing disk load."""
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = db_mock
    store._loaded = True
    return store


def _db_returning(rows: dict) -> MagicMock:
    """SessionDB mock where get_session maps session_id -> row dict."""
    db = MagicMock()
    db.get_session.side_effect = lambda sid: rows.get(sid)
    return db


# ---------------------------------------------------------------------------
# Crash-interrupted recovery tests
# ---------------------------------------------------------------------------

class TestCrashInterruptedRecovery:
    def test_crash_interrupted_session_not_pruned(self, tmp_path):
        """Sessions marked crash_interrupted should survive startup pruning."""
        db = _db_returning({"sid_crashed": {"end_reason": "crash_interrupted", "id": "sid_crashed"}})
        store = _make_store_with_db(tmp_path, db)
        store._entries["crashed_key"] = _make_entry("crashed_key", "sid_crashed")

        store._prune_stale_sessions_locked()

        assert "crashed_key" in store._entries, "crash_interrupted session should be kept"

    def test_session_reset_still_pruned(self, tmp_path):
        """Sessions ended with session_reset (intentional reset) should still be pruned."""
        db = _db_returning({"sid_reset": {"end_reason": "session_reset", "id": "sid_reset"}})
        store = _make_store_with_db(tmp_path, db)
        store._entries["reset_key"] = _make_entry("reset_key", "sid_reset")

        store._prune_stale_sessions_locked()

        assert "reset_key" not in store._entries, "session_reset should be pruned"

    def test_new_command_session_not_recoverable_via_find(self, tmp_path):
        """Sessions ended via /new (session_reset) are NOT recoverable."""
        db = MagicMock()
        db.find_latest_gateway_session_for_peer.return_value = None  # No recoverable session
        db.reopen_session.return_value = None

        store = _make_store_with_db(tmp_path, db)

        # Simulate session that was ended by /new
        db.get_session.return_value = {"end_reason": "session_reset", "id": "sid_new"}
        store._entries["new_key"] = _make_entry_with_origin("new_key", "sid_new")

        recovered = store._recover_session_from_db(
            session_key="new_key",
            source=store._entries["new_key"].origin,
            now=datetime.now(),
        )

        assert recovered is None, "/new-ended session should not recover"

    def test_crash_interrupted_session_recoverable_via_find(self, tmp_path):
        """Sessions marked crash_interrupted ARE recoverable via find_latest_gateway_session_for_peer."""
        db = MagicMock()
        db.find_latest_gateway_session_for_peer.return_value = {
            "id": "sid_crashed",
            "session_key": "agent:main:telegram:dm:5140768830",
            "started_at": 1782744974.0,
        }
        db.reopen_session.return_value = {"id": "sid_crashed"}

        store = _make_store_with_db(tmp_path, db)
        entry = _make_entry_with_origin("crashed_key", "sid_parent")
        store._entries["crashed_key"] = entry

        # When an entry points to a crash_interrupted session, recovery should succeed
        db.get_session.return_value = {"end_reason": "crash_interrupted", "id": "sid_parent"}

        recovered = store._recover_session_from_db(
            session_key="crashed_key",
            source=entry.origin,
            now=datetime.now(),
        )

        assert recovered is not None, "crash_interrupted session should recover"
        # The recovered entry should point to the session found via find_latest_gateway_session_for_peer
        assert recovered.session_id == "sid_crashed"
        db.find_latest_gateway_session_for_peer.assert_called_once()

    def test_idle_timeout_session_not_recoverable(self, tmp_path):
        """Idle timeout sessions (also session_reset) should NOT be recoverable."""
        db = _db_returning({"sid_idle": {"end_reason": "session_reset", "id": "sid_idle"}})
        store = _make_store_with_db(tmp_path, db)
        store._entries["idle_key"] = _make_entry("idle_key", "sid_idle")

        store._prune_stale_sessions_locked()

        assert "idle_key" not in store._entries, "idle-timeout session_reset should be pruned"