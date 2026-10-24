"""Meterless World Model engine, Python implementation for RiverTruth.

Follows the Meterless World Model spec (engines/world-model/AGENTS.md) and its reference implementation:
content-addressed stable IDs, an event-sourced single-writer store with an ordered audit log, mandatory
provenance on every write, idempotent ingest, bitemporal superseding edges, banded entity resolution,
and fact conflict policies. Standard library only.
"""
from .ids import (canonical_json, context_id_from_attrs, entity_id_from_name, external_id, fact_id,
                  normalize_name, relationship_id, sha256_hex)
from .resolve import decide, levenshtein, name_similarity
from .world import WorldModel

__all__ = ["WorldModel", "canonical_json", "context_id_from_attrs", "decide", "entity_id_from_name", "external_id",
           "fact_id", "levenshtein", "name_similarity", "normalize_name", "relationship_id", "sha256_hex"]
