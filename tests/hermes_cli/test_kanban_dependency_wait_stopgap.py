"""STOPGAP tests (2026-08-27): dependency-wait loop guard (Variant B).

Provisional local stopgap pending upstream #77280/#61366. Removed when
upstream supersedes. Tests the event-count guard in check_respawn_guard
plus the dispatcher demote-to-blocked on escalation.

Recovery semantics (safety bias):
- dependency_wait #1 -> cooldown only (5 min)
- after cooldown -> one retry permitted
- dependency_wait #2 -> hard-stop (dependency_wait_escalated)
- loop-generated promoted/status events do NOT bypass or reset
- hard-stop release: explicit operator action (unblock on blocked,
  or promote_task(force=True) from todo) resets the recurrence window
- failure_limit unchanged; needs_input/capability sticky semantics unchanged
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _simulate_dep_wait(conn, tid, reason="NO-GO parent"):
    """Simulate one full loop iteration: worker blocks dependency, recompute
    re-promotes (emitting the loop-generated 'promoted' event)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    kb.claim_task(conn, tid, claimer="worker")
    kb.block_task(conn, tid, kind="dependency", reason=reason)
    kb.recompute_ready(conn)


def _advance_time(conn, tid, seconds):
    """Advance the latest dependency_wait event's created_at by `seconds`
    (SQLite: simulate cooldown expiry without sleeping)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - ? "
            "WHERE id = (SELECT MAX(id) FROM task_events "
            "             WHERE task_id = ? AND kind = 'dependency_wait')",
            (seconds, tid),
        )


# ---------------------------------------------------------------------------
# 1. Linked done-but-NO-GO parent stops after two dependency waits
# ---------------------------------------------------------------------------

def test_linked_no_go_parent_hard_stops_after_threshold(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        tid = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb.claim_task(conn, tid, claimer="worker")

        # Wait #1 -> cooldown
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO parent")
        kb.recompute_ready(conn)
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_cooldown"
        _advance_time(conn, tid, 301)

        # Wait #2 (after cooldown) -> hard-stop
        _simulate_dep_wait(conn, tid)
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"

        # No busy-loop: still escalated on next tick
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"


# ---------------------------------------------------------------------------
# 2. Loop-generated promoted event does NOT bypass
# ---------------------------------------------------------------------------

def test_loop_promoted_does_not_bypass(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)  # emits loop-generated 'promoted'
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_cooldown"
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)  # wait #2 + another 'promoted'
        # promoted events did NOT reset the count -> still escalated
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"


# ---------------------------------------------------------------------------
# 3. First wait + genuine parent completion inside cooldown remains deferred
# ---------------------------------------------------------------------------

def test_first_wait_parent_completes_inside_cooldown_stays_deferred(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        tid = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb.claim_task(conn, tid, claimer="worker")
        # Worker blocks with a dependency reason (semantic NO-GO prerequisite)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO prerequisite")
        kb.recompute_ready(conn)
        # Still inside 5-min cooldown -> deferred (A: cooldown wins)
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_cooldown"


# ---------------------------------------------------------------------------
# 4. Same case after cooldown may retry once
# ---------------------------------------------------------------------------

def test_after_cooldown_retry_permitted(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        # Cooldown expired, only 1 wait -> guard passes
        assert kb.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# 5. Second wait hard-stops
# ---------------------------------------------------------------------------

def test_second_wait_hard_stops(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"


# ---------------------------------------------------------------------------
# 6. Proven explicit release action resets the recurrence window
# ---------------------------------------------------------------------------

def test_unblock_releases_hard_stop(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"
        # Dispatcher demotes to blocked on escalation (as applied)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "blocked",
                             {"reason": "dependency_wait_escalated",
                              "kind": "dependency", "stopgap": True})
        # Operator unblocks (works on 'blocked', emits 'unblocked')
        assert kb.unblock_task(conn, tid) is True
        # Recurrence window reset -> no more escalation; but the 5-min
        # cooldown still applies (safety bias) until it expires
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_cooldown"
        # After cooldown expires -> fully released
        _advance_time(conn, tid, 301)
        assert kb.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# 7. Parentless dependency wait converges
# ---------------------------------------------------------------------------

def test_parentless_dep_wait_converges(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="orphan")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"


# ---------------------------------------------------------------------------
# 8. failure_limit unchanged
# ---------------------------------------------------------------------------

def test_failure_limit_unchanged(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id=?", (tid,),
        ).fetchone()
        assert int(row["consecutive_failures"]) == 0


# ---------------------------------------------------------------------------
# 9. needs_input/capability unchanged (sticky)
# ---------------------------------------------------------------------------

def test_needs_input_sticky_unchanged(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="needs_input", reason="human")
        assert kb._has_sticky_block(conn, tid) is True
        # recompute_ready does NOT auto-promote sticky blocked
        kb.recompute_ready(conn)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"


# ---------------------------------------------------------------------------
# 10. No busy-loop after threshold (dispatcher-level)
# ---------------------------------------------------------------------------

def test_no_busy_loop_after_threshold(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)
        # Two consecutive ticks both escalate
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"
        assert kb.check_respawn_guard(conn, tid) == "dependency_wait_escalated"


# ---------------------------------------------------------------------------
# 11. History remains auditable
# ---------------------------------------------------------------------------

def test_history_auditable(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, kind="dependency", reason="NO-GO")
        kb.recompute_ready(conn)
        _advance_time(conn, tid, 301)
        _simulate_dep_wait(conn, tid)
        evs = [e["kind"] for e in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
            (tid,)).fetchall()]
        # dependency_wait events present and in order
        assert evs.count("dependency_wait") == 2
        assert evs.count("promoted") >= 1  # loop-generated, still recorded
        # No schema migration: task_events columns unchanged
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(task_events)").fetchall()]
        assert cols == ["id", "task_id", "run_id", "kind", "payload", "created_at"]


# ---------------------------------------------------------------------------
# 12. No schema migration
# ---------------------------------------------------------------------------

def test_no_schema_migration(kanban_home):
    with kb.connect_closing() as conn:
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(tasks)").fetchall()]
        # No new column added by the stopgap
        assert "dependency_wait_count" not in cols
        assert "dependency_escalated_at" not in cols
