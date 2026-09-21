"""BYOK HTTPS only, configured-origin only, bounded requests and streaming usage.

No telemetry, redirect following, arbitrary model tools, or application backend.
"""
from dataclasses import dataclass, field
import http.client
import json
import re
import socket
from threading import Lock
from time import monotonic
from typing import Callable
from urllib.parse import urlsplit

from .errors import CapabilityError, ValidationError, VelaTraceError
from .tokens import Prompt, TokenCounter


class ProviderError(VelaTraceError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class CallBudget:
    limit: int = 40
    used: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self):
        if type(self.limit) is not int or self.limit < 1 or self.limit > 1000:
            raise ValidationError("API call cap must be between 1 and 1000.")
        if type(self.used) is not int or not 0 <= self.used <= self.limit:
            raise ValidationError("Used API calls must be an integer within the cap.")

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self):
        with self._lock:
            if self.used >= self.limit:
                raise ProviderError("Hard API call cap reached. Start a new audit to make more calls.")
            self.used += 1


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    protocol: str = "gemini"
    search_model: str | None = None
    builtin_search: bool = False

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValidationError("Configure a plain HTTPS API base URL without credentials or query.")
        if self.protocol not in {"gemini", "openai"}:
            raise ValidationError("Provider protocol must be gemini or openai.")
        for model in (self.model, self.search_model):
            if model is not None and not re.fullmatch(r"[A-Za-z0-9._/-]+", model):
                raise ValidationError("Invalid provider model identifier.")
        if not self.model:
            raise ValidationError("Choose a model offered by the configured provider.")


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    source: str = "provider usage pending"
    text_characters: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage


class Provider:
    def __init__(self, config: ProviderConfig, budget: CallBudget | None = None,
                 tokenizer: TokenCounter | None = None,
                 disclosure_gate: Callable[[ProviderConfig], bool] | None = None):
        self.config = config
        self.budget = budget or CallBudget()
        self.tokenizer = tokenizer
        self.disclosure_gate = disclosure_gate

    def _authorize(self):
        if not self.config.api_key.strip():
            raise ProviderError("Missing API key. Configure your own provider API key.")
        if self.disclosure_gate is None or not self.disclosure_gate(self.config):
            raise ProviderError("Accept the first-use board-data disclosure for this provider before analysis.")

    def _request(self, path: str, body: dict, timeout: float,
                 on_event: Callable[[dict], None] | None = None) -> dict | None:
        self._authorize()
        self.budget.consume()
        parsed = urlsplit(self.config.endpoint)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if
                   on_event else "application/json"}
        if self.config.protocol == "gemini":
            headers["x-goog-api-key"] = self.config.api_key
        else:
            headers["Authorization"] = "Bearer " + self.config.api_key
        target = parsed.path.rstrip("/") + path
        deadline = monotonic() + timeout
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=timeout)
        pending = b""
        total = 0
        try:
            connection.connect()
            active_socket = connection.sock
            if monotonic() >= deadline:
                raise TimeoutError()
            if active_socket:
                active_socket.settimeout(max(0.001, deadline - monotonic()))
            connection.request("POST", target, json.dumps(body).encode("utf-8"), headers)
            if active_socket:
                active_socket.settimeout(max(0.001, deadline - monotonic()))
            response = connection.getresponse()
            if response.status in {401, 403}:
                raise ProviderError("Provider rejected the API key or permission. Check your key and model access.")
            if response.status == 429:
                raise ProviderError("Provider rate limit reached. Try again later.", True)
            if response.status >= 500:
                raise ProviderError("Provider outage or server error. Try again later.", True)
            if not 200 <= response.status < 300:
                raise ProviderError(f"Provider refused request (HTTP {response.status}). "
                                    "Check endpoint, model, and supported options.")
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                if active_socket:
                    active_socket.settimeout(remaining)
                chunk = response.read1(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > 4_000_000:
                    raise ProviderError("Provider response exceeds the safety size limit.")
                pending += chunk
                if on_event:
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        line = line.strip()
                        if line.startswith(b"data:"):
                            value = line[5:].strip()
                            if value and value != b"[DONE]":
                                on_event(json.loads(value))
            if on_event:
                if pending.startswith(b"data:") and pending[5:].strip() != b"[DONE]":
                    on_event(json.loads(pending[5:]))
                return None
            result = json.loads(pending)
            if not isinstance(result, dict):
                raise ProviderError("Provider returned an invalid response object.")
            return result
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError("Provider request timed out.", True) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderError("Cannot connect to configured provider. Check network and endpoint.", True) from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError("Provider returned malformed data; no results were applied.") from exc
        finally:
            connection.close()

    def _gemini_body(self, prompt: Prompt, max_tokens: int, model: str,
                     search: bool = False) -> dict:
        body = {"systemInstruction": {"parts": [{"text": prompt.system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens}}
        if search:
            body["tools"] = [{"google_search": {}}]
        else:
            body["generationConfig"]["responseMimeType"] = "application/json"
        return body

    def count_tokens(self, prompt: Prompt, max_tokens: int = 2048,
                     model: str | None = None, search: bool = False) -> tuple[int, str]:
        selected_model = model or self.config.model
        if self.config.protocol == "gemini":
            body = self._gemini_body(prompt, max_tokens, selected_model, search)
            body["model"] = "models/" + selected_model
            result = self._request(f"/models/{selected_model}:countTokens",
                                   {"generateContentRequest": body}, 30)
            count = result.get("totalTokens") if result else None
            if type(count) is not int or count < 0:
                raise ProviderError("Provider did not return a precise input token count.")
            return count, "provider countTokens on actual generation request"
        if self.tokenizer is None or selected_model != self.config.model or search:
            raise CapabilityError("Exact token count unavailable for this model/tool request. "
                                  "Configure a verified local tokenizer or use Gemini countTokens.")
        count = self.tokenizer.count(prompt)
        if type(count) is not int or count < 0:
            raise ProviderError("Tokenizer did not return a valid input token count.")
        return count, self.tokenizer.method

    def complete(self, prompt: Prompt, max_tokens: int,
                 on_usage: Callable[[Usage], None] | None = None,
                 search: bool = False, timeout: float = 60, retries: int = 2) -> Completion:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ValidationError("Every generation requires an output token cap between 1 and 65536.")
        if type(retries) is not int or retries < 0 or retries > 2:
            raise ValidationError("At most two retries are permitted.")
        if search and not self.config.builtin_search:
            raise CapabilityError("Configured provider has no enabled built-in search.")
        model = (self.config.search_model or self.config.model) if search else self.config.model
        deadline = monotonic() + min(timeout, 8 if search else timeout)
        for attempt in range(retries + 1):
            text_parts: list[str] = []
            usage = Usage()

            def event(data: dict):
                nonlocal usage
                if not isinstance(data, dict):
                    raise ProviderError("Provider returned an invalid streaming event.")
                if "error" in data:
                    raise ProviderError("Provider reported an error during streaming.")
                if self.config.protocol == "gemini":
                    candidates = data.get("candidates", [])
                    for candidate in candidates:
                        for part in candidate.get("content", {}).get("parts", []):
                            if "functionCall" in part:
                                raise ProviderError("Model requested an unsupported application tool.")
                            value = part.get("text", "")
                            if not isinstance(value, str):
                                raise ProviderError("Provider returned invalid text content.")
                            text_parts.append(value)
                    raw = data.get("usageMetadata", {})
                    input_count = raw.get("promptTokenCount", usage.input_tokens)
                    output_count = raw.get("candidatesTokenCount")
                    if output_count is not None:
                        thoughts = raw.get("thoughtsTokenCount", 0)
                        if (type(output_count) is not int or output_count < 0
                                or type(thoughts) is not int or thoughts < 0):
                            raise ProviderError("Provider returned invalid token usage.")
                        output_count += thoughts
                    elif usage.source == "provider usage":
                        output_count = usage.output_tokens
                else:
                    for choice in data.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("tool_calls") or delta.get("function_call"):
                            raise ProviderError("Model requested an unsupported application tool.")
                        value = delta.get("content") or ""
                        if not isinstance(value, str):
                            raise ProviderError("Provider returned invalid text content.")
                        text_parts.append(value)
                    raw = data.get("usage") or data.get("x_groq", {}).get("usage") or {}
                    input_count = raw.get("prompt_tokens", usage.input_tokens)
                    output_count = raw.get("completion_tokens")
                    if output_count is None and usage.source == "provider usage":
                        output_count = usage.output_tokens
                current = "".join(text_parts)
                source = "provider usage" if output_count is not None else "provider usage pending"
                if output_count is None and self.tokenizer and not search:
                    output_count = self.tokenizer.count_output(current)
                    source = "local visible-output tokenizer (excludes hidden reasoning)"
                if any(value is not None and (type(value) is not int or value < 0)
                       for value in (input_count, output_count)):
                    raise ProviderError("Provider returned invalid token usage.")
                usage = Usage(input_count, output_count, source, len(current))
                if on_usage:
                    on_usage(usage)

            if self.config.protocol == "gemini":
                path = f"/models/{model}:streamGenerateContent?alt=sse"
                body = self._gemini_body(prompt, max_tokens, model, search)
            else:
                path = "/chat/completions"
                body = {"model": model, "messages": prompt.messages(), "temperature": 0.1,
                        "max_tokens": max_tokens, "stream": True,
                        "stream_options": {"include_usage": True}}
                if search:
                    body["compound_custom"] = {"tools": {"enabled_tools": ["web_search"]}}
                else:
                    body["response_format"] = {"type": "json_object"}
            try:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ProviderError("Provider request timed out.")
                self._request(path, body, remaining, event)
                result = "".join(text_parts)
                if not result:
                    raise ProviderError("Provider returned no text. Check model access or content filtering.")
                return Completion(result, usage)
            except ProviderError as exc:
                if not exc.retryable or attempt == retries or monotonic() >= deadline:
                    raise
        raise ProviderError("Provider attempts exhausted.")
