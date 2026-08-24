import hashlib
import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def deterministic_sample(items: Sequence[T], count: int, seed: str) -> list[T]:
    """Return a stable pseudo-random sample without modifying the input."""
    if count <= 0 or not items:
        return []
    try:
        unique = list(dict.fromkeys(items))
    except TypeError:
        # Platform API payloads are dictionaries and therefore unhashable. Those
        # endpoints already provide unique product IDs, so preserve their order.
        unique = list(items)
    if len(unique) <= count:
        return unique
    numeric_seed = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:16], "big")
    return random.Random(numeric_seed).sample(unique, count)
