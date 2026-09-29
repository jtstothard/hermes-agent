"""Custom / Ollama (local) provider profile: any endpoint registered as
provider="custom" (Ollama, vLLM, llama.cpp, GLM-5.2 on ARK, …)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from agent.reasoning_effort import OPENAI_COMPAT_WIRE_EFFORTS, clamp_effort
from providers import register_provider
from providers.base import ProviderProfile
from utils import base_url_hostname, base_url_host_matches


def _looks_like_ollama_endpoint(base_url: str | None) -> bool:
    """True only for explicit Ollama signatures (port 11434 or an ``ollama`` host label).
    ``think`` is Ollama-native; strict hosts (Mistral, Groq) 422 on it, and
    arbitrary localhost may be llama.cpp / vLLM / LM Studio."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    try:  # urlparse raises ValueError on malformed ports ("host:99999"); treat as not-Ollama.
        if parsed.port == 11434:
            return True
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and (host == "ollama.com" or host.endswith(".ollama.com") or "ollama" in host.split("."))

logger = logging.getLogger(__name__)

# Shared-ai-proxy host: requests routed through this endpoint get durable
# profile/session/task attribution tags. Local custom endpoints (Ollama,
# vLLM, llama.cpp, GLM/ARK) are deliberately NOT tagged — the tags are only
# meaningful to the shared proxy's LiteLLM request_tags/metadata jsonb.
_SHARED_PROXY_HOST = "192.168.10.252"


def _is_shared_proxy_base_url(base_url: Any) -> bool:
    """True when *base_url* points at the shared proxy: the parsed canonical
    HOSTNAME equals the proxy host exactly. Substring matching is not a host
    check — ``http://192.168.10.252.evil.example/v1`` and
    ``http://evil.example/192.168.10.252/v1`` contain the string but are not
    the proxy — so the URL is parsed like every other provider host gate
    (``utils.base_url_hostname``). Unparseable/absent hosts never match."""
    try:
        return base_url_hostname(str(base_url or "")) == _SHARED_PROXY_HOST
    except Exception:
        logger.debug("attribution: base_url host check skipped", exc_info=True)
        return False


def _attribution_tags(*, session_id: str | None = None) -> list[str]:
    """Build low-risk attribution tags for shared-ai-proxy requests.

    Additive-only: never raises, never blocks the request. High-cardinality
    session/task ids go into the tags list (LiteLLM persists them to
    request_tags/metadata jsonb) — they are NOT written to 'user' or
    model_group, so routing/selection is untouched. Identity tags appear only
    when real identity exists: an absent session emits no session tag, and the
    task tag follows the kanban ownership contract (an inherited worker env
    without an owned card produces no task tag).
    """
    tags: list[str] = []
    try:
        from hermes_cli.profiles import current_profile_name
        tags.append(f"profile={current_profile_name(default='default') or 'default'}")
    except Exception:
        logger.debug("attribution: profile tag unavailable", exc_info=True)
    if session_id:
        tags.append(f"session={session_id}")
    try:
        from agent.delegation_context import owned_kanban_task
        task_id = owned_kanban_task()
    except Exception:
        logger.debug("attribution: task tag unavailable", exc_info=True)
        task_id = ""
    if task_id:
        tags.append(f"task={task_id}")
    return tags


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...]:
        """The OpenAI-compat wire set, mirroring this profile's own chat-completions clamp.

        Without this declaration the Responses transport clamps onto the OpenAI
        per-model ladder (``codex_supported_efforts``), where ``max`` is gpt-5.6-only —
        so a custom relay's model had a configured ``max`` silently demoted to
        ``xhigh`` while the same provider over chat-completions forwarded ``max``
        unchanged (#114249). A custom endpoint's vocabulary is undiscoverable, so
        the widest OpenAI-compat set is the honest ceiling; ``ultra`` still clamps
        to ``max`` via the shared ``clamp_effort`` policy.
        """
        return OPENAI_COMPAT_WIRE_EFFORTS

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        """Unset ``agent.reasoning_effort`` → ``medium``, as on the Nous / OpenRouter profiles.

        Leaving the field off lets the endpoint's own default apply, and for a hosted reasoning
        model that default can be its ceiling: kimi-k3 behind an OpenAI-compatible relay defaults
        to ``max`` — 3x the reasoning tokens and ~3x the latency of medium. The agent skips this
        default for models the catalog marks non-reasoning (``agent.reasoning_params``).
        """
        return {"enabled": True, "effort": "medium"}

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, ollama_num_ctx: int | None = None, **ctx: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Attribution tags for shared-ai-proxy-routed requests only. Gated on
        # the base_url's parsed hostname (exact match — substring checks admit
        # deceptive non-proxy URLs) so local custom endpoints stay untagged
        # and routing/selection is never altered.
        try:
            if _is_shared_proxy_base_url(ctx.get("base_url")):
                _tags = _attribution_tags(session_id=ctx.get("session_id"))
                if _tags:
                    existing = extra_body.get("tags")
                    if isinstance(existing, list):
                        extra_body["tags"] = existing + _tags
                    else:
                        extra_body["tags"] = _tags
        except Exception:
            logger.debug("attribution: tag injection skipped", exc_info=True)

        # Ollama context window
        if ollama_num_ctx:
            extra_body["options"] = {"num_ctx": ollama_num_ctx}
        # disabled -> top-level reasoning_effort="none" (Ollama's /v1 ignores
        # extra_body.think) plus think=False only on Ollama URLs; enabled+effort ->
        # top-level reasoning_effort clamped to the OpenAI-compat wire (GLM/ARK,
        # vLLM and SGLang all top out at "max"; "ultra" verbatim 400s); None ->
        # omit so the server default applies (auxiliary calls without an effort, and
        # the main loop after the route rejected the reasoning field — an unset main
        # effort arrives here already filled by default_reasoning_config). Never emit
        # think=True (Ollama-only flag).
        if reasoning_config and isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            if effort == "none" or reasoning_config.get("enabled", True) is False:
                # See #14820.
                top_level["reasoning_effort"] = "none"
                if _looks_like_ollama_endpoint(ctx.get("base_url")):
                    extra_body["think"] = False
            elif effort and base_url_host_matches(str(ctx.get("base_url") or ""), "api.groq.com"):
                # Groq's OpenAI-compatible wire accepts top-level reasoning_effort only as
                # "none" / "default"; any graded level ("medium", "high") 400s (#75089).
                top_level["reasoning_effort"] = "default"
            elif effort:
                top_level["reasoning_effort"] = clamp_effort(effort, OPENAI_COMPAT_WIRE_EFFORTS)
        return extra_body, top_level

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """base_url is user-configured; fetch only if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom", aliases=("ollama", "local", "vllm", "llamacpp", "llama.cpp", "llama-cpp"),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # An arbitrary client ceiling can exceed a local server's actual output limit.
    # The endpoint owns its generation default.
)

register_provider(custom)
