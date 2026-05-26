"""Self-contained 'protocol vs structural' check used by the corpus."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable


@runtime_checkable
class Sized(Protocol):
    def __len__(self) -> int: ...


@runtime_checkable
class Drawable(Protocol):
    fill: str
    stroke: str

    def bounds(self) -> tuple[float, float, float, float]: ...


def total_size(items: Iterable[Sized]) -> int:
    return sum(len(x) for x in items)


def bounding_box(shapes: Iterable[Drawable]) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = float("inf"), float("inf"), float("-inf"), float("-inf")
    for shape in shapes:
        x0, y0, x1, y1 = shape.bounds()
        if x0 < minx:
            minx = x0
        if y0 < miny:
            miny = y0
        if x1 > maxx:
            maxx = x1
        if y1 > maxy:
            maxy = y1
    return minx, miny, maxx, maxy


class Circle:
    fill: str = "#000"
    stroke: str = "#444"

    def __init__(self, cx: float, cy: float, r: float):
        self.cx, self.cy, self.r = cx, cy, r

    def __len__(self) -> int:
        return int(self.r)

    def bounds(self) -> tuple[float, float, float, float]:
        return (self.cx - self.r, self.cy - self.r, self.cx + self.r, self.cy + self.r)
