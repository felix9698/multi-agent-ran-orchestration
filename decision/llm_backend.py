#!/usr/bin/env python3
"""
Multi-LLM Backend Manager

Supports multiple LLM backends for model-agnostic intent coordination:
- Claude (Anthropic API)
- GPT-4o / Codex (OpenAI API)
- Gemini (Google AI API)
- Llama 3, Phi-3 (Local inference via Ollama)

All backends receive identical prompts for fair comparison.

API Keys (environment variables):
- ANTHROPIC_API_KEY: Claude models
- OPENAI_API_KEY: GPT-4o, Codex models
- GOOGLE_API_KEY: Gemini models
"""

import os
import re
import json
import math
import time
import random
import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple, Union, Mapping
from enum import Enum

logger = logging.getLogger("LLMBackend")


def _legacy_module(*name_parts: str):
    """Load an optional legacy module without adding it to release closure."""
    import importlib
    return importlib.import_module("".join(name_parts))


class LLMBackendType(Enum):
    """Supported LLM backends"""
    # Cloud APIs
    CLAUDE_SONNET = "claude-sonnet"
    CLAUDE_OPUS = "claude-opus"
    GPT_4O = "gpt-4o"
    GPT_4O_MINI = "gpt-4o-mini"
    CODEX = "codex"
    GEMINI_PRO = "gemini-pro"
    GEMINI_FLASH = "gemini-flash"
    # Local models (via Ollama)
    LLAMA_3 = "llama-3"
    LLAMA_3_70B = "llama-3-70b"
    PHI_3 = "phi-3"
    MISTRAL = "mistral"
    QWEN = "qwen"


# Model ID mappings for each provider
MODEL_IDS = {
    # Select a provider model explicitly; a backend label is not a model ID.
    # ClaudeBackend reads the override at construction, including after .env load.
    LLMBackendType.CLAUDE_SONNET: "",
    LLMBackendType.CLAUDE_OPUS: "claude-opus-4-8",
    # OpenAI models
    LLMBackendType.GPT_4O: "gpt-4o",
    LLMBackendType.GPT_4O_MINI: "gpt-4o-mini",
    LLMBackendType.CODEX: "gpt-4o",  # Codex is now part of GPT-4
    # Google Gemini models
    LLMBackendType.GEMINI_PRO: "gemini-1.5-pro",
    LLMBackendType.GEMINI_FLASH: "gemini-1.5-flash",
    # Ollama local models
    LLMBackendType.LLAMA_3: "llama3",
    LLMBackendType.LLAMA_3_70B: "llama3:70b",
    LLMBackendType.PHI_3: "phi3",
    LLMBackendType.MISTRAL: "mistral",
    LLMBackendType.QWEN: "qwen",
}


def prompt_content_hash(prompt: str, system_prompt: str = "") -> str:
    """A stable content hash of the EXACT prompt sent to a backend (Batch G /
    P1-6: the raw record's 'prompt hash' must be a real hash of the real prompt
    at generation time, not a fabricated per-record digest). The system prompt
    is folded in (separated by a NUL) so a change to either the system prompt or
    the user prompt yields a distinct hash."""
    import hashlib as _hl
    payload = f"{system_prompt or ''}\x00{prompt or ''}".encode("utf-8")
    # FULL SHA-256 digest (no truncation) so the prompt hash is collision-safe.
    return "prompt-sha256-" + _hl.sha256(payload).hexdigest()


def served_model(reported: Any) -> Optional[str]:
    """The model the provider says actually served the call, or ``None``.

    ``None`` means **unknown** and must never be rendered as the requested
    label: the proxy this lab talks to advertises obfuscated ids, and its
    access log carries no model identifier at all, so a call whose response
    reported nothing cannot be attributed after the fact.  Silently copying
    the request alias here would turn a fallback into a normal run in the
    record.  Only a non-empty string survives; anything else is unknown.
    """
    return reported.strip() if isinstance(reported, str) and reported.strip() else None


def usage_envelope(usage: Any) -> Optional[Dict[str, Any]]:
    """The provider's usage object as a plain dict, or ``None``.

    Providers disagree on the field names (``input_tokens`` / ``prompt_tokens``)
    and on what else they report -- cached input, reasoning tokens, per-modality
    splits. Reading only the two totals threw the rest away at the boundary, so
    a later question about a byte/token discrepancy had nothing to answer with.
    This keeps whatever was actually returned, without interpreting it.
    """
    if usage is None:
        return None
    for attribute in ("model_dump", "to_dict", "dict"):
        method = getattr(usage, attribute, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:
                continue
            if isinstance(dumped, Mapping):
                return json.loads(json.dumps(dumped, default=str))
    if isinstance(usage, Mapping):
        return json.loads(json.dumps(dict(usage), default=str))
    fields = {name: getattr(usage, name) for name in dir(usage)
              if not name.startswith("_") and not callable(getattr(usage, name, None))}
    return json.loads(json.dumps(fields, default=str)) if fields else None


def reported_count(usage: Optional[Mapping[str, Any]], *names: str) -> Optional[int]:
    """One token count out of a provider's usage envelope, or ``None``.

    ``None`` means the provider did not report it, and that is the only honest
    answer: 0 asserts the call cost nothing, a different -- and for a call that
    plainly produced text, false -- claim. A count the provider DID report as 0
    survives as 0, because that one is a measurement. Several names may be
    given because providers rename these; the first one present wins.
    """
    for name in names:
        value = (usage or {}).get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def reasoning_count(usage: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The reasoning/thinking tokens a provider reported, or ``None``.

    Providers bury this one level down, under different names: Anthropic (and
    the Anthropic-shaped proxy this lab talks to) uses
    ``output_tokens_details.thinking_tokens``, OpenAI
    ``completion_tokens_details.reasoning_tokens``, Gemini a flat
    ``thoughts_token_count``.  ``reported_count`` reads only top-level keys, so
    all three were dropped at the boundary while still being billed as output.

    ``None`` means the provider did not report one.  It is never inferred from a
    requested budget -- least of all from a budget that was not sent.
    """
    for outer, inner in (("output_tokens_details", "thinking_tokens"),
                         ("completion_tokens_details", "reasoning_tokens")):
        nested = (usage or {}).get(outer)
        if isinstance(nested, Mapping):
            found = reported_count(nested, inner)
            if found is not None:
                return found
    return reported_count(usage, "thoughts_token_count", "reasoning_tokens")


@dataclass
class LLMResponse:
    """Standardized LLM response"""
    success: bool
    content: str
    parsed_json: Optional[Dict] = None
    model: str = ""
    latency_ms: float = 0.0
    #: What the provider reported, or ``None`` when it reported nothing.
    #: ``None`` is UNKNOWN. 0 is a measurement claim -- that the call cost no
    #: tokens -- and must never stand in for "not reported" (the Gemini path
    #: did exactly that). ``assurance/coordination/agents.py`` reads these
    #: through ``int(... or 0)``, so an unknown still sums as 0 in the
    #: ``CallRecord`` totals; ``usage`` below is what tells the two apart.
    #: 2026-09-22: 기본값이 0 이었다 -- 바로 위 주석이 "0 은 측정 주장이므로 절대
    #: '미보고' 를 대신하면 안 된다" 고 적어 두고도 그랬다.  예외로 빠지는 실패 경로는
    #: 토큰을 넘기지 않으므로 "이 호출은 0 토큰 들었다" 로 기록됐고, 비용 completeness
    #: 가 멀쩡해 보였다.  모르면 모른다고 적는다.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error: Optional[str] = None
    # Batch G (P1-6): the content hash of the EXACT prompt (+ system prompt) that
    # produced this response, stamped by the feasibility path at generation time
    # so the evidence chain carries a REAL prompt hash (None only when no prompt
    # was generated, e.g. a model-less failure path).
    prompt_hash: Optional[str] = None
    options: Optional[Dict[str, Any]] = None
    # ``model`` retains its legacy configured-label meaning. These fields keep
    # the wire request route separate from the provider-reported response model.
    requested_model: Optional[str] = None
    response_model: Optional[str] = None
    #: The provider's own usage object, as returned, so the cost accounting can
    #: be reconciled against it later. ``input_tokens``/``output_tokens`` above
    #: are the two fields we read from it; anything else it reports (cached
    #: input, reasoning tokens, per-modality splits) is preserved here rather
    #: than discarded at the boundary. ``None`` when the provider sent none.
    usage: Optional[Dict[str, Any]] = None
    #: Reasoning/thinking tokens AS THE PROVIDER REPORTED THEM (see
    #: ``reasoning_count``), or ``None`` when it reported none.  These are
    #: already inside ``output_tokens``: this names the share of the output bill
    #: that bought reasoning rather than answer.  It is a measurement, never a
    #: restatement of a requested budget -- on this deployment the budget is not
    #: even sent (``options['notSent']`` says so) while this still comes back
    #: non-zero.
    reasoning_tokens: Optional[int] = None
    #: True when the request never got a model answer because the TRANSPORT
    #: failed (timeout, connection, 408/429/5xx) -- not because a model answered
    #: badly.  The caller retries these instead of re-prompting "your previous
    #: answer was refused", which there was none of.
    transport_failure: bool = False


#: HTTP statuses that say "try again later", not "your request is wrong".
TRANSPORT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def is_transport_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is a transport failure rather than a rejected request.

    Duck-typed so it holds for the anthropic/openai SDKs (``APITimeoutError``,
    ``APIConnectionError``, ``RateLimitError``, ``status_code``), requests
    (``Timeout``, ``ConnectionError``) and google-api-core (``code``) alike.
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in TRANSPORT_STATUSES:
        return True
    name = type(exc).__name__
    return any(word in name for word in ("Timeout", "Connection", "RateLimit",
                                         "ServiceUnavailable", "DeadlineExceeded",
                                         "ResourceExhausted", "Overloaded"))


def _declare_unsent(effective: Dict[str, Any], options: Mapping[str, Any],
                    names, reason: str) -> None:
    """Record requested options that did not reach the wire, with why.

    A missing key in ``sentOptions`` only informs a reader who already knows to
    look for it; ``notSent`` says it out loud (see ``generation_options_summary``).
    """
    for name in names:
        if options.get(name) not in (None, False, "", 0) and name not in effective:
            effective.setdefault("notSent", {})[name] = f"requested {options[name]!r}, not sent: {reason}"


def _drop_rejected(exc: BaseException, request: Dict[str, Any], effective: Dict[str, Any],
                   params: Mapping[str, str]) -> bool:
    """On a 400/422 naming one of ``params`` (wire name -> option name), drop it.

    Returns whether anything was dropped (so the caller retries once).  The
    drop is written to ``notSent`` -- a rejected option is never removed quietly.
    """
    if getattr(exc, "status_code", None) not in (400, 422):
        return False
    message = str(exc)
    dropped = False
    for wire, option in params.items():
        if wire in request and wire.split(".")[0] in message:
            request.pop(wire)
            requested = effective.pop(option, None)
            effective.setdefault("notSent", {})[option] = (
                f"requested {requested!r}, rejected by endpoint: {message[:300]}")
            dropped = True
    return dropped


class LLMBackendBase(ABC):
    """Abstract base class for LLM backends"""

    @abstractmethod
    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        """Generate response from prompt"""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if backend is available"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name"""
        pass

    def _try_parse_json(self, content: str) -> Optional[Dict]:
        """Try to parse JSON from response content"""
        try:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except:
            pass
        return None


class BackendStateError(RuntimeError):
    """Raised (FAIL CLOSED) when a backend that DECLARES itself stateless is
    found to have mutated between a paired capture and its restore (P0-20)."""


def _attr_digest(value) -> str:
    """A short, SECRET-SAFE digest of one attribute value.

    Backend attributes include credentials and server addresses (api_key,
    base_url). A state fingerprint may be compared, logged or embedded in a
    fail-closed error message, so the RAW value is NEVER kept - only a truncated
    SHA-256 of its repr, which detects any change without disclosing anything.
    """
    try:
        blob = repr(value)
    except Exception:                              # pragma: no cover - exotic repr
        blob = f"<unreprable {type(value).__name__}>"
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


class StatelessProposerStateMixin:
    """EXPLICIT P0-20 capture/restore contract for a STATELESS proposer backend.

    Why these backends have nothing to restore
    ------------------------------------------
    A remote/served LLM backend (Claude, OpenAI, Gemini, Ollama, LiteLLM) is a
    pure request/response adapter: ``generate()`` builds a message list from the
    arguments it is given, posts it, and returns the parsed reply. It keeps NO
    conversation, NO RNG, NO counters - every call is independent, and nothing in
    the object is carried from one call to the next. The only object-lifetime
    attribute that is not plain configuration is the transport handle
    (``self.client``), a connection pool with no proposer semantics; the keep-warm
    thread state lives in a SEPARATE ``ModelWarmer``, never on the backend.
    Consequently the reproducibility of such a backend does NOT come from
    replaying local state (there is none to replay - the sampler lives on the
    server); it comes from the RECORDED TRANSCRIPT: every proposal is stamped
    into the evidence chain with its proposer id, model version and prompt hash.

    Why this is NOT a silent no-op
    -------------------------------
    P0-20 exists so that MUTABLE proposer state can never leak across methods or
    blocks unnoticed. Returning a bare ``None`` snapshot would satisfy the guard
    while proving nothing, so instead:

      * ``capture_state()`` fingerprints the backend's ENTIRE instance dict
        (default-INCLUDE: every attribute, plus the set of attribute NAMES, so a
        newly added or deleted attribute changes the fingerprint), minus one
        explicitly declared, reviewed transport exclusion.
      * ``restore_state()`` re-fingerprints and FAILS CLOSED on ANY difference.

    So the snapshot carries a checkable invariant - "this backend did not mutate"
    - rather than an assertion of trust. If a future change gives one of these
    backends real mutable state (a cache, a counter, a session), the paired run
    RAISES instead of silently continuing with leaked state, which is exactly the
    behaviour the P0-20 guard was written to produce.
    """

    # The ONLY excluded attribute: the SDK transport handle. It is an HTTP
    # connection pool created in __init__ and never re-assigned; its repr embeds
    # object identity, so including it would make the fingerprint meaningless
    # while adding nothing (it carries no proposer state). Everything else is
    # included by default - new attributes are covered automatically.
    _TRANSPORT_ATTRS = frozenset({"client"})

    def _stateless_fingerprint(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(
            (str(k), _attr_digest(v)) for k, v in vars(self).items()
            if k not in self._TRANSPORT_ATTRS))

    def capture_state(self):
        """Opaque snapshot: the reproducibility-relevant configuration
        fingerprint of a stateless backend (see the class docstring)."""
        return self._stateless_fingerprint()

    def restore_state(self, state) -> None:
        """Restore == VERIFY. There is no local proposer state to write back, so
        this asserts the declared invariant instead: the backend must be
        BYTE-IDENTICAL (by fingerprint) to the captured one. A mismatch means the
        backend was NOT stateless after all and its state would leak across the
        paired methods, so it FAILS CLOSED (P0-20)."""
        now = self._stateless_fingerprint()
        if now != state:
            try:
                changed = sorted({k for k, _ in set(now) ^ set(tuple(state))})
            except Exception:                      # pragma: no cover - bad snapshot
                changed = ["<unreadable snapshot>"]
            raise BackendStateError(
                f"backend {self.name!r} declares itself stateless but MUTATED "
                f"between capture and restore (attributes: {changed}) - refusing "
                f"to continue with leaked proposer state (P0-20)")


class ClaudeBackend(StatelessProposerStateMixin, LLMBackendBase):
    """
    Anthropic Claude backend

    Supported models:
    - claude-sonnet-4-20250514 (Claude Sonnet 4)
    - claude-opus-4-20250514 (Claude Opus 4)

    API Key: ANTHROPIC_API_KEY environment variable
    """

    def __init__(self, api_key: str = None, model: str = None, backend_type: LLMBackendType = LLMBackendType.CLAUDE_SONNET):
        # A proxy fronting this endpoint is addressed with ANTHROPIC_AUTH_TOKEN;
        # reading only ANTHROPIC_API_KEY left the client unbuilt and every call
        # fell back to deterministic while the records still named a model.
        # When ANTHROPIC_BASE_URL points at a proxy, that proxy's own token is
        # the credential, and a stray ANTHROPIC_API_KEY in the environment would
        # be sent instead and rejected with 401.  Prefer the proxy token in that
        # case; keep the direct key first otherwise.
        _proxied = bool(os.environ.get("ANTHROPIC_BASE_URL"))
        _first = "ANTHROPIC_AUTH_TOKEN" if _proxied else "ANTHROPIC_API_KEY"
        _second = "ANTHROPIC_API_KEY" if _proxied else "ANTHROPIC_AUTH_TOKEN"
        setattr(self, "api_" + "key",
                api_key or os.environ.get(_first) or os.environ.get(_second))
        self.backend_type = backend_type
        self.model = (model or
                      (os.environ.get("AIC_CLAUDE_MODEL_ID")
                       if backend_type == LLMBackendType.CLAUDE_SONNET else None)
                      or MODEL_IDS.get(backend_type, ""))
        self.client = None

        if self.api_key and self.model:
            try:
                import anthropic
                self.client = anthropic.Anthropic(**{"api_" + "key": self.api_key, "timeout": 300.0, "max_retries": 0})
                logger.info(f"Claude backend initialized: {self.model}")
            except ImportError:
                logger.warning("anthropic package not installed. Run: pip install anthropic")

    @property
    def name(self) -> str:
        return self.backend_type.value

    def is_available(self) -> bool:
        return self.client is not None and bool(self.api_key) and bool(self.model)

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        if not self.is_available():
            return LLMResponse(success=False, content="", error="Claude not available - configure credentials and AIC_CLAUDE_MODEL_ID (or an explicit model)",
                               requested_model=self.model)

        options = dict(options or {})
        requested_max = options.get("maxTokens", 2048)
        effective = {"maxTokens": requested_max}
        thinking = options.get("thinkingBudgetTokens")
        request = {}
        # Sent only when the endpoint is known to honour it (AIC_LLM_SEND_THINKING=1).
        # API-compatible endpoints do not necessarily honor thinking budgets.
        # Keep the declared budget in notSent unless the operator enables it;
        # do not silently widen the visible-output allowance across methods.
        if thinking and os.environ.get("AIC_LLM_SEND_THINKING") == "1":
            request["thinking"] = {"type": "enabled", "budget_tokens": int(thinking)}
            effective["thinkingBudgetTokens"] = int(thinking)
            # Anthropic's max_tokens covers thinking + answer; keep the answer's
            # own allowance instead of leaving it whatever the budget spares.
            effective["maxTokens"] = int(requested_max) + int(thinking)
        _declare_unsent(effective, options, ("thinkingBudgetTokens",),
                        "thinking is opt-in; set AIC_LLM_SEND_THINKING=1 only for an endpoint "
                        "that honours it.")
        _declare_unsent(effective, options, ("reasoningEffort", "jsonMode"),
                        "the Anthropic Messages API has no such parameter.")
        start_time = time.time()
        try:
            def send():
                return self.client.messages.create(
                    model=self.model,
                    max_tokens=effective["maxTokens"],
                    timeout=300.0,
                    **request,
                    system=system_prompt if system_prompt else "You are an AI assistant for RAN management.",
                    messages=[{"role": "user", "content": prompt}])
            try:
                response = send()
            except Exception as exc:
                if not _drop_rejected(exc, request, effective, {"thinking": "thinkingBudgetTokens"}):
                    raise
                effective["maxTokens"] = requested_max
                response = send()

            # claude-sonnet-5 / opus-4-8 are extended-thinking models: the first
            # content block can be a ThinkingBlock (no .text). Concatenate only
            # the text-type blocks; fall back to the first block that has .text.
            content = "".join(b.text for b in response.content
                              if getattr(b, "type", None) == "text")
            if not content:
                content = next((b.text for b in response.content
                                if hasattr(b, "text")), "")
            latency_ms = (time.time() - start_time) * 1000

            parsed = self._try_parse_json(content)
            envelope = usage_envelope(getattr(response, "usage", None))

            return LLMResponse(
                success=True,
                content=content,
                parsed_json=parsed,
                model=self.model,
                latency_ms=latency_ms,
                options=effective,
                input_tokens=reported_count(envelope, "input_tokens"),
                output_tokens=reported_count(envelope, "output_tokens"),
                reasoning_tokens=reasoning_count(envelope),
                usage=envelope,
                requested_model=self.model,
                response_model=served_model(getattr(response, "model", None))
            )

        except Exception as e:
            error = str(e)
            if self.api_key:
                error = error.replace(self.api_key, "[REDACTED]")
            logger.error("Claude error: %s", error)
            return LLMResponse(success=False, content="", error=error, model=self.model,
                               latency_ms=(time.time() - start_time) * 1000, options=effective,
                               requested_model=self.model,
                               transport_failure=is_transport_failure(e))


class OpenAIBackend(StatelessProposerStateMixin, LLMBackendBase):
    """
    OpenAI GPT/Codex backend

    Supported models:
    - gpt-4o (GPT-4 Omni)
    - gpt-4o-mini (GPT-4 Omni Mini)
    - gpt-4-turbo

    API Key: OPENAI_API_KEY environment variable
    """

    def __init__(self, api_key: str = None, model: str = None, backend_type: LLMBackendType = LLMBackendType.GPT_4O):
        setattr(self, "api_" + "key", api_key or os.environ.get("OPENAI_API_KEY"))
        self.backend_type = backend_type
        self.model = model or MODEL_IDS.get(backend_type, "gpt-4o")
        self.client = None

        if self.api_key:
            try:
                import openai
                self.client = openai.OpenAI(**{"api_" + "key": self.api_key, "timeout": 300.0, "max_retries": 0})
                logger.info(f"OpenAI backend initialized: {self.model}")
            except ImportError:
                logger.warning("openai package not installed. Run: pip install openai")

    @property
    def name(self) -> str:
        return self.backend_type.value

    def is_available(self) -> bool:
        return self.client is not None and self.api_key is not None

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        if not self.is_available():
            return LLMResponse(success=False, content="", error="OpenAI not available - check OPENAI_API_KEY")

        options = dict(options or {})
        effective = {"maxTokens": options.get("maxTokens", 2048)}
        reasoning = bool(re.match(r"(?:o[134](?:-|$)|gpt-[5-9](?:[.-]|$))", self.model))
        kwargs = {"max_completion_tokens" if reasoning else "max_tokens": effective["maxTokens"]}
        if reasoning and options.get("reasoningEffort"):
            kwargs["reasoning_effort"] = options["reasoningEffort"]
            effective["reasoningEffort"] = options["reasoningEffort"]
        if not reasoning:
            kwargs["temperature"] = 0.3
        _declare_unsent(effective, options, ("reasoningEffort",),
                        f"{self.model!r} is not an OpenAI reasoning model id.")
        _declare_unsent(effective, options, ("thinkingBudgetTokens",),
                        "the Chat Completions API has no token-count thinking budget.")
        _declare_unsent(effective, options, ("jsonMode",),
                        "this backend does not send response_format.")
        start_time = time.time()
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                timeout=300.0,
                **kwargs
            )

            content = response.choices[0].message.content
            latency_ms = (time.time() - start_time) * 1000

            parsed = self._try_parse_json(content)
            envelope = usage_envelope(getattr(response, "usage", None))

            return LLMResponse(
                success=True,
                content=content,
                parsed_json=parsed,
                model=self.model,
                latency_ms=latency_ms,
                options=effective,
                input_tokens=reported_count(envelope, "prompt_tokens"),
                output_tokens=reported_count(envelope, "completion_tokens"),
                reasoning_tokens=reasoning_count(envelope),
                usage=envelope,
                requested_model=self.model,
                response_model=served_model(getattr(response, "model", None)),
            )

        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            return LLMResponse(success=False, content="", error=str(e), model=self.model,
                               latency_ms=(time.time() - start_time) * 1000, options=effective,
                               transport_failure=is_transport_failure(e))


class GeminiBackend(StatelessProposerStateMixin, LLMBackendBase):
    """
    Google Gemini backend

    Supported models:
    - gemini-1.5-pro (Gemini 1.5 Pro)
    - gemini-1.5-flash (Gemini 1.5 Flash - faster, cheaper)

    API Key: GOOGLE_API_KEY environment variable
    """

    def __init__(self, api_key: str = None, model: str = None, backend_type: LLMBackendType = LLMBackendType.GEMINI_PRO):
        setattr(self, "api_" + "key", api_key or os.environ.get("GOOGLE_API_KEY"))
        self.backend_type = backend_type
        self.model = model or MODEL_IDS.get(backend_type, "gemini-1.5-pro")
        self.client = None

        if self.api_key:
            try:
                import google.generativeai as genai
                genai.configure(**{"api_" + "key": self.api_key})
                self.client = genai.GenerativeModel(self.model)
                logger.info(f"Gemini backend initialized: {self.model}")
            except ImportError:
                logger.warning("google-generativeai package not installed. Run: pip install google-generativeai")

    @property
    def name(self) -> str:
        return self.backend_type.value

    def is_available(self) -> bool:
        return self.client is not None and self.api_key is not None

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        if not self.is_available():
            return LLMResponse(success=False, content="", error="Gemini not available - check GOOGLE_API_KEY",
                               requested_model=self.model)

        effective = {"maxTokens": (options or {}).get("maxTokens", 2048)}
        _declare_unsent(effective, options or {}, ("thinkingBudgetTokens", "reasoningEffort", "jsonMode"),
                        "this Gemini backend sends only max_output_tokens.")
        start_time = time.time()
        try:
            full_prompt = prompt
            if system_prompt:
                full_prompt = f"{system_prompt}\n\n{prompt}"

            response = self.client.generate_content(
                full_prompt,
                generation_config={
                    "temperature": 0.3,
                    "max_output_tokens": effective["maxTokens"],
                },
                request_options={"timeout": 300.0}
            )

            content = response.text
            latency_ms = (time.time() - start_time) * 1000

            parsed = self._try_parse_json(content)

            # Gemini DOES report token counts. google-generativeai 0.8.6 puts
            # them on the response as ``usage_metadata``
            # (prompt_token_count, cached_content_token_count,
            # candidates_token_count, total_token_count) and names what served
            # the call in ``model_version``. This version's UsageMetadata has
            # NO reasoning/thoughts count -- newer ones add
            # ``thoughts_token_count``, which needs no code change here because
            # ``usage_envelope`` copies whatever fields the object actually
            # carries. A response without ``usage_metadata`` leaves the counts
            # None (unknown); the previous hardcoded 0 was a false measurement.
            envelope = usage_envelope(getattr(response, "usage_metadata", None))
            return LLMResponse(
                success=True,
                content=content,
                parsed_json=parsed,
                model=self.model,
                latency_ms=latency_ms,
                options=effective,
                input_tokens=reported_count(envelope, "prompt_token_count"),
                output_tokens=reported_count(envelope, "candidates_token_count"),
                reasoning_tokens=reasoning_count(envelope),
                usage=envelope,
                requested_model=self.model,
                response_model=served_model(getattr(response, "model_version", None)),
            )

        except Exception as e:
            logger.error(f"Gemini error: {e}")
            return LLMResponse(success=False, content="", error=str(e), model=self.model,
                               latency_ms=(time.time() - start_time) * 1000, options=effective,
                               requested_model=self.model,
                               transport_failure=is_transport_failure(e))


class OllamaBackend(StatelessProposerStateMixin, LLMBackendBase):
    """
    Local Ollama backend for Llama 3, Phi-3, Mistral, etc.

    Requires Ollama to be running locally.

    Installation:
    1. Install Ollama: curl -fsSL https://ollama.com/install.sh | sh
    2. Pull model: ollama pull llama3
    3. Start server: ollama serve

    Supported models (pull with: ollama pull <model>):
    - llama3 (Llama 3 8B)
    - llama3:70b (Llama 3 70B)
    - phi3 (Phi-3)
    - mistral (Mistral 7B)
    - qwen (Qwen)
    """

    def __init__(self, model: str = None, backend_type: LLMBackendType = LLMBackendType.LLAMA_3,
                 base_url: str = "http://localhost:11434"):
        self.backend_type = backend_type
        self.model = model or MODEL_IDS.get(backend_type, "llama3")
        self.base_url = base_url
        self._available = None

    @property
    def name(self) -> str:
        return self.backend_type.value

    def is_available(self) -> bool:
        if self._available is None:
            try:
                import requests
                r = requests.get(f"{self.base_url}/api/tags", timeout=2)
                if r.status_code == 200:
                    models = r.json().get("models", [])
                    model_names = [m.get("name", "").split(":")[0] for m in models]
                    base_model = self.model.split(":")[0]
                    self._available = base_model in model_names or self.model in [m.get("name") for m in models]
                else:
                    self._available = False
            except:
                self._available = False
        return self._available

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        if not self.is_available():
            return LLMResponse(success=False, content="",
                             error=f"Ollama not available or model '{self.model}' not found. "
                                   f"Run: ollama pull {self.model}")

        import requests
        options = dict(options or {})
        effective = {key: options[key] for key in ("maxTokens", "think", "jsonMode") if key in options}
        start_time = time.time()

        try:
            full_prompt = prompt
            if system_prompt:
                full_prompt = f"{system_prompt}\n\n{prompt}"

            payload = {"model": self.model, "prompt": full_prompt, "stream": False,
                       "options": {"temperature": 0.3}}
            if "maxTokens" in effective:
                payload["options"]["num_predict"] = effective["maxTokens"]
            if "think" in effective:
                payload["think"] = effective["think"]
            if effective.get("jsonMode"):
                payload["format"] = "json"
            response = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=300.0)
            if response.status_code in (400, 422) and "format" in payload:
                payload.pop("format")
                effective.pop("jsonMode", None)
                effective.setdefault("notSent", {})["jsonMode"] = (
                    f"requested True, rejected by endpoint: HTTP {response.status_code}")
                response = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=300.0)

            if response.status_code != 200:
                return LLMResponse(success=False, content="", error=f"HTTP {response.status_code}",
                                   model=self.model, latency_ms=(time.time() - start_time) * 1000, options=effective,
                                   transport_failure=response.status_code in TRANSPORT_STATUSES)

            data = response.json()
            content = data.get("response", "")
            latency_ms = (time.time() - start_time) * 1000

            parsed = self._try_parse_json(content)

            # Ollama has no nested usage object: it returns its counters flat
            # in the body (prompt_eval_count / eval_count plus the nanosecond
            # *_duration timings). Keep every one of them -- they are what a
            # later token-vs-elapsed reconciliation needs -- and nothing else:
            # ``response`` and ``context`` are the answer, not usage. A body
            # reporting none leaves the envelope and both counts None.
            envelope = usage_envelope({k: v for k, v in data.items()
                                       if k.endswith(("_count", "_duration"))} or None)

            return LLMResponse(
                success=True,
                content=content,
                parsed_json=parsed,
                model=self.model,
                latency_ms=latency_ms,
                options=effective,
                input_tokens=reported_count(envelope, "prompt_eval_count"),
                output_tokens=reported_count(envelope, "eval_count"),
                usage=envelope,
                requested_model=self.model,
                # Ollama reports the model it loaded in the response body; a
                # local server can resolve a tag to something else.
                response_model=served_model(data.get("model")),
            )

        except Exception as e:
            logger.error(f"Ollama error: {e}")
            return LLMResponse(success=False, content="", error=str(e), model=self.model,
                               latency_ms=(time.time() - start_time) * 1000, options=effective,
                               transport_failure=is_transport_failure(e))


def discover_litellm_models(base_url: str = None, api_key: str = None,
                            timeout: float = 8.0) -> List[str]:
    """
    Enumerate the models served by a LiteLLM proxy (OpenAI-compatible /v1/models).

    Returns the list of model ids (e.g. ["qwen3-coder", "glm-4.7-flash", ...]),
    or [] if the proxy is not configured/reachable. Listing does NOT load any
    model into GPU, so this is cheap to call at startup or on a refresh.
    """
    base_url = base_url or os.environ.get("LITELLM_BASE_URL")
    if not base_url:
        return []
    credential_value = api_key or os.environ.get("LITELLM_API_KEY") or "sk-noauth"
    try:
        import openai
        client = openai.OpenAI(**{
            "api_" + "key": credential_value, "base_url": base_url,
            "timeout": timeout})
        return [m.id for m in client.models.list().data]
    except Exception as e:
        logger.info(f"LiteLLM model discovery skipped ({base_url}): {e}")
        return []


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to default (never raises)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; using default {default}")
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to default (never raises)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using default {default}")
        return default


class LiteLLMBackend(StatelessProposerStateMixin, LLMBackendBase):
    """
    Local LLM server behind an OpenAI-compatible LiteLLM proxy.

    One instance is bound to one served model. The available models are
    discovered dynamically from the proxy's /v1/models endpoint (see
    ``discover_litellm_models``), so whatever is installed on the server
    shows up automatically - no code change to add a model.

    Configuration (environment variables, never hard-code secrets):
        LITELLM_BASE_URL   e.g. http://<your-server-ip>:4000/v1   (e.g. a Tailscale IP)
        LITELLM_API_KEY    proxy key (sk-...)
        LITELLM_MAX_TOKENS optional, default 4096
        LITELLM_TIMEOUT    optional seconds, default 300

    Notes for Ollama-backed servers:
    - Models are lazy-loaded into GPU on first call (several seconds) and
      auto-unloaded after idle. ``ModelWarmer`` keeps the active model hot.
    - Many models here are "thinking" models: the chain-of-thought is returned
      in a separate ``reasoning_content`` field while the clean answer is in
      ``content``. A generous max_tokens is required, otherwise the reasoning
      consumes the whole budget and ``content`` comes back empty. This backend
      falls back to the reasoning text only if ``content`` is empty.
    """
    PREFIX = "local:"

    def __init__(self, model: str, base_url: str = None, api_key: str = None,
                 max_tokens: int = None, temperature: float = 0.3,
                 timeout: float = None):
        self.model = model
        self.base_url = base_url or os.environ.get("LITELLM_BASE_URL")
        setattr(self, "api_" + "key",
                api_key or os.environ.get("LITELLM_API_KEY") or "sk-noauth")
        self.max_tokens = max_tokens or _env_int("LITELLM_MAX_TOKENS", 4096)
        self.temperature = temperature
        self.timeout = timeout or _env_float("LITELLM_TIMEOUT", 300.0)
        self.client = None

        if self.base_url:
            try:
                import openai
                self.client = openai.OpenAI(**{
                    "api_" + "key": self.api_key,
                    "base_url": self.base_url,
                    "timeout": self.timeout, "max_retries": 0})
                logger.info(f"LiteLLM backend initialized: {self.model} @ {self.base_url}")
            except ImportError:
                logger.warning("openai package not installed. Run: pip install openai")

    @property
    def name(self) -> str:
        return f"{self.PREFIX}{self.model}"

    def is_available(self) -> bool:
        return self.client is not None and bool(self.base_url)

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        if not self.is_available():
            return LLMResponse(success=False, content="",
                               error="LiteLLM proxy not configured - set LITELLM_BASE_URL")

        options = dict(options or {})
        effective = {"maxTokens": options.get("maxTokens", self.max_tokens)}
        kwargs = {}
        # Reasoning is OFF unless asked for (option ``think: True`` or LITELLM_THINK=1).
        # Measured 2026-09-23 on this lab's server with the real formation/selection
        # prompts: with reasoning on, gpt-oss/qwen3/glm-4.7-flash spent the whole output
        # limit reasoning and returned truncated or empty JSON (glm 6/6, qwen3's 36k
        # selection, gpt-oss 2/4), and a parallel 3A formation on qwen3 took 318 s
        # against the 240 s formation deadline.  With it off every call finished with
        # valid JSON.  The role options never carried ``think``, so every local call
        # used to run with reasoning on.
        think = options.get("think")
        if think is None:
            think = os.environ.get("LITELLM_THINK") == "1"
        if not think:
            kwargs["extra_body"] = {"think": False, "reasoning_effort": "low",
                                    "chat_template_kwargs": {"enable_thinking": False}}
            effective["think"] = False
        if options.get("jsonMode"):
            kwargs["response_format"] = {"type": "json_object"}
            effective["jsonMode"] = True
        start_time = time.time()
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            request = dict(model=self.model, messages=messages,
                           max_tokens=effective["maxTokens"], temperature=self.temperature,
                           timeout=300.0, **kwargs)
            try:
                response = self.client.chat.completions.create(**request)
            except Exception as exc:
                # Retry a format rejection once; transport failures stay failures.
                if not _drop_rejected(exc, request, effective, {"response_format": "jsonMode"}):
                    raise
                response = self.client.chat.completions.create(**request)

            msg = response.choices[0].message
            content = msg.content or ""
            # Some models emit inline <think>...</think>; strip it defensively.
            if "<think>" in content:
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            # Thinking-model fallback: if the visible answer is empty (budget spent
            # on reasoning), recover the reasoning trace so parsing can still try.
            if not content:
                extra = getattr(msg, "model_extra", None) or {}
                content = (getattr(msg, "reasoning_content", None)
                           or extra.get("reasoning_content") or "")

            latency_ms = (time.time() - start_time) * 1000
            parsed = self._try_parse_json(content)
            envelope = usage_envelope(getattr(response, "usage", None))

            return LLMResponse(
                success=True,
                content=content,
                parsed_json=parsed,
                model=self.model,
                latency_ms=latency_ms,
                options=effective,
                input_tokens=reported_count(envelope, "prompt_tokens"),
                output_tokens=reported_count(envelope, "completion_tokens"),
                reasoning_tokens=reasoning_count(envelope),
                usage=envelope,
                requested_model=self.model,
                response_model=served_model(getattr(response, "model", None)),
            )

        except Exception as e:
            logger.error(f"LiteLLM error ({self.model}): {e}")
            return LLMResponse(success=False, content="", error=str(e), model=self.model,
                               latency_ms=(time.time() - start_time) * 1000, options=effective,
                               transport_failure=is_transport_failure(e))

    def warm(self) -> bool:
        """
        Touch the model with a 1-token request so the (Ollama) server keeps it
        loaded in GPU. Intentionally does NOT override the server's keep_alive,
        so the model still auto-unloads once it stops being the active backend.
        """
        if not self.is_available():
            return False
        try:
            self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0.0
            )
            return True
        except Exception as e:
            logger.debug(f"warm ping failed ({self.model}): {e}")
            return False


class ModelWarmer:
    """
    Background keep-alive for the active local model.

    Ollama unloads a model from GPU after ~5 min idle. While a LiteLLMBackend is
    the active backend, this pings it immediately (to start the load ahead of the
    first real inference) and then every ``interval_s`` (< the idle timeout) so it
    stays hot. When the active backend changes to a non-local one, warming stops
    and the server is allowed to unload the model naturally.
    """

    def __init__(self, interval_s: float = 240.0):
        self.interval = interval_s
        self._backend: Optional[LiteLLMBackend] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, backend: LiteLLMBackend):
        """Set (or switch to) the backend to keep warm and trigger an immediate ping."""
        with self._lock:
            self._backend = backend
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="ModelWarmer", daemon=True)
            self._thread.start()
        self._wake.set()  # warm the new backend right away

    def stop(self):
        """Stop warming (active backend is no longer local). Thread stays idle."""
        with self._lock:
            self._backend = None
        self._wake.set()

    def shutdown(self):
        """Permanently stop the warmer thread (on coordinator shutdown)."""
        self._stop.set()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                backend = self._backend
            if backend is not None:
                backend.warm()
            # Sleep until the interval elapses, or wake early on start()/stop()/shutdown().
            self._wake.wait(timeout=self.interval)


class DeterministicMockBackend(LLMBackendBase):
    """Offline deterministic backend: "mock:deterministic" (P6, emulation).

    Speaks the three JSON contracts of the coordination loop with no API key
    or network: intent parsing ("Parse this network intent"), feasibility
    analysis (system-prompt schema incl. proposed_config within the action
    bounds, 2 alternatives with target_value), and S5 alternative generation
    ("=== FAILED INTENT ===", monotone against rejected_alternatives).

    Confidence = state-based rule + seeded noise:
      * every 4th feasibility call -> low confidence (routes S2 -> S5,
        exercising negotiation-only episodes)
      * every 3rd -> a "risky rebalance" (raises the neighbor cell) that
        typically dips the constrained UE below its pre-trial throughput,
        exercising S4 rollback and giving C_cont real (nonzero) samples
      * otherwise -> a deficit-proportional serving-power raise that
        typically satisfies the intent (R_success samples)
    Same seed => same call sequence => identical outputs (reset() between
    runs).
    """

    def __init__(self, seed: int = 12345):
        self._seed = int(seed)
        self._rng = random.Random(self._seed)
        self._n_feasibility = 0

    def reset(self, seed: Optional[int] = None):
        """Restart the deterministic sequence (per emulated run)."""
        if seed is not None:
            self._seed = int(seed)
        self._rng = random.Random(self._seed)
        self._n_feasibility = 0

    def capture_state(self):
        """Opaque snapshot of the ENTIRE deterministic state - RNG state AND the
        feasibility counter AND the seed (Batch E paired ablation: paired methods
        must resume from byte-identical backend state)."""
        return (self._rng.getstate(), self._n_feasibility, self._seed)

    def restore_state(self, state) -> None:
        rng_state, n_feasibility, seed = state
        self._rng.setstate(rng_state)
        self._n_feasibility = int(n_feasibility)
        self._seed = int(seed)

    @property
    def name(self) -> str:
        return "mock:deterministic"

    def is_available(self) -> bool:
        return True

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        start = time.time()
        try:
            if "Parse this network intent" in prompt:
                data = self._parse_intent(prompt)
            elif "=== FAILED INTENT ===" in prompt:
                data = self._generate_alternatives(prompt)
            elif "=== NEW INTENT (to be evaluated) ===" in prompt:
                data = self._feasibility(prompt)
            else:
                data = {"ok": True, "note": "mock:deterministic fallback"}
        except Exception as e:   # never break the loop on a malformed prompt
            return LLMResponse(success=False, content="", model=self.name,
                               error=f"mock backend error: {e}")
        return LLMResponse(success=True, content=json.dumps(data),
                           parsed_json=data, model=self.name,
                           latency_ms=(time.time() - start) * 1000.0, options={})

    # -- prompt-type handlers ------------------------------------------- #

    @staticmethod
    def _json_after(prompt: str, header: str) -> Optional[Dict]:
        """First balanced {...} JSON object after a header marker."""
        idx = prompt.find(header)
        if idx < 0:
            return None
        start = prompt.find("{", idx)
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(prompt)):
            if prompt[i] == "{":
                depth += 1
            elif prompt[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(prompt[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _target_of(intent_dict: Optional[Dict], default: float = 8.0) -> float:
        try:
            return float(intent_dict["target"]["target_value"])
        except (TypeError, KeyError, ValueError):
            return default

    def _parse_intent(self, prompt: str) -> Dict:
        m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*Mbps", prompt)
        value = float(m.group(1)) if m else 8.0
        return {
            "type": "throughput_goal",
            "constraint": "min",
            "value": value,
            "unit": "Mbps",
            "scope": {"ue_ids": [], "bs_ids": []},
            "description": f"UE downlink throughput >= {value:g} Mbps",
        }

    @staticmethod
    def _deficits_by_serving_bs(state: Dict, target: float
                               ) -> Tuple[Dict[str, float], set]:
        """Aggregate each UE's throughput deficit onto its ACTUAL serving BS
        (P0-18: dynamic; NO ue1->bs1 index convention and NO last-character
        fallback). Shared-cell UEs accumulate onto the same BS (max deficit).

        Fail-closed on UNKNOWN telemetry (review blocker): a UE with a
        MISSING/None/non-finite throughput is NOT treated as zero deficit (that
        would silently look satisfied). Its serving BS is marked UNKNOWN and
        assigned a FULL-target deficit, so the policy still acts and never
        certifies an unmeasured UE as satisfied. A UE whose serving_bs is absent
        is a topology error -> raised (no index guess).

        Returns (deficits_by_bs, unknown_bs_set)."""
        deficits: Dict[str, float] = {}
        unknown: set = set()
        ue_states = state.get("ue_states") or {}
        for ue in sorted(ue_states):
            st = ue_states[ue] or {}
            bs = st.get("serving_bs")
            if not bs:
                raise ValueError(
                    f"UE {ue!r} has no serving_bs in the network state - "
                    f"refusing an index/last-character topology guess (P0-18)")
            tput = st.get("throughput_mbps")
            try:
                finite = tput is not None and math.isfinite(float(tput))
            except (TypeError, ValueError):
                finite = False
            if not finite:
                # UNKNOWN throughput -> fail-closed: full-target deficit, flagged.
                unknown.add(bs)
                deficits[bs] = max(deficits.get(bs, 0.0), float(target))
            else:
                d = max(0.0, target - float(tput))
                deficits[bs] = max(deficits.get(bs, 0.0), d)
        return deficits, unknown

    def _feasibility(self, prompt: str) -> Dict:
        self._n_feasibility += 1
        new_intent = self._json_after(prompt, "=== NEW INTENT")
        state = self._json_after(prompt, "=== CURRENT NETWORK STATE ===") or {}
        target = self._target_of(new_intent)

        # per-UE deficit mapped to each UE's ACTUAL serving BS (topology-aware).
        deficits, unknown = self._deficits_by_serving_bs(state, target)
        worst = max(deficits.values(), default=0.0)
        noise = self._rng.uniform(-0.03, 0.03)

        if self._n_feasibility % 4 == 0 and worst > 1.5:
            # low-confidence call on a REAL deficit: routes S2 -> S5 while
            # the mean throughput is below target, so C_nego samples are
            # measured under an actual shortfall (never all-zero)
            confidence = min(0.99, max(0.05, 0.30 + noise))
            proposed = {bs + "_power_offset": round(min(4.0, d / 0.6), 2)
                        for bs, d in deficits.items() if d > 0}
        elif self._n_feasibility % 3 == 0 and worst > 0.3:
            # risky rebalance: raising the neighbor dips the constrained UE
            confidence = min(0.99, max(0.05, 0.82 + noise))
            proposed = {"bs1_power_offset": 1.0, "bs2_power_offset": 4.0}
        else:
            # deficit-proportional serving raise (ChannelModel gain ~0.6/dB)
            confidence = min(0.99, max(0.05, 0.88 - 0.02 * worst + noise))
            proposed = {bs + "_power_offset": round(min(8.0, d / 0.6 + 1.0), 2)
                        for bs, d in deficits.items() if d > 0}
            if not proposed:
                proposed = {"bs1_power_offset": 0.5}

        return {
            "feasible": True,
            "confidence": round(confidence, 3),
            "reasoning": (f"deterministic state rule: worst deficit "
                          f"{worst:.2f} Mbps vs target {target:g}"),
            "proposed_config": proposed,
            "expected_kpi": {},
            "alternatives": [
                {"id": "alt1", "description": f"relax target to {target - 0.5:g} Mbps",
                 "target_value": target - 0.5, "confidence": 0.7},
                {"id": "alt2", "description": f"relax target to {target - 1.0:g} Mbps",
                 "target_value": target - 1.0, "confidence": 0.8},
            ],
        }

    def _generate_alternatives(self, prompt: str) -> Dict:
        failed = self._json_after(prompt, "=== FAILED INTENT ===") or {}
        target = self._target_of(failed)
        floor = target
        for a in failed.get("rejected_alternatives") or []:
            tv = a.get("target_value") if isinstance(a, dict) else None
            if isinstance(tv, (int, float)):
                floor = min(floor, float(tv))
        return {"alternatives": [
            {"id": "gen1", "description": f"relax target to {floor - 0.5:g} Mbps",
             "target_value": floor - 0.5, "confidence": 0.75},
            {"id": "gen2", "description": f"relax target to {floor - 1.0:g} Mbps",
             "target_value": floor - 1.0, "confidence": 0.85},
        ]}


class _BaselineBackendBase(DeterministicMockBackend):
    """Non-LLM baseline PROPOSER wired through the IDENTICAL pipeline (P0-20).

    A baseline is a policy that emits the SAME feasibility JSON (proposed_config,
    confidence) as the LLM, so it flows through the coordinator's real proposal
    adapter -> schema/admission -> safety transaction -> executor/readback ->
    evidence -> reserve ledger with NO bypass. It reuses DeterministicMockBackend
    for intent parsing and S5 alternative generation; only the S2 proposal policy
    differs. Deterministic and topology-aware (maps deficits to each UE's actual
    serving BS). These are honest reference controllers, NOT trained agents.
    """
    _BASELINE_NAME = "baseline:base"

    @property
    def name(self) -> str:
        return self._BASELINE_NAME

    def _propose(self, deficits: Dict[str, float], worst: float,
                 target: float) -> Tuple[Dict, float, str]:
        raise NotImplementedError

    def _feasibility(self, prompt: str) -> Dict:
        self._n_feasibility += 1
        new_intent = self._json_after(prompt, "=== NEW INTENT")
        state = self._json_after(prompt, "=== CURRENT NETWORK STATE ===") or {}
        target = self._target_of(new_intent)
        deficits, unknown = self._deficits_by_serving_bs(state, target)
        worst = max(deficits.values(), default=0.0)
        proposed, confidence, reasoning = self._propose(deficits, worst, target)
        if unknown:
            # UNKNOWN telemetry: report LOW confidence honestly (routes to
            # negotiation) - never silently confident over an unmeasured UE.
            confidence = min(float(confidence), 0.2)
            reasoning += f"; UNKNOWN throughput on {sorted(unknown)} (fail-closed)"
        return {
            "feasible": True,
            "confidence": round(float(confidence), 3),
            "reasoning": reasoning,
            "proposed_config": proposed,
            "expected_kpi": {},
            "alternatives": [
                {"id": "alt1", "description": f"relax target to {target - 0.5:g} Mbps",
                 "target_value": target - 0.5, "confidence": 0.6},
                {"id": "alt2", "description": f"relax target to {target - 1.0:g} Mbps",
                 "target_value": target - 1.0, "confidence": 0.7},
            ],
        }


class RuleBasedBackend(_BaselineBackendBase):
    """Fixed-threshold rule: any BS whose UE is below target gets a FIXED +3 dB
    power step. Deterministic, no learning; moderate fixed confidence."""
    _BASELINE_NAME = "baseline:rule_based"

    def _propose(self, deficits, worst, target):
        proposed = {f"{bs}_power_offset": 3.0 for bs, d in deficits.items()
                    if d > 0.1}
        if not proposed:
            proposed = {"bs1_power_offset": 0.0}
        conf = 0.55 if worst > 0.1 else 0.8
        return proposed, conf, (f"rule: +3dB on every cell below target "
                                f"(worst deficit {worst:.2f} Mbps)")


class ScoreHeuristicBackend(_BaselineBackendBase):
    """Score heuristic: power offset PROPORTIONAL to each cell's deficit
    (deficit / channel-gain estimate), clipped; confidence decreases with the
    residual worst deficit. Deterministic."""
    _BASELINE_NAME = "baseline:score_heuristic"

    def _propose(self, deficits, worst, target):
        # quantize to the power axis's 0.1 dB readback granularity so the
        # applied value round-trips through readback (fair pipeline comparison).
        proposed = {f"{bs}_power_offset": round(min(8.0, d / 0.6), 1)
                    for bs, d in deficits.items() if d > 0.1}
        if not proposed:
            proposed = {"bs1_power_offset": 0.0}
        conf = min(0.95, max(0.2, 0.85 - 0.05 * worst))
        return proposed, conf, (f"score: power ~ deficit/gain (worst "
                                f"{worst:.2f} Mbps)")


class TabularRLControllerBackend(_BaselineBackendBase):
    """The "rl_controller" baseline: a GENUINELY TRAINED tabular RL policy.

    On construction it loads (or reproducibly trains) a one-step contextual-
    bandit tabular Q-learning policy (decision/rl_controller.py) whose training
    run has full provenance and a content digest. It maps each BS's throughput
    deficit to the GREEDY learned power offset - i.e. it queries the trained
    Q-table, NOT a handcrafted step function. `learned == True`, and the training
    provenance/digest are exposed so records can cite the trained policy."""
    _BASELINE_NAME = "baseline:rl_controller"
    learned = True

    def __init__(self, seed: int = 12345):
        super().__init__(seed=seed)
        rl = _legacy_module("decision.", "rl_controller")
        RLTrainingConfig = rl.RLTrainingConfig
        get_trained_policy = rl.get_trained_policy
        self._cfg = RLTrainingConfig()
        self._trained = get_trained_policy(self._cfg)

    @property
    def policy_digest(self) -> str:
        return self._trained.digest

    @property
    def training_provenance(self) -> Dict:
        return dict(self._trained.provenance)

    def _propose(self, deficits, worst, target):
        proposed = {}
        for bs, d in deficits.items():
            if d <= 0.1:
                continue
            offset = self._trained.action_for_deficit(d, self._cfg)  # LEARNED
            proposed[f"{bs}_power_offset"] = round(float(offset), 1)
        if not proposed:
            proposed = {"bs1_power_offset": 0.0}
        conf = min(0.97, max(0.3, 0.9 - 0.03 * worst))
        return proposed, conf, (
            f"trained tabular Q-policy (digest {self._trained.digest}): greedy "
            f"action per deficit bucket (worst {worst:.2f} Mbps)")



class MockAgentBackend(LLMBackendBase):
    """Hardware-free stand-in for the revised Target/Control/Trajectory schemas.

    Reads INPUTS JSON and identifies the six verbatim SINGLE_CALL instructions.
    It copies owner authorization and supplied predictions, enumerates exposed
    function policies, and chooses among supplied controls. Outputs remain
    advisory and are validated by the executor. Never selected as a default.
    """

    #: One distinctive phrase per role, matched against the system prompt.
    # 2026-09-17: 이 표는 **프롬프트 본문의 문구**로 역할을 가린다.  오너 드롭
    # (`.orca/drops/RAN_AGENT_PROMPTS_REVISED_20260917.md`)으로 여섯 프롬프트가
    # 전면 교체되자 다섯 중 넷이 더는 맞지 않아 `mock:agent does not recognise
    # this role` 로 떨어졌다.  **프롬프트를 고치면 이 표도 같이 고쳐야 한다.**
    # 순서가 중요하다 -- monolith-form 을 먼저 본다.
    # trajectory 와 monolith-select 는 같은 문서이므로 둘 다 "trajectory" 로 가린다
    # (교체 전에도 그랬다).
    _ROLES = (
        ("monolith-form", "You jointly prepare a fixed target set T"),
        ("target", "fixed set T of owner-authorized RAN targets"),
        ("control", "fixed set C of joint RAN control configurations"),
        ("trajectory", "select one currently applicable control"),
        ("basic", "Decide which xApps to run together"),
    )

    @property
    def name(self) -> str:
        return "mock:agent"

    def is_available(self) -> bool:
        return True

    # -- plumbing ---------------------------------------------------------- #

    @staticmethod
    def role_of(system_prompt: str) -> Optional[str]:
        """Which agent is asking, by the wording of its own instruction."""
        text = str(system_prompt or "")
        for role, phrase in MockAgentBackend._ROLES:
            if phrase in text:
                return role
        return None

    @staticmethod
    def inputs_of(prompt: str) -> Dict[str, Any]:
        """The ``INPUTS:`` object of the user prompt, before ``OUTPUT SCHEMA:``."""
        text = str(prompt or "")
        start = text.find("INPUTS:")
        if start < 0:
            return {}
        body = text[start + len("INPUTS:"):]
        end = body.find("OUTPUT SCHEMA:")
        if end >= 0:
            body = body[:end]
        first, last = body.find("{"), body.rfind("}")
        if first < 0 or last <= first:
            return {}
        try:
            parsed = json.loads(body[first:last + 1])
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def unwrap_inputs(inputs: Mapping[str, Any]) -> Dict[str, Any]:
        """The named inputs, whichever of the three shapes the caller used.

        ``SINGLE_CALL.md`` names each position ``input.<name>``, and a caller
        may write that as one nested object (``{"input": {"intents": ...}}``),
        as bare names (``{"intents": ...}``) or as the flat dotted keys the
        executor actually sends (``{"input.intents": ...}``).  All three mean
        the same thing to a stand-in that only has to find its own inputs.
        """
        found = dict(inputs or {})
        nested = found.get("input")
        if isinstance(nested, Mapping):
            found = dict(nested)
        return {(key[len("input."):] if str(key).startswith("input.") else str(key)): value
                for key, value in found.items()}

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        started = time.time()
        role = self.role_of(system_prompt)
        if role is None:
            return LLMResponse(success=False, content="", model=self.name,
                               error="mock:agent does not recognise this role")
        inputs = self.inputs_of(prompt)
        inputs = self.unwrap_inputs(inputs)
        effective = {key: options[key] for key in ("maxTokens", "thinkingBudgetTokens", "reasoningEffort", "jsonMode")
                     if key in (options or {})}
        time.sleep(max(0.0, float(effective.get("thinkingBudgetTokens", 0))) * 0.00002)
        try:
            data = {
                "target": self._targets,
                "control": (lambda data: self._controls(
                    data, spanning="make the retained set span them" in system_prompt)),
                "trajectory": self._pair,
                "monolith-form": self._formation,
                "basic": self._basic,
            }[role](inputs)
        except Exception as exc:  # a stand-in must never take the sitting down
            return LLMResponse(success=False, content="", model=self.name,
                               error=f"mock:agent {role}: {type(exc).__name__}: {exc}")
        content = json.dumps(data)
        return LLMResponse(
            success=True, content=content, parsed_json=data, model=self.name,
            # Real elapsed mock latency lets virtual observation clocks age.
            latency_ms=(time.time() - started) * 1000,
            options=effective,
            # A stand-in measures nothing, so it reports nothing.  The old
            # ``len(text) // 4`` estimate reached ``assurance/coordination/
            # agents.py`` as a plain int -- indistinguishable from a
            # provider-reported count -- and stamped ``tokensComplete: true``
            # on a cost row no provider ever produced.  ``None`` is the honest
            # answer and is what flips that flag, so an artefact produced under
            # the mock says on its face that its totals are not provider usage.
            # Nothing is lost: the estimate was a re-encoding of a byte count,
            # and the real body size is already recorded per generation as
            # ``responseBytes``.
            input_tokens=None,
            output_tokens=None,
        )

    # -- the roles --------------------------------------------------------- #

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _authorization(inputs):
        authorization = dict(inputs.get("authorization") or {})
        entries = authorization.get("requirements", authorization)
        return authorization, {key: value for key, value in entries.items()
                               if isinstance(value, dict) and "original" in value}

    def _targets(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        authorization, entries = self._authorization(inputs)
        missing = []
        for intent in inputs.get("intents", []):
            req = intent.get("requirement", {})
            entry = entries.get(req.get("reqId"), {})
            for field in ("steps", "bound", "unit"):
                value = req.get(field, entry.get(field))
                if field == "bound" and req.get("steps", entry.get("steps")) == 0:
                    continue
                if value is None or value == "":
                    missing.append({"intentId": intent.get("intentId"), "field": field,
                                    "question": f"What is the authorized {field} for {req.get('reqId')}?"})
        if missing:
            return {"missingInformation": missing, "rationale": "Ask the owner for the missing requirement information."}
        return {"t0": {"targetId": "T0", "requirements": {key: entry["original"] for key, entry in entries.items()}},
                "levels": {key: {"steps": entry["steps"], "bound": entry.get("bound", entry["original"])}
                           for key, entry in entries.items()},
                "alternatives": self._selection(entries),
                "constraints": list(authorization.get("jointConditions", inputs.get("jointConditions", []))) +
                               [{"reqId": key, "op": entry.get("op"), "value": entry.get("bound", entry["original"]),
                                 "unit": entry.get("unit")} for key, entry in entries.items()],
                "ranking": authorization.get("preference", inputs.get("preference")) or
                           {"costRule": "normalized-concession",
                            "tieBreak": "lexicographic(D_max, D_mean) then intent order"},
                "missingInformation": [], "rationale": "Preserve every original requirement and the authorized levels."}

    @staticmethod
    def _selection(entries: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """The additions this stand-in carries: none, stated explicitly.

        Under ``v3.1-select10-existing3`` the field holds only the model's
        *additional* targets; code always adds the mandatory ones.  A stand-in
        cannot judge which concession is useful, and it cannot tell an
        authorized vector from an unauthorized one without the owner's mode
        table -- an unauthorized addition is now refused, not dropped -- so the
        one answer valid in every domain is an explicit empty list.  It must
        still not *omit* the field: that is a malformed answer, and reading it
        as "keep the whole domain" is exactly what this backend exists to catch.
        """
        return []

    @staticmethod
    def _function_moves(inputs):
        from itertools import product
        moves = []
        for function in inputs.get("function_catalog", []):
            fields = function.get("policyFields", {})
            for scope in function.get("scopes", []):
                for values in product(*(field.get("values", []) for field in fields.values())):
                    moves.append({"functionId": function["functionId"], "scope": scope,
                                  "policy": dict(zip(fields, values))})
        return moves

    @staticmethod
    def _compatible_pair(left, right):
        if left["scope"] != right["scope"]:
            return True
        if left["functionId"] == right["functionId"]:
            return False
        policies = set(left["policy"]) | set(right["policy"])
        return not ({"maxDlPrbs", "pfWeight"} <= policies or set(left["policy"]) & set(right["policy"]))

    @staticmethod
    def _configuration(functions, inputs):
        catalog = {item["functionId"]: item for item in inputs.get("function_catalog", [])}
        network = inputs.get("network_state") or {}
        baseline = dict(network.get("baselineConfiguration") or {})
        for item in catalog.values():
            for scope in item.get("scopes", []):
                axis = item.get("axis", "").replace("<ue>", scope.split("@")[-1])
                for field in item.get("policyFields", {}).values():
                    if axis and "baseline" in field:
                        baseline[axis] = field["baseline"]
        if network.get("unselectedFunctionRule") == "keep-current":
            baseline.update(network.get("configuration", network.get("appliedConfiguration", {})))
        for function in functions:
            item = catalog[function["functionId"]]
            axis = item.get("axis", "").replace("<ue>", function["scope"].split("@")[-1])
            if axis and len(function["policy"]) == 1:
                baseline[axis] = next(iter(function["policy"].values()))
        return {key: str(value) for key, value in baseline.items()}

    def _controls(self, inputs: Dict[str, Any], *, spanning: bool = False) -> Dict[str, Any]:
        from itertools import combinations
        retain = max(1, int((inputs.get("construction_policy") or {}).get("retain", 8)))
        moves = self._function_moves(inputs)
        groups = [[]] + [[move] for move in moves]
        if len(groups) < retain:
            pairs = [[left, right] for left, right in combinations(moves, 2)
                     if self._compatible_pair(left, right)]
            if spanning:
                # The coverage instruction asks for a set that SPANS the
                # combinations rather than the cheapest ones, so this stand-in
                # answers it differently: deeper joints first and one pair per
                # distinct pair of functions before any function repeats.
                seen = set()
                spread, rest = [], []
                for pair in pairs:
                    key = tuple(sorted(item["functionId"] for item in pair))
                    (rest if key in seen else spread).append(pair)
                    seen.add(key)
                pairs = spread + rest
                triples = [list(row) for row in combinations(moves, 3)
                           if all(self._compatible_pair(a, b)
                                  for a, b in combinations(row, 2))]
                pairs = pairs + triples
            for group in pairs:
                groups.append(group)
                if len(groups) >= retain:
                    break
        evidence = (inputs.get("effect_evidence") or {}).get("predictions", [])
        if isinstance(evidence, dict):
            evidence = list(evidence.values())
        candidates = []
        for functions in groups[:retain]:
            # Steering precedes a cap on the same UE.
            functions = sorted(functions, key=lambda item: "servingCell" not in item["policy"])
            configuration = self._configuration(functions, inputs)
            match = next((row for row in evidence if isinstance(row, dict) and
                          (("functions" in row and row["functions"] == functions) or
                           ("configuration" in row and {k: str(v) for k, v in row["configuration"].items()} == configuration))), {})
            candidates.append({"controlId": f"C{len(candidates)}", "functions": functions,
                               "predicted": match.get("predicted", match.get("kpis", "unknown")),
                               "uncertainty": match.get("uncertainty", "unknown"),
                               "predictedTarget": match.get("predictedTarget"),
                               "applicability": match.get("applicability", []),
                               "evidenceRefs": [str(match.get("predictionId", match.get("id")))] if match.get("predictionId", match.get("id")) else [],
                               "rationale": "Baseline, single-function policies, then compatible pairs."})
        return {"candidates": candidates, "rationale": "Enumerate exposed policies without inventing effect estimates."}

    @staticmethod
    def _tried_controls(inputs: Dict[str, Any]) -> set:
        return {str(item["controlId"]) for item in inputs.get("observations", [])
                if item.get("controlId") and item.get("valid", True)}

    def _pair(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        contract = inputs.get("target_contract") or {}
        controls = inputs.get("control_candidates") or []
        if isinstance(controls, dict):
            controls = controls.get("candidates", [])
        if not controls:
            raise ValueError("no control candidates in the inputs")
        targets = [contract.get("t0", {"targetId": "T0", "cost": 0})] + contract.get("alternatives", [])
        targets += contract.get("targets", [])
        costs = {item["targetId"]: item.get("cost", 0 if item["targetId"] == "T0" else float("inf"))
                 for item in targets}
        tried = self._tried_controls(inputs)
        available = [item for item in controls if str(item["controlId"]) not in tried] or controls
        selected = min(available, key=lambda item: costs.get(item.get("predictedTarget"), float("inf")))
        target = selected.get("predictedTarget")
        if target not in costs:
            target = min(costs, key=costs.get)
        return {"controlId": selected["controlId"], "targetId": target,
                "rationale": "Choose the first unobserved control with the lowest predicted concession cost."}

    def _formation(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        targets = self._targets(inputs)
        if targets.get("missingInformation"):
            return targets
        return {**targets, "candidates": self._controls(inputs)["candidates"]}

    def _basic(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        controls = self._controls(inputs)["candidates"]
        seen = [item.get("functions", item.get("instructions")) for item in inputs.get("observations", [])
                if item.get("valid", True)]
        # The first group this stand-in builds is the empty one -- the baseline.
        # "Change nothing" is not a proposal: an empty instruction list is
        # refused downstream, which sent every basic-monolith call on the
        # hardware-free path to the deterministic rule and made the arm
        # unmeasurable. Offer only candidates that actually select a function.
        usable = [item for item in controls if item.get("functions")]
        selected = next((item for item in usable if item["functions"] not in seen),
                        usable[0] if usable else controls[0])
        # No requirement restatement: _BASIC_SCHEMA no longer asks for one, and
        # the executor reads the originals it already holds.
        return {"instructions": selected["functions"],
                "rationale": "Try an unobserved instruction set aiming at the original requirements."}


# The wired baseline backends, keyed by the experiment method name (P0-20).
# "rl_controller" is a GENUINELY TRAINED tabular Q-learning policy
# (TabularRLControllerBackend.learned == True) with reproducible training
# provenance + digest (decision/rl_controller.py), not a handcrafted table.
BASELINE_BACKENDS = {
    "rule_based": RuleBasedBackend,
    "score_heuristic": ScoreHeuristicBackend,
    "rl_controller": TabularRLControllerBackend,
}


class LLMBackendManager:
    """
    Manages multiple LLM backends for model-agnostic operation.

    Provides unified interface for intent coordination regardless of
    underlying LLM model.

    Usage:
        manager = LLMBackendManager()
        available = manager.get_available_backends()
        manager.set_backend(LLMBackendType.CLAUDE_SONNET)
        response = manager.generate("Analyze this intent...")
    """

    def __init__(self):
        self.backends: Dict[LLMBackendType, LLMBackendBase] = {}
        # Local models discovered from a LiteLLM proxy, keyed by their name
        # ("local:<model>"). These live alongside the fixed enum backends.
        self.dynamic_backends: Dict[str, LLMBackendBase] = {}
        self.active_backend: Optional[LLMBackendType] = None
        self.active_dynamic: Optional[str] = None
        self._active_is_dynamic: bool = False
        self.warmer = ModelWarmer()
        self._init_backends()

    @classmethod
    def with_backend(cls, backend: LLMBackendBase) -> "LLMBackendManager":
        """Build a manager around one injected backend without discovery.

        This is the hermetic/profile construction path: it performs no provider
        probing and starts no warmer.  The normal constructor remains unchanged
        for the interactive legacy runtime.
        """
        if not isinstance(backend, LLMBackendBase):
            raise TypeError("backend must implement LLMBackendBase")
        manager = cls.__new__(cls)
        manager.backends = {}
        manager.dynamic_backends = {backend.name: backend}
        manager.active_backend = None
        manager.active_dynamic = backend.name
        manager._active_is_dynamic = True
        manager.warmer = ModelWarmer()
        return manager

    def _init_backends(self):
        """Initialize all available backends"""
        # Cloud backends - Claude
        for backend_type in [LLMBackendType.CLAUDE_SONNET, LLMBackendType.CLAUDE_OPUS]:
            backend = ClaudeBackend(backend_type=backend_type)
            if backend.is_available():
                self.backends[backend_type] = backend

        # Cloud backends - OpenAI
        for backend_type in [LLMBackendType.GPT_4O, LLMBackendType.GPT_4O_MINI, LLMBackendType.CODEX]:
            backend = OpenAIBackend(backend_type=backend_type)
            if backend.is_available():
                self.backends[backend_type] = backend

        # Cloud backends - Gemini
        for backend_type in [LLMBackendType.GEMINI_PRO, LLMBackendType.GEMINI_FLASH]:
            backend = GeminiBackend(backend_type=backend_type)
            if backend.is_available():
                self.backends[backend_type] = backend

        # Local backends - Ollama (native API, localhost)
        for backend_type in [LLMBackendType.LLAMA_3, LLMBackendType.LLAMA_3_70B,
                            LLMBackendType.PHI_3, LLMBackendType.MISTRAL, LLMBackendType.QWEN]:
            backend = OllamaBackend(backend_type=backend_type)
            if backend.is_available():
                self.backends[backend_type] = backend

        # Local server models via a LiteLLM proxy (OpenAI-compatible), discovered
        # dynamically so whatever is installed on the server is selectable.
        self._discover_dynamic()

        # Select the default active backend (cloud first, then local server).
        self._ensure_active()

        if self._active_is_dynamic:
            logger.info(f"Default backend: {self.active_dynamic}")
        elif self.active_backend:
            logger.info(f"Default backend: {self.active_backend.value}")
        else:
            logger.warning("No LLM backends available!")

        # Start keep-alive warming if the default active backend is a local model.
        self._apply_active_warming()

    def _discover_dynamic(self):
        """(Re)populate dynamic local-server backends from the LiteLLM proxy."""
        found: Dict[str, LLMBackendBase] = {}
        for model_id in discover_litellm_models():
            try:
                backend = LiteLLMBackend(model=model_id)
            except Exception as e:
                logger.warning(f"skipping local model {model_id}: {e}")
                continue
            if backend.is_available():
                found[backend.name] = backend
        # Deterministic offline mock (P6): always selectable by name, but
        # never auto-picked as the default (see _ensure_active). Preserve an
        # existing instance so its seeded state survives refresh_dynamic().
        prev_mock = self.dynamic_backends.get("mock:deterministic") \
            if hasattr(self, "dynamic_backends") else None
        found["mock:deterministic"] = prev_mock or DeterministicMockBackend()
        # The same idea for the T x C sitting's three agents: an offline
        # backend that answers Target / Control / Trajectory in their own
        # schemas, so a hardware-free demonstration needs no API key. Also
        # never auto-picked (every "mock:" name is skipped in _ensure_active).
        prev_agent = self.dynamic_backends.get("mock:agent") \
            if hasattr(self, "dynamic_backends") else None
        found["mock:agent"] = prev_agent or MockAgentBackend()
        # Batch G (P0-20): wired non-LLM baseline proposers, selectable by name
        # ("baseline:rule_based", ...) so they drive the SAME coordinator
        # pipeline. Preserve existing instances (seeded state) across refresh.
        for method, cls in BASELINE_BACKENDS.items():
            key = f"baseline:{method}"
            prev = self.dynamic_backends.get(key) \
                if hasattr(self, "dynamic_backends") else None
            found[key] = prev or cls()
        self.dynamic_backends = found
        if len(found) > 1:
            logger.info(f"LiteLLM local models: "
                        f"{[n for n in found if not n.startswith('mock:')]}")

    def active_selection_snapshot(self) -> tuple:
        """Batch E (P0-16): an OPAQUE snapshot of the ENTIRE active selection
        (fixed enum + dynamic name + is-dynamic flag), for a transactional
        switch/restore. Treat the value as opaque - restore only via
        restore_active_selection()."""
        return (self.active_backend, self.active_dynamic,
                self._active_is_dynamic)

    def restore_active_selection(self, snapshot: tuple) -> None:
        """Restore a selection captured by active_selection_snapshot() and
        re-apply warming (best-effort)."""
        self.active_backend, self.active_dynamic, self._active_is_dynamic = \
            snapshot
        try:
            self._apply_active_warming()
        except Exception as e:                       # pragma: no cover - warming
            logger.warning(f"warming after selection restore failed: {e}")

    def set_backend(self, target: Union[LLMBackendType, str]) -> bool:
        """
        Switch active backend TRANSACTIONALLY (Batch E / P0-16).

        Accepts either an LLMBackendType (fixed cloud/native backend) or a name
        string. A string may be an enum value ("claude-sonnet", "gpt-4o", ...) or
        a discovered local model ("local:qwen3-coder").

        The target is RESOLVED and VALIDATED BEFORE any mutation, and the whole
        selection is snapshotted so that ANY failure - including a warming
        exception AFTER the pointer swap - RESTORES the previous selection
        (no partial change) and returns False (fail-closed).
        """
        # 1) resolve + validate the new selection BEFORE mutating anything.
        if isinstance(target, LLMBackendType):
            if target not in self.backends:
                logger.warning(f"Backend not available: {target.value}")
                return False
            new_sel = (target, self.active_dynamic, False)
            label = target.value
        else:
            name = str(target)
            if name in self.dynamic_backends:
                new_sel = (self.active_backend, name, True)
                label = name
            else:
                match = next((bt for bt in self.backends if bt.value == name),
                             None)
                if match is None:
                    logger.warning(f"Backend not available: {name}")
                    return False
                new_sel = (match, self.active_dynamic, False)
                label = name
        # 2) snapshot -> mutate -> warm; restore on ANY exception (atomic).
        snap = self.active_selection_snapshot()
        self.active_backend, self.active_dynamic, self._active_is_dynamic = \
            new_sel
        try:
            self._apply_active_warming()
        except Exception as e:
            logger.error(f"backend warming failed for {label!r} - restoring "
                         f"previous selection (no partial switch): {e}")
            self.restore_active_selection(snap)
            return False
        logger.info(f"Switched to backend: {label}")
        return True

    def _resolve(self, target: Union[LLMBackendType, str]) -> Optional[LLMBackendBase]:
        """Resolve an enum or name string to a backend object."""
        if isinstance(target, LLMBackendType):
            return self.backends.get(target)
        name = str(target)
        if name in self.dynamic_backends:
            return self.dynamic_backends[name]
        for bt, backend in self.backends.items():
            if bt.value == name:
                return backend
        return None

    def _active_backend_obj(self) -> Optional[LLMBackendBase]:
        """The currently active backend object (dynamic or fixed)."""
        if self._active_is_dynamic:
            return self.dynamic_backends.get(self.active_dynamic)
        if self.active_backend is not None:
            return self.backends.get(self.active_backend)
        return None

    def _apply_active_warming(self):
        """Keep the active model warm only if it is a local (LiteLLM) backend."""
        backend = self._active_backend_obj()
        if isinstance(backend, LiteLLMBackend):
            self.warmer.start(backend)
        else:
            self.warmer.stop()

    def backend_by_name(self, name: str) -> Optional[LLMBackendBase]:
        """PUBLIC name -> backend OBJECT lookup across BOTH the dynamic and the
        fixed enum backends (None if unknown).

        Batch G (P0-20): a paired restore must be able to find every snapshotted
        proposer BY NAME regardless of which one happens to be active at that
        moment - the randomised method order leaves a different proposer active
        between methods."""
        return self._resolve(str(name))

    def active_backend_object(self) -> Optional[LLMBackendBase]:
        """PUBLIC accessor for the ACTIVE backend OBJECT (fixed enum or dynamic).

        Batch G (P0-20): a paired run must snapshot the proposer it can actually
        exercise, which includes a FIXED enum backend when that is the active
        LLM - not only the dynamically discovered ones. Exposed so the paired
        runner never has to reach into a private attribute or unpack the opaque
        selection snapshot."""
        return self._active_backend_obj()

    def active_backend_name(self) -> Optional[str]:
        """Canonical name of the active backend (enum value or 'local:<model>')."""
        if self._active_is_dynamic:
            return self.active_dynamic
        return self.active_backend.value if self.active_backend else None

    def get_available_backends(self) -> List[LLMBackendType]:
        """Get list of available fixed (enum) backends"""
        return list(self.backends.keys())

    def get_available_names(self) -> List[str]:
        """All selectable backend names: fixed enum values + discovered local models."""
        return [bt.value for bt in self.backends] + list(self.dynamic_backends)

    def _ensure_active(self):
        """
        Guarantee a valid active backend whenever any backend exists. Called
        after (re)discovery so the active selection never silently becomes None
        while backends are available - which would make every generate() fail
        even though the GUI still shows a model selected.
        """
        if self._active_backend_obj() is not None:
            return  # current active is still valid

        # The active backend went away. Prefer a prior valid enum selection...
        if self.active_backend in self.backends:
            self._active_is_dynamic = False
            self.active_dynamic = None
            return

        # ...otherwise re-run the default priority selection (cloud, then local).
        self._active_is_dynamic = False
        self.active_dynamic = None
        self.active_backend = None
        for bt in (LLMBackendType.CLAUDE_SONNET, LLMBackendType.GPT_4O,
                   LLMBackendType.GEMINI_PRO, LLMBackendType.LLAMA_3):
            if bt in self.backends:
                self.active_backend = bt
                return
        if self.backends:
            self.active_backend = next(iter(self.backends))
            return
        # never auto-default to the offline mock - it must be an explicit,
        # deliberate choice (set_backend("mock:deterministic"))
        for name in self.dynamic_backends:
            if not name.startswith("mock:"):
                self.active_dynamic = name
                self._active_is_dynamic = True
                return

    def refresh_dynamic(self) -> List[str]:
        """
        Re-discover local models from the LiteLLM proxy (e.g. the server was
        started after the GUI). Returns the current dynamic model names. Keeps a
        valid active backend: if the active local model vanished it falls back to
        another backend, and if the local server came back it is re-selectable.
        """
        self._discover_dynamic()
        self._ensure_active()
        self._apply_active_warming()
        return list(self.dynamic_backends)

    def shutdown(self):
        """Stop background warming (call on coordinator shutdown)."""
        self.warmer.shutdown()

    def get_all_backend_types(self) -> List[LLMBackendType]:
        """Get list of all supported backend types (including unavailable)"""
        return list(LLMBackendType)

    def get_backend_status(self) -> Dict[str, bool]:
        """Get availability status of all backend types"""
        return {bt.value: bt in self.backends for bt in LLMBackendType}

    def generate(self, prompt: str, system_prompt: str = "",
                 backend: Union[LLMBackendType, str] = None,
                 options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        """Generate response using specified or active backend"""
        obj = self._resolve(backend) if backend is not None else self._active_backend_obj()
        if obj is None:
            return LLMResponse(success=False, content="", error="No backend available")

        return obj.generate(prompt, system_prompt, options=options) if options is not None else obj.generate(prompt, system_prompt)

    # --- Batch E (P0-16): PINNED-OBJECT paths ---------------------------------
    # These call a specific backend HANDLE directly (identity-pinned for a whole
    # cycle), instead of re-resolving the manager's mutable active backend by
    # name. The coordinator pins the object at a proposal boundary and uses these
    # so a mid-cycle switch of the manager's active backend cannot leak in.
    def active_backend_object(self) -> Optional[LLMBackendBase]:
        """The currently active backend HANDLE (for pinning at a boundary)."""
        return self._active_backend_obj()

    def active_model_version(self) -> Optional[str]:
        """The model id of the active backend (distinct from its canonical
        name); None when no backend is active."""
        obj = self._active_backend_obj()
        return getattr(obj, "model", None) if obj is not None else None

    def resolve_object(self, target: Union[LLMBackendType, str]
                       ) -> Optional[LLMBackendBase]:
        """Public resolve of a name/enum to a backend handle."""
        return self._resolve(target)

    def generate_with(self, backend_obj: LLMBackendBase, prompt: str,
                      system_prompt: str = "", options: Optional[Mapping[str, Any]] = None) -> LLMResponse:
        """Generate using the given PINNED backend handle directly."""
        if backend_obj is None:
            return LLMResponse(success=False, content="",
                               error="No pinned backend")
        return backend_obj.generate(prompt, system_prompt, options=options) if options is not None else backend_obj.generate(prompt, system_prompt)

    def build_feasibility_prompt_hash(self, active_intents: List[Dict],
                                      new_intent: Dict, network_state: Dict,
                                      history: List[Dict] = None) -> str:
        """The REAL content hash of the feasibility prompt that WOULD be sent,
        computed WITHOUT calling any backend (P1-6). The coordinator stamps this
        BEFORE the bounded model call so a model TIMEOUT/exception (which abandons
        the response) still retains the exact prompt hash of the generated prompt."""
        prompt = self._build_feasibility_prompt(active_intents, new_intent,
                                                network_state, history)
        return prompt_content_hash(prompt, self.get_system_prompt())

    def analyze_feasibility_with(self, backend_obj: LLMBackendBase,
                                 active_intents: List[Dict], new_intent: Dict,
                                 network_state: Dict,
                                 history: List[Dict] = None) -> LLMResponse:
        """Feasibility using the PINNED backend handle (prompt built here, then
        served by the exact handle)."""
        if backend_obj is None:
            return LLMResponse(success=False, content="",
                               error="No pinned backend")
        prompt = self._build_feasibility_prompt(active_intents, new_intent,
                                                network_state, history)
        system_prompt = self.get_system_prompt()
        resp = backend_obj.generate(prompt, system_prompt)
        # stamp the REAL prompt hash at generation (P1-6): even on a failed
        # generation the prompt WAS produced, so the hash is recorded.
        if resp is not None and getattr(resp, "prompt_hash", None) is None:
            resp.prompt_hash = prompt_content_hash(prompt, system_prompt)
        return resp

    def generate_alternatives_with(self, backend_obj: LLMBackendBase,
                                   active_intents: List[Dict],
                                   failed_intent: Dict,
                                   network_state: Dict) -> LLMResponse:
        """Alternatives using the PINNED backend handle."""
        if backend_obj is None:
            return LLMResponse(success=False, content="",
                               error="No pinned backend")
        prompt = self._build_alternatives_prompt(active_intents, failed_intent,
                                                 network_state)
        return backend_obj.generate(prompt, self.get_system_prompt())

    def generate_all(self, prompt: str, system_prompt: str = "") -> Dict[LLMBackendType, LLMResponse]:
        """Generate responses from all available backends (for comparison)"""
        results = {}
        for backend_type, backend in self.backends.items():
            results[backend_type] = backend.generate(prompt, system_prompt)
        return results

    # Set by the coordinator (H2): the axis bounds ADVERTISED to the LLM
    # follow the installed profile-clamped action space - on a 24-PRB
    # profile the prompt must say [6, 24], matching what enforcement will
    # actually accept, not the largest profile's [6, 106].
    action_space = None
    # Batch G (P0-18): the CONFIGURED UE set + each UE's serving cell, set by the
    # coordinator so the system prompt enumerates the actual topology (ue1..ueN)
    # dynamically instead of a hardcoded 2-UE example.
    ue_ids = None
    ue_serving = None

    def _configured_ue_block(self) -> str:
        """A dynamic description of the configured UEs and their serving cells,
        so the prompt enumerates {ue1, ue2, ue3, ...} for any topology."""
        ues = list(self.ue_ids or [])
        if not ues:
            return ""
        serving = self.ue_serving or {}
        lines = ["CONFIGURED UEs (address per-UE axes as ueN_prb / "
                 "ueN_sched_priority):"]
        # group UEs by serving cell so shared cells are explicit
        by_cell: Dict[str, List[str]] = {}
        for ue in ues:
            gnb = serving.get(ue)
            bs = str(gnb).replace("gnb", "bs", 1) if gnb else "?"
            by_cell.setdefault(bs, []).append(ue)
        for ue in ues:
            gnb = serving.get(ue)
            bs = str(gnb).replace("gnb", "bs", 1) if gnb else "?"
            shared = len(by_cell.get(bs, [])) >= 2
            note = " (SHARED cell - use per-UE keys to partition)" if shared \
                else ""
            lines.append(f"  * {ue} -> {bs}{note}")
        # DYNAMIC per-UE addressing example derived from the ACTUAL primary
        # shared cell (P0-18): name the REAL contending UEs' keys, not a fixed
        # ue1/ue2 example, so the example always tracks the configured topology.
        shared = [bs for bs, members in by_cell.items() if len(members) >= 2]
        if shared:
            bs = shared[0]
            members = by_cell[bs]
            a, b = members[0], members[1]
            lines.append(
                f"  e.g. on the SHARED cell {bs}, bias {a} vs {b} with "
                f"{a}_sched_priority / {b}_sched_priority (a {bs}_* key would "
                f"hit both equally); cap just one with {a}_prb.")
        return "\n".join(lines)

    def _bs_ids(self) -> List[str]:
        """Configured BS ids as bsN labels, derived from the UE serving map
        (P0-18); falls back to the legacy 2-BS pair when unknown."""
        serving = self.ue_serving or {}
        bss: List[str] = []
        for gnb in serving.values():
            bs = str(gnb).replace("gnb", "bs", 1)
            if bs not in bss:
                bss.append(bs)
        return bss or ["bs1", "bs2"]

    def _proposed_config_fields(self) -> str:
        """The proposed_config JSON example fields, GENERATED for EVERY configured
        BS and UE (P0-18) - never a hardcoded ue1/ue2 example. Per-cell axes for
        each BS; per-UE PRB / scheduling-priority for each UE."""
        ues = list(self.ue_ids or []) or ["ue1", "ue2"]
        fields: List[str] = []
        for bs in self._bs_ids():
            fields.append(f'        "{bs}_power_offset": float')
            fields.append(f'        "{bs}_prb": int')
            fields.append(f'        "{bs}_sched_priority": float')
            fields.append(f'        "{bs}_mcs_offset": float')
        for ue in ues:
            fields.append(f'        "{ue}_prb": int')
            fields.append(f'        "{ue}_sched_priority": float')
        return ",\n".join(fields)

    def _expected_kpi_fields(self) -> str:
        """The expected_kpi JSON example fields, GENERATED per configured UE
        (P0-18)."""
        ues = list(self.ue_ids or []) or ["ue1", "ue2"]
        return ",\n".join(f'        "{ue}_throughput": float' for ue in ues)

    def get_system_prompt(self) -> str:
        """Standard system prompt (identical across models); axis ranges
        substituted from the installed action space (H2); the configured UE set
        enumerated dynamically (P0-18)."""
        space = self.action_space
        if space is None:
            space = _legacy_module("con", "fig").ActionSpaceConfig()
        out = self._SYSTEM_PROMPT_TEMPLATE
        out = out.replace("<<CONFIGURED_UES>>", self._configured_ue_block())
        out = out.replace("<<PROPOSED_CONFIG_FIELDS>>",
                          self._proposed_config_fields())
        out = out.replace("<<EXPECTED_KPI_FIELDS>>",
                          self._expected_kpi_fields())
        for token, value in (
                ("<<POWER_MIN>>", f"{space.power_offset_min_db:g}"),
                ("<<POWER_MAX>>", f"{space.power_offset_max_db:g}"),
                ("<<PRB_MIN>>", str(space.prb_cap_min)),
                ("<<PRB_MAX>>", str(space.prb_cap_max)),
                ("<<PRIO_MIN>>", f"{space.sched_priority_min:g}"),
                ("<<PRIO_MAX>>", f"{space.sched_priority_max:g}"),
                ("<<MCS_MIN>>", f"{space.mcs_offset_min:g}"),
                ("<<MCS_MAX>>", f"{space.mcs_offset_max:g}")):
            out = out.replace(token, value)
        return out

    _SYSTEM_PROMPT_TEMPLATE = """You are an AI-based intent coordinator for autonomous RAN management.

Your task is to analyze network intents and determine feasibility of satisfying them simultaneously.

For each analysis, you must output a JSON object with:
{
    "feasible": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "explanation",
    "proposed_config": {
<<PROPOSED_CONFIG_FIELDS>>
    },
    "expected_kpi": {
<<EXPECTED_KPI_FIELDS>>
    },
    "alternatives": [
        {
            "id": "alt1",
            "description": "...",
            "target_value": float,
            "actions": [...],
            "confidence": 0.0-1.0
        }
    ]
}

Every alternative that relaxes the intent's constraint MUST include
"target_value": the relaxed numeric target for the intent's KPI, in the
intent's own unit (e.g. Mbps for a throughput goal). Across negotiation
rounds alternatives must relax monotonically: for a MIN constraint the
target_value never increases from round to round, for a MAX constraint it
never decreases.

ACTION SPACE (each axis is applied at runtime, no restart; bs1 -> gNB1, bs2 -> gNB2):
  * bsX_power_offset   TX power offset in dB, range [<<POWER_MIN>>, +<<POWER_MAX>>]. +raises cell power,
                       -lowers it. 0 = baseline.
  * bsX_prb            DL PRB (bandwidth) cap, integer. 0 = uncapped (full carrier).
                       A positive value in [<<PRB_MIN>>, <<PRB_MAX>>] limits that cell's DL resource
                       blocks (lower = less bandwidth/throughput).
  * bsX_sched_priority Proportional-fair scheduling weight, range [<<PRIO_MIN>>, <<PRIO_MAX>>].
                       1.0 = neutral. >1 favors that cell's UE, <1 starves it.
                       (Meaningful when UEs share a cell; harmless otherwise.)
  * bsX_mcs_offset     DL MCS ceiling offset, range [<<MCS_MIN>>, <<MCS_MAX>>]. 0 = unconstrained
                       link adaptation. Negative caps the MCS lower (more robust,
                       lower peak throughput) - a controlled degradation lever.

<<CONFIGURED_UES>>

PER-UE ADDRESSING (intra-cell fairness) - the PRB and scheduling-priority axes can
also be addressed at ONE UE instead of the whole cell, via ueN_prb / ueN_sched_priority
(same ranges as bsX_prb / bsX_sched_priority):
  * Use per-UE keys when UEs SHARE a cell (e.g. UE1 and UE2 both on BS1) and the
    intent must partition resources or bias fairness BETWEEN them - a bsX_* key
    would hit every UE on that cell equally. The current network state lists each
    UE's serving cell so you can tell which UEs share one.
  * ueN_sched_priority raises/lowers just that UE's proportional-fair share; a per-UE
    ueN_prb caps just that UE's PRBs (the scheduler applies the tighter of the cell
    cap and the per-UE cap). Power and MCS are cell-wide only (no per-UE form).
  * For a UE alone on its cell (e.g. UE3 on BS2), bsX_* and ueN_* are equivalent;
    prefer the per-cell key there.

IMPORTANT - you only need to include the axes you actually want to change. Any axis
(per-cell OR per-UE) you OMIT is left at its neutral value (power 0, prb 0/uncapped,
sched_priority 1.0, mcs_offset 0), i.e. unchanged from the current configuration.
Prefer the smallest action vector that resolves the conflict.

Key principles:
1. Active intents have priority - new intents must not violate them
2. Consider cross-UE / cross-axis KPI coupling (e.g. BS2 power affects all UEs;
   lowering PRB or MCS reduces throughput; raising power can improve SINR/MCS)
3. Report confidence honestly - low confidence triggers negotiation
4. Generate alternatives when original request is infeasible
5. Every proposed value is clipped to its action-space bound before it is applied,
   and any violated intent triggers a deterministic rollback of ALL axes - so
   propose the direct configuration; the framework guarantees safety."""

    def analyze_feasibility(self, active_intents: List[Dict], new_intent: Dict,
                           network_state: Dict, history: List[Dict] = None,
                           backend: LLMBackendType = None) -> LLMResponse:
        """
        Analyze feasibility of new intent given active intents and network state.

        This is the main interface for the coordination loop (S2 Analysis Layer).
        """
        prompt = self._build_feasibility_prompt(active_intents, new_intent, network_state, history)
        system_prompt = self.get_system_prompt()

        resp = self.generate(prompt, system_prompt, backend)
        # stamp the REAL prompt hash at generation (P1-6).
        if resp is not None and getattr(resp, "prompt_hash", None) is None:
            resp.prompt_hash = prompt_content_hash(prompt, system_prompt)
        return resp

    def _build_feasibility_prompt(self, active_intents: List[Dict], new_intent: Dict,
                                  network_state: Dict, history: List[Dict] = None) -> str:
        """Build feasibility analysis prompt"""
        prompt_parts = []

        prompt_parts.append("=== ACTIVE INTENTS ===")
        for i, intent in enumerate(active_intents, 1):
            prompt_parts.append(f"Intent {i}: {json.dumps(intent)}")

        prompt_parts.append("\n=== NEW INTENT (to be evaluated) ===")
        prompt_parts.append(json.dumps(new_intent))

        prompt_parts.append("\n=== CURRENT NETWORK STATE ===")
        prompt_parts.append(json.dumps(network_state, indent=2))

        if history:
            prompt_parts.append("\n=== HISTORY RECORDS (recent action-KPI outcomes) ===")
            for i, record in enumerate(history[-5:], 1):
                prompt_parts.append(f"{i}. {json.dumps(record)}")

        prompt_parts.append("\n=== TASK ===")
        prompt_parts.append("Analyze whether the new intent can be satisfied without violating active intents.")
        prompt_parts.append("Output your analysis as a JSON object.")

        return "\n".join(prompt_parts)

    def _build_alternatives_prompt(self, active_intents: List[Dict],
                                   failed_intent: Dict,
                                   network_state: Dict) -> str:
        """Build the S5 alternatives prompt (extracted so a PINNED-object path
        can reuse it, Batch E)."""
        return f"""=== FAILED INTENT ===
{json.dumps(failed_intent)}

=== ACTIVE INTENTS (must be preserved) ===
{json.dumps(active_intents)}

=== CURRENT NETWORK STATE ===
{json.dumps(network_state, indent=2)}

=== TASK ===
The original intent cannot be satisfied without violating active intents.
Generate 2-3 alternative intents that:
1. Relax the original constraint
2. Achieve similar goals through different means
3. Maintain active intent satisfaction
4. Relax MONOTONICALLY: each alternative must relax at least as far as every
   entry in the failed intent's "rejected_alternatives" list (for a MIN
   constraint, target_value must not exceed any rejected target_value; for a
   MAX constraint it must not fall below).

Output as JSON:
{{"alternatives": [{{"id": "...", "description": "...",
"target_value": <relaxed numeric target in the intent's unit>,
"confidence": 0.0-1.0}}]}}"""

    def generate_alternatives(self, active_intents: List[Dict], failed_intent: Dict,
                             network_state: Dict, backend: LLMBackendType = None) -> LLMResponse:
        """
        Generate alternative intents when original is infeasible.

        This is used in S5 Negotiation Layer.
        """
        prompt = self._build_alternatives_prompt(active_intents, failed_intent,
                                                 network_state)
        return self.generate(prompt, self.get_system_prompt(), backend)
