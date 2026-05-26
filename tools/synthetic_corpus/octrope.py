"""Octree-flavoured spatial bucket used by the synthetic corpus."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass(frozen=True)
class Cube:
    cx: float
    cy: float
    cz: float
    half: float

    def contains(self, x: float, y: float, z: float) -> bool:
        return (
            abs(x - self.cx) <= self.half
            and abs(y - self.cy) <= self.half
            and abs(z - self.cz) <= self.half
        )

    def child(self, ix: int, iy: int, iz: int) -> "Cube":
        h = self.half / 2
        return Cube(
            self.cx + (ix - 0.5) * h * 2,
            self.cy + (iy - 0.5) * h * 2,
            self.cz + (iz - 0.5) * h * 2,
            h,
        )


@dataclass
class Bucket:
    bounds: Cube
    capacity: int = 4
    points: list[tuple[float, float, float]] = field(default_factory=list)
    children: list["Bucket"] | None = None

    def _split(self) -> None:
        self.children = [
            Bucket(self.bounds.child(ix, iy, iz), self.capacity)
            for ix in (0, 1)
            for iy in (0, 1)
            for iz in (0, 1)
        ]
        carried = self.points
        self.points = []
        for p in carried:
            self.insert(*p)

    def insert(self, x: float, y: float, z: float) -> bool:
        if not self.bounds.contains(x, y, z):
            return False
        if self.children is None:
            self.points.append((x, y, z))
            if len(self.points) > self.capacity:
                self._split()
            return True
        for c in self.children:
            if c.insert(x, y, z):
                return True
        return False

    def walk(self) -> Iterator[tuple[float, float, float]]:
        if self.children is None:
            yield from self.points
        else:
            for c in self.children:
                yield from c.walk()


def grid_seed(side: int, jitter: float = 0.0) -> Bucket:
    root = Bucket(Cube(0.0, 0.0, 0.0, side / 2))
    for i in range(side):
        for j in range(side):
            for k in range(side):
                root.insert(i + jitter, j + jitter, k + jitter)
    return root
