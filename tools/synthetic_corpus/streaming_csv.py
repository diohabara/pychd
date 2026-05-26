"""Streaming CSV-like parser with quoted fields and stateful escaping."""

from __future__ import annotations

from collections.abc import Iterator


def _step(state: str, ch: str, field: list[str], row: list[str]):
    if state == "field":
        if ch == ",":
            row.append("".join(field))
            field.clear()
            return "field"
        if ch == "\n":
            row.append("".join(field))
            field.clear()
            return "emit"
        if ch == '"' and not field:
            return "quoted"
        field.append(ch)
        return "field"
    if state == "quoted":
        if ch == '"':
            return "maybe_close"
        field.append(ch)
        return "quoted"
    if state == "maybe_close":
        if ch == '"':
            field.append('"')
            return "quoted"
        if ch == ",":
            row.append("".join(field))
            field.clear()
            return "field"
        if ch == "\n":
            row.append("".join(field))
            field.clear()
            return "emit"
        # Lone quote outside a quoted field — keep going as raw field.
        field.append(ch)
        return "field"
    raise ValueError(f"bad state {state!r}")


def parse_lines(text: str) -> Iterator[list[str]]:
    state = "field"
    field: list[str] = []
    row: list[str] = []
    for ch in text:
        new_state = _step(state, ch, field, row)
        if new_state == "emit":
            yield row
            row = []
            state = "field"
        else:
            state = new_state
    if field or row:
        row.append("".join(field))
        yield row


def materialise(text: str) -> list[list[str]]:
    return list(parse_lines(text))
