"""Algorithm-R reservoir sampler over an iterator of any element type."""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")


def reservoir_sample(
    stream: Iterable[T], k: int, *, seed: int | None = None
) -> list[T]:
    if k <= 0:
        return []
    rng = random.Random(seed) if seed is not None else random.Random()
    reservoir: list[T] = []
    it = iter(stream)
    for idx in range(k):
        try:
            reservoir.append(next(it))
        except StopIteration:
            return reservoir
    for tail_index, item in enumerate(it, start=k):
        slot = rng.randrange(0, tail_index + 1)
        if slot < k:
            reservoir[slot] = item
    return reservoir


def weighted_walk(
    weights: list[float], length: int, seed: int | None = None
) -> Iterator[int]:
    if not weights:
        return
    rng = random.Random(seed) if seed is not None else random.Random()
    total = sum(weights)
    cumulative = []
    running = 0.0
    for w in weights:
        running += w
        cumulative.append(running / total)
    for _ in range(length):
        r = rng.random()
        for idx, threshold in enumerate(cumulative):
            if r <= threshold:
                yield idx
                break


def windowed_unique(items: Iterable[T], window: int) -> Iterator[T]:
    if window <= 0:
        return
    seen: list[T] = []
    for item in items:
        if item not in seen:
            yield item
        seen.append(item)
        if len(seen) > window:
            seen.pop(0)
