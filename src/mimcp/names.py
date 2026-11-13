"""Human-readable session names, Docker-style ``adjective-animal``.

Assigned deterministically from a monotonic counter so names are stable, unique
within a run (up to len(ADJECTIVES) * len(ANIMALS) = 900 before a numeric suffix
is appended), and free of any randomness.
"""

from __future__ import annotations

ADJECTIVES = [
    "swift", "calm", "brave", "clever", "cosmic", "gentle", "jolly", "keen",
    "lively", "mighty", "nimble", "polished", "quiet", "rapid", "shiny",
    "sturdy", "sunny", "tidy", "vivid", "witty", "amber", "azure", "coral",
    "crimson", "golden", "ivory", "jade", "scarlet", "teal", "violet",
]

ANIMALS = [
    "otter", "falcon", "lynx", "panda", "heron", "ibex", "koala", "marmot",
    "narwhal", "ocelot", "puffin", "quokka", "raven", "seal", "tapir",
    "urchin", "viper", "walrus", "yak", "zebra", "badger", "cobra", "dingo",
    "egret", "ferret", "gecko", "hare", "jackal", "kestrel", "lemur",
]

_SPAN = len(ADJECTIVES) * len(ANIMALS)


def name_for_index(i: int) -> str:
    """Deterministic ``adjective-animal`` name for the i-th session (0-based)."""
    adj = ADJECTIVES[i % len(ADJECTIVES)]
    animal = ANIMALS[(i // len(ADJECTIVES)) % len(ANIMALS)]
    base = f"{adj}-{animal}"
    wrap = i // _SPAN
    return base if wrap == 0 else f"{base}-{wrap + 1}"
