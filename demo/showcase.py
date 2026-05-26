"""Source file used in the README's demo walkthrough.

The demo decompiles this file end-to-end (rules-only → hybrid →
hybrid-rewrite) and prints the results so reviewers can see what
each tier of recovery actually produces.

If you change this file, re-run ``just demo`` and commit the
refreshed ``demo/expected/*.py`` snapshots so CI keeps the demo
honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Module-level dict comprehension — exercises MAP_ADD recovery
_ALIAS = {old: new for old, new in [("uid", "uuid"), ("msg", "message")]}

# Module-level for-loop — exercises FOR_ITER skip (loop variable
# must NOT leak to module scope)
_REGISTRY: list[str] = []
for _name in ("alpha", "beta", "gamma"):
    _REGISTRY.append(_name.upper())


@dataclass(frozen=True)
class AgentMessage:
    """Frozen dataclass — exercises PEP 749 lazy annotations + decorator with args."""

    type: str
    uuid: str
    agent_id: str
    message: Any = None
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("type must be non-empty")
        object.__setattr__(self, "type", self.type.lower())

    @classmethod
    def from_json(cls, value: dict) -> "AgentMessage":
        """Single-statement classmethod — trivial-body matcher should lift this."""
        return cls(
            type=value["type"],
            uuid=value["uuid"],
            agent_id=value["agentId"],
            message=value.get("message"),
        )


def fibonacci(n: int) -> int:
    """Recursive function with branching body — needs the LLM."""
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


if (
    __name__ == "__main__"
):  # bytecode: LOAD_NAME __name__; LOAD_CONST '__main__'; COMPARE_OP
    msg = AgentMessage.from_json(
        {"type": "Hello", "uuid": "u-1", "agentId": "a-1", "message": "world"}
    )
    print(msg)
