"""Tests for respawn_guarded hard-stop notification delivery.

Verifies that the notifier delivers a user-visible message when a
dependency_wait hard-stop fires (reason == "dependency_wait_escalated"),
and stays silent for routine cooldown/auth/recent-success guards.

Scope: notification rendering and cursor behavior only. Does NOT test
the dependency-wait stopgap logic itself (covered separately).
"""

import asyncio
import json

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class _RecordingAdapter:
    def __init__(self):
        self.sent: list[dict] = []
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _emit_respawn_guarded(conn, tid, reason):
    """Emit a respawn_guarded event with the given reason directly."""
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "respawn_guarded", {"reason": reason})


def _count_unseen(conn, tid):
    """Count unseen terminal events for the default subscription."""
    _, _, events = kb.claim_unseen_events_for_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id="chat-1",
        kinds=[
            "completed", "blocked", "gave_up", "crashed", "timed_out",
            "status", "archived", "unblocked", "block_loop_detected",
            "respawn_guarded",
        ],
    )
    return len(events)


# ── Test 1: hard-stop is delivered ────────────────────────────────

def test_hard_stop_is_delivered(tmp_path, monkeypatch):
    """respawn_guarded(reason=dependency_wait_escalated) sends a notification."""
    db_path = tmp_path / "hard-stop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_respawn_guarded(conn, tid, "dependency_wait_escalated")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, f"Expected 1 delivery, got {len(adapter.sent)}"
    msg = adapter.sent[0]["text"]
    assert tid in msg
    assert "hard-stopped" in msg


# ── Test 2: message states explicit unblock requirement ───────────

def test_message_states_unblock_required(tmp_path, monkeypatch):
    """The hard-stop message explicitly says unblock is required."""
    db_path = tmp_path / "unblock-msg.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="needs unblock", assignee="ops")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_respawn_guarded(conn, tid, "dependency_wait_escalated")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    msg = adapter.sent[0]["text"]
    assert "explicit unblock required" in msg
    # Must NOT claim automatic retry
    assert "will retry" not in msg.lower()
    assert "retrying" not in msg.lower()


# ── Test 3: ordinary cooldown is silent ───────────────────────────

def test_cooldown_guard_is_silent(tmp_path, monkeypatch):
    """respawn_guarded(reason=dependency_wait_cooldown) must NOT notify."""
    db_path = tmp_path / "cooldown-silent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cooldown task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_respawn_guarded(conn, tid, "dependency_wait_cooldown")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 0, f"Expected 0 deliveries, got {len(adapter.sent)}: {adapter.sent}"


# ── Test 4: other guard reasons are silent ────────────────────────

def test_other_guard_reasons_are_silent(tmp_path, monkeypatch):
    """blocker_auth, recent_success, active_pr guards must NOT notify."""
    db_path = tmp_path / "other-guards.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    silent_reasons = ["blocker_auth", "recent_success", "active_pr"]
    for reason in silent_reasons:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title=f"guard {reason}", assignee="worker")
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
            _emit_respawn_guarded(conn, tid, reason)
        finally:
            conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 0, f"Expected 0 deliveries for silent reasons, got {len(adapter.sent)}"


# ── Test 5: cursor advances on successful send ────────────────────

def test_cursor_advances_on_success(tmp_path, monkeypatch):
    """After delivery, the cursor advances so the event is not replayed."""
    db_path = tmp_path / "cursor-advance.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cursor test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_respawn_guarded(conn, tid, "dependency_wait_escalated")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1

    # Second tick should find no new events
    adapter2 = _RecordingAdapter()
    runner2 = _make_runner(adapter2)
    asyncio.run(_run_one_tick(monkeypatch, runner2))

    assert len(adapter2.sent) == 0, "Event replayed after cursor advance"


# ── Test 6: send failure retains retry behavior ───────────────────

def test_send_failure_rewinds_cursor(tmp_path, monkeypatch):
    """On send failure, the cursor rewinds so the next tick retries."""
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    class _FailAdapter:
        def __init__(self):
            self.attempts = 0
            self.sent = []
            self.handled = []

        async def send(self, chat_id, text, metadata=None):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("Telegram 500")
            self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

        async def handle_message(self, event):
            self.handled.append(event)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_respawn_guarded(conn, tid, "dependency_wait_escalated")
    finally:
        conn.close()

    # First tick: send fails
    adapter1 = _FailAdapter()
    runner1 = _make_runner(adapter1)
    asyncio.run(_run_one_tick(monkeypatch, runner1))
    assert adapter1.attempts == 1
    assert len(adapter1.sent) == 0

    # Second tick: send succeeds
    adapter2 = _RecordingAdapter()
    runner2 = _make_runner(adapter2)
    asyncio.run(_run_one_tick(monkeypatch, runner2))
    assert len(adapter2.sent) == 1
    assert "hard-stopped" in adapter2.sent[0]["text"]


# ── Test 7: existing event types unchanged ────────────────────────

def test_completed_still_notified(tmp_path, monkeypatch):
    """completed events still deliver as before — this patch is additive."""
    db_path = tmp_path / "unchanged-completed.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="done task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="finished")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()


def test_blocked_still_notified(tmp_path, monkeypatch):
    """blocked events still deliver as before."""
    db_path = tmp_path / "unchanged-blocked.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason="needs review", kind="needs_input")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "blocked" in adapter.sent[0]["text"].lower()


# ── Test 8: unsubscribed card stays unsubscribed ──────────────────

def test_unsubscribed_card_not_notified(tmp_path, monkeypatch):
    """A card without a subscription receives no notification."""
    db_path = tmp_path / "no-sub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no sub task", assignee="worker")
        # Intentionally NO add_notify_sub call
        _emit_respawn_guarded(conn, tid, "dependency_wait_escalated")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 0


# ── Test 9: respawn_guarded cursor advances even when silent ──────

def test_silent_guard_advances_cursor(tmp_path, monkeypatch):
    """A silent guard reason still advances the cursor (no wedge)."""
    db_path = tmp_path / "silent-cursor.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="silent cursor", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_respawn_guarded(conn, tid, "dependency_wait_cooldown")
        # Then emit a completed event
        kb.complete_task(conn, tid, summary="done after cooldown")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    # Only the completed event should notify — cooldown was silently consumed
    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()
