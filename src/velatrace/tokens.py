"""Exact preflight counts and deterministic budgets; never guess tokens from characters."""
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Protocol

from .errors import CapabilityError, ValidationError


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str

    def messages(self) -> list[dict[str, str]]:
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": self.user}]

    @property
    def fingerprint(self) -> str:
        return sha256(json.dumps(self.messages(), ensure_ascii=False,
                                 sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class TokenEstimate:
    input_tokens: int
    output_cap: int
    prompt_fingerprint: str
    method: str
    calls_max: int = 1
    provider_identity: tuple[str, ...] = ()

    @property
    def confirmation_fingerprint(self) -> str:
        """Bind approval to the actual prompt, provider and approved token budget."""
        payload = (self.prompt_fingerprint, self.input_tokens, self.output_cap,
                   self.calls_max, self.provider_identity)
        return sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()

    def __post_init__(self):
        if type(self.input_tokens) is not int or self.input_tokens < 0:
            raise ValidationError("Input token count must be a nonnegative integer.")
        for value in (self.output_cap, self.calls_max):
            if type(value) is not int or value < 1:
                raise ValidationError("Output cap and maximum calls must be positive integers.")

    def cost_ceiling(self, input_per_million: Decimal,
                     output_per_million: Decimal) -> Decimal:
        if (not input_per_million.is_finite() or not output_per_million.is_finite()
                or input_per_million < 0 or output_per_million < 0):
            raise ValidationError("Token prices cannot be negative.")
        return Decimal(self.calls_max) * (Decimal(self.input_tokens) * input_per_million +
                Decimal(self.output_cap) * output_per_million) / Decimal(1_000_000)


class TokenCounter(Protocol):
    method: str

    def count(self, prompt: Prompt) -> int: ...

    def count_output(self, text: str) -> int: ...


class LocalChatTokenizer:
    """Offline tokenizer with explicitly verified provider/model chat framing.

    Installing a tokenizer is a deliberate setup step. No runtime downloads or
    remote tokenizer code are allowed. Model and template must be verified by the
    integrator against provider usage before relying on its count.
    """
    method = "verified local model tokenizer and chat template"

    def __init__(self, directory: Path, model: str, verified_model: str):
        if not model or model != verified_model:
            raise CapabilityError("Tokenizer must be verified for this exact configured model.")
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise CapabilityError("Install velatrace[tokenizer] and a local model tokenizer, "
                                  "or use Gemini's exact countTokens API.") from exc
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(directory), local_files_only=True, trust_remote_code=False)
        if not self.tokenizer.chat_template:
            raise CapabilityError("This tokenizer has no verified chat template.")

    def count(self, prompt: Prompt) -> int:
        return len(self.tokenizer.apply_chat_template(
            prompt.messages(), tokenize=True, add_generation_prompt=True))

    def count_output(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))
