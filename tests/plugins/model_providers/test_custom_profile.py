"""Unit tests for the custom provider profile's reasoning wiring.

``provider=custom`` covers any OpenAI-compatible endpoint the user points
Hermes at — local Ollama, vLLM, llama.cpp, and hosted reasoning APIs like
GLM-5.2 on Volcengine ARK. Before #57601's salvage, ``CustomProfile`` emitted
nothing when reasoning was *enabled*, so a configured ``reasoning_effort``
was silently dropped for every custom endpoint.

These tests pin the wire-shape contract:
    - disabled on Ollama  → extra_body.think = False + reasoning_effort=none
    - disabled elsewhere  → reasoning_effort=none, no think (strict APIs 422)
    - enabled + effort    → top-level reasoning_effort (native OpenAI-compat
                          format GLM/ARK expect), passed through verbatim
                          including ``max``/``xhigh``
    - enabled + no effort → nothing emitted (endpoint's server default applies)
    - ollama_num_ctx      → extra_body.options.num_ctx, orthogonal to reasoning

The attribution tests pin the shared-ai-proxy request-tag contract:
    - gate            → parsed canonical hostname equality only (deceptive
                        URLs that merely CONTAIN the proxy IP never tag)
    - tags            → exactly profile=<runtime-owner profile>,
                        session=<id> when a session exists, task=<id> only
                        for an OWNED kanban task — complete ordered list,
                        no sentinels, no extras
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def custom_profile():
    """Resolve the registered custom profile via the global registry.

    Importing ``model_tools`` triggers plugin discovery, which registers the
    ``custom`` profile. Going through ``get_provider_profile`` keeps the test
    honest — if the registered class is ever downgraded to a plain
    ``ProviderProfile``, the assertions below collapse.
    """
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("custom")
    assert profile is not None, "custom provider profile must be registered"
    return profile


class TestCustomReasoningWireShape:
    """``build_api_kwargs_extras`` produces the correct wire format."""

    def test_no_reasoning_config_emits_nothing(self, custom_profile):
        """Unset reasoning → omit everything so the endpoint's default applies."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5.2"
        )
        assert eb == {}
        assert tl == {}

    def test_disabled_sends_think_false(self, custom_profile):
        """enabled=False on an Ollama URL → reasoning_effort='none' + think=False.

        Both fields are required on Ollama: /v1/chat/completions silently
        ignores extra_body.think (only /api/chat honours it — ollama#14820)
        but respects top-level reasoning_effort (#25758). think=False stays
        for proxies and the native /api/chat path.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url="http://127.0.0.1:11434/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_effort_none_sends_think_false(self, custom_profile):
        """effort='none' is the disable alias → same dual emission on Ollama."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="qwen3",
            base_url="http://localhost:11434/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_omits_think_on_mistral(self, custom_profile):
        """Strict OpenAI-compat hosts forbid extra ``think`` (HTTP 422)."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="mistral-small-latest",
            base_url="https://api.mistral.ai/v1",
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_omits_think_without_base_url(self, custom_profile):
        """Unknown custom endpoint — do not send the Ollama-only flag."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5.2"
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://127.0.0.1:8080/v1",
            "http://localhost:1234/v1",
            "https://api.groq.com/openai/v1",
        ],
    )
    def test_disabled_omits_think_on_non_ollama_relays(self, custom_profile, base_url):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"effort": "none"},
            model="llama3",
            base_url=base_url,
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_sends_think_false_on_ollama_cloud_host(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url="https://ollama.com/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://myhost:99999/v1",  # out-of-range port: OpenAI client accepts it
            "http://localhost:80a/v1",  # non-integer port
            "http://localhost:11434./v1",  # trailing-dot port
        ],
    )
    def test_malformed_port_does_not_raise(self, custom_profile, base_url):
        """Malformed ports must not raise — urlparse's ``port`` is ValueError-happy.

        The OpenAI client accepts ``http://myhost:99999/v1`` at construction
        (only httpx fails later), so these URLs reach ``build_api_kwargs_extras``
        in production. The heuristic must treat them as non-Ollama rather than
        killing the kwargs build.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url=base_url,
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_enabled_effort_goes_top_level(self, custom_profile, effort):
        """enabled + effort → TOP-LEVEL reasoning_effort, passed through verbatim.

        GLM-5.2/ARK and OpenAI-compatible reasoning APIs read reasoning_effort
        as a top-level string, not nested in extra_body. ``max`` is GLM's
        native deep-reasoning level and must survive.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}, model="glm-5.2"
        )
        assert tl == {"reasoning_effort": effort}
        assert "reasoning_effort" not in eb
        assert "think" not in eb


    def test_does_not_force_think_true_on_enable(self, custom_profile):
        """We must never send think=True on enable — it's Ollama-only and
        would 400 on GLM/vLLM endpoints that don't recognize it."""
        eb, _ = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}, model="glm-5.2"
        )
        assert eb.get("think") is not True

    @pytest.mark.parametrize(
        "reasoning_config, expected",
        [({"enabled": True, "effort": "high"}, "default"), ({"enabled": False, "effort": "medium"}, "none")],
    )
    def test_groq_host_clamps_effort_to_groq_vocabulary(self, custom_profile, reasoning_config, expected):
        """api.groq.com accepts top-level reasoning_effort only as 'none' / 'default' (#75089).

        Drives the main transport so the clamp is proven where production reads it.
        """
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="qwen/qwen3.6-27b", messages=[{"role": "user", "content": "ping"}], tools=None,
            provider_profile=custom_profile, reasoning_config=reasoning_config,
            base_url="https://api.groq.com/openai/v1", provider_name="custom",
        )
        assert kwargs["reasoning_effort"] == expected
        assert "think" not in kwargs.get("extra_body", {}) and "reasoning" not in kwargs.get("extra_body", {})


class TestCustomReasoningWithNumCtx:
    """Ollama num_ctx and reasoning are independent and compose."""

    def test_num_ctx_alone(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, ollama_num_ctx=8192, model="qwen3"
        )
        assert eb == {"options": {"num_ctx": 8192}}
        assert tl == {}


class TestCustomResponsesEffortVocabulary:
    """The Responses transport honours a custom endpoint's configured ``max`` (#114249).

    A custom relay's models are not on the OpenAI per-model ladder; without the
    profile declaration the Responses transport clamped a configured ``max`` to
    ``xhigh`` while chat-completions on the same provider forwarded ``max``.
    """

    def test_named_relay_keeps_configured_max_where_chat_completions_does(self, custom_profile):
        from agent.transports.codex import _resolve_reasoning

        reasoning = {"enabled": True, "effort": "max"}
        _, top_level = custom_profile.build_api_kwargs_extras(
            reasoning_config=reasoning, base_url="https://relay.example/v1"
        )
        effort, enabled = _resolve_reasoning(
            "deepseek-flash",
            {"provider": "custom:relay", "base_url": "https://relay.example/v1", "reasoning_config": reasoning},
        )
        assert (effort, enabled) == (top_level["reasoning_effort"], True) == ("max", True)

    def test_custom_entry_at_the_official_openai_origin_keeps_the_per_model_ladder(self):
        """api.openai.com's vocabulary is known per model; the custom declaration must not
        put ``max`` on the wire for a slug OpenAI rejects it on."""
        from agent.transports.codex import _resolve_reasoning

        effort, enabled = _resolve_reasoning(
            "gpt-5.2",
            {
                "provider": "custom:oai",
                "base_url": "https://api.openai.com/v1",
                "reasoning_config": {"enabled": True, "effort": "max"},
            },
        )
        assert (effort, enabled) == ("xhigh", True)


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    """No launch pins or worker lineage in the parent shell: each test sets
    exactly the identity inputs it is proving."""
    for var in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_profiles_root(tmp_path, monkeypatch):
    """Profiles under the env-derived default home so ``get_active_profile_name`` —
    whose profiles root is anchored to that home, NOT to the context-local override —
    can name a profile bound via ``set_hermes_home_override``. Points ``HERMES_HOME``
    at a dedicated fake root (not a profile dir of the sandbox test home), so the
    canonicalization ``<root>/profiles/<name>`` is stable; monkeypatch restores env
    and ``get_default_hermes_root``'s env-keyed memo is reset below."""
    root = tmp_path / "hermes-root"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_constants import _default_hermes_root_memo
    monkeypatch.setattr("hermes_constants._default_hermes_root_memo", None)
    return root


def _extras_for(custom_profile, **overrides):
    """Call ``build_api_kwargs_extras`` against the shared proxy with the
    common shape; keyword *overrides* replace the defaults."""
    kwargs = dict(reasoning_config=None, model="infra-debug",
                  base_url="http://192.168.10.252:8318/v1", session_id="sess-xyz")
    kwargs.update(overrides)
    return custom_profile.build_api_kwargs_extras(**kwargs)


class TestSharedProxyGate:
    """The gate is the base_url's parsed canonical HOSTNAME, exactly.

    Substring matching admits deceptive URLs (host-suffix, path, userinfo
    tricks and neighboring IPs); the contract is: tag the proxy host, never
    anything that merely contains its string.
    """

    @pytest.mark.parametrize(
        ("base_url", "tagged"),
        [
            # The proxy itself, in the shapes production sends it.
            ("http://192.168.10.252:8318/v1", True),
            ("http://192.168.10.252/v1", True),          # default port
            ("192.168.10.252", True),                    # bare host (utils parse tolerance)
            ("HTTP://192.168.10.252:8318/V1", True),     # case canonicalization
            # Deceptive: host SUFFIX containing the proxy IP.
            ("http://192.168.10.252.evil.example/v1", False),
            # Deceptive: proxy IP in the PATH, not the host.
            ("http://evil.example/192.168.10.252/v1", False),
            # Deceptive: proxy IP as userinfo; real host is evil.example.
            ("http://192.168.10.252@evil.example/v1", False),
            # Deceptive: userinfo+host double-@ trick.
            ("http://192.168.10.252:8318@evil.example/v1", False),
            # Neighboring IP on the same /24.
            ("http://192.168.10.251:8318/v1", False),
            ("http://10.0.0.252:8318/v1", False),
            # Lookalike host with dashes instead of dots.
            ("http://192-168-10-252.example/v1", False),
            # No URL at all.
            ("", False),
            (None, False),
        ],
    )
    def test_gate_tags_proxy_host_only(self, custom_profile, monkeypatch, base_url, tagged):
        """Tagged rows emit exactly the default-profile tag list; every
        non-proxy row (deceptive or not) emits NO ``tags`` key at all.

        The profile pin makes the expected list independent of the sandbox
        ``HERMES_HOME`` the test session runs under."""
        monkeypatch.setenv("HERMES_PROFILE", "default")
        eb, tl = _extras_for(custom_profile, base_url=base_url)
        if tagged:
            assert eb == {"tags": ["profile=default", "session=sess-xyz"]}
        else:
            assert eb == {}
        assert tl == {}  # the gate never touches top-level kwargs


class TestAttributionTagConstruction:
    """Exact, ordered, ownership-aware tag construction."""

    def test_full_tag_list_exact_order(self, custom_profile, fake_profiles_root, monkeypatch):
        """Owning dispatcher worker: complete ordered list, nothing else."""
        monkeypatch.setenv("HERMES_PROFILE", "custom")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
        eb, tl = _extras_for(custom_profile)
        assert eb == {"tags": ["profile=custom", "session=sess-xyz", "task=t_abc123"]}
        assert tl == {}

    def test_profile_from_launch_pin_when_no_override(self, custom_profile, fake_profiles_root, monkeypatch):
        """Without a bound override the launcher pin (HERMES_PROFILE) is the
        runtime identity — the dispatcher pins it on its workers."""
        monkeypatch.setenv("HERMES_PROFILE", "dispatch-worker")
        eb, _ = _extras_for(custom_profile)
        assert eb == {"tags": ["profile=dispatch-worker", "session=sess-xyz"]}

    def test_multiplexed_profile_scoping_emits_bound_profile(
        self, custom_profile, fake_profiles_root, monkeypatch
    ):
        """A→B→A multiplex proof: per-turn HERMES_HOME overrides bind the tag
        source. Jarvis-bound traffic emits exactly profile=jarvis, engineer
        traffic exactly profile=engineer, and after release the launch pin
        applies again. The launch pin must NOT re-label a bound turn.

        The fake profiles live under ``<HERMES_HOME>/profiles`` — the root
        ``get_active_profile_name`` reads (see ``fake_profiles_root``) — while
        the *binding* is the context-local override, exactly how multiplex
        binds a turn in production."""
        from hermes_constants import get_hermes_home_override, reset_hermes_home_override, set_hermes_home_override

        monkeypatch.setenv("HERMES_PROFILE", "launch-host")

        def tags_under(profile_home):
            token = set_hermes_home_override(profile_home)
            try:
                eb, _ = _extras_for(custom_profile)
                return eb["tags"]
            finally:
                reset_hermes_home_override(token)

        jarvis = fake_profiles_root / "profiles" / "jarvis"
        engineer = fake_profiles_root / "profiles" / "engineer"
        jarvis.mkdir()
        engineer.mkdir()
        assert tags_under(jarvis) == ["profile=jarvis", "session=sess-xyz"]
        assert tags_under(engineer) == ["profile=engineer", "session=sess-xyz"]
        # Released (override fully unbound, A again): the launcher pin is honored.
        assert get_hermes_home_override() is None
        assert tags_under(None) == ["profile=launch-host", "session=sess-xyz"]
        assert get_hermes_home_override() is None

    @pytest.mark.parametrize("non_owner", ["non_dispatcher_owned_context", "delegated_child"])
    def test_inherited_task_env_without_ownership_not_tagged(
        self, custom_profile, fake_profiles_root, monkeypatch, non_owner
    ):
        """Inherited worker env does not confer ownership: cron executions and
        delegate children run with HERMES_KANBAN_TASK in os.environ but own no
        card — no task tag. The env var itself is never mutated."""
        import os

        from agent import delegation_context

        monkeypatch.setenv("HERMES_PROFILE", "custom")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned_by_dispatcher")
        if non_owner == "non_dispatcher_owned_context":
            ctx = delegation_context.non_dispatcher_owned_context()
        else:
            ctx = delegation_context.delegated_child_context(session_id=None)
        with ctx:
            eb, _ = _extras_for(custom_profile)
            assert eb == {"tags": ["profile=custom", "session=sess-xyz"]}
        assert os.environ["HERMES_KANBAN_TASK"] == "t_owned_by_dispatcher"

    @pytest.mark.parametrize("absent_session", [None, ""])
    def test_absent_session_omits_tag_entirely(self, custom_profile, fake_profiles_root, monkeypatch, absent_session):
        """No session, no session tag — never a ``session=none`` sentinel."""
        monkeypatch.setenv("HERMES_PROFILE", "custom")
        eb, _ = _extras_for(custom_profile, session_id=absent_session)
        assert eb == {"tags": ["profile=custom"]}
        assert not any(t.startswith("session=") for t in eb["tags"])

    def test_no_duplicate_or_extra_tags_with_reasoning(self, custom_profile, fake_profiles_root, monkeypatch):
        """Tags compose with the reasoning wire shape and stay exactly three,
        unique, in order."""
        monkeypatch.setenv("HERMES_PROFILE", "custom")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dup")
        eb, tl = _extras_for(
            custom_profile, reasoning_config={"enabled": True, "effort": "high"}, model="glm-5.2"
        )
        tags = eb["tags"]
        assert tags == ["profile=custom", "session=sess-xyz", "task=t_dup"]
        assert len(tags) == len(set(tags))
        assert tl == {"reasoning_effort": "high"}
