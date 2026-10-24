"""Stable, deterministic, content-addressable IDs (spec: docs/stable-ids.md). Same input, same ID, every run."""
import hashlib
import json
import re
import unicodedata


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value) -> str:
    """Deterministic JSON: object keys sorted, None members of objects dropped (JS `undefined` semantics)."""
    if isinstance(value, dict):
        keys = sorted(k for k, v in value.items() if v is not None)
        return "{" + ",".join(json.dumps(k) + ":" + canonical_json(value[k]) for k in keys) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


_ZERO_WIDTH = re.compile("[​-‍﻿]")


def normalize_name(name: str) -> str:
    """NFKD -> strip combining marks -> strip zero-width -> lowercase -> collapse whitespace -> trim.
    Never abbreviates or stems: 'Acme Corp' and 'Acme Corporation' stay different."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _ZERO_WIDTH.sub("", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def external_id(type_: str, system: str, ext_id: str) -> str:
    """Externally keyed entity/context: the literal grammar type:system:externalId."""
    return f"{type_}:{system}:{ext_id}"


def entity_id_from_name(type_: str, name: str) -> str:
    """Name-keyed entity: ent_<first 16 hex of sha256(type:normalizeName(name))>."""
    return "ent_" + sha256_hex(f"{type_}:{normalize_name(name)}")[:16]


def context_id_from_attrs(type_: str, attrs: dict, parent: str | None = None) -> str:
    return "ctx_" + sha256_hex(f"{type_}:{canonical_json(attrs)}:{parent or ''}")[:16]


def relationship_id(from_: str, type_: str, to: str, context: str | None = None, valid_from: str | None = None) -> str:
    """Re-adding the same edge is a no-op."""
    return "rel_" + sha256_hex(f"{from_}|{type_}|{to}|{context or ''}|{valid_from or ''}")[:16]


def fact_id(about: str, predicate: str, value, asserted_at: str, source_kind: str, source_url: str | None = None) -> str:
    """Asserting the identical fact twice is a no-op."""
    return "fact_" + sha256_hex(
        f"{about}|{predicate}|{canonical_json(value)}|{asserted_at}|{source_kind}|{source_url or ''}")[:16]
