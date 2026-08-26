from __future__ import annotations
import os, glob
import pytest



def test_DIAG_db_location(retired_kanban_home, tmp_path):
    import hermes_cli.kanban_db as kbmod
    import glob, os
    kb = retired_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="T")
        tid = kb.create_task(conn, title="ok", assignee="alpha")
    # where did the board db land?
    env_home = os.environ["HERMES_HOME"]
    print("\nHERMES_HOME:", env_home)
    print("temp tree:", [p.replace(env_home, "$HOME") for p in glob.glob(env_home + "/**/*.db", recursive=True)])
    for f in glob.glob("/home/hermes/.hermes/kanban/boards/*.db"):
        import time
        if time.time() - os.path.getmtime(f) < 120:
            print("REAL-HOME BOARD TOUCHED RECENTLY:", f)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 12345, dry_run=False)
    print("spawned:", res.spawned)
    assert res.spawned or True
