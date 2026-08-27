"""Regression: dependency hard-stop produces exactly ONE notification.

Proves that the paired event sequence from the stopgap
(respawn_guarded + blocked in one write_txn) results in exactly
one user-visible notification, delivered via the blocked event.

The respawn_guarded event is NOT in TERMINAL_KINDS — only
the blocked event is claimed and rendered.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class _RecordingAdapter:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def _send_with_retry(self, chat_id=None, content=None, metadata=None, **kwargs):
        from gateway.platforms.base import SendResult
        try:
            await self.send(chat_id, content, metadata=metadata)
            return SendResult(success=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def handle_message(self, event):
        pass


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


def _emit_stopgap_paired_events(conn, tid):
    """Reproduce the exact paired event sequence from lines 8706-8736."""
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "respawn_guarded",
                         {"reason": "dependency_wait_escalated"})
        demoted = conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            "claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL "
            "WHERE id = ? AND status = 'ready'",
            (tid,),
        )
        if demoted.rowcount == 1:
            kb._append_event(conn, tid, "blocked",
                             {"reason": "dependency_wait_escalated",
                              "kind": "dependency",
                              "stopgap": True})


def test_hard_stop_sends_exactly_one_notification(tmp_path, monkeypatch):
    """Paired stopgap events → exactly 1 message (via blocked, not respawn_guarded)."""
    db_path = tmp_path / "single-notification.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="dep wait task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _emit_stopgap_paired_events(conn, tid)
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"Expected exactly 1 notification for hard-stop, got {len(adapter.sent)}. "
        f"Messages: {[m['text'] for m in adapter.sent]}"
    )

    msg = adapter.sent[0]["text"]
    assert "blocked" in msg.lower(), f"Expected blocked event message: {msg!r}"
    assert tid in msg, f"Message missing task ID: {msg!r}"
    assert "hard-stopped" not in msg, (
        f"Phase A respawn_guarded message reappeared: {msg!r}"
    )


def test_cooldown_guard_stays_silent(tmp_path, monkeypatch):
    """Respawn_guarded with non-escalation reason → 0 notifications."""
    db_path = tmp_path / "cooldown-silent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cooldown task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded",
                             {"reason": "dependency_wait_cooldown"})
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 0, (
        f"Cooldown guard should be silent, got {len(adapter.sent)}"
    )


def test_ordinary_blocked_still_notified(tmp_path, monkeypatch):
    """Non-stopgap blocked events still notify normally."""
    db_path = tmp_path / "ordinary-blocked.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ordinary block", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason="needs review", kind="needs_input")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "blocked" in adapter.sent[0]["text"].lower()
