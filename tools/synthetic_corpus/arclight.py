"""Tiny three-state traffic-light FSM with hysteresis counters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class Phase(Enum):
    GREEN = "g"
    AMBER = "a"
    RED = "r"


_NEXT_PHASE = {
    Phase.GREEN: Phase.AMBER,
    Phase.AMBER: Phase.RED,
    Phase.RED: Phase.GREEN,
}

_PHASE_DWELL = {Phase.GREEN: 25, Phase.AMBER: 4, Phase.RED: 22}


@dataclass
class Arclight:
    phase: Phase = Phase.RED
    tick: int = 0
    cycles: int = 0
    dwell_table: dict[Phase, int] = field(default_factory=lambda: dict(_PHASE_DWELL))

    def step(self) -> Phase:
        self.tick += 1
        if self.tick >= self.dwell_table[self.phase]:
            self.phase = _NEXT_PHASE[self.phase]
            self.tick = 0
            if self.phase == Phase.RED:
                self.cycles += 1
        return self.phase

    def run(self, ticks: int) -> Iterator[Phase]:
        for _ in range(ticks):
            yield self.step()

    def reset(self) -> None:
        self.phase = Phase.RED
        self.tick = 0
        self.cycles = 0


def simulate(seconds: int, override: dict[Phase, int] | None = None) -> list[Phase]:
    lamp = Arclight()
    if override:
        lamp.dwell_table.update(override)
    return list(lamp.run(seconds))
