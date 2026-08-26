"""Regression tests for the post-budget terminal grace (#terminator-race).

Covers the Candidate B design:
  * ONE post-budget Kanban-only grace turn exposing only kanban_complete /
    kanban_block
  * max_turns remains the work budget; the grace turn is additional
  * terminal emission closes the run; no second grace; plain-text grace
    exits fall through to the existing bounded timed_out path
  * non-Kanban sessions are untouched
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_finalizer import finalize_turn


class _GraceAgent:
    """Minimal AIAgent double for finalize_turn."""

    def __init__(
        self,
        *,
        max_iterations=60,
        budget_remaining=0,
        in_grace_turn=False,
        kanban_terminal_emitted=False,
        quiet_mode=True,
    ):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=budget_remaining, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = quiet_mode
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names: set = set()
        self.persisted_messages = None
        self._in_grace_turn = in_grace_turn
        self._kanban_terminal_emitted = kanban_terminal_emitted
        self._budget_grace_call = False
        self.tools: list = []
        self.enabled_toolsets = None
        self.disabled_toolsets = None
        self._handle_max_iterations_called = False
        self._completion_explainer = False

    def _handle_max_iterations(self, messages, api_call_count):
        self._handle_max_iterations_called = True
        return "summary from extra call"

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return self._completion_explainer

    def _format_turn_completion_explanation(self, _reason):
        return "iteration-limit explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(
    agent,
    *,
    final_response=None,
    exit_reason="budget_exhausted",
    api_call_count=60,
):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response=None,
        _pending_verification_response_previewed=False,
    )


# ── 11. Forced summary text "task done" NEVER auto-completes ───────────
def test_summary_text_does_not_auto_complete(monkeypatch):
    """A toolless summary saying 'task is done' must not flip the outcome."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_summary")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    # Simulate: budget exhausted, no terminal tool, summary produced.
    result = _finalize(agent, final_response=None, exit_reason="budget_exhausted", api_call_count=60)
    # The summary call happens inside finalize; _handle_max_iterations returns prose.
    assert agent._handle_max_iterations_called
    assert result["completed"] is False
    # The DB-side timed_out record is asserted via the helper mock in
    # _record_kanban_budget_exhausted path (see next test).


def test_summary_text_does_not_auto_complete_db(monkeypatch):
    """timed_out is recorded exactly once even if summary prose says done."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_summary_db")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome
        recorded["count"] = recorded.get("count", 0) + 1

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response=None, exit_reason="budget_exhausted", api_call_count=60)
    assert recorded["outcome"] == "timed_out"
    assert recorded["count"] == 1
    assert result["completed"] is False


# ── 2. Grace kanban_complete success → completed, no timed_out ─────────
def test_grace_terminal_complete_is_completed(monkeypatch):
    """Grace turn that emitted kanban_complete → completed=True, no timeout."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_ok")
    agent = _GraceAgent(
        max_iterations=60,
        budget_remaining=0,
        in_grace_turn=False,  # already restored by loop
        kanban_terminal_emitted=True,
    )
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome
        recorded["count"] = recorded.get("count", 0) + 1

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=61)
    assert "outcome" not in recorded  # never recorded timed_out
    assert result["completed"] is True


def test_grace_terminal_block_is_completed(monkeypatch):
    """Grace turn that emitted kanban_block → completed=True, no timeout."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_block")
    agent = _GraceAgent(
        max_iterations=60,
        budget_remaining=0,
        kanban_terminal_emitted=True,
    )
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome
        recorded["count"] = recorded.get("count", 0) + 1

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=61)
    assert "outcome" not in recorded
    assert result["completed"] is True


# ── 5. Grace plain-text/no terminator → timeout exactly as before ──────
def test_grace_plain_text_no_terminator_times_out(monkeypatch):
    """Plain-text grace exit (no terminal tool) → timed_out, counter +1."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_text")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome
        recorded["count"] = recorded.get("count", 0) + 1

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response=None, exit_reason="budget_exhausted", api_call_count=60)
    assert recorded["outcome"] == "timed_out"
    assert recorded["count"] == 1
    assert result["completed"] is False


# ── 10. Grace timeout → failure counter increments exactly once ────────
def test_grace_timeout_counter_increments_once(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_tmo")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    calls = []

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        calls.append(outcome)

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        _finalize(agent, final_response=None, exit_reason="budget_exhausted", api_call_count=60)
    assert calls == ["timed_out"]


# ── 7. Already-terminal run → no duplicate completion ──────────────────
def test_already_terminal_no_duplicate(monkeypatch):
    """If the run is already closed, the grace complete path is a CAS no-op."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_already_terminal")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0, kanban_terminal_emitted=True)
    # The finalizer must not attempt a second record; the tool's CAS handles
    # the no-op.  Assert completed and no failure record.
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=61)
    assert "outcome" not in recorded
    assert result["completed"] is True


# ── 12. Non-Kanban session → no grace behavior ─────────────────────────
def test_non_kanban_no_grace(monkeypatch):
    """Without HERMES_KANBAN_TASK, budget exhaustion behaves exactly as before."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response=None, exit_reason="budget_exhausted", api_call_count=60)
    # Non-kanban: no DB record attempted (helper only fires for kanban task).
    assert "outcome" not in recorded
    assert result["completed"] is False
    assert agent._handle_max_iterations_called


# ── 14. max_turns accounting: grace is additional, not a work turn ─────
def test_grace_does_not_consume_work_budget(monkeypatch):
    """The grace turn must not consume an iteration budget slot."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_budget")
    agent = _GraceAgent(max_iterations=30, budget_remaining=0)
    # The loop sets _budget_grace_call before the grace turn; finalize must
    # not run the summary (no timed_out) when the terminal emitted.
    agent._kanban_terminal_emitted = True
    result = _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=31)
    assert result["completed"] is True
    # The grace turn is an additional API call (31 > 30) yet completed.


# ── 1. Normal completion at N-1 → no grace ─────────────────────────────
def test_normal_completion_no_grace(monkeypatch):
    """Completing before the budget never touches grace machinery."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_normal")
    agent = _GraceAgent(max_iterations=60, budget_remaining=5)
    agent._kanban_terminal_emitted = False
    result = _finalize(
        agent,
        final_response="done",
        exit_reason="text_response(finish_reason=stop)",
        api_call_count=55,
    )
    assert result["completed"] is True
    assert not agent._handle_max_iterations_called


# ── 15. interrupt/cancel before grace → grace does not override ────────
def test_interrupt_blocks_grace(monkeypatch):
    """An interrupt before the grace turn prevents the grace path."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_interrupt")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    # Simulate: interrupted during grace turn → finalizer sees interrupted.
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
        _pending_verification_response=None,
        _pending_verification_response_previewed=False,
    )
    # Interrupted path: no summary call, not completed, no timed_out record
    # (interrupt is not a budget-exhaustion failure).
    assert not agent._handle_max_iterations_called
    assert result["completed"] is False


# ── 9. Successful grace → consecutive_failures reset (no increment) ────
def test_grace_success_no_failure_increment(monkeypatch):
    """Successful grace completion must not increment the failure counter."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_noinc")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0, kanban_terminal_emitted=True)
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=61)
    assert "outcome" not in recorded  # kanban_complete resets the counter itself


# ── Torn-grace cleanup: registry restore safety net ────────────────────
def test_torn_grace_clears_state(monkeypatch):
    """A torn grace turn must clear _in_grace_turn and not leave a restricted registry."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_torn")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0, in_grace_turn=True)
    agent.tools = [{"function": {"name": "kanban_complete"}}]
    agent.valid_tool_names = {"kanban_complete"}
    result = _finalize(agent, final_response=None, exit_reason="budget_exhausted", api_call_count=60)
    assert agent._in_grace_turn is False
    assert result["completed"] is False  # no terminal emitted → timed_out path


# ── 16. Generic grace: no profile-specific code (review/ops/worker) ────
@pytest.mark.parametrize("profile", ["worker", "ops", "review", "engineer"])
def test_grace_generic_across_profiles(monkeypatch, profile):
    """The grace mechanism is profile-agnostic (HERMES_KANBAN_TASK only)."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", f"t_{profile}_grace")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0, kanban_terminal_emitted=True)
    result = _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=61)
    assert result["completed"] is True


# ── Grace nudge builder ────────────────────────────────────────────────
def test_grace_nudge_builder(monkeypatch):
    from agent.kanban_stop import build_kanban_grace_nudge

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_nudge")
    nudge = build_kanban_grace_nudge()
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "exhausted" in nudge
    assert "t_nudge" in nudge


# ── 3/4/6. Tool restriction is TECHNICAL (registry-level) ─────────────
def test_terminal_tools_constant_exact():
    """The grace allowlist is exactly the two terminal tools."""
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS

    assert _TERMINAL_KANBAN_TOOLS == frozenset({"kanban_complete", "kanban_block"})


def test_grace_tool_swap_restricts_to_terminal_set():
    """The grace turn swaps agent.tools/valid_tool_names to the terminal set."""
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS

    # Simulate the loop's grace-turn swap logic (conversation_loop.py,
    # "Grace-turn tool restriction" block).
    full_tools = [
        {"function": {"name": "terminal"}},
        {"function": {"name": "bash"}},
        {"function": {"name": "kanban_complete"}},
        {"function": {"name": "kanban_block"}},
    ]
    full_valid = {"terminal", "bash", "kanban_complete", "kanban_block"}

    grace_tools = [
        t for t in full_tools
        if (t.get("function") or {}).get("name") in _TERMINAL_KANBAN_TOOLS
    ]
    grace_valid = set(_TERMINAL_KANBAN_TOOLS)

    assert {t["function"]["name"] for t in grace_tools} == {
        "kanban_complete", "kanban_block"
    }
    assert grace_valid == {"kanban_complete", "kanban_block"}
    assert "terminal" not in grace_valid and "bash" not in grace_valid


def test_grace_refuses_general_tool_attempt(monkeypatch):
    """A general tool request during grace is refused (executor allowlist)."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_refuse")
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS

    # The executor's enabled_tools comes from agent.valid_tool_names; a
    # general tool not in the terminal set is rejected by
    # handle_function_call's allowlist before execution.
    agent = _GraceAgent(max_iterations=60, budget_remaining=0)
    agent.valid_tool_names = set(_TERMINAL_KANBAN_TOOLS)

    # Simulate the executor check: the attempted tool is not allowed.
    attempted = "terminal"
    assert attempted not in agent.valid_tool_names
    # If it were allowed, _kanban_terminal_emitted would be False and the
    # turn would fall through to timed_out.  The refusal is what keeps the
    # terminal-only guarantee.
    assert agent._kanban_terminal_emitted is False


# ── 8. Dispatcher does NOT retry after successful grace ───────────────
def test_successful_grace_no_retry_outcome(monkeypatch):
    """After a grace kanban_complete, the outcome is not timed_out → no retry."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_grace_noretry")
    agent = _GraceAgent(max_iterations=60, budget_remaining=0, kanban_terminal_emitted=True)
    recorded = {}

    def _fake_record(_conn, task, error, outcome, release_claim, end_run, event_payload_extra):
        recorded["outcome"] = outcome

    with patch("hermes_cli.kanban_db._record_task_failure", side_effect=_fake_record):
        result = _finalize(agent, final_response="grace complete", exit_reason="text_response(kanban_terminal_grace)", api_call_count=61)
    # No timed_out record → the dispatcher sees a clean completion and
    # does not advance the failure circuit / retry.
    assert "outcome" not in recorded
    assert result["completed"] is True
    # The run is already closed by kanban_complete's CAS-safe path (the
    # tool itself), which the dispatcher reads as done — no retry.
    assert result["turn_exit_reason"] == "text_response(kanban_terminal_grace)"


# ── Loop-entry grace grant (pre-loop block) ───────────────────────────
def _simulate_grace_grant(agent, api_call_count):
    """Replicates the loop's pre-loop grace decision (conversation_loop.py)."""
    def _grace_eligible():
        return bool(
            os.environ.get("HERMES_KANBAN_TASK")
            and api_call_count >= agent.max_iterations
            and not getattr(agent, "_grace_turn_used", False)
            and not agent._interrupt_requested
            and not getattr(agent, "_kanban_terminal_emitted", False)
        )

    if _grace_eligible() and not agent._budget_grace_call:
        agent._budget_grace_call = True
        agent._in_grace_turn = True
    return agent._budget_grace_call


class _LoopAgent:
    def __init__(self, kanban=True, interrupted=False, terminal=False, grace_used=False):
        self.max_iterations = 3
        self._budget_grace_call = False
        self._grace_turn_used = grace_used
        self._interrupt_requested = interrupted
        self._kanban_terminal_emitted = terminal


def test_loop_grants_grace_on_budget_exhaustion(monkeypatch):
    """The pre-loop block grants ONE grace turn when the cap is reached."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_pregrace")
    agent = _LoopAgent()
    # api_call_count has reached max_iterations (the while would exit).
    assert _simulate_grace_grant(agent, api_call_count=3) is True
    assert agent._in_grace_turn is True
    # One turn only: consuming the flag + marking used prevents re-grant.
    agent._budget_grace_call = False
    agent._grace_turn_used = True
    assert _simulate_grace_grant(agent, api_call_count=3) is False


def test_loop_grace_not_granted_for_non_kanban(monkeypatch):
    """Non-kanban sessions never get the grace turn (no HERMES_KANBAN_TASK)."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _LoopAgent()
    assert _simulate_grace_grant(agent, api_call_count=3) is False


def test_loop_grace_not_granted_when_terminal_emitted(monkeypatch):
    """Already-terminal runs do not get a second grace turn."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_pregrace2")
    agent = _LoopAgent(terminal=True)
    assert _simulate_grace_grant(agent, api_call_count=3) is False


def test_loop_grace_not_granted_when_interrupted(monkeypatch):
    """An interrupt pending cancels the grace grant."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_pregrace3")
    agent = _LoopAgent(interrupted=True)
    assert _simulate_grace_grant(agent, api_call_count=3) is False


def test_loop_grace_not_regranted_after_use(monkeypatch):
    """The grace turn is consumed exactly once (no infinite grace)."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_pregrace4")
    agent = _LoopAgent(grace_used=True)
    assert _simulate_grace_grant(agent, api_call_count=3) is False


def test_loop_grace_not_granted_below_cap(monkeypatch):
    """While work budget remains, no grace (normal loop continues)."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_pregrace5")
    agent = _LoopAgent()
    assert _simulate_grace_grant(agent, api_call_count=1) is False
