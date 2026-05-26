"""PEP 695 generic 'Box' with constrained type parameter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class Box[T: (int, float, str)]:
    def __init__(self, payload: T) -> None:
        self._payload: T = payload
        self._history: list[T] = [payload]

    def get(self) -> T:
        return self._payload

    def set(self, value: T) -> None:
        self._payload = value
        self._history.append(value)

    def history(self) -> tuple[T, ...]:
        return tuple(self._history)


@dataclass
class Ledger[K, V]:
    entries: dict[K, V]

    def post(self, key: K, value: V) -> "Ledger[K, V]":
        return Ledger({**self.entries, key: value})

    def keys_sorted(self) -> list[K]:
        return sorted(self.entries.keys())


def collect_into_box[T: (int, float, str)](seed: T, more: Iterable[T]) -> Box[T]:
    box: Box[T] = Box(seed)
    for item in more:
        box.set(item)
    return box
