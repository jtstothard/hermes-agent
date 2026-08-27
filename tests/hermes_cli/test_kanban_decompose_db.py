"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=children, author="decomposer",
        )
    assert child_ids is not None and len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    assert c0.status == "ready" and c0.assignee == "researcher"
    assert c1.status == "todo" and c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None
    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)
    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


# ---------------------------------------------------------------------------
# Stage 2.3 — decomposer home-fallback tests
# ---------------------------------------------------------------------------

_FAKE_HOME = {
    "platform": "telegram",
    "chat_id": "99999",
    "thread_id": "42",
    "name": "TestHome",
}


@contextlib.contextmanager
def _patch_home(homes=None):
    """Context manager that mocks configured_home_channels so the decomposer
    fallback can execute in tests (which have no real config.yaml).

    The inlined fallback also calls load_config() and cfg_get() — we mock
    load_config to return a truthy dict so the auto_subscribe_on_create
    gate passes.
    """
    if homes is None:
        homes = [_FAKE_HOME]
    fake_cfg = {"kanban": {"auto_subscribe_on_create": True}}
    with patch.object(kb, "configured_home_channels", return_value=list(homes)):
        # Patch the load_config imported inside the inlined code via the
        # module-level function it resolves to.
        # The inlined code does: from hermes_cli.config import load_config
        # We patch the module attribute so the import picks up the mock.
        import hermes_cli.config as _cfg_mod
        with patch.object(_cfg_mod, "load_config", return_value=fake_cfg):
            yield


# 1. Subscribed parent → children inherit, no duplicate Home sub
def test_decompose_child_inherits_subscribed_parent(kanban_home):
    """Children inherit the parent's subscription exactly; the home fallback
    must NOT add a duplicate when the parent already has a subscription."""
    with kb.connect() as conn:
        root = _create_triage(conn, title="subscribed root")
        kb.add_notify_sub(
            conn, task_id=root, platform="telegram",
            chat_id="11111", thread_id="10", notifier_profile="default",
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orch",
            children=[{"title": "child A", "assignee": "w1"}],
            author="decomposer",
        )
    assert child_ids is not None
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, child_ids[0])
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "11111"
    assert subs[0]["thread_id"] == "10"


# 2. Unsubscribed parent → child gets Home subscription
def test_decompose_child_gets_home_when_parent_unsubscribed(kanban_home):
    """When the parent has zero subscriptions, the child must receive
    exactly one configured-home subscription."""
    with _patch_home():
        with kb.connect() as conn:
            root = _create_triage(conn, title="bare root")
            child_ids = kb.decompose_triage_task(
                conn, root, root_assignee="orch",
                children=[{"title": "orphan child", "assignee": "w1"}],
                author="decomposer",
            )
    assert child_ids is not None
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, child_ids[0])
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "99999"
    assert subs[0]["thread_id"] == "42"


# 3. Multiple children from unsubscribed parent → each gets Home
def test_decompose_multiple_children_all_get_home(kanban_home):
    with _patch_home():
        with kb.connect() as conn:
            root = _create_triage(conn, title="bare root multi")
            child_ids = kb.decompose_triage_task(
                conn, root, root_assignee="orch",
                children=[
                    {"title": "child 1", "assignee": "w1"},
                    {"title": "child 2", "assignee": "w2"},
                    {"title": "child 3", "assignee": "w3", "parents": [0, 1]},
                ],
                author="decomposer",
            )
    assert child_ids is not None and len(child_ids) == 3
    with kb.connect() as conn:
        for cid in child_ids:
            subs = kb.list_notify_subs(conn, cid)
            assert len(subs) >= 1, f"{cid} has no subscription"
            assert subs[0]["chat_id"] == "99999"


# 4. No Home configured → child creation succeeds with zero subscriptions
def test_decompose_child_no_home_configured(kanban_home):
    with _patch_home([]):
        with kb.connect() as conn:
            root = _create_triage(conn, title="no home root")
            child_ids = kb.decompose_triage_task(
                conn, root, root_assignee="orch",
                children=[{"title": "silent child", "assignee": "w1"}],
                author="decomposer",
            )
    assert child_ids is not None
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, child_ids[0])
    assert len(subs) == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, child_ids[0])
    assert task is not None and task.title == "silent child"


# 5. Origin subscription remains authoritative (no Home duplicate)
def test_decompose_origin_sub_not_duplicated_by_home(kanban_home):
    """If the root was subscribed via origin session (not Home), the child
    inherits that origin sub. The Home fallback must not add a second."""
    with _patch_home():
        with kb.connect() as conn:
            root = _create_triage(conn, title="origin sub root")
            kb.add_notify_sub(
                conn, task_id=root, platform="telegram",
                chat_id="55555", thread_id="99", notifier_profile="default",
            )
            child_ids = kb.decompose_triage_task(
                conn, root, root_assignee="orch",
                children=[{"title": "child", "assignee": "w1"}],
                author="decomposer",
            )
    assert child_ids is not None
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, child_ids[0])
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "55555"


# 6. Subscription cursor starts at creation-time high-water
def test_decompose_home_sub_cursor_at_creation_high_water(kanban_home):
    """The Home subscription created by the fallback must start caught-up
    to the child's current event high-water (the 'created' event)."""
    with _patch_home():
        with kb.connect() as conn:
            root = _create_triage(conn, title="cursor root")
            child_ids = kb.decompose_triage_task(
                conn, root, root_assignee="orch",
                children=[{"title": "cursor child", "assignee": "w1"}],
                author="decomposer",
            )
    assert child_ids is not None
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, child_ids[0])
        assert len(subs) == 1
        # Cursor must be at the final high-water after all events
        # (created, linked, promoted from recompute_ready).
        max_event = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?",
            (child_ids[0],),
        ).fetchone()[0]
        assert subs[0]["last_event_id"] == max_event
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (child_ids[0],)
        ).fetchall()
        assert len(events) >= 1
        assert events[0][0] == "created"


# 7. No historical replay (cursor = created event, no unseen)
def test_decompose_home_sub_no_historical_replay(kanban_home):
    with _patch_home():
        with kb.connect() as conn:
            root = _create_triage(conn, title="replay root")
            child_ids = kb.decompose_triage_task(
                conn, root, root_assignee="orch",
                children=[{"title": "replay child", "assignee": "w1"}],
                author="decomposer",
            )
    assert child_ids is not None
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, child_ids[0])
        assert len(subs) == 1
        _, unseen = kb.unseen_events_for_sub(
            conn, task_id=child_ids[0],
            platform="telegram", chat_id="99999", thread_id="42",
        )
        assert len(unseen) == 0
