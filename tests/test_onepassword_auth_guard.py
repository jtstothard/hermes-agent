"""Test the guard for unauthenticated 1Password integration.

This test verifies that when a profile has secrets.onepassword.enabled and op://
mappings but the configured service_account_token_env is absent/empty, a clear
actionable warning is emitted instead of a silent failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import onepassword as op


@pytest.fixture(autouse=True)
def _reset_caches():
    op._reset_cache_for_tests()
    yield
    op._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _clean_op_env(monkeypatch):
    """Start every test from a known 1Password auth state."""
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_ACCOUNT", raising=False)
    yield


def _ok(value: str):
    return mock.Mock(returncode=0, stdout=value, stderr="")


def _err(code: int, stderr: str):
    return mock.Mock(returncode=code, stdout="", stderr=stderr)


def test_guard_warns_when_no_token_and_no_session(monkeypatch, tmp_path):
    """When no service account token or interactive session exists, warn."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("value"))
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    
    # No OP_SERVICE_ACCOUNT_TOKEN set
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    
    # No OP_SESSION_* either
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"OPENAI_API_KEY": "op://Private/OpenAI/api key"},
        binary=fake_op,
        use_cache=False,
    )
    
    # Should get a warning about unauthenticated state
    assert len(warnings) == 1
    assert "1Password is configured" in warnings[0]
    assert "no authentication is available" in warnings[0]
    assert "OP_SERVICE_ACCOUNT_TOKEN" in warnings[0]
    assert "Secrets will not be resolved" in warnings[0]


def test_guard_no_warning_when_token_present(monkeypatch, tmp_path):
    """When token is present, no warning should be emitted."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved"))
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "fake-token")
    
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"OPENAI_API_KEY": "op://Private/OpenAI/api key"},
        binary=fake_op,
        use_cache=False,
    )
    
    # No auth warning when token is present
    auth_warnings = [w for w in warnings if "no authentication is available" in w]
    assert len(auth_warnings) == 0
    # Secret should be resolved
    assert secrets == {"OPENAI_API_KEY": "resolved"}


def test_guard_no_warning_when_session_present(monkeypatch, tmp_path):
    """When interactive session is present, no warning should be emitted."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved"))
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SESSION_myacct", "sess123")
    
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"OPENAI_API_KEY": "op://Private/OpenAI/api key"},
        binary=fake_op,
        use_cache=False,
    )
    
    # No auth warning when session is present
    auth_warnings = [w for w in warnings if "no authentication is available" in w]
    assert len(auth_warnings) == 0
    # Secret should be resolved
    assert secrets == {"OPENAI_API_KEY": "resolved"}


def test_guard_no_warning_when_no_refs(monkeypatch, tmp_path):
    """When there are no op:// refs, no warning needed."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    
    # No token, no session, but also no refs to fetch
    secrets, warnings = op.fetch_onepassword_secrets(
        references={},
        binary=fake_op,
        use_cache=False,
    )
    
    # No warnings at all when there's nothing to fetch
    assert len(warnings) == 0
    assert secrets == {}


def test_guard_warning_uses_custom_token_env(monkeypatch, tmp_path):
    """Warning message references the configured token env var."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("value"))
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    
    # No auth at all
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("MY_CUSTOM_TOKEN", raising=False)
    
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"OPENAI_API_KEY": "op://Private/OpenAI/api key"},
        token_env="MY_CUSTOM_TOKEN",  # Custom token env var
        binary=fake_op,
        use_cache=False,
    )
    
    # Warning should mention the custom token env var
    assert len(warnings) == 1
    assert "MY_CUSTOM_TOKEN" in warnings[0]
    assert "no authentication is available" in warnings[0]


def test_guard_pluralizes_correctly(monkeypatch, tmp_path):
    """Warning message uses correct pluralization."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("value"))
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    
    # No auth
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    
    # Single ref
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"OPENAI_API_KEY": "op://Private/OpenAI/api key"},
        binary=fake_op,
        use_cache=False,
    )
    assert len(warnings) == 1
    assert "1 op:// reference" in warnings[0]
    
    # Multiple refs
    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "OPENAI_API_KEY": "op://Private/OpenAI/api key",
            "ANTHROPIC_API_KEY": "op://Private/Anthropic/credential",
        },
        binary=fake_op,
        use_cache=False,
    )
    assert len(warnings) == 1
    assert "2 op:// references" in warnings[0]


def test_guard_does_not_block_secrets_when_token_present(monkeypatch, tmp_path):
    """Guard does not prevent secret resolution when auth is valid."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved-val"))
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "valid-token")
    
    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "OPENAI_API_KEY": "op://Private/OpenAI/api key",
            "ANTHROPIC_API_KEY": "op://Private/Anthropic/credential",
        },
        binary=fake_op,
        use_cache=False,
    )
    
    # No warnings, secrets resolved
    assert len(warnings) == 0
    assert secrets == {
        "OPENAI_API_KEY": "resolved-val",
        "ANTHROPIC_API_KEY": "resolved-val",
    }