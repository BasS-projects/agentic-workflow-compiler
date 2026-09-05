"""Backend extension contracts. No remote backend is implemented in this MVP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class BackendCapabilities:
    """Explicit capabilities used before handing IR to a backend implementation."""

    ir_versions: tuple[str, ...] = ("0.1",)
    step_kinds: frozenset[str] = frozenset({"tool"})
    sequential_steps: bool = True
    conditional_steps: bool = False
    bounded_retries: bool = False
    hard_timeouts: bool = False
    resumable_runs: bool = False
    process_isolation: bool = False
    parallel_steps: bool = False
    loops: bool = False
    human_approval: bool = False


LOCAL_RUNTIME_CAPABILITIES = BackendCapabilities(
    step_kinds=frozenset({"tool", "ai"}),
    conditional_steps=True,
    bounded_retries=True,
    hard_timeouts=True,
    resumable_runs=True,
    process_isolation=True,
)


@runtime_checkable
class BackendAdapter(Protocol):
    """Future compiler target boundary; adapters must reject unsupported features.

    compile produces backend-specific data and must not deploy or run it. Runtime
    execution and authorization remain separate concerns of the caller.
    """

    @property
    def name(self) -> str:
        ...

    @property
    def capabilities(self) -> BackendCapabilities:
        ...

    def compile(self, workflow: dict[str, Any]) -> dict[str, Any]:
        ...
