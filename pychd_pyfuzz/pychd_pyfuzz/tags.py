"""Syntactic-feature tags used by the fuzzer.

Every AST builder registers a tag when it succeeds. The CLI emits a
sidecar ``.tags.json`` per sample so downstream benchmarks can break
recovery rate out by feature ("pychd is at 90 % on match-statements
but 60 % on try_star").

Tag names track the AST node names from ``ast.dump`` for greppability;
we keep them as a flat string set rather than an enum so adding a new
builder does not require a corresponding registry edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TagSet:
    """A mutable, ordered set of syntactic feature tags."""

    _tags: set[str] = field(default_factory=set)

    def add(self, tag: str) -> None:
        self._tags.add(tag)

    def __contains__(self, tag: object) -> bool:
        return tag in self._tags

    def __iter__(self):
        return iter(sorted(self._tags))

    def __len__(self) -> int:
        return len(self._tags)

    def as_sorted_list(self) -> list[str]:
        return sorted(self._tags)


__all__ = ["TagSet"]
