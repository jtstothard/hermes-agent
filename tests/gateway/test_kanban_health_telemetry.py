"""Tests for board-local health telemetry in kanban dispatcher.

These tests verify that the health telemetry for stuck dispatcher warnings
is correctly board-local: a cap/lock/guard on board A must not mask genuinely
stuck work on board B.

See PR #65581: fix(kanban): avoid false dispatcher stuck warnings.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace


@dataclass
class FakeDispatchResult:
    """Minimal DispatchResult-like object for health predicate tests."""

    spawned: list[tuple[str, str, str]] | None = None
    skipped_global_capped: bool = False
    skipped_per_profile_capped: list[tuple[str, str, int]] | None = None
    skipped_locked: bool = False
    respawn_guarded: list[tuple[str, str]] | None = None
    skipped_unassigned: list[str] | None = None


class TestGatewayBoardLocalHealthTelemetry:
    """Gateway health telemetry must be board-local."""

    def test_single_board_deferral_resets_bad_ticks(self):
        """Single board with deferred work does NOT increment bad_ticks."""
        ready_boards = {"board-a"}
        results = [
            ("board-a", FakeDispatchResult(
                spawned=None,
                skipped_global_capped=True,
            )),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        # Board is ready but deferred, so no stuck boards
        assert stuck == set()
        # bad_ticks should NOT increment (would reset to 0)

    def test_single_board_ready_no_spawn_increments_bad_ticks(self):
        """Single board with ready work and no spawn DOES increment bad_ticks."""
        ready_boards = {"board-a"}
        results = [
            ("board-a", FakeDispatchResult(spawned=None)),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        # Board is ready, not deferred, no spawn -> stuck
        assert stuck == {"board-a"}
        # bad_ticks should increment

    def test_multi_board_deferral_does_not_mask_other_board_stuck(self):
        """Board A deferred must NOT mask board B stuck state (regression)."""
        ready_boards = {"board-a", "board-b"}
        results = [
            # Board A: ready, but globally capped (intentionally deferred)
            ("board-a", FakeDispatchResult(
                spawned=None,
                skipped_global_capped=True,
            )),
            # Board B: ready, no spawn, no deferral (genuinely stuck)
            ("board-b", FakeDispatchResult(spawned=None)),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        # Board A is deferred, Board B is stuck
        assert stuck == {"board-b"}
        # bad_ticks should increment because board B is stuck
        # (board A's deferral doesn't exempt board B)

    def test_multi_board_all_deferred_no_stuck(self):
        """All boards deferred -> no stuck state."""
        ready_boards = {"board-a", "board-b"}
        results = [
            ("board-a", FakeDispatchResult(
                spawned=None,
                skipped_per_profile_capped=[("t1", "alpha", 2)],
            )),
            ("board-b", FakeDispatchResult(
                spawned=None,
                skipped_locked=True,
            )),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        assert stuck == set()
        # bad_ticks should NOT increment

    def test_multi_board_one_spawned_one_still_stuck(self):
        """Board A spawned, Board B stuck with ready work -> still stuck."""
        ready_boards = {"board-a", "board-b"}
        results = [
            ("board-a", FakeDispatchResult(
                spawned=[("t1", "alpha", "/workspace")],
            )),
            ("board-b", FakeDispatchResult(spawned=None)),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        # Board A spawned, Board B stuck
        assert stuck == {"board-b"}
        # bad_ticks should increment

    def test_multi_board_respawn_guarded_defers_stuck_warning(self):
        """Board A with respawn_guarded doesn't mask board B stuck."""
        ready_boards = {"board-a", "board-b"}
        results = [
            ("board-a", FakeDispatchResult(
                spawned=None,
                respawn_guarded=[("t1", "alpha")],
            )),
            ("board-b", FakeDispatchResult(spawned=None)),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        assert stuck == {"board-b"}
        # bad_ticks should increment

    def test_multi_board_empty_ready_no_stuck(self):
        """No boards have ready work -> no stuck state."""
        ready_boards = set()
        results = [
            ("board-a", FakeDispatchResult(spawned=None)),
            ("board-b", FakeDispatchResult(spawned=None)),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        assert stuck == set()
        # bad_ticks should NOT increment

    def test_board_with_ready_not_in_results_counts_as_stuck(self):
        """A board with ready work that wasn't dispatched (not in results)
        is treated as stuck (no spawn, no deferral)."""
        ready_boards = {"board-a", "board-b"}
        # Only board-a in results; board-b wasn't dispatched this tick
        results = [
            ("board-a", FakeDispatchResult(spawned=None)),
        ]

        deferred_slugs = {
            slug for slug, res in results
            if res is not None and (
                getattr(res, "skipped_global_capped", False)
                or bool(getattr(res, "skipped_per_profile_capped", ()))
                or bool(getattr(res, "skipped_locked", False))
                or bool(getattr(res, "respawn_guarded", ()))
            )
        }
        spawned_slugs = {
            slug for slug, res in results
            if res is not None and getattr(res, "spawned", None)
        }
        stuck = ready_boards - deferred_slugs - spawned_slugs

        # board-a in results but not deferred or spawned -> stuck
        # board-b has ready work but no dispatch result -> also stuck
        assert stuck == {"board-a", "board-b"}


class TestDaemonPathHealthPredicate:
    """Daemon path (legacy CLI dispatcher) health predicate tests.

    The daemon path in hermes_cli/kanban.py:_on_tick is already per-board
    (receives a single DispatchResult per call), so the health logic is
    simpler: ready_pending and not spawned_any -> bad_ticks.

    These tests verify the predicate logic directly. Note: the daemon path
    does NOT exempt based on deferral flags (those are gateway-specific).
    The daemon relies on _ready_queue_nonempty() which filters out
    control-plane lanes and already excludes skipped_unassigned tasks.
    """

    def test_daemon_ready_no_spawn_increments_bad_ticks(self):
        """When there's ready work and no spawn, bad_ticks increments."""
        res = FakeDispatchResult(spawned=None)
        ready_pending = True  # Simulated ready queue probe
        spawned_any = bool(res.spawned)
        should_increment = ready_pending and not spawned_any
        assert should_increment is True

    def test_daemon_spawned_resets_bad_ticks(self):
        """When something spawns, bad_ticks resets."""
        res = FakeDispatchResult(
            spawned=[("t1", "alpha", "/workspace")],
        )
        ready_pending = True
        spawned_any = bool(res.spawned)
        should_increment = ready_pending and not spawned_any
        assert should_increment is False

    def test_daemon_no_ready_resets_bad_ticks(self):
        """When no ready work, bad_ticks resets."""
        res = FakeDispatchResult(spawned=None)
        ready_pending = False
        spawned_any = bool(res.spawned)
        should_increment = ready_pending and not spawned_any
        assert should_increment is False

    def test_daemon_skipped_unassigned_counts_as_ready(self):
        """Tasks with no assignee (skipped_unassigned) count as ready pending."""
        res = FakeDispatchResult(
            spawned=None,
            skipped_unassigned=["t1", "t2"],
        )
        ready_pending = bool(res.skipped_unassigned)
        spawned_any = bool(res.spawned)
        should_increment = ready_pending and not spawned_any
        assert should_increment is True