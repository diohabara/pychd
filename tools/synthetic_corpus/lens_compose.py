"""Immutable nested-attribute lens with composition."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Generic, TypeVar

S = TypeVar("S")
T = TypeVar("T")
A = TypeVar("A")


@dataclass(frozen=True)
class Lens(Generic[S, A]):
    getter: Callable[[S], A]
    setter: Callable[[S, A], S]

    def view(self, source: S) -> A:
        return self.getter(source)

    def set(self, source: S, value: A) -> S:
        return self.setter(source, value)

    def modify(self, source: S, fn: Callable[[A], A]) -> S:
        return self.setter(source, fn(self.getter(source)))

    def then(self, other: "Lens[A, T]") -> "Lens[S, T]":
        def _get(s: S) -> T:
            return other.getter(self.getter(s))

        def _set(s: S, v: T) -> S:
            return self.setter(s, other.setter(self.getter(s), v))

        return Lens(_get, _set)


def attr_lens(field_name: str) -> Lens[Any, Any]:
    return Lens(
        getter=lambda obj: getattr(obj, field_name),
        setter=lambda obj, val: replace(obj, **{field_name: val}),
    )


def index_lens(key: int) -> Lens[tuple, Any]:
    def _get(seq: tuple) -> Any:
        return seq[key]

    def _set(seq: tuple, val: Any) -> tuple:
        return seq[:key] + (val,) + seq[key + 1 :]

    return Lens(_get, _set)


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Segment:
    start: Point
    end: Point


def translate_segment_x(segment: Segment, dx: float) -> Segment:
    start_x = attr_lens("start").then(attr_lens("x"))
    end_x = attr_lens("end").then(attr_lens("x"))
    moved = start_x.modify(segment, lambda v: v + dx)
    return end_x.modify(moved, lambda v: v + dx)
