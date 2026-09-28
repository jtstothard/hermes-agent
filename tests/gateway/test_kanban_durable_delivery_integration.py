"""Disposable integration tests for durable delivery (Stage 1.4).

Test A — gateway crash after mark_attempting (sweep recovery)
Test B — prolonged transport outage (>12 failures, notifier retry)
Test C — ambiguous send/crash (at-least-once via sweep)
"""

import asyncio
import os
import time
from pathlib import Path

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


# ── Helpers ────────────────────────────────────────────────────────


class _RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id=None, text=None, metadata=None, **kwargs):
        content = kwargs.get("content") or text
        self.sent.append({"chat_id": chat_id, "text": content, "metadata": metadata or {}})
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def _send_with_retry(self, chat_id=None, content=None, metadata=None, **kwargs):
        from gateway.platforms.base import SendResult
        try:
            await self.send(chat_id, content, metadata=metadata)
            return SendResult(success=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def handle_message(self, event):
        self.handled.append(event)


class _FailThenSucceedAdapter:
    """Fails N times, then succeeds."""
    def __init__(self, fail_until_attempt: int):
        self.attempts = 0
        self.sent = []
        self.handled = []
        self.fail_until_attempt = fail_until_attempt

    async def send(self, chat_id=None, text=None, metadata=None, **kwargs):
        content = kwargs.get("content") or text
        self.attempts += 1
        if self.attempts <= self.fail_until_attempt:
            raise RuntimeError(f"simulated failure #{self.attempts}")
        self.sent.append({"chat_id": chat_id, "text": content, "metadata": metadata or {}})

    async def _send_with_retry(self, chat_id=None, content=None, metadata=None, **kwargs):
        return await self.send(chat_id, content, metadata=metadata)

    async def handle_message(self, event):
        self.handled.append(event)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_ticks(monkeypatch, runner, n: int = 1):
    real_sleep = asyncio.sleep
    tick_count = [0]

    async def fake_sleep(delay):
        tick_count[0] += 1
        if tick_count[0] >= n:
            runner._running = False
        await real_sleep(0)

    # Patch kanban_watchers.asyncio.sleep directly (module-level ref)
    import gateway.kanban_watchers as _kw
    original_kw_sleep = _kw.asyncio.sleep
    _kw.asyncio.sleep = fake_sleep
    try:
        await runner._kanban_notifier_watcher(interval=1)
    finally:
        _kw.asyncio.sleep = original_kw_sleep


def _patch_ledger_db(monkeypatch, tmp_path):
    """Redirect delivery_ledger to use a test DB."""
    import gateway.delivery_ledger as dl
    test_ledger_db = str(tmp_path / "state.db")
    monkeypatch.setattr(dl, "_db_path", lambda: Path(test_ledger_db))
    dl._connect()  # trigger schema creation
    return test_ledger_db


# ── Test A — gateway crash after mark_attempting ───────────────────


def test_crash_recovery_sweep_recoverable(tmp_path, monkeypatch):
    """Prove that a gateway crash after mark_attempting is recoverable.

    1. Create subscribed task, generate terminal event
    2. Record obligation / enter attempting state
    3. Simulate process death (stale owner)
    4. _redeliver_pending_obligations finds + sends it
    5. Obligation reaches delivered terminal state
    6. No duplicate storm
    """
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_ledger_db(monkeypatch, tmp_path)

    # 1. Create task + subscription + terminal event
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="crash recovery task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    # 2. Record obligation / enter attempting
    import gateway.delivery_ledger as dl
    session_key = f"agent:kanban:notifier:telegram:chat-1:"
    obligation_id = dl.compute_obligation_id(session_key, tid + "completed", "test content")
    dl.record_obligation(
        obligation_id=obligation_id,
        session_key=session_key,
        platform="telegram",
        chat_id="chat-1",
        thread_id=None,
        content="test content",
    )
    dl.mark_attempting(obligation_id)

    # 3. Simulate process death: owner pid → dead process
    ledger_conn = __import__("sqlite3").connect(dl._db_path())
    try:
        ledger_conn.execute(
            "UPDATE delivery_obligations SET owner_pid = 999999, owner_started_at = 1.0 "
            "WHERE obligation_id = ?",
            (obligation_id,)
        )
        ledger_conn.commit()
    finally:
        ledger_conn.close()

    # 4. _redeliver_pending_obligations does sweep + send
    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    redelivered = asyncio.run(runner._redeliver_pending_obligations())
    assert redelivered >= 1, f"Expected >=1 redelivery, got {redelivered}"
    assert len(adapter.sent) >= 1, "Notification not delivered after sweep"

    # 5. Obligation reaches delivered
    ledger_conn = __import__("sqlite3").connect(dl._db_path())
    try:
        row = ledger_conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id = ?",
            (obligation_id,)
        ).fetchone()
        assert row[0] == "delivered", f"Expected 'delivered', got '{row[0]}'"
    finally:
        ledger_conn.close()

    # 6. No duplicate storm — exactly one redelivery
    assert len(adapter.sent) == 1, f"Expected 1 redelivery, got {len(adapter.sent)}"

    print(f"\n  Test A: crash recovery — obligation {obligation_id[:16]}... "
          f"recovered and delivered ✓")


# ── Test B — prolonged transport outage ────────────────────────────


def test_prolonged_outage_subscription_survives(tmp_path, monkeypatch):
    """Simulate >12 failed notifier attempts with a NON-TERMINAL event.

    Prove:
    - subscription remains present (not deleted by failure handler)
    - no permanent silence
    - E1 remains recoverable
    - after transport recovery E1 is handled

    Note: terminal events (completed/gave_up/crashed) trigger normal
    terminal-state cleanup which removes the subscription — that's
    correct behavior. This test uses a 'blocked' event which is
    non-terminal, to prove the failure handler retains subscriptions.
    """
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_ledger_db(monkeypatch, tmp_path)

    # Create task + subscription + NON-terminal event
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="outage task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason="dependency_wait")
    finally:
        conn.close()

    # Phase 1: transport is permanently down — adapter always fails.
    # (Cursor must stay rewound so the event is still claimable.)
    failing = _FailThenSucceedAdapter(fail_until_attempt=999)
    runner = _make_runner(failing)
    asyncio.run(_run_ticks(monkeypatch, runner, n=20))

    # Subscription still present
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
            (tid,)
        ).fetchone()
        assert row[0] == 1, f"Subscription deleted after {failing.attempts} failures!"
    finally:
        conn.close()

    # Cursor rewound — event still claimable
    conn = kb.connect()
    try:
        _, _, events = kb.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["blocked"],
        )
        assert len(events) >= 1, "Event lost after prolonged outage"
        kb.rewind_notify_cursor(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            claimed_cursor=events[-1].id, old_cursor=0,
        )
    finally:
        conn.close()

    # Phase 2: Transport recovers — new adapter succeeds
    recovery = _RecordingAdapter()
    runner2 = _make_runner(recovery)
    asyncio.run(_run_ticks(monkeypatch, runner2, n=5))

    assert len(recovery.sent) >= 1, "Event not delivered after transport recovery"
    assert "blocked" in recovery.sent[0]["text"].lower(), f"Wrong message: {recovery.sent[0]['text']}"

    print(f"\n  Test B: prolonged outage — {failing.attempts} failures, "
          f"recovery delivered ✓")


# ── Test C — ambiguous send/crash ──────────────────────────────────


def test_ambiguous_send_crash_at_least_once(tmp_path, monkeypatch):
    """Prove at-least-once semantics: if the adapter succeeds but the
    gateway crashes before marking delivered, _redeliver_pending_obligations
    will re-deliver — resulting in a duplicate."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_ledger_db(monkeypatch, tmp_path)

    # Create task + subscription + terminal event
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ambiguous task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    # Record obligation, mark attempting (simulates: send happened, crash before mark_delivered)
    import gateway.delivery_ledger as dl
    session_key = f"agent:kanban:notifier:telegram:chat-1:"
    obligation_id = dl.compute_obligation_id(session_key, tid + "completed", "ambiguous content")
    dl.record_obligation(
        obligation_id=obligation_id,
        session_key=session_key,
        platform="telegram",
        chat_id="chat-1",
        thread_id=None,
        content="ambiguous content",
    )
    dl.mark_attempting(obligation_id)

    # Simulate crash: owner dies, state stays 'attempting'
    ledger_conn = __import__("sqlite3").connect(dl._db_path())
    try:
        ledger_conn.execute(
            "UPDATE delivery_obligations SET owner_pid = 999999, owner_started_at = 1.0 "
            "WHERE obligation_id = ?",
            (obligation_id,)
        )
        ledger_conn.commit()
    finally:
        ledger_conn.close()

    # _redeliver_pending_obligations sends it (this IS the duplicate)
    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    redelivered = asyncio.run(runner._redeliver_pending_obligations())
    assert redelivered >= 1, "Redelivery not sent after sweep"
    assert len(adapter.sent) >= 1, "No notification sent"

    # At-least-once: obligation reaches terminal state
    ledger_conn = __import__("sqlite3").connect(dl._db_path())
    try:
        row = ledger_conn.execute(
            "SELECT state, attempts FROM delivery_obligations WHERE obligation_id = ?",
            (obligation_id,)
        ).fetchone()
        assert row[0] == "delivered", f"Expected 'delivered', got '{row[0]}'"
        assert row[1] >= 1, f"Expected >=1 attempt, got {row[1]}"
    finally:
        ledger_conn.close()

    print(f"\n  Test C: ambiguous send/crash — at-least-once semantics proven, "
          f"obligation redelivered ✓")
