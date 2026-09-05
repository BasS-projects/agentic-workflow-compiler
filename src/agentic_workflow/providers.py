"""Explicit, optional HTTP AI boundary compatible with chat completions APIs.

No provider is enabled automatically. Constructing a provider makes no request;
calling extract or invoking it as an AI tool sends text to the chosen endpoint.
There are no SDK dependencies, provider retries, or bundled credentials.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .ir import ValidationError, validate_json, validate_workflow


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_TIMEOUT_SECONDS = 3600

_EXTRACTION_INSTRUCTION = """Convert the user's Skill into exactly one workflow IR JSON object.
Do not execute anything. Do not include commentary. The object must obey all of these rules:
- The only top-level keys are ir_version, id, inputs, steps, outputs; all are required.
- ir_version is exactly "0.1". Workflow, input, and step IDs match [A-Za-z_][A-Za-z0-9_]*.
- inputs is an object mapping input names to {"type": TYPE, "default": OPTIONAL_LITERAL}.
  TYPE is string, number, integer, boolean, object, or array. Defaults must match TYPE;
  without a default the input is required. Boolean is not a number. Integers use integer JSON literals.
- steps is an ordered array of 1..1000 steps, with unique IDs and no loops or parallel execution.
- Each step has exactly the required keys id, kind, tool, args and may also have retry,
  timeout_seconds, when. kind is "tool" or "ai". tool is a nonblank registered tool/provider
  name (up to 256 characters, no leading/trailing whitespace or control characters).
- args and outputs are objects mapping names to expressions. An expression is literal JSON,
  a nested object/array of expressions, or an object containing exactly one key "$ref".
  References are {"$ref":"inputs.NAME"} or {"$ref":"steps.ID.FIELD"}; additional .FIELD
  segments traverse nested object fields. Inputs must be declared. Step references must name
  a strictly earlier step; outputs may reference any step. References cannot replace the entire
  args or outputs map. Objects containing $ref must have no other keys. Skipped results are null.
- The only condition is when: {"equals": [EXPRESSION, EXPRESSION]}, exactly two operands.
- Optional retry is {"max_attempts": INTEGER, "delay_seconds": OPTIONAL_NUMBER}; max_attempts
  is 1..10, delay_seconds is 0..3600. Optional timeout_seconds is a number greater than 0
  and at most 3600. No other retry keys. No unknown schema keys anywhere.
- All values must be JSON: no NaN, Infinity, duplicate keys, executable code, imports,
  shell commands, arbitrary expressions, or nesting beyond 64 levels.
- Known built-in kind=tool names and args/results:
  files.exists: args {path: string}, result {exists: boolean, path: string};
  files.require_exists: args {path: string}, result {exists: true, path: string} or fails;
  files.read_text: args {path: string}, result {text: string};
  text.normalize: args {text: string}, result {text: string};
  files.write_text: args {path: string, text: string}, result {path: string, bytes: integer};
  core.value: args {value: EXPRESSION}, result {value: JSON}.
  File paths are confined to the runtime workspace.
- Only use these built-in tools or additional tools/providers explicitly named in the Skill.
  A chat-completions AI provider accepts args.text and optional args.instruction, returning
  {text: string}. Use kind=ai and the explicitly named provider registration.
- Do not invent capabilities for unsupported actions such as approvals, shell commands,
  external integrations, loops, or parallelism. A workflow must accurately represent the Skill.
"""


class ProviderError(RuntimeError):
    """A sanitized provider failure, without endpoint, response body, or credentials."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward text or Authorization headers away from the explicit URL."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON number")


def _decode_json(text: str) -> Any:
    result = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    validate_json(result)
    return result


def _unfence(text: str) -> str:
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) >= 3:
        opening = re.fullmatch(r"(`{3,}|~{3,})(?:json|workflow-ir)?[ \t]*", lines[0])
        if opening:
            marker = opening.group(1)
            closing = re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*"
            if re.fullmatch(closing, lines[-1]):
                return "\n".join(lines[1:-1])
    return stripped


class ChatCompletionsProvider:
    """Optional SemanticExtractor and runtime AI callable using an explicit URL.

    timeout_seconds is passed to urllib's network timeout; a runtime step timeout
    provides a separate process-enforced wall-clock limit. Retries belong to the
    workflow runtime. Redirects are refused. Response bodies are limited to 4 MiB.
    """

    def __init__(self, endpoint: str, model: str, api_key: str | None = None, timeout_seconds: float = 30):
        if type(endpoint) is not str or not endpoint or any(character.isspace() or ord(character) < 32 for character in endpoint):
            raise ValueError("endpoint must be an explicit HTTP(S) URL without whitespace")
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            valid_endpoint = (
                parsed.scheme in ("http", "https")
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
            )
            parsed.port  # Validate a malformed/out-of-range port without exposing the URL.
        except ValueError:
            raise ValueError("endpoint must be a valid HTTP(S) URL without embedded credentials or fragments") from None
        if not valid_endpoint:
            raise ValueError("endpoint must be a valid HTTP(S) URL without embedded credentials or fragments")
        if type(model) is not str or not model.strip() or len(model) > 256 or any(ord(character) < 32 for character in model):
            raise ValueError("model must be a nonblank string of at most 256 characters")
        if api_key is not None and (type(api_key) is not str or not api_key.strip() or any(ord(character) < 32 for character in api_key)):
            raise ValueError("api_key must be a nonblank string without control characters, or None")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be a finite number greater than 0 and at most 3600")
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, text: str, instruction: str, idempotency_key: str | None = None) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": text}],
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = "Bearer " + self._api_key
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise ProviderError(f"AI request failed (HTTP {code}).") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeError, ValueError):
            raise ProviderError("AI request failed or timed out.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("AI response exceeded the 4 MiB size limit.")
        try:
            body = _decode_json(raw.decode("utf-8"))
            if type(body) is not dict or "error" in body:
                raise ValueError("invalid envelope")
            choices = body["choices"]
            if type(choices) is not list or not choices or type(choices[0]) is not dict:
                raise ValueError("invalid choices")
            choice = choices[0]
            if choice.get("finish_reason") in ("length", "content_filter"):
                raise ValueError("incomplete response")
            message = choice["message"]
            if type(message) is not dict or message.get("refusal"):
                raise ValueError("invalid message")
            content = message["content"]
            if type(content) is not str or not content.strip():
                raise ValueError("missing text")
        except (ValueError, KeyError, TypeError, RecursionError, UnicodeError):
            raise ProviderError("AI endpoint returned an invalid, incomplete, or empty chat response.") from None
        return content

    def extract(self, text: str) -> dict:
        """Interpret Skill prose through the endpoint, then strictly validate its IR."""
        if type(text) is not str or not text.strip():
            raise ValidationError("Skill text must be a nonblank string.")
        content = self._request(text, _EXTRACTION_INSTRUCTION)
        try:
            candidate = _decode_json(_unfence(content))
            return validate_workflow(candidate)
        except (ValueError, TypeError, RecursionError):
            raise ValidationError("AI extraction returned invalid workflow IR.") from None

    def __call__(self, args: dict, context: Any) -> dict:
        """Run an AI step, forwarding only the context's stable idempotency key.

        Whether Idempotency-Key deduplicates requests depends on the server.
        """
        if type(args) is not dict or set(args) - {"text", "instruction"}:
            raise ValidationError("AI task args must contain text and optional instruction only.")
        text = args.get("text")
        if type(text) is not str or not text.strip():
            raise ValidationError("AI task requires a nonblank string args.text.")
        instruction = args.get("instruction", "Follow the user's request and respond with useful plain text.")
        if type(instruction) is not str or not instruction.strip():
            raise ValidationError("AI task instruction must be a nonblank string.")
        idempotency_key = getattr(context, "idempotency_key", None)
        if idempotency_key is not None and (
            type(idempotency_key) is not str
            or not idempotency_key
            or len(idempotency_key) > 256
            or any(ord(character) < 32 or ord(character) > 126 for character in idempotency_key)
        ):
            raise ValidationError("AI task context contains an invalid idempotency key.")
        return {"text": self._request(text, instruction, idempotency_key)}
