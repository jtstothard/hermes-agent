"""Tests for kanban.retired_assignees — dispatch-level profile retirement.

Retirement is a *dispatch-level* block independent of filesystem state:
a retired profile whose directory still exists (or is renamed back /
re-imported) is still never spawned by the dispatcher. Historical tasks
keep their retired assignee (visible + queryable, simply non-spawnable);
new creation/assignment/reassignment to a retired profile is rejected.

Guardrails covered:
  1. existing profile + not retired -> spawns
  2. existing profile + retired -> no spawn
  3. missing profile + retired -> no spawn (no crash)
  4. retired-ready task unchanged (status/claim intact)
  5. retry/reclaim cannot bypass retirement
  6. review path cannot bypass retirement
  7. dependency promotion cannot bypass retirement
  8. removing retirement restores spawn
  9. worker/ops unaffected
  10. health telemetry ignores retired-ready backlog (no stuck signal)
  11. retired default_assignee treated as invalid (unset + no crash)
  12. config key actually read (and documented in defaults)
  13. NEW create with retired assignee -> rejected
  14. NEW assign/reassign to retired assignee -> rejected
  15. historical existing retired-assignee card remains readable/queryable
  16. DispatchResult reports skipped_retired distinctly
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest


@pytest.fixture()
def retired_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with kanban DB + alpha/beta/retired profiles.

    ``retired`` exists on disk (so it is a *valid* profile as far as
    profile_exists() is concerned) and is initially NOT retired — cards
    are created first (as they would be in production before retirement
    is declared), then tests declare retirement via ``_set_retired``.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_retired_test_")
    for prof in ("alpha", "beta", "retired"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    with open(os.path.join(test_home, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("kanban:\n  retired_assignees: []\n")
    # monkeypatch.setenv auto-restores HERMES_HOME after the test, so no
    # module-state leakage into later test files (unlike del sys.modules).
    monkeypatch.setenv("HERMES_HOME", test_home)
    from hermes_cli import kanban_db

    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 12345


def _set_retired(monkeypatch, *retired: str) -> None:
    """Declare retirement in the temp HERMES_HOME config.yaml.

    ``load_config`` caches on (mtime_ns, size), so rewriting the file with
    a different body changes both and naturally invalidates the cache.
    """
    test_home = os.environ["HERMES_HOME"]
    body = "kanban:\n  retired_assignees:\n"
    for name in retired:
        body += f"    - {name}\n"
    with open(os.path.join(test_home, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write(body)
    monkeypatch.setenv("HERMES_HOME", test_home)



# --- 1. existing profile + not retired -> spawns --------------------------

def test_not_retired_spawns(retired_kanban_home):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="ok", assignee="alpha")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert [s[0] for s in res.spawned] == [tid]
    assert res.skipped_retired == []


# --- 2. existing profile + retired -> no spawn ----------------------------

def test_retired_profile_never_spawns(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="retired task", assignee="retired")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.spawned == []
    assert res.skipped_retired == [tid]


# --- 3. missing profile + retired -> no spawn (no crash) ------------------

def test_missing_profile_retired_no_spawn(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    # Remove the retired profile dir: the dispatcher must still skip the
    # task (bucket skipped_retired, not crash, not spawn).
    os.rmdir(os.path.join(os.environ["HERMES_HOME"], "profiles", "retired"))
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="ghost retired", assignee="retired")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.spawned == []
    # Retired check takes precedence over the missing-profile check: the
    # task lands in the retired bucket, not nonspawnable.
    assert res.skipped_retired == [tid]
    assert tid not in res.skipped_nonspawnable


# --- 4. retired-ready task unchanged --------------------------------------

def test_retired_ready_task_unchanged(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="keep me", assignee="retired")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.skipped_retired == [tid]
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["assignee"] == "retired"


# --- 5. retry/reclaim cannot bypass ---------------------------------------

def test_reclaim_cannot_bypass_retirement(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="retired run", assignee="retired")
    _set_retired(monkeypatch, "retired")
    # First tick: skipped (retired).
    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res1.skipped_retired == [tid]
    # Force the task into a reclaimed state (as a stale-claim reclaim or
    # failure-retry would), then dispatch again: still skipped, never spawns.
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL "
                "WHERE id = ?", (tid,)
            )
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res2.spawned == []
    assert res2.skipped_retired == [tid]


# --- 6. review path cannot bypass -----------------------------------------

def test_review_path_cannot_bypass_retirement(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="needs review", assignee="retired")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (tid,)
            )
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.spawned == []
    assert res.skipped_retired == [tid]
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "review"  # intact, untouched
    assert row["claim_lock"] is None


# --- 7. dependency promotion cannot bypass --------------------------------

def test_dependency_promotion_cannot_bypass(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        parent = kb.create_task(conn, title="parent", assignee="alpha")
        child = kb.create_task(
            conn, title="child", assignee="retired", parents=[parent],
        )
        assert child  # created (status will be 'todo' — parents not done)
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (parent,)
            )
        ok, reason = kb.promote_task(conn, child, actor="test")
        assert ok, reason  # promotes child -> ready
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (child,)
        ).fetchone()
        assert row["status"] == "ready"
        assert row["assignee"] == "retired"
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.spawned == []
    assert res.skipped_retired == [child]


# --- 8. removing retirement restores spawn --------------------------------

def test_removing_retirement_restores_spawn(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="back to life", assignee="retired")
    # Declare retirement first — the task is now blocked.
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res_blocked = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res_blocked.spawned == []
    assert res_blocked.skipped_retired == [tid]
    # Remove retirement (empty retired_assignees) — same task now spawns.
    _set_retired(monkeypatch)
    with kb.connect_closing() as conn:
        res_free = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert [s[0] for s in res_free.spawned] == [tid]
    assert res_free.skipped_retired == []


# --- 9. worker/ops unaffected ---------------------------------------------

def test_unrelated_profiles_unaffected(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="alpha task", assignee="alpha")
        kb.create_task(conn, title="beta task", assignee="beta")
        rid = kb.create_task(conn, title="retired task", assignee="retired")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    spawned_assignees = sorted(s[1] for s in res.spawned)
    assert spawned_assignees == ["alpha", "beta"]
    assert res.skipped_retired == [rid]
    # Non-retired profiles are not accidentally filtered.
    assert all(who != "retired" for _, who, _ in res.spawned)


# --- 10. health telemetry ignores retired-ready backlog -------------------

def test_health_telemetry_ignores_retired_backlog(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="r1", assignee="retired")
        kb.create_task(conn, title="r2", assignee="retired")
    # With retirement active, a backlog of retired-ready tasks is
    # "correctly idle" — has_spawnable_ready must be False (no stuck).
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is False
    # Without retirement, the same backlog IS spawnable.
    _set_retired(monkeypatch)
    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is True


# --- 11. retired default_assignee treated as invalid ----------------------

def test_retired_default_assignee_ignored(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="unassigned")  # no assignee
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="retired",
        )
    # The retired default is treated as unset: task stays unassigned,
    # nothing spawns, no crash.
    assert res.spawned == []
    assert len(res.skipped_unassigned) == 1
    assert res.auto_assigned_default == []


# --- 12. config key actually read (and documented in defaults) ------------

def test_config_key_read_and_documented():
    from hermes_cli import kanban_db

    # Direct helper read: the key is honored when present...
    assert kanban_db.retired_assignees(
        kanban_cfg={"retired_assignees": ["coder", "  ghost  ", ""]}
    ) == frozenset({"coder", "ghost"})
    # ...and empty (historical default) when absent.
    assert kanban_db.retired_assignees(kanban_cfg={}) == frozenset()
    # A single bare string is tolerated and normalized.
    assert kanban_db.retired_assignees(kanban_cfg={"retired_assignees": "coder"}) == frozenset({"coder"})
    # A CLI-written JSON-array string (e.g. `hermes config set
    # kanban.retired_assignees '["coder"]'`) must decode, not be treated
    # as one bogus profile name (live-config regression, 2026-08-26).
    assert kanban_db.retired_assignees(
        kanban_cfg={"retired_assignees": '["coder"]'}
    ) == frozenset({"coder"})
    assert kanban_db.retired_assignees(
        kanban_cfg={"retired_assignees": '["coder", "ghost"]'}
    ) == frozenset({"coder", "ghost"})

    # The key must be documented in config_defaults so an unpatched install
    # can't silently no-op (the exact failure mode this feature closes).
    import hermes_cli.config_defaults as cd
    source = open(cd.__file__, encoding="utf-8").read()
    assert "retired_assignees" in source
    assert "retirement is a dispatch-level block" in source.lower() or "retired_assignees" in source


# --- 13. NEW create with retired assignee -> rejected ---------------------

def test_create_with_retired_assignee_rejected(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="retired"):
            kb.create_task(conn, title="bad", assignee="retired")


# --- 14. NEW assign/reassign to retired assignee -> rejected --------------

def test_assign_to_retired_rejected(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="move me", assignee="alpha")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="retired"):
            kb.assign_task(conn, tid, "retired")
    # Reassign path (reassign_task -> assign_task) hits the same guard.
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="retired"):
            kb.reassign_task(conn, tid, "retired", reclaim_first=True)
    # The task is unchanged.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["assignee"] == "alpha"


# --- 15. historical existing retired-assignee card remains queryable ------

def test_historical_retired_card_readable(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="history", assignee="retired")
    _set_retired(monkeypatch, "retired")
    # The card still exists, is queryable, and keeps its assignee after a
    # dispatch tick that skips it.
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT id, title, assignee, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["title"] == "history"
    assert row["assignee"] == "retired"
    assert row["status"] == "ready"
    assert res.skipped_retired == [tid]


# --- 16. DispatchResult reports skipped_retired distinctly ----------------

def test_skipped_retired_bucket_distinct(retired_kanban_home, monkeypatch):
    kb = retired_kanban_home
    # Two tasks: one assigned to a retired profile, one assigned to a
    # control-plane lane (non-existent profile dir -> skipped_nonspawnable).
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        rid = kb.create_task(conn, title="retired one", assignee="retired")
        kb.create_task(conn, title="terminal lane", assignee="orion-cc")
    _set_retired(monkeypatch, "retired")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.skipped_retired == [rid]
    assert len(res.skipped_nonspawnable) == 1
    assert rid not in res.skipped_nonspawnable
    assert all(t != rid for t in res.skipped_nonspawnable)


# --- config-load integration: real config.yaml honors the key -------------

def test_retired_assignees_reads_live_config(monkeypatch, tmp_path):
    """End-to-end: a config.yaml with the key is honored by the helper."""
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    (cfg_home / "config.yaml").write_text(
        "kanban:\n  retired_assignees:\n    - coder\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(cfg_home))
    # The helper re-reads load_config() on every call; fresh env is enough.
    from hermes_cli import kanban_db
    assert kanban_db.retired_assignees() == frozenset({"coder"})
    # Removing the key restores an empty set (config reload honors changes).
    (cfg_home / "config.yaml").write_text("kanban:\n  retired_assignees: []\n")
    assert kanban_db.retired_assignees() == frozenset()
