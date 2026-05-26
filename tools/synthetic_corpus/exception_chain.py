"""Exception-table heavy module with try/except/finally cascades."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


class CapacityError(Exception):
    pass


class RetryExhausted(Exception):
    def __init__(self, attempts: int, last: BaseException | None):
        super().__init__(f"gave up after {attempts} attempts")
        self.attempts = attempts
        self.last = last


@contextmanager
def transient_bucket(capacity: int) -> Iterator[list[int]]:
    bucket: list[int] = []
    try:
        yield bucket
    except CapacityError:
        bucket.clear()
        raise
    finally:
        if len(bucket) > capacity:
            del bucket[capacity:]


def retry_call(attempts: int, fn, *args, **kwargs):
    last_error: BaseException | None = None
    for n in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (TimeoutError, ConnectionError) as exc:
            last_error = exc
            continue
        except CapacityError:
            raise
    raise RetryExhausted(attempts, last_error)


def safe_pipeline(numbers: list[int], divisor: int) -> list[float]:
    out: list[float] = []
    for n in numbers:
        try:
            out.append(n / divisor)
        except ZeroDivisionError:
            out.append(float("inf"))
        except TypeError, ValueError:
            continue
        finally:
            if divisor == 0:
                divisor = 1
    return out
