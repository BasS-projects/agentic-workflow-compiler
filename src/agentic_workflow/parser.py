"""Compile a structured Skill into validated workflow IR, without execution."""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

from .ir import ValidationError, validate_workflow


@runtime_checkable
class SemanticExtractor(Protocol):
    """A caller-owned semantic interpretation boundary (for example an LLM).

    Implementations return a JSON-compatible dict; all results are validated by
    compile_skill. The built-in extractor does not infer workflows from prose.
    """

    def extract(self, text: str) -> dict:
        ...


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"workflow JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise ValidationError(f"workflow JSON: non-finite number {token!r} is forbidden")


class StructuredFenceExtractor:
    """Extract exactly one Markdown code fence whose info string is workflow-ir."""

    def extract(self, text: str) -> dict:
        fence_char: str | None = None
        fence_length = 0
        target = False
        current: list[str] = []
        blocks: list[str] = []
        opening = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
        for line in text.splitlines():
            if fence_char is None:
                match = opening.match(line)
                if match:
                    marker, info = match.groups()
                    fence_char, fence_length = marker[0], len(marker)
                    target = info == "workflow-ir"
                    current = []
            elif re.fullmatch(r" {0,3}" + re.escape(fence_char) + "{" + str(fence_length) + r",}[ \t]*", line):
                if target:
                    blocks.append("\n".join(current))
                fence_char = None
                target = False
            elif target:
                current.append(line)
        if fence_char is not None and target:
            raise ValidationError("Skill: unterminated workflow-ir fence")
        if len(blocks) != 1:
            raise ValidationError(f"Skill: expected exactly one workflow-ir JSON fence; found {len(blocks)}")
        try:
            return json.loads(blocks[0], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"workflow JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}") from exc
        except ValidationError:
            raise
        except (ValueError, OverflowError) as exc:
            raise ValidationError("workflow JSON: numeric literal exceeds parser limits") from exc
        except RecursionError as exc:
            raise ValidationError("workflow JSON: nesting is too deep") from exc


def compile_skill(text: str, extractor: SemanticExtractor | None = None) -> dict:
    """Extract explicit structure by default, then validate every compiler result."""
    if type(text) is not str:
        raise ValidationError("Skill: expected text")
    if extractor is None:
        extractor = StructuredFenceExtractor()
    if not callable(getattr(extractor, "extract", None)):
        raise ValidationError("extractor must implement extract(text) -> dict")
    return validate_workflow(extractor.extract(text))
