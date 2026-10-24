"""Entity resolution with banded thresholds (spec: merge detection).

    similarity >= 0.85          -> auto-merge   (0.85 exactly merges)
    0.82 <= similarity < 0.85   -> operator review queue (0.82 exactly queues)
    similarity < 0.82           -> new entity
Similarity = 1 - levenshtein / max(len) on NORMALIZED names.
"""
from .ids import normalize_name


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = curr
    return prev[-1]


def name_similarity(raw_a: str, raw_b: str) -> float:
    a, b = normalize_name(raw_a), normalize_name(raw_b)
    m = max(len(a), len(b))
    if m == 0:
        return 1.0
    return 1 - levenshtein(a, b) / m


def decide(similarity: float, auto_merge_at: float = 0.85, review_lower: float = 0.82) -> str:
    if similarity >= auto_merge_at:
        return "auto-merge"
    if similarity >= review_lower:
        return "review"
    return "new"
