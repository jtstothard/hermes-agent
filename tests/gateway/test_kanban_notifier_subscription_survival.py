"""Regression: subscription survives transport failure.

Proves that 12+ consecutive send failures do NOT delete the
subscription. The notifier rewinds the cursor and retries on
every tick. Delivery failure may delay but must never remove
future notification coverage.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class _FailingAdapter:
    """Adapter that always fails on send."""
    def __init__(self):
        self.attempts = 0
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError(f"simulated transport failure #{self.attempts}")

    async def _send_with_retry(self, chat_id=None, content=None, metadata=None, **kwargs):
        return await self.send(chat_id, content, metadata=metadata)

    async def handle_message(self, event):
        self.handled.append(event)


class _FailThenSucceedAdapter:
    """Adapter that fails N times, then succeeds on all subsequent sends."""
    def __init__(self, fail_until_attempt: int):
        self.attempts = 0
        self.sent = []
        self.handled = []
        self.fail_until_attempt = fail_until_attempt

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        if self.attempts <= self.fail_until_attempt:
            raise RuntimeError(f"simulated failure #{self.attempts}")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

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

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _count_subs(conn, tid):
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM kanban_notify_subs WHERE task_id = ?",
        (tid,)
    ).fetchone()
    return row["cnt"] if row else 0


# ── Test A: 12+ failures do NOT delete subscription ───────────────

def test_subscription_survives_12plus_failures(tmp_path, monkeypatch):
    """After 12+ consecutive send failures, subscription remains present."""
    db_path = tmp_path / "sub-survival.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="failing task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = _FailingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_ticks(monkeypatch, runner, n=15))

    conn = kb.connect()
    try:
        subs = _count_subs(conn, tid)
        assert subs == 1, f"Subscription deleted after {adapter.attempts} failures! Expected 1, got {subs}"
    finally:
        conn.close()

    print(f"\n  Subscription survived {adapter.attempts} consecutive failures ✓")


# ── Test B: cursor remains rewound for retry ──────────────────────

def test_cursor_remains_recoverable_after_failures(tmp_path, monkeypatch):
    """After failures, cursor is rewound so the event is retryable."""
    db_path = tmp_path / "cursor-recovery.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = _FailingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_ticks(monkeypatch, runner, n=5))

    conn = kb.connect()
    try:
        _, _, events = kb.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["completed"],
        )
        assert len(events) >= 1, "Event lost after failures — cursor not rewound"
    finally:
        conn.close()

    print(f"  Cursor rewound, event still retryable after {adapter.attempts} failures ✓")


# ── Test C: recovery delivers the event after failures ─────────────

def test_event_delivered_after_recovery(tmp_path, monkeypatch):
    """After 13 failures, the event is delivered on recovery."""
    db_path = tmp_path / "recovery-delivery.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="recovery task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = _FailThenSucceedAdapter(fail_until_attempt=13)
    runner = _make_runner(adapter)
    asyncio.run(_run_ticks(monkeypatch, runner, n=30))

    assert len(adapter.sent) >= 1, (
        f"No delivery after recovery. attempts={adapter.attempts}"
    )
    assert "done" in adapter.sent[0]["text"].lower(), (
        f"Unexpected message: {adapter.sent[0]['text']}"
    )
    print(f"  Event delivered after {adapter.attempts} attempts ✓")


# ── Test D: successful recovery resumes normal processing ──────────

def test_successful_recovery_resumes_normal_processing(tmp_path, monkeypatch):
    """After transport recovery, normal notifications resume immediately."""
    db_path = tmp_path / "recovery-normal.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="recovery task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = _FailThenSucceedAdapter(fail_until_attempt=13)
    runner = _make_runner(adapter)
    asyncio.run(_run_ticks(monkeypatch, runner, n=30))

    assert len(adapter.sent) >= 1, "No delivery after recovery"
    assert "done" in adapter.sent[0]["text"].lower()

    fail_count = runner._kanban_sub_fail_counts.get(
        (tid, "telegram", "chat-1", ""), 0
    )
    assert fail_count == 0, f"Failure counter not reset after success: {fail_count}"

    print(f"  Normal processing resumed after recovery, counter reset ✓")
