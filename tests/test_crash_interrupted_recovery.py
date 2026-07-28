"""Tests for crash_interrupted end_reason (#71916).

Crash-interrupted sessions should be recoverable in /resume, distinguishing them
from intentional /new or idle-timeout resets that also use session_reset today.
"""

import time
from pathlib import Path

from hermes_state import SessionDB


def test_gateway_session_recovery_reopens_crash_interrupted_rows(tmp_path):
    """Sessions ended with crash_interrupted on startup must be recoverable."""
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    db.create_session(
        "crashed-gw-session",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    db.append_message("crashed-gw-session", "user", "hello")
    db.end_session("crashed-gw-session", "crash_interrupted")

    # Recovery should find the crash_interrupted session
    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    assert recovered is not None
    assert recovered["id"] == "crashed-gw-session"
    assert recovered["end_reason"] == "crash_interrupted"

    # Reopen should work
    db.reopen_session("crashed-gw-session")
    row = db.get_session("crashed-gw-session")
    assert row["ended_at"] is None
    assert row["end_reason"] is None


def test_gateway_session_recovery_does_not_reopen_intentional_session_reset_rows(tmp_path):
    """Sessions ended with session_reset (intentional /new) must NOT be recoverable.

    This test ensures the fix doesn't accidentally make ALL session_reset rows
    recoverable — only crash_interrupted ones.
    """
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    db.create_session(
        "reset-gw-session",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    db.append_message("reset-gw-session", "user", "hello")
    # Intentional /new reset
    db.end_session("reset-gw-session", "session_reset")

    # Recovery should NOT find the session_reset session
    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    assert recovered is None


def test_gateway_session_recovery_distinguishes_crash_from_reset(tmp_path):
    """Multiple sessions for same peer: crash_interrupted wins, session_reset does not."""
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    # Create an older intentional reset session
    db.create_session(
        "reset-sid",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    db.append_message("reset-sid", "user", "old")
    db.end_session("reset-sid", "session_reset")

    # Create a newer crash-interrupted session
    time.sleep(0.001)  # Ensure different timestamps
    db.create_session(
        "crash-sid",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    db.append_message("crash-sid", "user", "new")
    db.end_session("crash-sid", "crash_interrupted")

    # Recovery should find the crash_interrupted session (newer), not the reset one
    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    assert recovered is not None
    assert recovered["id"] == "crash-sid"
    assert recovered["end_reason"] == "crash_interrupted"