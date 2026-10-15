#!/usr/bin/env python3
"""Deterministic metadata controller for the auto-evolution skill.

This CLI never executes project commands or modifies candidate source. It manages
an append-only event store plus replayable archive/state views, protocol-separated
evaluation evidence, context selection, and Campaign metadata using only the
Python standard library.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
import ntpath
import os
import posixpath
import re
import sqlite3
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable


PROJECT_SCHEMA_VERSION = 3
CANDIDATE_SCHEMA_VERSION = 3
PROTOCOL_EVALUATION_SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 1
CAMPAIGN_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 3
EVENT_SCHEMA_VERSION = 1
SCHEMA_VERSION = PROJECT_SCHEMA_VERSION
MODES = ("repair", "exploit", "explore", "campaign", "research")
ENVIRONMENTS = ("native", "wsl", "container", "remote")
CAMPAIGN_TERMINAL_STATUSES = ("completed", "aborted", "budget_exhausted")
EVALUATION_PROTOCOLS = ("development", "validation", "holdout")
ISOLATION_ENFORCEMENTS = ("trusted_process", "os_acl", "container", "remote")
DESCRIPTOR_POLICIES = ("allow_novel", "exact")


class EvolutionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise EvolutionError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvolutionError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvolutionError(f"Expected a JSON object in {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvolutionError(
                    f"Invalid JSONL at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise EvolutionError(
                    f"Expected a JSON object at {path}:{line_number}"
                )
            records.append(value)
    return records


def atomic_write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for value in values:
                handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def open_event_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            entity_kind TEXT,
            entity_id TEXT,
            UNIQUE(entity_kind, entity_id)
        )
        """
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(events)").fetchall()
    }
    if "entity_kind" not in columns:
        connection.execute("ALTER TABLE events ADD COLUMN entity_kind TEXT")
    if "entity_id" not in columns:
        connection.execute("ALTER TABLE events ADD COLUMN entity_id TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS events_unique_entity "
        "ON events(entity_kind, entity_id) "
        "WHERE entity_kind IS NOT NULL AND entity_id IS NOT NULL"
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS events_no_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """
    )
    connection.commit()
    return connection


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise EvolutionError(
            f"event store is missing: {path}; run migrate-state for legacy state"
        )
    try:
        with closing(open_event_store(path)) as connection:
            rows = connection.execute(
                "SELECT sequence, schema_version, event_type, recorded_at, payload_json "
                "FROM events ORDER BY sequence"
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise EvolutionError(f"event store is corrupt: {path}: {exc}") from exc
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise EvolutionError(
                f"invalid event payload at sequence {row['sequence']}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise EvolutionError(
                f"event payload at sequence {row['sequence']} must be an object"
            )
        events.append(
            {
                "sequence": int(row["sequence"]),
                "schema_version": int(row["schema_version"]),
                "event_type": str(row["event_type"]),
                "recorded_at": str(row["recorded_at"]),
                "payload": payload,
            }
        )
    return events


def append_events(
    path: Path,
    events: Iterable[tuple[str, dict[str, Any], str | None, str | None]],
    expected_last_sequence: int | None = None,
) -> list[int]:
    prepared = list(events)
    if not prepared:
        return []
    sequences: list[int] = []
    try:
        with closing(open_event_store(path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM events"
                ).fetchone()[0]
            )
            if (
                expected_last_sequence is not None
                and current_sequence != expected_last_sequence
            ):
                raise EvolutionError(
                    "event store changed while planning; recompute the proposal "
                    f"(expected {expected_last_sequence}, found {current_sequence})"
                )
            for event_type, payload, entity_kind, entity_id in prepared:
                cursor = connection.execute(
                    "INSERT INTO events(schema_version, event_type, recorded_at, payload_json, "
                    "entity_kind, entity_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        EVENT_SCHEMA_VERSION,
                        event_type,
                        utc_now(),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        entity_kind,
                        entity_id,
                    ),
                )
                sequences.append(int(cursor.lastrowid))
            connection.commit()
    except EvolutionError:
        raise
    except sqlite3.DatabaseError as exc:
        raise EvolutionError(f"failed to append event: {exc}") from exc
    return sequences


def append_event(
    path: Path,
    event_type: str,
    payload: dict[str, Any],
    entity_kind: str | None = None,
    entity_id: str | None = None,
    expected_last_sequence: int | None = None,
) -> int:
    return append_events(
        path,
        [(event_type, payload, entity_kind, entity_id)],
        expected_last_sequence=expected_last_sequence,
    )[0]


def event_store_integrity(path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        with closing(open_event_store(path)) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        return f"error: {exc}"
    return str(row[0]) if row else "unknown"


def portable_is_absolute(raw: str) -> bool:
    if not raw:
        return False
    return PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute()


def portable_normalize(raw: str) -> str:
    if PureWindowsPath(raw).is_absolute():
        value = ntpath.normpath(raw).replace("\\", "/").rstrip("/").casefold()
    else:
        value = posixpath.normpath(raw).rstrip("/")
    return value or "/"


def paths_overlap(left: str, right: str) -> bool:
    a = portable_normalize(left)
    b = portable_normalize(right)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def path_is_within(child: str, parent: str) -> bool:
    normalized_child = portable_normalize(child)
    normalized_parent = portable_normalize(parent)
    return normalized_child == normalized_parent or normalized_child.startswith(
        normalized_parent + "/"
    )


def resolve_state_root(config: dict[str, Any], config_path: Path) -> Path:
    project = config.get("project", {})
    raw = str(project.get("state_root", "")).strip()
    if not raw:
        raise EvolutionError("project.state_root is empty")
    environment = project.get("execution_environment", "native")
    if os.name == "nt" and environment == "wsl" and raw.startswith("/"):
        raise EvolutionError(
            "Run the metadata CLI inside WSL for a WSL project with POSIX state paths"
        )
    candidate = Path(raw)
    if portable_is_absolute(raw):
        return candidate
    project_root = config_path.parent.parent
    return project_root / candidate


def state_paths(config: dict[str, Any], config_path: Path) -> dict[str, Path]:
    root = resolve_state_root(config, config_path)
    return {
        "root": root,
        "archive": root / "archive.jsonl",
        "events": root / "events.sqlite3",
        "evaluations": root / "protocol_evaluations.jsonl",
        "proposals_view": root / "proposals.jsonl",
        "proposals": root / "proposals",
        "mode_state": root / "mode_state.json",
        "context_pack": root / "context_pack.json",
        "failure_summary": root / "failure_summary.json",
        "campaigns": root / "campaigns",
    }


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "attempt_count": 0,
        "valid_candidate_count": 0,
        "global_best_id": None,
        "global_best_score": None,
        "development_best_id": None,
        "development_best_score": None,
        "development_protocol_fingerprint": None,
        "validation_evaluation_count": 0,
        "validation_protocol_fingerprint": None,
        "verified_best_id": None,
        "verified_best_score": None,
        "verified_evaluation_id": None,
        "last_validation": None,
        "holdout_status": "not_run",
        "holdout_evaluation_id": None,
        "holdout_candidate_id": None,
        "holdout_valid": None,
        "holdout_score": None,
        "holdout_protocol_fingerprint": None,
        "cell_elites": {},
        "cell_elite_scores": {},
        "cell_attempts": {},
        "mode_stats": {},
        "context_use_counts": {},
        "parent_use_counts": {},
        "target_cell_attempts": {},
        "target_cell_reservations": {},
        "proposal_counts": {"created": 0, "consumed": 0, "cancelled": 0},
        "valid_score_history": [],
        "active_campaign": None,
        "repeated_bottleneck_rounds": 0,
        "last_bottleneck": None,
        "last_mode": None,
        "last_mode_reason": None,
        "updated_at": None,
    }


def load_project(config_path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    config_path = config_path.resolve()
    config = load_json(config_path)
    return config, state_paths(config, config_path)


def numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_evaluation_evidence(
    evaluation: dict[str, Any],
    config: dict[str, Any],
    expected_protocol: str,
    require_descriptors: bool,
) -> list[str]:
    errors: list[str] = []
    if evaluation.get("protocol") != expected_protocol:
        errors.append(f"evaluation.protocol must be {expected_protocol!r}")
    if not str(evaluation.get("protocol_id", "")).strip():
        errors.append("evaluation.protocol_id is required")
    provenance = evaluation.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("evaluation.provenance must be an object")
        provenance = {}
    expected_dataset = str(config.get("datasets", {}).get(expected_protocol, "")).strip()
    if provenance.get("dataset_id") != expected_dataset:
        errors.append(
            f"evaluation.provenance.dataset_id must equal configured {expected_protocol} dataset"
        )
    for key in ("dataset_digest", "evaluator_id", "evaluator_digest", "command_digest"):
        if not str(provenance.get(key, "")).strip():
            errors.append(f"evaluation.provenance.{key} is required")
    environment = provenance.get("environment")
    if not isinstance(environment, dict) or not environment:
        errors.append("evaluation.provenance.environment must be a nonempty object")
    samples = evaluation.get("samples")
    if not isinstance(samples, list) or not all(numeric(value) for value in samples):
        errors.append("evaluation.samples must be a finite numeric list")
        samples = []
    aggregation = evaluation.get("aggregation")
    if not isinstance(aggregation, dict):
        errors.append("evaluation.aggregation must be an object")
        aggregation = {}
    if not str(aggregation.get("method", "")).strip():
        errors.append("evaluation.aggregation.method is required")

    valid = evaluation.get("valid") is True
    primary_score = evaluation.get("primary_score")
    aggregate_value = aggregation.get("value")
    if valid:
        if not samples:
            errors.append("valid evaluation requires at least one measurement sample")
        if not numeric(aggregate_value):
            errors.append("valid evaluation requires finite aggregation.value")
        elif numeric(primary_score) and not math.isclose(
            float(aggregate_value), float(primary_score), rel_tol=1e-12, abs_tol=1e-15
        ):
            errors.append("evaluation.aggregation.value must equal primary_score")
    elif aggregate_value is not None and not numeric(aggregate_value):
        errors.append("invalid evaluation aggregation.value must be null or finite numeric")

    if require_descriptors and not isinstance(evaluation.get("descriptors"), dict):
        errors.append("evaluation.descriptors must be an object")
    return errors


def better(new: float, old: float | None, direction: str, noise_band: float) -> bool:
    if old is None:
        return True
    threshold = max(abs(old), 1e-12) * max(noise_band, 0.0)
    if direction == "maximize":
        return new - old > threshold
    return old - new > threshold


def reaches_target(score: float | None, target: Any, direction: str) -> bool:
    if score is None or not numeric(target):
        return False
    return score >= target if direction == "maximize" else score <= target


def quality_sort_key(record: dict[str, Any], direction: str) -> tuple[float, int]:
    score = record.get("evaluation", {}).get("primary_score")
    if not numeric(score):
        return (float("inf"), int(record.get("sequence", 0)))
    return ((-float(score) if direction == "maximize" else float(score)), int(record.get("sequence", 0)))


def hard_constraints_pass(record: dict[str, Any], config: dict[str, Any]) -> bool:
    evaluation = record.get("evaluation", {})
    if evaluation.get("valid") is not True:
        return False
    hard = evaluation.get("hard_constraints", {})
    required = config.get("evaluation", {}).get("hard_constraints", [])
    return all(hard.get(name) is True for name in required)


def descriptors_complete(record: dict[str, Any], config: dict[str, Any]) -> bool:
    descriptors = record.get("evaluation", {}).get("descriptors", {})
    keys = config.get("evaluation", {}).get("descriptor_keys", [])
    return all(key in descriptors and descriptors[key] not in (None, "") for key in keys)


def cell_key(record: dict[str, Any], config: dict[str, Any]) -> str:
    descriptors = record.get("evaluation", {}).get("descriptors", {})
    return cell_key_from_descriptors(descriptors, config)


def cell_key_from_descriptors(
    descriptors: dict[str, Any], config: dict[str, Any]
) -> str:
    keys = config.get("evaluation", {}).get("descriptor_keys", [])
    cell = {key: descriptors.get(key, "__missing__") for key in keys}
    return json.dumps(cell, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_intended_descriptors(
    descriptors: Any, config: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    if not isinstance(descriptors, dict):
        return ["intended_descriptors must be an object"]
    keys = config["evaluation"]["descriptor_keys"]
    if set(descriptors) != set(keys):
        errors.append(
            "intended_descriptors must contain exactly the configured descriptor_keys"
        )
    space = config["evaluation"].get("descriptor_space", {})
    for key in keys:
        if descriptors.get(key) in (None, ""):
            errors.append(f"intended descriptor is missing: {key}")
        allowed = space.get(key)
        if allowed and descriptors.get(key) not in allowed:
            errors.append(
                f"intended descriptor {key}={descriptors.get(key)!r} is outside descriptor_space"
            )
    return errors


def descriptor_space_cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    keys = config["evaluation"]["descriptor_keys"]
    space = config["evaluation"].get("descriptor_space", {})
    if not all(isinstance(space.get(key), list) and space[key] for key in keys):
        return []
    return [
        dict(zip(keys, values))
        for values in itertools.product(*(space[key] for key in keys))
    ]


def campaign_current_milestone(campaign: dict[str, Any]) -> dict[str, Any] | None:
    if campaign.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        return None
    index = int(campaign.get("current_milestone_index", 0))
    milestones = campaign.get("milestones", [])
    if 0 <= index < len(milestones):
        return milestones[index]
    return None


def choose_target_descriptors(
    mode: str,
    parent: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
    campaign: dict[str, Any] | None = None,
    explicit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    if explicit is not None:
        errors = validate_intended_descriptors(explicit, config)
        if errors:
            raise EvolutionError("Invalid explicit target:\n- " + "\n- ".join(errors))
        return copy.deepcopy(explicit), "explicit_target"
    if mode == "campaign":
        if campaign is None:
            raise EvolutionError("Campaign target selection requires an active campaign")
        milestone = campaign_current_milestone(campaign)
        if milestone is None:
            raise EvolutionError("Active Campaign has no remaining v2 milestone")
        target = milestone.get("target_descriptors") or campaign.get("target_descriptors")
        errors = validate_intended_descriptors(target, config)
        if errors:
            raise EvolutionError("Campaign target is invalid:\n- " + "\n- ".join(errors))
        return copy.deepcopy(target), "campaign_milestone_target"
    if mode == "explore":
        cells = descriptor_space_cells(config)
        if not cells:
            raise EvolutionError(
                "Explore target selection requires a complete descriptor_space or "
                "--target-descriptors"
            )
        elites = state.get("cell_elites", {})
        attempts = state.get("target_cell_attempts", {})
        reservations = state.get("target_cell_reservations", {})
        ranked = sorted(
            cells,
            key=lambda descriptors: (
                cell_key_from_descriptors(descriptors, config) in elites,
                int(attempts.get(cell_key_from_descriptors(descriptors, config), 0))
                + int(
                    reservations.get(
                        cell_key_from_descriptors(descriptors, config), 0
                    )
                ),
                cell_key_from_descriptors(descriptors, config),
            ),
        )
        return ranked[0], "least_attempted_or_uncovered_cell"
    parent_descriptors = parent.get("evaluation", {}).get("descriptors", {})
    errors = validate_intended_descriptors(parent_descriptors, config)
    if errors:
        raise EvolutionError("Parent descriptors cannot define target")
    return copy.deepcopy(parent_descriptors), "parent_cell"


def valid_records(records: Iterable[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if hard_constraints_pass(record, config)
        and descriptors_complete(record, config)
        and numeric(record.get("evaluation", {}).get("primary_score"))
    ]


def repair_records(
    records: Iterable[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    metric = config.get("evaluation", {}).get("repair_metric")
    if not isinstance(metric, str) or not metric:
        return []
    return [
        record
        for record in records
        if record.get("evaluation", {}).get("valid") is False
        and numeric(
            record.get("evaluation", {})
            .get("auxiliary_metrics", {})
            .get(metric)
        )
        and record.get("evaluation", {})
        .get("hard_constraints", {})
        .get("build_passed")
        is True
        and record.get("evaluation", {})
        .get("hard_constraints", {})
        .get("scope_passed")
        is True
    ]


def protocol_identity(evaluation: dict[str, Any]) -> dict[str, Any]:
    provenance = evaluation.get("provenance") or {}
    return {
        "protocol": evaluation.get("protocol"),
        "protocol_id": evaluation.get("protocol_id"),
        "dataset_id": provenance.get("dataset_id"),
        "dataset_digest": provenance.get("dataset_digest"),
        "evaluator_id": provenance.get("evaluator_id"),
        "evaluator_digest": provenance.get("evaluator_digest"),
        "command_digest": provenance.get("command_digest"),
    }


def identity_fingerprint(identity: dict[str, Any]) -> str | None:
    if any(value in (None, "") for value in identity.values()):
        return None
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def protocol_fingerprint(evaluation: dict[str, Any]) -> str | None:
    return identity_fingerprint(protocol_identity(evaluation))


def protocol_matches(state: dict[str, Any], fingerprint: str | None) -> bool:
    existing = state.get("development_protocol_fingerprint")
    if not existing or fingerprint == existing:
        return True
    return any(item["old_fingerprint"] == existing and item["new_fingerprint"] == fingerprint
               for item in state.get("protocol_compatibilities", []))


def validate_compatibility(record: dict[str, Any], state: dict[str, Any]) -> None:
    if (record.get("schema_version") != 1 or not record.get("compatibility_id")
            or not str(record.get("authorization", "")).strip()
            or not str(record.get("reason", "")).strip()):
        raise EvolutionError("Compatibility requires ID, authorization and reason")
    old, new = record.get("old_identity"), record.get("new_identity")
    keys = set(protocol_identity({}))
    if not isinstance(old, dict) or not isinstance(new, dict) or set(old) != keys or set(new) != keys:
        raise EvolutionError("Compatibility requires complete protocol identities")
    if old.get("protocol") != "development" or any(not isinstance(v,str) or not v for v in [*old.values(),*new.values()]):
        raise EvolutionError("Compatibility supports complete development identities only")
    changed = {key for key in keys if old[key] != new[key]}
    if changed != {"evaluator_digest"}:
        raise EvolutionError("Compatibility may only change evaluator_digest")
    for obj in (old,new):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}",obj["evaluator_digest"]):
            raise EvolutionError("Invalid evaluator digest")
    if (identity_fingerprint(old) != state.get("development_protocol_fingerprint")
            or record.get("old_fingerprint") != identity_fingerprint(old)
            or record.get("new_fingerprint") != identity_fingerprint(new)):
        raise EvolutionError("Compatibility fingerprint does not match established protocol")
    if any(item["compatibility_id"] == record["compatibility_id"] or item["new_fingerprint"] == record["new_fingerprint"]
           for item in state.get("protocol_compatibilities", [])):
        raise EvolutionError("Compatibility already registered")
    evidence = record.get("evidence")
    if not isinstance(evidence,list) or not evidence:
        raise EvolutionError("Compatibility requires hashed review evidence")
    for item in evidence:
        if (not isinstance(item,dict) or not Path(item.get("path","")).is_absolute()
                or not re.fullmatch(r"[0-9a-f]{64}",item.get("sha256",""))):
            raise EvolutionError("Invalid compatibility evidence")


def cmd_register_compatibility(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    events = read_events(paths["events"])
    _, _, proposals, state, _ = replay_events(config, events)
    if any(item.get("status") == "open" for item in proposals) or state.get("active_campaign"):
        raise EvolutionError("Close proposals and Campaign before registering compatibility")
    record = load_json(Path(args.record).resolve())
    validate_compatibility(record,state)
    for item in record["evidence"]:
        if sha256_file(Path(item["path"])) != item["sha256"]:
            raise EvolutionError("Compatibility evidence changed: " + item["path"])
    sequence, _ = commit_event_and_rebuild(config,paths,"protocol_compatibility_registered",{"record":record},
        entity_kind="protocol_compatibility",entity_id=record["compatibility_id"],
        expected_last_sequence=events[-1]["sequence"] if events else 0)
    emit({"registered":True,"sequence":sequence,"compatibility_id":record["compatibility_id"],
          "old_fingerprint":record["old_fingerprint"],"new_fingerprint":record["new_fingerprint"]})


def apply_candidate_record(
    state: dict[str, Any], record: dict[str, Any], config: dict[str, Any]
) -> tuple[bool, bool]:
    fingerprint = protocol_fingerprint(record.get("evaluation", {}))
    existing_fingerprint = state.get("development_protocol_fingerprint")
    if fingerprint and existing_fingerprint and not protocol_matches(state, fingerprint):
        raise EvolutionError(
            "development protocol fingerprint changed within one event store"
        )
    if fingerprint and not existing_fingerprint:
        state["development_protocol_fingerprint"] = fingerprint
    mode = record["mode"]
    mode_stats = state.setdefault("mode_stats", {}).setdefault(
        mode,
        {"attempts": 0, "valid": 0, "cell_promotions": 0, "global_promotions": 0},
    )
    mode_stats["attempts"] += 1
    state["attempt_count"] += 1
    cell = record["cell_key"]
    state.setdefault("cell_attempts", {})[cell] = int(
        state.setdefault("cell_attempts", {}).get(cell, 0)
    ) + 1

    evaluation = record["evaluation"]
    promoted_cell = False
    promoted_global = False
    if hard_constraints_pass(record, config) and descriptors_complete(record, config):
        score = float(evaluation["primary_score"])
        direction = config["evaluation"]["direction"]
        noise = float(config["evaluation"].get("noise_band", 0.0))
        floor = config["evaluation"].get("minimum_cell_quality")
        floor_pass = True
        if numeric(floor):
            floor_pass = score >= floor if direction == "maximize" else score <= floor
        state["valid_candidate_count"] += 1
        mode_stats["valid"] += 1
        state.setdefault("valid_score_history", []).append(
            {"candidate_id": record["candidate_id"], "score": score}
        )
        old_cell_score = state.setdefault("cell_elite_scores", {}).get(cell)
        if floor_pass and better(score, old_cell_score, direction, noise):
            state.setdefault("cell_elites", {})[cell] = record["candidate_id"]
            state["cell_elite_scores"][cell] = score
            mode_stats["cell_promotions"] += 1
            promoted_cell = True
        if better(score, state.get("global_best_score"), direction, noise):
            state["global_best_id"] = record["candidate_id"]
            state["global_best_score"] = score
            state["development_best_id"] = record["candidate_id"]
            state["development_best_score"] = score
            mode_stats["global_promotions"] += 1
            promoted_global = True

    bottlenecks = record.get("bottlenecks") or []
    failure = evaluation.get("failure") or {}
    current_bottleneck = bottlenecks[0] if bottlenecks else failure.get("category")
    if current_bottleneck:
        if current_bottleneck == state.get("last_bottleneck") and not promoted_global:
            state["repeated_bottleneck_rounds"] = int(
                state.get("repeated_bottleneck_rounds", 0)
            ) + 1
        else:
            state["repeated_bottleneck_rounds"] = 1
        state["last_bottleneck"] = current_bottleneck
    elif promoted_global:
        state["repeated_bottleneck_rounds"] = 0
        state["last_bottleneck"] = None
    state["last_mode"] = mode
    return promoted_cell, promoted_global


def apply_protocol_evaluation(
    state: dict[str, Any], record: dict[str, Any], config: dict[str, Any]
) -> bool:
    protocol = record["protocol"]
    evaluation = record["evaluation"]
    fingerprint = protocol_fingerprint(evaluation)
    fingerprint_key = f"{protocol}_protocol_fingerprint"
    existing_fingerprint = state.get(fingerprint_key)
    if fingerprint and existing_fingerprint and fingerprint != existing_fingerprint:
        raise EvolutionError(
            f"{protocol} protocol fingerprint changed within one event store"
        )
    if fingerprint and not existing_fingerprint:
        state[fingerprint_key] = fingerprint
    promoted = False
    if protocol == "validation":
        state["validation_evaluation_count"] += 1
        state["last_validation"] = {
            "evaluation_id": record["evaluation_id"],
            "candidate_id": record["candidate_id"],
            "valid": evaluation["valid"],
            "score": evaluation.get("primary_score"),
        }
        if hard_constraints_pass(record, config) and numeric(evaluation.get("primary_score")):
            score = float(evaluation["primary_score"])
            if better(
                score,
                state.get("verified_best_score"),
                config["evaluation"]["direction"],
                float(config["evaluation"].get("noise_band", 0.0)),
            ):
                state["verified_best_id"] = record["candidate_id"]
                state["verified_best_score"] = score
                state["verified_evaluation_id"] = record["evaluation_id"]
                promoted = True
    elif protocol == "holdout":
        state["holdout_status"] = "completed"
        state["holdout_evaluation_id"] = record["evaluation_id"]
        state["holdout_candidate_id"] = record["candidate_id"]
        state["holdout_valid"] = evaluation["valid"]
        state["holdout_score"] = evaluation.get("primary_score")
    return promoted


def validate_campaign_budget_correction(record, state, campaigns):
    """Validate an explicitly authorized accounting correction, never a timer reset."""
    if not isinstance(record, dict):
        raise EvolutionError("Campaign budget correction requires a record")
    campaign_id = record.get("campaign_id")
    campaign = campaigns.get(campaign_id)
    active = state.get("active_campaign") or {}
    if not campaign or campaign.get("status") != "active" or active.get("campaign_id") != campaign_id:
        raise EvolutionError("Budget correction requires the named active Campaign")
    if campaign.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        raise EvolutionError("Budget correction requires the current Campaign schema")
    if not str(record.get("authorization", "")).strip() or not str(record.get("reason", "")).strip():
        raise EvolutionError("Budget correction requires explicit authorization and reason")
    old = record.get("old_budget")
    new = record.get("new_budget")
    if old != campaign["budget"]:
        raise EvolutionError("Budget correction old_budget does not match current budget")
    if not isinstance(new, dict) or set(new) != set(old):
        raise EvolutionError("Budget correction must preserve the budget fields")
    if any(new[k] != old[k] for k in old if k != "max_evaluator_runs"):
        raise EvolutionError("Only max_evaluator_runs may change; candidate/time limits are frozen")
    value = new["max_evaluator_runs"]
    if type(value) is not int or value <= 0 or value == old["max_evaluator_runs"]:
        raise EvolutionError("Corrected max_evaluator_runs must be a different positive integer")
    if value < int(campaign.get("budget_used", {}).get("evaluator_runs", 0)):
        raise EvolutionError("Corrected budget cannot be below recorded usage")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise EvolutionError("Budget correction requires hashed authorization evidence")
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise EvolutionError("Invalid budget correction evidence reference")


def cmd_campaign_correct_budget(args):
    config, paths = load_project(Path(args.config))
    events = read_events(paths["events"])
    _, _, _, state, campaigns = replay_events(config, events)
    record = load_json(Path(args.record).resolve())
    validate_campaign_budget_correction(record, state, campaigns)
    for item in record["evidence"]:
        if sha256_file(Path(item["path"])) != item["sha256"]:
            raise EvolutionError("Budget correction evidence changed: " + item["path"])
    sequence, _ = commit_event_and_rebuild(
        config, paths, "campaign_budget_corrected", {"record": record},
        entity_kind="campaign_budget_correction", entity_id=canonical_digest(record),
        expected_last_sequence=events[-1]["sequence"] if events else 0,
    )
    emit({"corrected": True, "sequence": sequence, "campaign_id": record["campaign_id"],
          "old_budget": record["old_budget"], "new_budget": record["new_budget"]})


def replay_events(
    config: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    records: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    state = empty_state()
    campaigns: dict[str, dict[str, Any]] = {}
    candidate_ids: set[str] = set()
    evaluation_ids: set[str] = set()
    proposals_by_id: dict[str, dict[str, Any]] = {}

    for event in events:
        if event.get("schema_version") != EVENT_SCHEMA_VERSION:
            raise EvolutionError(
                f"unsupported event schema at sequence {event.get('sequence')}"
            )
        event_type = event["event_type"]
        payload = event["payload"]
        recorded_at = event["recorded_at"]
        if event_type == "candidate_recorded":
            record = copy.deepcopy(payload.get("record"))
            if not isinstance(record, dict):
                raise EvolutionError("candidate_recorded event requires record object")
            candidate_id = str(record.get("candidate_id", ""))
            if not candidate_id or candidate_id in candidate_ids:
                raise EvolutionError(f"duplicate or missing candidate ID in events: {candidate_id!r}")
            if record.get("schema_version") == CANDIDATE_SCHEMA_VERSION:
                candidate_errors = validate_candidate_result(record, config)
                if candidate_errors:
                    raise EvolutionError(
                        f"invalid candidate event {candidate_id}: "
                        + "; ".join(candidate_errors)
                    )
            elif record.get("schema_version") not in (1, 2):
                raise EvolutionError(
                    f"unsupported candidate schema in events: {record.get('schema_version')}"
                )
            parent_id = record.get("parent_id")
            if parent_id is not None and parent_id not in candidate_ids:
                raise EvolutionError(
                    f"candidate event parent precedes or is absent from events: {parent_id}"
                )
            if record.get("schema_version") == CANDIDATE_SCHEMA_VERSION:
                proposal_id = record.get("proposal_id")
                seed = payload.get("seed") is True
                if seed:
                    if records or proposal_id is not None:
                        raise EvolutionError("seed event must be the first candidate and unbound")
                else:
                    proposal = proposals_by_id.get(str(proposal_id))
                    if proposal is None or proposal.get("status") != "open":
                        raise EvolutionError(
                            f"candidate event does not reference an open proposal: {proposal_id}"
                        )
                    binding_errors = validate_candidate_binding(record, proposal, config)
                    if binding_errors:
                        raise EvolutionError(
                            f"candidate event violates proposal {proposal_id}: "
                            + "; ".join(binding_errors)
                        )
            candidate_ids.add(candidate_id)
            record["sequence"] = len(records) + 1
            record["recorded_at"] = recorded_at
            if not str(record.get("cell_key", "")).strip():
                record["cell_key"] = cell_key(record, config)
            records.append(record)
            apply_candidate_record(state, record, config)
        elif event_type == "protocol_compatibility_registered":
            record = copy.deepcopy(payload.get("record"))
            if not isinstance(record, dict):
                raise EvolutionError("Compatibility event requires record")
            if any(item.get("status") == "open" for item in proposals) or state.get("active_campaign"):
                raise EvolutionError("Compatibility event overlaps active work")
            validate_compatibility(record, state)
            state.setdefault("protocol_compatibilities", []).append(record)
        elif event_type == "protocol_evaluation_recorded":
            record = copy.deepcopy(payload.get("record"))
            if not isinstance(record, dict):
                raise EvolutionError("protocol_evaluation_recorded event requires record object")
            evaluation_id = str(record.get("evaluation_id", ""))
            if not evaluation_id or evaluation_id in evaluation_ids:
                raise EvolutionError(
                    f"duplicate or missing evaluation ID in events: {evaluation_id!r}"
                )
            evaluation_errors = validate_protocol_evaluation_result(record, config)
            if evaluation_errors:
                raise EvolutionError(
                    f"invalid protocol evaluation event {evaluation_id}: "
                    + "; ".join(evaluation_errors)
                )
            if record.get("candidate_id") not in candidate_ids:
                raise EvolutionError(
                    "protocol evaluation candidate precedes or is absent from events: "
                    f"{record.get('candidate_id')}"
                )
            evaluation_ids.add(evaluation_id)
            record["sequence"] = len(evaluations) + 1
            record["recorded_at"] = recorded_at
            evaluations.append(record)
            apply_protocol_evaluation(state, record, config)
        elif event_type == "proposal_created":
            proposal = copy.deepcopy(payload.get("proposal"))
            if not isinstance(proposal, dict):
                raise EvolutionError("proposal_created event requires proposal object")
            proposal_id = str(proposal.get("proposal_id", ""))
            if not proposal_id or proposal_id in proposals_by_id:
                raise EvolutionError(f"duplicate or missing proposal ID: {proposal_id!r}")
            if proposal.get("base_event_sequence") != int(event["sequence"]) - 1:
                raise EvolutionError(
                    f"proposal {proposal_id} was not based on the immediately preceding event"
                )
            proposal_errors = validate_proposal_record(proposal, config, records, campaigns)
            if proposal_errors:
                raise EvolutionError(
                    f"invalid proposal event {proposal_id}: " + "; ".join(proposal_errors)
                )
            proposal["status"] = "open"
            proposal["created_at"] = recorded_at
            proposal["consumed_at"] = None
            proposal["candidate_id"] = None
            proposals.append(proposal)
            proposals_by_id[proposal_id] = proposal
            state["proposal_counts"]["created"] += 1
            target = proposal["target_cell"]
            reservations = state["target_cell_reservations"]
            reservations[target] = int(reservations.get(target, 0)) + 1
        elif event_type == "proposal_consumed":
            proposal_id = str(payload.get("proposal_id", ""))
            proposal = proposals_by_id.get(proposal_id)
            if proposal is None or proposal.get("status") != "open":
                raise EvolutionError(
                    f"proposal consumption requires an open proposal: {proposal_id}"
                )
            candidate_id = str(payload.get("candidate_id", ""))
            if not records or records[-1].get("candidate_id") != candidate_id:
                raise EvolutionError(
                    f"proposal consumption is not paired with candidate event: {candidate_id}"
                )
            proposal["status"] = "consumed"
            proposal["consumed_at"] = recorded_at
            proposal["candidate_id"] = candidate_id
            state["proposal_counts"]["consumed"] += 1
            target = proposal["target_cell"]
            reservations = state["target_cell_reservations"]
            reservations[target] = max(int(reservations.get(target, 0)) - 1, 0)
            attempts = state["target_cell_attempts"]
            attempts[target] = int(attempts.get(target, 0)) + 1
            parent_id = proposal["parent"]["candidate_id"]
            parent_uses = state["parent_use_counts"]
            parent_uses[parent_id] = int(parent_uses.get(parent_id, 0)) + 1
            context_uses = state["context_use_counts"]
            for inspiration_id in proposal["context"].get("inspiration_ids", []):
                context_uses[inspiration_id] = int(
                    context_uses.get(inspiration_id, 0)
                ) + 1
            campaign_id = proposal.get("campaign_id")
            if campaign_id is not None:
                campaign = campaigns.get(str(campaign_id))
                if campaign is None or campaign.get("status") != "active":
                    raise EvolutionError(
                        f"consumed proposal Campaign is not active: {campaign_id}"
                    )
                used = campaign.setdefault(
                    "budget_used", {"candidates": 0, "evaluator_runs": 0}
                )
                used["candidates"] = int(used.get("candidates", 0)) + 1
                samples = records[-1].get("evaluation", {}).get("samples", [])
                used["evaluator_runs"] = int(used.get("evaluator_runs", 0)) + max(
                    len(samples), 1
                )
                milestone_usage = campaign.setdefault("milestone_usage", {})
                milestone_id = str(proposal.get("milestone_id"))
                item = milestone_usage.setdefault(
                    milestone_id, {"candidates": 0, "evaluator_runs": 0}
                )
                item["candidates"] += 1
                item["evaluator_runs"] += max(len(samples), 1)
        elif event_type == "proposal_cancelled":
            proposal_id = str(payload.get("proposal_id", ""))
            proposal = proposals_by_id.get(proposal_id)
            if proposal is None or proposal.get("status") != "open":
                raise EvolutionError(
                    f"proposal cancellation requires an open proposal: {proposal_id}"
                )
            proposal["status"] = "cancelled"
            proposal["cancelled_at"] = recorded_at
            proposal["cancel_reason"] = payload.get("reason", "")
            state["proposal_counts"]["cancelled"] += 1
            target = proposal["target_cell"]
            reservations = state["target_cell_reservations"]
            reservations[target] = max(int(reservations.get(target, 0)) - 1, 0)
        elif event_type == "mode_selected":
            state["last_mode"] = payload.get("mode")
            state["last_mode_reason"] = payload.get("reason")
        elif event_type == "context_selected":
            counts = state.setdefault("context_use_counts", {})
            for candidate_id in payload.get("inspiration_ids", []):
                counts[candidate_id] = int(counts.get(candidate_id, 0)) + 1
        elif event_type == "campaign_started":
            campaign = copy.deepcopy(payload.get("campaign"))
            if not isinstance(campaign, dict):
                raise EvolutionError("campaign_started event requires campaign object")
            campaign_id = str(campaign["campaign_id"])
            campaigns[campaign_id] = campaign
            if campaign.get("schema_version") == CAMPAIGN_SCHEMA_VERSION:
                milestone = campaign_current_milestone(campaign)
                state["active_campaign"] = {
                    "campaign_id": campaign_id,
                    "head_candidate_id": campaign["head_candidate_id"],
                    "step": int(campaign.get("step", 0)),
                    "milestone_id": milestone.get("milestone_id")
                    if milestone
                    else None,
                    "milestone_count": len(campaign["milestones"]),
                    "budget": copy.deepcopy(campaign["budget"]),
                }
            else:
                state["active_campaign"] = {
                    "campaign_id": campaign_id,
                    "head_candidate_id": campaign["head_candidate_id"],
                    "step": int(campaign.get("step", 0)),
                    "max_steps": int(campaign["max_steps"]),
                }
        elif event_type == "campaign_budget_corrected":
            record = copy.deepcopy(payload.get("record"))
            validate_campaign_budget_correction(record, state, campaigns)
            campaign = campaigns[record["campaign_id"]]
            campaign["budget"] = copy.deepcopy(record["new_budget"])
            campaign.setdefault("budget_corrections", []).append(
                {"record": record, "recorded_at": recorded_at}
            )
            campaign["updated_at"] = recorded_at
            state["active_campaign"]["budget"] = copy.deepcopy(record["new_budget"])
        elif event_type == "campaign_advanced":
            campaign_id = str(payload.get("campaign_id", ""))
            campaign = campaigns.get(campaign_id)
            if campaign is None:
                raise EvolutionError(f"campaign advance precedes start: {campaign_id}")
            new_step = int(campaign.get("step", 0)) + 1
            if campaign.get("schema_version") == CAMPAIGN_SCHEMA_VERSION:
                milestone = campaign_current_milestone(campaign)
                if milestone is None or payload.get("milestone_id") != milestone.get(
                    "milestone_id"
                ):
                    raise EvolutionError("campaign advance milestone is out of order")
                milestone_label = milestone["milestone_id"]
            else:
                milestone_label = payload.get("milestone", "legacy")
            campaign.setdefault("history", []).append(
                {
                    "step": new_step,
                    "candidate_id": payload["candidate_id"],
                    "milestone_id": milestone_label,
                    "note": payload.get("note", ""),
                    "recorded_at": recorded_at,
                }
            )
            campaign["step"] = new_step
            campaign["head_candidate_id"] = payload["candidate_id"]
            campaign["updated_at"] = recorded_at
            if campaign.get("schema_version") == CAMPAIGN_SCHEMA_VERSION:
                campaign["current_milestone_index"] = new_step
                campaign["milestone_start_candidate_id"] = payload["candidate_id"]
                next_milestone = campaign_current_milestone(campaign)
                state["active_campaign"] = {
                    "campaign_id": campaign_id,
                    "head_candidate_id": payload["candidate_id"],
                    "step": new_step,
                    "milestone_id": next_milestone.get("milestone_id")
                    if next_milestone
                    else None,
                    "milestone_count": len(campaign["milestones"]),
                    "budget": copy.deepcopy(campaign["budget"]),
                }
            else:
                state["active_campaign"] = {
                    "campaign_id": campaign_id,
                    "head_candidate_id": payload["candidate_id"],
                    "step": new_step,
                    "max_steps": campaign["max_steps"],
                }
        elif event_type == "campaign_closed":
            campaign_id = str(payload.get("campaign_id", ""))
            campaign = campaigns.get(campaign_id)
            if campaign is None:
                raise EvolutionError(f"campaign close precedes start: {campaign_id}")
            campaign["status"] = payload["status"]
            campaign["close_reason"] = payload["reason"]
            campaign["closed_at"] = recorded_at
            campaign["updated_at"] = recorded_at
            state["active_campaign"] = None
        elif event_type == "campaign_snapshot_imported":
            campaign = copy.deepcopy(payload.get("campaign"))
            if not isinstance(campaign, dict) or not campaign.get("campaign_id"):
                raise EvolutionError(
                    "campaign_snapshot_imported event requires campaign object"
                )
            campaign_id = str(campaign["campaign_id"])
            campaigns[campaign_id] = campaign
            if campaign.get("status") == "active":
                if campaign.get("schema_version") == CAMPAIGN_SCHEMA_VERSION:
                    milestone = campaign_current_milestone(campaign)
                    state["active_campaign"] = {
                        "campaign_id": campaign_id,
                        "head_candidate_id": campaign["head_candidate_id"],
                        "step": int(campaign.get("step", 0)),
                        "milestone_id": milestone.get("milestone_id")
                        if milestone
                        else None,
                        "milestone_count": len(campaign["milestones"]),
                        "budget": copy.deepcopy(campaign["budget"]),
                    }
                else:
                    state["active_campaign"] = {
                        "campaign_id": campaign_id,
                        "head_candidate_id": campaign["head_candidate_id"],
                        "step": int(campaign.get("step", 0)),
                        "max_steps": int(campaign["max_steps"]),
                    }
        elif event_type == "legacy_state_imported":
            state["context_use_counts"] = copy.deepcopy(
                payload.get("context_use_counts", {})
            )
            state["last_mode"] = payload.get("last_mode", state.get("last_mode"))
            state["last_mode_reason"] = payload.get("last_mode_reason")
        else:
            raise EvolutionError(
                f"unknown event type at sequence {event.get('sequence')}: {event_type}"
            )
        state["updated_at"] = recorded_at
    return records, evaluations, proposals, state, campaigns


def load_runtime(
    config: dict[str, Any], paths: dict[str, Path]
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    return replay_events(config, read_events(paths["events"]))


def materialize_views(
    paths: dict[str, Path],
    records: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    state: dict[str, Any],
    campaigns: dict[str, dict[str, Any]],
) -> None:
    atomic_write_jsonl(paths["archive"], records)
    atomic_write_jsonl(paths["evaluations"], evaluations)
    atomic_write_jsonl(paths["proposals_view"], proposals)
    atomic_write_json(paths["mode_state"], state)
    paths["campaigns"].mkdir(parents=True, exist_ok=True)
    paths["proposals"].mkdir(parents=True, exist_ok=True)
    expected_proposal_ids = {proposal["proposal_id"] for proposal in proposals}
    extra_proposal_paths = [
        path
        for path in paths["proposals"].glob("*.json")
        if path.stem not in expected_proposal_ids
    ]
    if extra_proposal_paths:
        orphaned = paths["proposals"] / "_orphaned"
        orphaned.mkdir(parents=True, exist_ok=True)
        for path in extra_proposal_paths:
            destination = orphaned / f"{path.stem}.{time.time_ns()}.json"
            os.replace(path, destination)
    for proposal in proposals:
        atomic_write_json(
            paths["proposals"] / f"{proposal['proposal_id']}.json", proposal
        )
    expected_campaign_ids = set(campaigns)
    extra_campaign_paths = [
        path
        for path in paths["campaigns"].glob("*.json")
        if path.stem not in expected_campaign_ids
    ]
    if extra_campaign_paths:
        orphaned = paths["campaigns"] / "_orphaned"
        orphaned.mkdir(parents=True, exist_ok=True)
        for path in extra_campaign_paths:
            destination = orphaned / f"{path.stem}.{time.time_ns()}.json"
            os.replace(path, destination)
    for campaign_id, campaign in campaigns.items():
        atomic_write_json(campaign_file(paths, campaign_id), campaign)


def rebuild_views(
    config: dict[str, Any], paths: dict[str, Path]
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    runtime = load_runtime(config, paths)
    materialize_views(paths, *runtime)
    return runtime


def commit_event_and_rebuild(
    config: dict[str, Any],
    paths: dict[str, Path],
    event_type: str,
    payload: dict[str, Any],
    entity_kind: str | None = None,
    entity_id: str | None = None,
    expected_last_sequence: int | None = None,
) -> tuple[
    int,
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ],
]:
    sequence = append_event(
        paths["events"],
        event_type,
        payload,
        entity_kind,
        entity_id,
        expected_last_sequence,
    )
    try:
        runtime = rebuild_views(config, paths)
    except Exception as exc:
        raise EvolutionError(
            f"event {sequence} was committed, but materialized views were not refreshed: "
            f"{type(exc).__name__}: {exc}; run rebuild-state and do not resubmit the event"
        ) from exc
    return sequence, runtime


def commit_events_and_rebuild(
    config: dict[str, Any],
    paths: dict[str, Path],
    events: Iterable[tuple[str, dict[str, Any], str | None, str | None]],
    expected_last_sequence: int | None = None,
) -> tuple[
    list[int],
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ],
]:
    sequences = append_events(
        paths["events"], events, expected_last_sequence=expected_last_sequence
    )
    try:
        runtime = rebuild_views(config, paths)
    except Exception as exc:
        committed = f"{sequences[0]}..{sequences[-1]}" if sequences else "none"
        raise EvolutionError(
            f"events {committed} were committed, but materialized views were not refreshed: "
            f"{type(exc).__name__}: {exc}; run rebuild-state and do not repeat migration"
        ) from exc
    return sequences, runtime


def validate_config(config: dict[str, Any], config_path: Path, require_state: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    for section in (
        "project",
        "adapter",
        "isolation",
        "commands",
        "evaluation",
        "datasets",
        "search",
        "completion",
    ):
        if not isinstance(config.get(section), dict):
            errors.append(f"{section} must be an object")
    if errors:
        return errors, warnings

    project = config["project"]
    if not str(project.get("name", "")).strip():
        errors.append("project.name is required")
    if project.get("execution_environment") not in ENVIRONMENTS:
        errors.append(f"project.execution_environment must be one of {ENVIRONMENTS}")
    candidate_root = str(project.get("candidate_root", "")).strip()
    if not portable_is_absolute(candidate_root):
        errors.append("project.candidate_root must be an absolute Windows or POSIX path")
    if not str(project.get("state_root", "")).strip():
        errors.append("project.state_root is required")

    adapter = config["adapter"]
    if adapter.get("interface_version") != 1:
        errors.append("adapter.interface_version must be 1")
    adapter_path = str(adapter.get("path", "")).strip()
    if not portable_is_absolute(adapter_path):
        errors.append("adapter.path must be an absolute Windows or POSIX path")
    adapter_sha256 = str(adapter.get("sha256", "")).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", adapter_sha256):
        errors.append("adapter.sha256 must be a lowercase 64-character SHA-256")
    elif adapter_path:
        local_adapter = Path(adapter_path)
        if not local_adapter.is_file():
            errors.append(f"adapter file is missing: {adapter_path}")
        elif sha256_file(local_adapter) != adapter_sha256:
            errors.append("adapter.sha256 does not match adapter.path")

    mutable = project.get("mutable_roots")
    protected = project.get("protected_roots")
    if not isinstance(mutable, list) or not mutable:
        errors.append("project.mutable_roots must be a nonempty list")
        mutable = []
    if not isinstance(protected, list):
        errors.append("project.protected_roots must be a list")
        protected = []
    for label, roots in (("mutable_roots", mutable), ("protected_roots", protected)):
        for root in roots:
            if not isinstance(root, str) or not portable_is_absolute(root):
                errors.append(f"project.{label} contains a non-absolute path: {root!r}")
    for writable in mutable:
        for readonly in protected:
            if isinstance(writable, str) and isinstance(readonly, str) and paths_overlap(writable, readonly):
                errors.append(
                    f"mutable/protected roots overlap: {writable!r} and {readonly!r}"
                )
    if candidate_root and mutable and not any(
        isinstance(root, str) and path_is_within(candidate_root, root) for root in mutable
    ):
        errors.append("project.candidate_root must be inside a mutable root")

    try:
        resolved_state_root = str(resolve_state_root(config, config_path))
    except EvolutionError as exc:
        errors.append(str(exc))
        resolved_state_root = ""
    if resolved_state_root:
        for writable in mutable:
            if isinstance(writable, str) and paths_overlap(resolved_state_root, writable):
                errors.append(
                    f"state root must be isolated from mutable roots: {resolved_state_root!r} and {writable!r}"
                )
        for writable in mutable:
            if isinstance(writable, str) and paths_overlap(str(config_path), writable):
                errors.append(
                    "project adapter must be outside every mutable root so workers cannot rewrite policy"
                )

    isolation = config["isolation"]
    if isolation.get("enforcement") not in ISOLATION_ENFORCEMENTS:
        errors.append(
            f"isolation.enforcement must be one of {ISOLATION_ENFORCEMENTS}"
        )
    if isolation.get("enforcement") == "trusted_process" and isolation.get(
        "trusted_process_acknowledged"
    ) is not True:
        errors.append(
            "trusted_process isolation requires trusted_process_acknowledged=true"
        )
    worker_workspace_root = str(isolation.get("worker_workspace_root", "")).strip()
    if not portable_is_absolute(worker_workspace_root):
        errors.append(
            "isolation.worker_workspace_root must be an absolute Windows or POSIX path"
        )
    elif mutable and not any(
        isinstance(root, str) and path_is_within(worker_workspace_root, root)
        for root in mutable
    ):
        errors.append("isolation.worker_workspace_root must be inside a mutable root")
    controller_only = isolation.get("controller_only_roots")
    if not isinstance(controller_only, list) or not controller_only:
        errors.append("isolation.controller_only_roots must be a nonempty list")
        controller_only = []
    for root in controller_only:
        if not isinstance(root, str) or not portable_is_absolute(root):
            errors.append(
                f"isolation.controller_only_roots contains a non-absolute path: {root!r}"
            )
            continue
        for writable in mutable:
            if isinstance(writable, str) and paths_overlap(root, writable):
                errors.append(
                    f"controller-only/mutable roots overlap: {root!r} and {writable!r}"
                )
    if resolved_state_root and controller_only and not any(
        isinstance(root, str) and path_is_within(resolved_state_root, root)
        for root in controller_only
    ):
        errors.append("state root must be inside a controller-only root")
    if controller_only and not any(
        isinstance(root, str) and path_is_within(str(config_path), root)
        for root in controller_only
    ):
        errors.append("project adapter must be inside a controller-only root")
    if adapter_path:
        for writable in mutable:
            if isinstance(writable, str) and paths_overlap(adapter_path, writable):
                errors.append("adapter.path must be outside every mutable root")
        if controller_only and not any(
            isinstance(root, str) and path_is_within(adapter_path, root)
            for root in controller_only
        ):
            errors.append("adapter.path must be inside a controller-only root")
    for root in protected:
        if isinstance(root, str) and worker_workspace_root and paths_overlap(
            worker_workspace_root, root
        ):
            errors.append(
                f"worker workspace/protected roots overlap: {worker_workspace_root!r} and {root!r}"
            )

    commands = config["commands"]
    for name in ("isolation_preflight", "build", "smoke", "development"):
        if not str(commands.get(name, "")).strip():
            errors.append(f"commands.{name} is required")

    evaluation = config["evaluation"]
    if not str(evaluation.get("primary_metric", "")).strip():
        errors.append("evaluation.primary_metric is required")
    if evaluation.get("direction") not in ("maximize", "minimize"):
        errors.append("evaluation.direction must be maximize or minimize")
    if not numeric(evaluation.get("noise_band", 0.0)) or evaluation.get("noise_band", 0.0) < 0:
        errors.append("evaluation.noise_band must be a finite nonnegative number")
    hard = evaluation.get("hard_constraints")
    if not isinstance(hard, list) or not hard or not all(isinstance(item, str) and item for item in hard):
        errors.append("evaluation.hard_constraints must be a nonempty string list")
    elif "scope_passed" not in hard:
        errors.append("evaluation.hard_constraints must include scope_passed")
    descriptors = evaluation.get("descriptor_keys")
    if not isinstance(descriptors, list) or not descriptors or not all(isinstance(item, str) and item for item in descriptors):
        errors.append("evaluation.descriptor_keys must be a nonempty string list")
    elif len(set(descriptors)) != len(descriptors):
        errors.append("evaluation.descriptor_keys contains duplicates")
    descriptor_space = evaluation.get("descriptor_space", {})
    if not isinstance(descriptor_space, dict):
        errors.append("evaluation.descriptor_space must be an object")
    else:
        for key, values in descriptor_space.items():
            if key not in (descriptors or []):
                errors.append(f"descriptor_space contains an undeclared key: {key}")
            if not isinstance(values, list) or not values:
                errors.append(f"descriptor_space.{key} must be a nonempty list")
            elif len({json.dumps(value, sort_keys=True) for value in values}) != len(values):
                errors.append(f"descriptor_space.{key} contains duplicate values")
    floor = evaluation.get("minimum_cell_quality")
    if floor is not None and not numeric(floor):
        errors.append("evaluation.minimum_cell_quality must be null or finite numeric")
    repair_metric = evaluation.get("repair_metric")
    if repair_metric is not None and (
        not isinstance(repair_metric, str) or not repair_metric.strip()
    ):
        errors.append("evaluation.repair_metric must be null or a nonempty string")
    if repair_metric is not None and evaluation.get("repair_direction") not in (
        "maximize",
        "minimize",
    ):
        errors.append("evaluation.repair_direction must be maximize or minimize")

    datasets = config["datasets"]
    for name in ("smoke", "development"):
        if not str(datasets.get(name, "")).strip():
            errors.append(f"datasets.{name} is required")
    dataset_values = [
        str(value).strip() for value in datasets.values() if str(value).strip()
    ]
    if len(set(dataset_values)) != len(dataset_values):
        errors.append("nonempty dataset identifiers must be distinct")
    completion = config["completion"]
    if completion.get("require_validation"):
        if not str(commands.get("validation", "")).strip() or not str(datasets.get("validation", "")).strip():
            errors.append("validation command and dataset are required by completion policy")
    if completion.get("require_holdout_at_stage_end"):
        if not str(commands.get("holdout", "")).strip() or not str(datasets.get("holdout", "")).strip():
            errors.append("holdout command and dataset are required by completion policy")

    search = config["search"]
    for name in (
        "max_attempts",
        "max_valid_candidates",
        "bootstrap_valid_candidates",
        "plateau_window",
        "target_cells",
        "repeated_bottleneck_threshold",
        "max_inspirations",
        "max_negative_lessons",
        "max_open_proposals",
    ):
        value = search.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"search.{name} must be a positive integer")
    coverage_target = search.get("coverage_target")
    if not numeric(coverage_target) or not 0 <= coverage_target <= 1:
        errors.append("search.coverage_target must be between 0 and 1")
    target = completion.get("target_primary_score")
    if target is not None and not numeric(target):
        errors.append("completion.target_primary_score must be null or finite numeric")

    if require_state and not errors:
        try:
            paths = state_paths(config, config_path)
        except EvolutionError as exc:
            errors.append(str(exc))
        else:
            integrity = event_store_integrity(paths["events"])
            if integrity != "ok":
                errors.append(f"event store integrity check failed: {integrity}")
            else:
                for name in ("archive", "evaluations", "proposals_view", "mode_state"):
                    if not paths[name].exists():
                        errors.append(f"materialized view is missing: {paths[name]}")
                try:
                    (
                        expected_records,
                        expected_evaluations,
                        expected_proposals,
                        expected_state,
                        expected_campaigns,
                    ) = (
                        load_runtime(config, paths)
                    )
                    actual_records = read_jsonl(paths["archive"])
                    actual_evaluations = read_jsonl(paths["evaluations"])
                    actual_proposals = read_jsonl(paths["proposals_view"])
                    actual_state = load_json(paths["mode_state"])
                except EvolutionError as exc:
                    errors.append(str(exc))
                else:
                    if actual_records != expected_records:
                        errors.append(
                            "archive view differs from authoritative events; run rebuild-state"
                        )
                    if actual_evaluations != expected_evaluations:
                        errors.append(
                            "protocol evaluation view differs from authoritative events; run rebuild-state"
                        )
                    if actual_proposals != expected_proposals:
                        errors.append(
                            "proposal view differs from authoritative events; run rebuild-state"
                        )
                    for expected_proposal in expected_proposals:
                        proposal_id = expected_proposal["proposal_id"]
                        path = paths["proposals"] / f"{proposal_id}.json"
                        try:
                            actual_proposal = load_json(path)
                        except EvolutionError as exc:
                            errors.append(str(exc))
                        else:
                            if actual_proposal != expected_proposal:
                                errors.append(
                                    f"proposal file differs from authoritative events: "
                                    f"{proposal_id}; run rebuild-state"
                                )
                    actual_proposal_ids = {
                        path.stem for path in paths["proposals"].glob("*.json")
                    } if paths["proposals"].exists() else set()
                    extra_proposal_ids = sorted(
                        actual_proposal_ids
                        - {item["proposal_id"] for item in expected_proposals}
                    )
                    if extra_proposal_ids:
                        errors.append(
                            "proposal files have no authoritative event: "
                            f"{extra_proposal_ids}; run rebuild-state"
                        )
                    if actual_state != expected_state:
                        errors.append(
                            "mode state differs from authoritative events; run rebuild-state"
                        )
                    for campaign_id, expected_campaign in expected_campaigns.items():
                        path = campaign_file(paths, campaign_id)
                        try:
                            actual_campaign = load_json(path)
                        except EvolutionError as exc:
                            errors.append(str(exc))
                        else:
                            if actual_campaign != expected_campaign:
                                errors.append(
                                    f"campaign view differs from authoritative events: {campaign_id}; "
                                    "run rebuild-state"
                                )
                    actual_campaign_ids = {
                        path.stem for path in paths["campaigns"].glob("*.json")
                    } if paths["campaigns"].exists() else set()
                    extra_campaign_ids = sorted(
                        actual_campaign_ids - set(expected_campaigns)
                    )
                    if extra_campaign_ids:
                        errors.append(
                            "campaign views have no authoritative event: "
                            f"{extra_campaign_ids}; run rebuild-state"
                        )

    if not project.get("required_branch"):
        warnings.append("project.required_branch is unset; branch enforcement is project-defined")
    if not project.get("baseline_root"):
        warnings.append("project.baseline_root is unset")
    else:
        baseline = str(project["baseline_root"])
        if not portable_is_absolute(baseline):
            errors.append("project.baseline_root must be an absolute Windows or POSIX path")
        elif not any(
            isinstance(root, str)
            and paths_overlap(baseline, root)
            and portable_normalize(baseline).startswith(portable_normalize(root))
            for root in protected
        ):
            errors.append("project.baseline_root must be inside a protected root")
    if not commands.get("profile"):
        warnings.append("commands.profile is unset")
    return errors, warnings


def descriptor_total_cells(config: dict[str, Any]) -> int:
    evaluation = config.get("evaluation", {})
    keys = evaluation.get("descriptor_keys", [])
    space = evaluation.get("descriptor_space", {})
    if keys and isinstance(space, dict) and all(
        isinstance(space.get(key), list) and space.get(key) for key in keys
    ):
        return math.prod(len(space[key]) for key in keys)
    return int(config.get("search", {}).get("target_cells", 1))


def score_improving(state: dict[str, Any], config: dict[str, Any]) -> tuple[bool, bool]:
    history = [item for item in state.get("valid_score_history", []) if numeric(item.get("score"))]
    window = int(config["search"]["plateau_window"])
    if len(history) <= window:
        return False, False
    older = [float(item["score"]) for item in history[:-window]]
    recent = [float(item["score"]) for item in history[-window:]]
    direction = config["evaluation"]["direction"]
    older_best = max(older) if direction == "maximize" else min(older)
    recent_best = max(recent) if direction == "maximize" else min(recent)
    improving = better(
        recent_best,
        older_best,
        direction,
        float(config["evaluation"].get("noise_band", 0.0)),
    )
    return improving, not improving


def choose_mode(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    search = config["search"]
    direction = config["evaluation"]["direction"]
    target = config["completion"].get("target_primary_score")
    if state.get("holdout_status") == "completed":
        return {"mode": "stop", "reason": "sealed_holdout_recorded_stage_closed"}
    if state["attempt_count"] >= search["max_attempts"]:
        return {"mode": "stop", "reason": "max_attempts_reached"}
    if state["valid_candidate_count"] >= search["max_valid_candidates"]:
        return {"mode": "stop", "reason": "max_valid_candidates_reached"}
    if reaches_target(state.get("global_best_score"), target, direction):
        return {"mode": "stop", "reason": "target_primary_score_reached; run completion audit"}
    if state.get("active_campaign"):
        return {"mode": "campaign", "reason": "active_campaign"}
    if state["attempt_count"] > 0 and state["valid_candidate_count"] == 0:
        if config["evaluation"].get("repair_metric"):
            return {"mode": "repair", "reason": "no_valid_candidate_repair_bootstrap"}
        return {"mode": "stop", "reason": "no_valid_candidate_and_no_repair_metric"}
    if state["valid_candidate_count"] < search["bootstrap_valid_candidates"]:
        return {"mode": "explore", "reason": "bootstrap_archive"}

    total_cells = max(descriptor_total_cells(config), 1)
    coverage = min(len(state.get("cell_elites", {})) / total_cells, 1.0)
    improving, plateau = score_improving(state, config)
    repeated = int(state.get("repeated_bottleneck_rounds", 0))
    if plateau and repeated >= search["repeated_bottleneck_threshold"]:
        return {
            "mode": "research",
            "reason": "plateau_with_repeated_bottleneck",
            "archive_coverage": coverage,
        }
    if plateau and coverage < search["coverage_target"]:
        return {
            "mode": "explore",
            "reason": "plateau_with_low_archive_coverage",
            "archive_coverage": coverage,
        }
    if improving:
        return {
            "mode": "exploit",
            "reason": "recent_global_improvement_beyond_noise",
            "archive_coverage": coverage,
        }
    if coverage < search["coverage_target"]:
        return {
            "mode": "explore",
            "reason": "archive_coverage_below_target",
            "archive_coverage": coverage,
        }

    stats = state.get("mode_stats", {})
    exploit_attempts = int(stats.get("exploit", {}).get("attempts", 0))
    explore_attempts = int(stats.get("explore", {}).get("attempts", 0))
    mode = "explore" if explore_attempts <= exploit_attempts else "exploit"
    return {
        "mode": mode,
        "reason": "balanced_adaptive_fallback",
        "archive_coverage": coverage,
    }


def records_by_id(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record["candidate_id"]): record
        for record in records
        if record.get("candidate_id")
    }


def choose_parent(
    mode: str,
    config: dict[str, Any],
    state: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = records_by_id(records)
    feasible = valid_records(records, config)
    if mode == "repair":
        repair = repair_records(records, config)
        if not repair:
            raise EvolutionError("No measured invalid candidate is eligible for repair")
        metric = config["evaluation"]["repair_metric"]
        repair_direction = config["evaluation"].get("repair_direction", "maximize")
        ranked = sorted(
            repair,
            key=lambda record: (
                -float(record["evaluation"]["auxiliary_metrics"][metric])
                if repair_direction == "maximize"
                else float(record["evaluation"]["auxiliary_metrics"][metric]),
                -int(record.get("sequence", 0)),
            ),
        )
        return {
            "parent_id": ranked[0]["candidate_id"],
            "reason": f"best_invalid_repair_metric:{metric}",
        }
    if not feasible:
        raise EvolutionError("No valid, descriptor-complete candidate is available as a parent")
    direction = config["evaluation"]["direction"]

    if mode == "campaign":
        campaign = state.get("active_campaign")
        if not isinstance(campaign, dict) or not campaign.get("head_candidate_id"):
            raise EvolutionError("Campaign mode requires active_campaign.head_candidate_id")
        candidate_id = str(campaign["head_candidate_id"])
        if candidate_id not in by_id:
            raise EvolutionError(f"Campaign head is absent from archive: {candidate_id}")
        return {"parent_id": candidate_id, "reason": "active_campaign_head"}

    global_id = state.get("global_best_id")
    if mode in ("exploit", "research") and global_id in by_id:
        return {
            "parent_id": global_id,
            "reason": "global_best" if mode == "exploit" else "global_best_research_anchor",
        }

    if mode == "explore":
        elites = [by_id[candidate_id] for candidate_id in state.get("cell_elites", {}).values() if candidate_id in by_id]
        pool = elites or feasible
        attempts = state.get("cell_attempts", {})
        uses = state.get("parent_use_counts", {})
        ranked = sorted(
            pool,
            key=lambda record: (
                int(attempts.get(cell_key(record, config), 0)),
                int(uses.get(record["candidate_id"], 0)),
                quality_sort_key(record, direction),
            ),
        )
        return {"parent_id": ranked[0]["candidate_id"], "reason": "underused_cell_elite"}

    ranked = sorted(feasible, key=lambda record: quality_sort_key(record, direction))
    return {"parent_id": ranked[0]["candidate_id"], "reason": "best_feasible_fallback"}


def descriptor_distance(left: dict[str, Any], right: dict[str, Any], config: dict[str, Any]) -> float:
    keys = config["evaluation"]["descriptor_keys"]
    if not keys:
        return 0.0
    a = left.get("evaluation", {}).get("descriptors", {})
    b = right.get("evaluation", {}).get("descriptors", {})
    return sum(a.get(key) != b.get(key) for key in keys) / len(keys)


def set_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def quality_percentiles(records: list[dict[str, Any]], direction: str) -> dict[str, float]:
    ordered = sorted(records, key=lambda record: quality_sort_key(record, direction))
    if len(ordered) == 1:
        return {ordered[0]["candidate_id"]: 1.0}
    return {
        record["candidate_id"]: 1.0 - index / (len(ordered) - 1)
        for index, record in enumerate(ordered)
    }


def inspiration_reason(mode: str, record: dict[str, Any], parent: dict[str, Any], config: dict[str, Any]) -> str:
    distance = descriptor_distance(record, parent, config)
    if mode == "explore":
        return f"diverse mechanism candidate; descriptor_distance={distance:.2f}"
    if mode == "campaign":
        return f"potentially compatible component donor; descriptor_distance={distance:.2f}"
    if mode == "research":
        return f"representative alternative elite; descriptor_distance={distance:.2f}"
    if mode == "repair":
        return f"correctness-repair evidence; descriptor_distance={distance:.2f}"
    return f"high-quality nearby mechanism; descriptor_distance={distance:.2f}"


def choose_context(
    mode: str,
    parent_id: str,
    config: dict[str, Any],
    state: dict[str, Any],
    records: list[dict[str, Any]],
    target_descriptors: dict[str, Any] | None = None,
    target_cell: str | None = None,
) -> dict[str, Any]:
    by_id = records_by_id(records)
    if parent_id not in by_id:
        raise EvolutionError(f"Parent is absent from archive: {parent_id}")
    parent = by_id[parent_id]
    feasible = [record for record in valid_records(records, config) if record["candidate_id"] != parent_id]
    direction = config["evaluation"]["direction"]
    quality = quality_percentiles(feasible, direction) if feasible else {}
    uses = state.get("context_use_counts", {})
    parent_tags = parent.get("tags", [])
    parent_bottlenecks = parent.get("bottlenecks", [])
    cell_attempts = state.get("cell_attempts", {})

    weights = {
        "repair": (0.00, 0.20, 0.45, 0.35),
        "exploit": (0.45, -0.30, 0.25, 0.00),
        "explore": (0.20, 0.45, 0.15, 0.20),
        "campaign": (0.20, -0.45, 0.35, 0.00),
        "research": (0.25, 0.35, 0.20, 0.20),
    }[mode]

    scored: list[tuple[float, dict[str, Any]]] = []
    max_cell_attempts = max([int(value) for value in cell_attempts.values()] or [1])
    for record in feasible:
        candidate_id = record["candidate_id"]
        distance = descriptor_distance(record, parent, config)
        relevance = max(
            set_similarity(record.get("tags", []), parent_tags),
            set_similarity(record.get("bottlenecks", []), parent_bottlenecks),
        )
        underuse = 1.0 - int(cell_attempts.get(cell_key(record, config), 0)) / max(max_cell_attempts, 1)
        score = (
            weights[0] * quality.get(candidate_id, 0.0)
            + weights[1] * distance
            + weights[2] * relevance
            + weights[3] * underuse
            - 0.03 * int(uses.get(candidate_id, 0))
        )
        scored.append((score, record))

    maximum = int(config["search"]["max_inspirations"])
    selected: list[dict[str, Any]] = []
    remaining = sorted(scored, key=lambda item: (-item[0], item[1]["candidate_id"]))
    while remaining and len(selected) < maximum:
        best_index = 0
        best_value = -float("inf")
        for index, (base_score, record) in enumerate(remaining):
            diversity_bonus = 0.0
            if selected and mode in ("explore", "research"):
                diversity_bonus = 0.15 * min(
                    descriptor_distance(record, chosen["record"], config)
                    for chosen in selected
                )
            value = base_score + diversity_bonus
            if value > best_value:
                best_value = value
                best_index = index
        _, record = remaining.pop(best_index)
        selected.append(
            {
                "candidate_id": record["candidate_id"],
                "reason": inspiration_reason(mode, record, parent, config),
                "record": record,
            }
        )

    invalid = [record for record in records if record.get("evaluation", {}).get("valid") is False]
    negative_limit = int(config["search"]["max_negative_lessons"])
    invalid.sort(
        key=lambda record: (
            -max(
                set_similarity(record.get("tags", []), parent_tags),
                set_similarity(record.get("bottlenecks", []), parent_bottlenecks),
            ),
            -int(record.get("sequence", 0)),
        )
    )
    lessons = []
    for record in invalid[:negative_limit]:
        failure = record.get("evaluation", {}).get("failure") or {}
        lessons.append(
            {
                "candidate_id": record.get("candidate_id"),
                "category": failure.get("category", "unknown"),
                "message": failure.get("message", ""),
                "hypothesis": record.get("hypothesis", ""),
                "artifacts": record.get("evaluation", {}).get("artifacts", {}),
            }
        )

    global_id = state.get("global_best_id")
    anchor = None
    if global_id and global_id != parent_id and global_id in by_id:
        anchor = {"candidate_id": global_id, "record": by_id[global_id]}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "mode": mode,
        "target_cell": target_cell
        or cell_key_from_descriptors(
            target_descriptors
            or parent.get("evaluation", {}).get("descriptors", {}),
            config,
        ),
        "intended_descriptors": copy.deepcopy(
            target_descriptors
            or parent.get("evaluation", {}).get("descriptors", {})
        ),
        "parent": {"candidate_id": parent_id, "record": parent},
        "quality_anchor": anchor,
        "inspirations": selected,
        "inspiration_ids": [item["candidate_id"] for item in selected],
        "negative_lessons": lessons,
        "negative_lesson_ids": [item["candidate_id"] for item in lessons],
    }


def add_supplemental_lessons(pack: dict[str, Any], index_path: str | None, config: dict[str, Any]) -> None:
    """Bind compact, controller-supplied experience without changing evaluator facts."""
    if not index_path:
        return
    index = load_json(Path(index_path).resolve())
    lessons = index.get("lessons")
    if index.get("schema_version") != 1 or not isinstance(lessons, list):
        raise EvolutionError("Lesson index requires schema_version=1 and lessons list")
    parent = pack["parent"]["record"]
    ranked = []
    for item in lessons:
        if not isinstance(item, dict):
            raise EvolutionError("Invalid supplemental lesson")
        for key in ("candidate_id", "mechanism", "mechanism_family", "outcome", "applicability", "conclusion", "retry_condition"):
            if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 1600:
                raise EvolutionError(f"Supplemental lesson needs compact nonempty {key}")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence or len(evidence) > 12:
            raise EvolutionError("Supplemental lesson needs 1--12 evidence references")
        for ref in evidence:
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
                raise EvolutionError("Invalid lesson evidence reference")
            path = Path(ref["path"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                raise EvolutionError("Supplemental lesson evidence missing or changed: " + str(path))
        # Prefer experiments on this parent and its measured bottleneck. The
        # parent's last edited file alone is a poor proxy for its next bottleneck.
        relevance = (3 * len(set(item.get("bottlenecks", [])) & set(parent.get("bottlenecks", [])))
                     + int(item.get("parent_id") == parent["candidate_id"]))
        delta = (item.get("actual") or {}).get("relative_score_change")
        regression = max(0.0, -float(delta)) if numeric(delta) else 0.0
        ranked.append((relevance, regression, int(item.get("sequence", 0)), item))
    ranked.sort(key=lambda value: (-value[0], -value[1], -value[2], value[3]["candidate_id"]))
    chosen = []; families = set()
    limit = min(int(config["search"]["max_negative_lessons"]), 5)
    for _, _, _, item in ranked:
        if len(chosen) >= limit:
            break
        if item["mechanism_family"] in families:
            continue
        families.add(item["mechanism_family"])
        chosen.append(copy.deepcopy(item))
    pack["supplemental_lessons"] = chosen
    pack["supplemental_index_digest"] = canonical_digest(index)


def proposal_digest_value(proposal: dict[str, Any]) -> str:
    value = copy.deepcopy(proposal)
    for key in (
        "proposal_digest",
        "status",
        "created_at",
        "consumed_at",
        "cancelled_at",
        "cancel_reason",
        "candidate_id",
    ):
        value.pop(key, None)
    return canonical_digest(value)


def validate_workspace_root(raw: Any, config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    workspace = str(raw or "").strip()
    if not portable_is_absolute(workspace):
        return ["workspace_root must be an absolute Windows or POSIX path"]
    isolation_root = str(config["isolation"]["worker_workspace_root"])
    if not path_is_within(workspace, isolation_root):
        errors.append("workspace_root must be inside isolation.worker_workspace_root")
    if not any(
        path_is_within(workspace, root)
        for root in config["project"].get("mutable_roots", [])
    ):
        errors.append("workspace_root must be inside a project mutable root")
    for root in config["project"].get("protected_roots", []):
        if paths_overlap(workspace, root):
            errors.append(f"workspace_root overlaps protected root: {root}")
    for root in config["isolation"].get("controller_only_roots", []):
        if paths_overlap(workspace, root):
            errors.append(f"workspace_root overlaps controller-only root: {root}")
    return errors


def controller_roots_digest(config: dict[str, Any]) -> str:
    roots = sorted(
        portable_normalize(str(root))
        for root in config["isolation"].get("controller_only_roots", [])
    )
    return canonical_digest(roots)


def validate_isolation_proof(
    proof: Any, workspace_root: str, config: dict[str, Any]
) -> list[str]:
    if not isinstance(proof, dict):
        return ["isolation_proof must be an object"]
    errors = []
    if proof.get("passed") is not True:
        errors.append("isolation_proof.passed must be true")
    if proof.get("enforcement") != config["isolation"]["enforcement"]:
        errors.append("isolation proof enforcement does not match project adapter")
    if config["isolation"]["enforcement"] == "trusted_process" and proof.get(
        "risk_acknowledged"
    ) is not True:
        errors.append("trusted_process proof requires risk_acknowledged=true")
    if portable_normalize(str(proof.get("workspace_root", ""))) != portable_normalize(
        workspace_root
    ):
        errors.append("isolation proof workspace_root does not match proposal")
    if proof.get("controller_only_roots_digest") != controller_roots_digest(config):
        errors.append("isolation proof controller-only-roots digest does not match")
    if proof.get("preflight_command_digest") != canonical_digest(
        config["commands"]["isolation_preflight"]
    ):
        errors.append("isolation proof command digest does not match project adapter")
    if not str(proof.get("preflight_artifact_digest", "")).strip():
        errors.append("isolation_proof.preflight_artifact_digest is required")
    if not str(proof.get("protected_inputs_digest", "")).strip():
        errors.append("isolation_proof.protected_inputs_digest is required")
    return errors


def validate_proposal_record(
    proposal: dict[str, Any],
    config: dict[str, Any],
    records: list[dict[str, Any]],
    campaigns: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        errors.append(f"proposal schema_version must be {PROPOSAL_SCHEMA_VERSION}")
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", proposal_id
    ):
        errors.append("proposal_id must contain only letters, digits, '.', '_' or '-'")
    base_sequence = proposal.get("base_event_sequence")
    if not isinstance(base_sequence, int) or isinstance(base_sequence, bool) or base_sequence < 0:
        errors.append("base_event_sequence must be a nonnegative integer")
    default_mode = proposal.get("default_mode")
    selected_mode = proposal.get("selected_mode")
    if default_mode not in MODES or selected_mode not in MODES:
        errors.append(f"proposal modes must be one of {MODES}")
    override = proposal.get("override")
    if selected_mode != default_mode:
        if not isinstance(override, dict):
            errors.append("mode override requires a structured override object")
        else:
            if not str(override.get("reason", "")).strip():
                errors.append("mode override requires reason")
            evidence = override.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence
            ):
                errors.append("mode override requires nonempty evidence references")
    elif override is not None:
        errors.append("override must be null when selected_mode equals default_mode")
    intended = proposal.get("intended_descriptors")
    errors.extend(validate_intended_descriptors(intended, config))
    if isinstance(intended, dict):
        expected_cell = cell_key_from_descriptors(intended, config)
        if proposal.get("target_cell") != expected_cell:
            errors.append("target_cell does not match intended_descriptors")
    if proposal.get("descriptor_policy") not in DESCRIPTOR_POLICIES:
        errors.append(f"descriptor_policy must be one of {DESCRIPTOR_POLICIES}")
    parent = proposal.get("parent")
    by_id = records_by_id(records)
    if not isinstance(parent, dict):
        errors.append("proposal.parent must be an object")
    else:
        parent_record = by_id.get(str(parent.get("candidate_id", "")))
        valid_parent = parent_record in valid_records(
            [parent_record] if parent_record else [], config
        )
        repair_parent = proposal.get("selected_mode") == "repair" and parent_record in repair_records(
            [parent_record] if parent_record else [], config
        )
        if parent_record is None or not (valid_parent or repair_parent):
            errors.append("proposal parent must be valid or eligible for repair mode")
        elif parent.get("code_ref") != parent_record.get("code_ref"):
            errors.append("proposal parent code_ref does not match archive")
    errors.extend(validate_workspace_root(proposal.get("workspace_root"), config))
    worker_proposal_path = str(proposal.get("worker_proposal_path", "")).strip()
    if not portable_is_absolute(worker_proposal_path) or not path_is_within(
        worker_proposal_path, str(proposal.get("workspace_root", ""))
    ):
        errors.append("worker_proposal_path must be absolute and inside workspace_root")
    errors.extend(
        validate_isolation_proof(
            proposal.get("isolation_proof"),
            str(proposal.get("workspace_root", "")),
            config,
        )
    )
    context = proposal.get("context")
    if not isinstance(context, dict):
        errors.append("proposal.context must be an object")
    else:
        if proposal.get("context_digest") != canonical_digest(context):
            errors.append("context_digest does not match proposal.context")
        if context.get("mode") != selected_mode:
            errors.append("context mode does not match selected_mode")
        if isinstance(parent, dict) and context.get("parent", {}).get(
            "candidate_id"
        ) != parent.get("candidate_id"):
            errors.append("context parent does not match proposal parent")
        if context.get("target_cell") != proposal.get("target_cell"):
            errors.append("context target_cell does not match proposal target")
    campaign_id = proposal.get("campaign_id")
    milestone_id = proposal.get("milestone_id")
    if selected_mode == "campaign":
        campaign = campaigns.get(str(campaign_id))
        if campaign is None or campaign.get("status") != "active":
            errors.append("campaign proposal requires an active Campaign")
        else:
            milestone = campaign_current_milestone(campaign)
            if milestone is None or milestone.get("milestone_id") != milestone_id:
                errors.append("proposal milestone does not match current Campaign milestone")
    elif campaign_id is not None or milestone_id is not None:
        errors.append("non-Campaign proposal must not name campaign or milestone")
    if proposal.get("proposal_digest") != proposal_digest_value(proposal):
        errors.append("proposal_digest does not match proposal content")
    return errors


def validate_candidate_binding(
    candidate: dict[str, Any], proposal: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    for candidate_key, proposal_key in (
        ("proposal_id", "proposal_id"),
        ("proposal_digest", "proposal_digest"),
        ("context_digest", "context_digest"),
        ("workspace_root", "workspace_root"),
    ):
        if candidate.get(candidate_key) != proposal.get(proposal_key):
            errors.append(f"candidate {candidate_key} does not match proposal")
    canonical_expectations = {
        "parent_id": proposal.get("parent", {}).get("candidate_id"),
        "mode": proposal.get("selected_mode"),
        "campaign_id": proposal.get("campaign_id"),
        "milestone_id": proposal.get("milestone_id"),
        "target_cell": proposal.get("target_cell"),
        "intended_descriptors": proposal.get("intended_descriptors"),
    }
    for key, expected in canonical_expectations.items():
        if key in candidate and candidate.get(key) != expected:
            errors.append(f"candidate self-declared {key} conflicts with proposal")
    scope = candidate.get("scope")
    if not isinstance(scope, dict):
        errors.append("candidate.scope must be an object")
        scope = {}
    if scope.get("workspace_root") != proposal.get("workspace_root"):
        errors.append("candidate scope workspace_root does not match proposal")
    for key in ("manifest_digest", "protected_inputs_digest"):
        if not str(scope.get(key, "")).strip():
            errors.append(f"candidate.scope.{key} is required")
    if scope.get("protected_inputs_digest") != proposal.get(
        "isolation_proof", {}
    ).get("protected_inputs_digest"):
        errors.append("candidate protected_inputs_digest does not match isolation proof")
    hard = candidate.get("evaluation", {}).get("hard_constraints", {})
    if not isinstance(hard.get("scope_passed"), bool):
        errors.append("candidate evaluation must report boolean scope_passed")
    for changed in candidate.get("changed_files", []):
        path = PurePosixPath(str(changed).replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            errors.append(f"changed file must be a safe workspace-relative path: {changed!r}")
    if candidate.get("evaluation", {}).get("valid") is True:
        actual = candidate["evaluation"].get("descriptors", {})
        intended = proposal.get("intended_descriptors", {})
        if proposal.get("descriptor_policy") == "exact" and actual != intended:
            errors.append("actual descriptors do not exactly match proposal target")
    return errors


def validate_candidate_result(record: dict[str, Any], config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        errors.append(f"candidate schema_version must be {CANDIDATE_SCHEMA_VERSION}")
    candidate_id = record.get("candidate_id")
    if not isinstance(candidate_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate_id):
        errors.append("candidate_id must contain only letters, digits, '.', '_' or '-'")
    proposal_id = record.get("proposal_id")
    if proposal_id is not None and (
        not isinstance(proposal_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", proposal_id)
    ):
        errors.append("proposal_id must be null or a safe identifier")
    if proposal_id is not None:
        for key in ("proposal_digest", "context_digest"):
            if not str(record.get(key, "")).strip():
                errors.append(f"{key} is required for a proposed candidate")
    errors.extend(validate_workspace_root(record.get("workspace_root"), config))
    if not str(record.get("code_ref", "")).strip():
        errors.append("code_ref is required")
    if not str(record.get("hypothesis", "")).strip():
        errors.append("hypothesis is required")
    for key in ("changed_files", "tags", "bottlenecks"):
        value = record.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{key} must be a string list")
    scope = record.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope must be an object")
        scope = {}
    if scope.get("workspace_root") != record.get("workspace_root"):
        errors.append("scope.workspace_root must equal candidate workspace_root")
    for key in ("manifest_digest", "protected_inputs_digest"):
        if not str(scope.get(key, "")).strip():
            errors.append(f"scope.{key} is required")
    evaluation = record.get("evaluation")
    if not isinstance(evaluation, dict):
        return errors + ["evaluation must be an object"]
    errors.extend(
        validate_evaluation_evidence(
            evaluation, config, expected_protocol="development", require_descriptors=True
        )
    )
    valid = evaluation.get("valid")
    if not isinstance(valid, bool):
        errors.append("evaluation.valid must be boolean")
        return errors
    hard = evaluation.get("hard_constraints")
    if not isinstance(hard, dict):
        errors.append("evaluation.hard_constraints must be an object")
        hard = {}
    elif not all(isinstance(name, str) and isinstance(value, bool) for name, value in hard.items()):
        errors.append("evaluation.hard_constraints values must be boolean")
    if not isinstance(hard.get("scope_passed"), bool):
        errors.append("evaluation.hard_constraints.scope_passed must be boolean")
    required = config["evaluation"]["hard_constraints"]
    descriptors = evaluation.get("descriptors")
    if not isinstance(descriptors, dict):
        descriptors = {}
    for key in ("auxiliary_metrics", "artifacts"):
        if not isinstance(evaluation.get(key), dict):
            errors.append(f"evaluation.{key} must be an object")
    if valid:
        for name in required:
            if hard.get(name) is not True:
                errors.append(f"valid candidate is missing passing hard constraint: {name}")
        if not numeric(evaluation.get("primary_score")):
            errors.append("valid candidate requires a finite numeric primary_score")
        for key in config["evaluation"]["descriptor_keys"]:
            if descriptors.get(key) in (None, ""):
                errors.append(f"valid candidate is missing descriptor: {key}")
            allowed = config["evaluation"].get("descriptor_space", {}).get(key)
            if allowed and descriptors.get(key) not in allowed:
                errors.append(
                    f"descriptor {key}={descriptors.get(key)!r} is outside descriptor_space"
                )
        if evaluation.get("failure") is not None:
            errors.append("valid candidate evaluation.failure must be null")
    else:
        if evaluation.get("primary_score") is not None:
            errors.append("invalid candidate primary_score must be null")
        failure = evaluation.get("failure")
        if not isinstance(failure, dict) or not str(failure.get("category", "")).strip():
            errors.append("invalid candidate requires failure.category")
    return errors


def validate_protocol_evaluation_result(
    record: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != PROTOCOL_EVALUATION_SCHEMA_VERSION:
        errors.append(
            f"protocol evaluation schema_version must be {PROTOCOL_EVALUATION_SCHEMA_VERSION}"
        )
    for key in ("evaluation_id", "candidate_id"):
        value = record.get(key)
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", value
        ):
            errors.append(f"{key} must contain only letters, digits, '.', '_' or '-'")
    protocol = record.get("protocol")
    if protocol not in EVALUATION_PROTOCOLS[1:]:
        errors.append("protocol evaluation must use validation or holdout")
        return errors
    evaluation = record.get("evaluation")
    if not isinstance(evaluation, dict):
        return errors + ["evaluation must be an object"]
    errors.extend(
        validate_evaluation_evidence(
            evaluation, config, expected_protocol=protocol, require_descriptors=False
        )
    )
    valid = evaluation.get("valid")
    if not isinstance(valid, bool):
        errors.append("evaluation.valid must be boolean")
        return errors
    hard = evaluation.get("hard_constraints")
    if not isinstance(hard, dict):
        errors.append("evaluation.hard_constraints must be an object")
        hard = {}
    elif not all(isinstance(name, str) and isinstance(value, bool) for name, value in hard.items()):
        errors.append("evaluation.hard_constraints values must be boolean")
    for key in ("auxiliary_metrics", "artifacts"):
        if not isinstance(evaluation.get(key), dict):
            errors.append(f"evaluation.{key} must be an object")
    if valid:
        for name in config["evaluation"]["hard_constraints"]:
            if hard.get(name) is not True:
                errors.append(f"valid evaluation is missing passing hard constraint: {name}")
        if not numeric(evaluation.get("primary_score")):
            errors.append("valid evaluation requires a finite numeric primary_score")
        if evaluation.get("failure") is not None:
            errors.append("valid evaluation.failure must be null")
    else:
        if evaluation.get("primary_score") is not None:
            errors.append("invalid evaluation primary_score must be null")
        failure = evaluation.get("failure")
        if not isinstance(failure, dict) or not str(failure.get("category", "")).strip():
            errors.append("invalid evaluation requires failure.category")
    return errors


def update_archive(
    config: dict[str, Any],
    paths: dict[str, Path],
    state: dict[str, Any],
    records: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    campaigns: dict[str, dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    errors = validate_candidate_result(candidate, config)
    if errors:
        raise EvolutionError("Candidate result is invalid:\n- " + "\n- ".join(errors))
    candidate_id = candidate["candidate_id"]
    if candidate_id in records_by_id(records):
        raise EvolutionError(f"Candidate ID already exists: {candidate_id}")
    if state.get("holdout_status") == "completed":
        raise EvolutionError(
            "The sealed holdout is already recorded; this stage is closed to new candidates"
        )
    proposal_id = str(candidate.get("proposal_id") or "")
    proposal = next(
        (item for item in proposals if item.get("proposal_id") == proposal_id), None
    )
    if proposal is None or proposal.get("status") != "open":
        raise EvolutionError("Candidate requires an open proposal")
    binding_errors = validate_candidate_binding(candidate, proposal, config)
    if binding_errors:
        raise EvolutionError(
            "Candidate violates proposal:\n- " + "\n- ".join(binding_errors)
        )
    active = state.get("active_campaign")
    if active:
        if proposal.get("campaign_id") != active.get("campaign_id"):
            raise EvolutionError(
                "Active Campaign rejects non-Campaign or different-Campaign proposal"
            )
    elif proposal.get("campaign_id") is not None:
        raise EvolutionError("Proposal names a Campaign that is not active")
    if proposal.get("campaign_id") is not None:
        campaign = campaigns[str(proposal["campaign_id"])]
        budget_errors = campaign_budget_errors(campaign, candidate)
        if budget_errors:
            raise EvolutionError(
                "Campaign budget rejected candidate:\n- " + "\n- ".join(budget_errors)
            )
    fingerprint = protocol_fingerprint(candidate["evaluation"])
    existing_fingerprint = state.get("development_protocol_fingerprint")
    if existing_fingerprint and not protocol_matches(state, fingerprint):
        raise EvolutionError(
            "Development evaluator/dataset/command protocol changed; register an approved evaluator-only compatibility or start a new state root"
        )
    record = copy.deepcopy(candidate)
    record["parent_id"] = proposal["parent"]["candidate_id"]
    record["mode"] = proposal["selected_mode"]
    record["campaign_id"] = proposal.get("campaign_id")
    record["milestone_id"] = proposal.get("milestone_id")
    record["target_cell"] = proposal["target_cell"]
    record["intended_descriptors"] = copy.deepcopy(
        proposal["intended_descriptors"]
    )
    record["descriptor_policy"] = proposal["descriptor_policy"]
    record["descriptor_match"] = (
        record["evaluation"].get("descriptors")
        == proposal["intended_descriptors"]
    )
    record["cell_key"] = cell_key(record, config)
    cell = record["cell_key"]
    previous_cell_id = state.get("cell_elites", {}).get(cell)
    previous_global_id = state.get("global_best_id")
    _, runtime = commit_events_and_rebuild(
        config,
        paths,
        [
            (
                "candidate_recorded",
                {"record": record},
                "candidate",
                candidate_id,
            ),
            (
                "proposal_consumed",
                {"proposal_id": proposal_id, "candidate_id": candidate_id},
                "proposal-terminal",
                proposal_id,
            ),
        ],
    )
    _, _, rebuilt_proposals, rebuilt_state, _ = runtime
    evaluation = record["evaluation"]
    promoted_cell = (
        rebuilt_state.get("cell_elites", {}).get(cell) == candidate_id
        and previous_cell_id != candidate_id
    )
    promoted_global = (
        rebuilt_state.get("global_best_id") == candidate_id
        and previous_global_id != candidate_id
    )
    return {
        "candidate_id": candidate_id,
        "recorded": True,
        "valid": evaluation["valid"],
        "cell_key": cell,
        "promoted_cell": promoted_cell,
        "promoted_global": promoted_global,
        "global_best_id": rebuilt_state.get("global_best_id"),
        "global_best_score": rebuilt_state.get("global_best_score"),
        "development_best_id": rebuilt_state.get("development_best_id"),
        "development_best_score": rebuilt_state.get("development_best_score"),
        "proposal_id": proposal_id,
        "proposal_status": next(
            item["status"]
            for item in rebuilt_proposals
            if item["proposal_id"] == proposal_id
        ),
        "descriptor_match": record["descriptor_match"],
    }


def seed_archive(
    config: dict[str, Any],
    paths: dict[str, Path],
    records: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if records:
        raise EvolutionError("seed is allowed only when the candidate archive is empty")
    errors = validate_candidate_result(candidate, config)
    if errors:
        raise EvolutionError("Seed result is invalid:\n- " + "\n- ".join(errors))
    if candidate.get("proposal_id") is not None:
        raise EvolutionError("seed candidate must set proposal_id to null")
    record = copy.deepcopy(candidate)
    record["parent_id"] = None
    record["mode"] = "explore"
    record["campaign_id"] = None
    record["milestone_id"] = None
    descriptors = record["evaluation"].get("descriptors", {})
    record["target_cell"] = cell_key_from_descriptors(descriptors, config)
    record["intended_descriptors"] = copy.deepcopy(descriptors)
    record["descriptor_policy"] = "exact"
    record["descriptor_match"] = True
    record["cell_key"] = cell_key(record, config)
    _, runtime = commit_event_and_rebuild(
        config,
        paths,
        "candidate_recorded",
        {"record": record, "seed": True},
        entity_kind="candidate",
        entity_id=record["candidate_id"],
    )
    rebuilt_records, _, _, rebuilt_state, _ = runtime
    return {
        "seeded": True,
        "candidate_id": rebuilt_records[0]["candidate_id"],
        "development_best_id": rebuilt_state.get("development_best_id"),
        "development_best_score": rebuilt_state.get("development_best_score"),
    }


def failure_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "candidate_ids": [], "messages": Counter(), "tags": Counter()}
    )
    for record in records:
        evaluation = record.get("evaluation", {})
        if evaluation.get("valid") is not False:
            continue
        failure = evaluation.get("failure") or {}
        category = str(failure.get("category") or "unknown")
        message = str(failure.get("message") or "").splitlines()[0][:240]
        group = groups[category]
        group["count"] += 1
        group["candidate_ids"].append(record.get("candidate_id"))
        if message:
            group["messages"][message] += 1
        group["tags"].update(record.get("tags") or [])
    rendered = {}
    for category, group in sorted(groups.items(), key=lambda item: (-item[1]["count"], item[0])):
        rendered[category] = {
            "count": group["count"],
            "recent_candidate_ids": group["candidate_ids"][-5:],
            "common_messages": group["messages"].most_common(5),
            "common_tags": group["tags"].most_common(5),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "invalid_candidate_count": sum(group["count"] for group in groups.values()),
        "groups": rendered,
    }


def validate_campaign_spec(
    campaign: dict[str, Any],
    config: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    campaign_id = campaign.get("campaign_id")
    if campaign.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        errors.append(f"campaign schema_version must be {CAMPAIGN_SCHEMA_VERSION}")
    if not isinstance(campaign_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", campaign_id or ""
    ):
        errors.append("campaign_id must contain only letters, digits, '.', '_' or '-'")
    for key in ("objective", "rationale", "head_candidate_id"):
        if not str(campaign.get(key, "")).strip():
            errors.append(f"campaign.{key} is required")
    if not str(campaign.get("external_contract_digest", "")).strip():
        errors.append("campaign.external_contract_digest is required")
    affected = campaign.get("affected_modules")
    if not isinstance(affected, list) or not affected or not all(
        isinstance(item, str) and item.strip() for item in affected
    ):
        errors.append("campaign.affected_modules must be a nonempty string list")
    errors.extend(validate_intended_descriptors(campaign.get("target_descriptors"), config))
    if campaign.get("descriptor_policy") not in DESCRIPTOR_POLICIES:
        errors.append(f"campaign.descriptor_policy must be one of {DESCRIPTOR_POLICIES}")
    budget = campaign.get("budget")
    if not isinstance(budget, dict):
        errors.append("campaign.budget must be an object")
        budget = {}
    for key in ("max_candidates", "max_evaluator_runs", "max_elapsed_seconds"):
        value = budget.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"campaign.budget.{key} must be a positive integer")
    milestones = campaign.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        errors.append("campaign.milestones must be a nonempty list")
        milestones = []
    milestone_ids: list[str] = []
    configured_hard = set(config["evaluation"]["hard_constraints"])
    for index, milestone in enumerate(milestones):
        if not isinstance(milestone, dict):
            errors.append(f"campaign.milestones[{index}] must be an object")
            continue
        milestone_id = milestone.get("milestone_id")
        if not isinstance(milestone_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", milestone_id
        ):
            errors.append(f"campaign.milestones[{index}].milestone_id is invalid")
        else:
            milestone_ids.append(milestone_id)
        if not str(milestone.get("objective", "")).strip():
            errors.append(f"campaign.milestones[{index}].objective is required")
        errors.extend(
            f"campaign.milestones[{index}]: {error}"
            for error in validate_intended_descriptors(
                milestone.get("target_descriptors"), config
            )
        )
        if milestone.get("descriptor_policy") not in DESCRIPTOR_POLICIES:
            errors.append(
                f"campaign.milestones[{index}].descriptor_policy must be one of "
                f"{DESCRIPTOR_POLICIES}"
            )
        required = milestone.get("required_hard_constraints")
        if not isinstance(required, list) or not required or not all(
            isinstance(item, str) and item in configured_hard for item in required
        ):
            errors.append(
                f"campaign.milestones[{index}].required_hard_constraints must be "
                "a nonempty subset of configured hard constraints"
            )
        regression = milestone.get("max_primary_regression_fraction")
        if not numeric(regression) or regression < 0:
            errors.append(
                f"campaign.milestones[{index}].max_primary_regression_fraction "
                "must be nonnegative"
            )
    if len(milestone_ids) != len(set(milestone_ids)):
        errors.append("campaign milestone IDs must be unique")

    final_gate = campaign.get("final_gate")
    if not isinstance(final_gate, dict):
        errors.append("campaign.final_gate must be an object")
        final_gate = {}
    final_required = final_gate.get("required_hard_constraints")
    if not isinstance(final_required, list) or not final_required or not all(
        isinstance(item, str) and item in configured_hard for item in final_required
    ):
        errors.append(
            "campaign.final_gate.required_hard_constraints must be a nonempty "
            "subset of configured hard constraints"
        )
    final_regression = final_gate.get("max_regression_from_start_fraction")
    if not numeric(final_regression) or final_regression < 0:
        errors.append(
            "campaign.final_gate.max_regression_from_start_fraction must be nonnegative"
        )
    min_score = final_gate.get("min_primary_score")
    if min_score is not None and not numeric(min_score):
        errors.append("campaign.final_gate.min_primary_score must be null or numeric")
    if not isinstance(final_gate.get("require_validation"), bool):
        errors.append("campaign.final_gate.require_validation must be boolean")
    termination = campaign.get("termination")
    if not isinstance(termination, dict) or not str(
        termination.get("rollback_candidate_id", "")
    ).strip():
        errors.append("campaign.termination.rollback_candidate_id is required")
    by_id = records_by_id(records)
    head_id = str(campaign.get("head_candidate_id", ""))
    head = by_id.get(head_id)
    if head is None:
        errors.append(f"campaign head is absent from archive: {head_id}")
    elif head not in valid_records([head], config):
        errors.append("campaign head must be a valid, descriptor-complete candidate")
    elif isinstance(termination, dict) and termination.get(
        "rollback_candidate_id"
    ) not in by_id:
        errors.append("campaign rollback candidate is absent from archive")
    return errors


def score_within_regression(
    score: float,
    baseline: float,
    direction: str,
    fraction: float,
) -> bool:
    allowance = max(abs(baseline), 1e-12) * max(fraction, 0.0)
    if direction == "maximize":
        return score >= baseline - allowance
    return score <= baseline + allowance


def campaign_elapsed_seconds(campaign: dict[str, Any]) -> float:
    started = datetime.fromisoformat(str(campaign["started_at"]))
    return max((datetime.now(timezone.utc) - started).total_seconds(), 0.0)


def campaign_budget_errors(
    campaign: dict[str, Any], candidate: dict[str, Any] | None = None
) -> list[str]:
    budget = campaign["budget"]
    used = campaign.get(
        "budget_used", {"candidates": 0, "evaluator_runs": 0}
    )
    additional_candidates = 1 if candidate is not None else 0
    additional_runs = 0
    if candidate is not None:
        samples = candidate.get("evaluation", {}).get("samples", [])
        additional_runs = max(len(samples), 1)
    errors = []
    if int(used.get("candidates", 0)) + additional_candidates > int(
        budget["max_candidates"]
    ):
        errors.append("Campaign candidate budget would be exceeded")
    if int(used.get("evaluator_runs", 0)) + additional_runs > int(
        budget["max_evaluator_runs"]
    ):
        errors.append("Campaign evaluator-run budget would be exceeded")
    if campaign_elapsed_seconds(campaign) > int(budget["max_elapsed_seconds"]):
        errors.append("Campaign elapsed-time budget is exhausted")
    return errors


def campaign_is_exhausted(campaign: dict[str, Any]) -> bool:
    budget = campaign["budget"]
    used = campaign.get(
        "budget_used", {"candidates": 0, "evaluator_runs": 0}
    )
    return (
        int(used.get("candidates", 0)) >= int(budget["max_candidates"])
        or int(used.get("evaluator_runs", 0))
        >= int(budget["max_evaluator_runs"])
        or campaign_elapsed_seconds(campaign) >= int(budget["max_elapsed_seconds"])
    )


def campaign_final_gate_errors(
    campaign: dict[str, Any],
    state: dict[str, Any],
    records: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if int(campaign.get("step", 0)) != len(campaign.get("milestones", [])):
        errors.append("not all Campaign milestones are complete")
    by_id = records_by_id(records)
    head = by_id.get(str(campaign.get("head_candidate_id", "")))
    if head is None or head not in valid_records([head] if head else [], config):
        return errors + ["Campaign head is not a valid candidate"]
    gate = campaign["final_gate"]
    hard = head["evaluation"]["hard_constraints"]
    for name in gate["required_hard_constraints"]:
        if hard.get(name) is not True:
            errors.append(f"Campaign final hard constraint did not pass: {name}")
    score = float(head["evaluation"]["primary_score"])
    start_score = float(campaign["start_primary_score"])
    if not score_within_regression(
        score,
        start_score,
        config["evaluation"]["direction"],
        float(gate["max_regression_from_start_fraction"]),
    ):
        errors.append("Campaign final score exceeds allowed regression from start")
    minimum = gate.get("min_primary_score")
    if numeric(minimum):
        if config["evaluation"]["direction"] == "maximize" and score < minimum:
            errors.append("Campaign final score is below min_primary_score")
        if config["evaluation"]["direction"] == "minimize" and score > minimum:
            errors.append("Campaign final score is above min_primary_score")
    if gate.get("require_validation") and state.get("verified_best_id") != head.get(
        "candidate_id"
    ):
        errors.append("Campaign head is not the validation-verified leader")
    return errors


def campaign_file(paths: dict[str, Path], campaign_id: str) -> Path:
    return paths["campaigns"] / f"{campaign_id}.json"


def cmd_migrate_project(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    old_version = config.get("schema_version")
    if old_version not in (1, 2):
        raise EvolutionError("migrate-project accepts only project schema version 1 or 2")
    migrated = copy.deepcopy(config)
    migrated["schema_version"] = PROJECT_SCHEMA_VERSION
    adapter_path = Path(args.adapter_path).resolve()
    if not adapter_path.is_file():
        raise EvolutionError(f"adapter file is missing: {adapter_path}")
    adapter_sha256 = args.adapter_sha256 or sha256_file(adapter_path)
    migrated["adapter"] = {
        "interface_version": 1,
        "path": str(adapter_path),
        "sha256": adapter_sha256,
    }
    migrated["isolation"] = {
        "enforcement": args.isolation_enforcement,
        "trusted_process_acknowledged": args.trusted_process_acknowledged,
        "worker_workspace_root": args.worker_workspace_root,
        "controller_only_roots": list(args.controller_only_root),
    }
    migrated.setdefault("commands", {})[
        "isolation_preflight"
    ] = args.isolation_preflight_command
    hard = migrated.setdefault("evaluation", {}).setdefault(
        "hard_constraints", []
    )
    if "scope_passed" not in hard:
        hard.append("scope_passed")
    migrated.setdefault("search", {}).setdefault("max_open_proposals", 3)
    errors, warnings = validate_config(migrated, config_path, require_state=False)
    if errors:
        raise EvolutionError(
            "Migrated project adapter would be invalid:\n- " + "\n- ".join(errors)
        )
    backup = config_path.with_name(f"project.schema{old_version}.backup.json")
    if backup.exists():
        raise EvolutionError(f"Refusing to overwrite project backup: {backup}")
    atomic_write_json(backup, config)
    atomic_write_json(config_path, migrated)
    emit(
        {
            "migrated": True,
            "config": str(config_path),
            "backup": str(backup),
            "warnings": warnings,
            "next": "Run rebuild-state, then doctor. Abort or migrate any active Campaign v1 before planning.",
        }
    )


def cmd_init(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    evolution_dir = project_root / ".evolution"
    config_path = evolution_dir / "project.json"
    if config_path.exists():
        raise EvolutionError(f"Refusing to overwrite existing project adapter: {config_path}")
    template_path = Path(__file__).resolve().parent.parent / "assets" / "project.template.json"
    config = load_json(template_path)
    candidate_root = Path(args.candidate_root).resolve()
    state_root_value = args.state_root or ".evolution/state"
    config["project"].update(
        {
            "name": args.name or project_root.name,
            "execution_environment": args.execution_environment,
            "candidate_root": str(candidate_root),
            "state_root": state_root_value,
            "mutable_roots": [str(candidate_root)],
        }
    )
    resolved_initial_state = (
        Path(state_root_value)
        if portable_is_absolute(state_root_value)
        else project_root / state_root_value
    ).resolve()
    config["isolation"].update(
        {
            "worker_workspace_root": str(candidate_root),
            "controller_only_roots": sorted(
                {str(project_root), str(resolved_initial_state)}
            ),
        }
    )
    evolution_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config_path, config)
    paths = state_paths(config, config_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["campaigns"].mkdir(parents=True, exist_ok=True)
    with closing(open_event_store(paths["events"])):
        pass
    rebuild_views(config, paths)
    emit(
        {
            "initialized": True,
            "config": str(config_path),
            "state_root": str(paths["root"]),
            "next": "Fill project commands, protected roots, datasets, metrics, and completion policy; then run validate.",
        }
    )


def cmd_validate(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    errors, warnings = validate_config(config, config_path, not args.no_state)
    emit({"valid": not errors, "errors": errors, "warnings": warnings})
    if errors:
        raise SystemExit(1)


def cmd_plan_round(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    events = read_events(paths["events"])
    records, _, proposals, state, campaigns = replay_events(config, events)
    open_proposals = [item for item in proposals if item.get("status") == "open"]
    if len(open_proposals) >= int(config["search"]["max_open_proposals"]):
        raise EvolutionError("Maximum number of open proposals is already reached")
    decision = choose_mode(config, state)
    default_mode = decision["mode"]
    if default_mode == "stop":
        raise EvolutionError(f"Cannot create proposal: {decision['reason']}")
    selected_mode = args.mode or default_mode
    override = None
    if selected_mode != default_mode:
        if default_mode == "campaign":
            raise EvolutionError("Active Campaign mode cannot be overridden")
        if not args.override_reason or not args.override_evidence:
            raise EvolutionError(
                "Mode override requires --override-reason and at least one --override-evidence"
            )
        override = {
            "reason": args.override_reason,
            "evidence": list(args.override_evidence),
        }
    elif args.override_reason or args.override_evidence:
        raise EvolutionError("Override evidence is only valid when selected mode differs")

    active = state.get("active_campaign")
    campaign = None
    if active:
        if selected_mode != "campaign":
            raise EvolutionError("An active Campaign requires Campaign proposals")
        campaign = campaigns.get(str(active["campaign_id"]))
        if campaign is None or campaign.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
            raise EvolutionError(
                "Active legacy Campaign must be closed or migrated before plan-round"
            )
        if campaign_is_exhausted(campaign):
            raise EvolutionError("Campaign budget is exhausted")
        if any(
            proposal.get("status") == "open"
            and proposal.get("campaign_id") == campaign.get("campaign_id")
            for proposal in proposals
        ):
            raise EvolutionError("Active Campaign already has an open proposal")
    elif selected_mode == "campaign":
        raise EvolutionError("Campaign mode requires an active Campaign")

    parent_choice = choose_parent(selected_mode, config, state, records)
    parent = records_by_id(records)[parent_choice["parent_id"]]
    explicit_target = (
        load_json(Path(args.target_descriptors).resolve())
        if args.target_descriptors
        else None
    )
    intended, target_reason = choose_target_descriptors(
        selected_mode,
        parent,
        config,
        state,
        campaign=campaign,
        explicit=explicit_target,
    )
    target_cell = cell_key_from_descriptors(intended, config)
    descriptor_policy = args.descriptor_policy
    if campaign is not None:
        milestone = campaign_current_milestone(campaign)
        descriptor_policy = milestone.get(
            "descriptor_policy", campaign.get("descriptor_policy", descriptor_policy)
        )
    workspace_root = str(args.workspace_root).strip()
    workspace_errors = validate_workspace_root(workspace_root, config)
    if workspace_errors:
        raise EvolutionError("Invalid worker workspace:\n- " + "\n- ".join(workspace_errors))
    isolation_proof = load_json(Path(args.isolation_proof).resolve())
    proof_errors = validate_isolation_proof(isolation_proof, workspace_root, config)
    if proof_errors:
        raise EvolutionError(
            "Isolation preflight did not establish the worker boundary:\n- "
            + "\n- ".join(proof_errors)
        )
    if any(
        paths_overlap(str(proposal.get("workspace_root", "")), workspace_root)
        for proposal in proposals
    ):
        raise EvolutionError(
            "worker workspace overlaps a workspace already assigned to a proposal"
        )
    worker_proposal_path = str(args.worker_proposal_output).strip()
    if not portable_is_absolute(worker_proposal_path) or not path_is_within(
        worker_proposal_path, workspace_root
    ):
        raise EvolutionError(
            "--worker-proposal-output must be an absolute path inside workspace_root"
        )

    context = choose_context(
        selected_mode,
        parent["candidate_id"],
        config,
        state,
        records,
        target_descriptors=intended,
        target_cell=target_cell,
    )
    add_supplemental_lessons(context, getattr(args, "lesson_index", None), config)
    proposal_id = args.proposal_id or (
        "p-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    milestone = campaign_current_milestone(campaign) if campaign else None
    base_sequence = events[-1]["sequence"] if events else 0
    proposal = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "base_event_sequence": base_sequence,
        "default_mode": default_mode,
        "selected_mode": selected_mode,
        "default_mode_reason": decision["reason"],
        "override": override,
        "target_cell": target_cell,
        "target_selection_reason": target_reason,
        "intended_descriptors": intended,
        "descriptor_policy": descriptor_policy,
        "parent": {
            "candidate_id": parent["candidate_id"],
            "code_ref": parent["code_ref"],
        },
        "parent_selection_reason": parent_choice["reason"],
        "workspace_root": workspace_root,
        "worker_proposal_path": worker_proposal_path,
        "isolation_proof": isolation_proof,
        "context_digest": canonical_digest(context),
        "context": context,
        "campaign_id": campaign.get("campaign_id") if campaign else None,
        "milestone_id": milestone.get("milestone_id") if milestone else None,
        "status": "open",
    }
    proposal["proposal_digest"] = proposal_digest_value(proposal)
    errors = validate_proposal_record(proposal, config, records, campaigns)
    if errors:
        raise EvolutionError("Generated proposal is invalid:\n- " + "\n- ".join(errors))
    _, runtime = commit_event_and_rebuild(
        config,
        paths,
        "proposal_created",
        {"proposal": proposal},
        entity_kind="proposal",
        entity_id=proposal_id,
        expected_last_sequence=base_sequence,
    )
    _, _, rebuilt_proposals, _, _ = runtime
    created = next(
        item for item in rebuilt_proposals if item["proposal_id"] == proposal_id
    )
    try:
        atomic_write_json(Path(worker_proposal_path), created)
    except Exception as exc:
        raise EvolutionError(
            f"proposal {proposal_id} was committed but worker copy failed: "
            f"{type(exc).__name__}: {exc}; copy the controller proposal view or cancel it"
        ) from exc
    emit(
        {
            "created": True,
            "proposal_id": proposal_id,
            "proposal_digest": created["proposal_digest"],
            "mode": selected_mode,
            "target_cell": target_cell,
            "parent_id": parent["candidate_id"],
            "context_digest": created["context_digest"],
            "workspace_root": workspace_root,
            "proposal_file": str(paths["proposals"] / f"{proposal_id}.json"),
            "worker_proposal_file": worker_proposal_path,
        }
    )


def cmd_cancel_proposal(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, proposals, _, _ = load_runtime(config, paths)
    del records
    proposal = next(
        (item for item in proposals if item.get("proposal_id") == args.proposal_id),
        None,
    )
    if proposal is None or proposal.get("status") != "open":
        raise EvolutionError("Proposal is absent or not open")
    commit_event_and_rebuild(
        config,
        paths,
        "proposal_cancelled",
        {"proposal_id": args.proposal_id, "reason": args.reason},
        entity_kind="proposal-terminal",
        entity_id=args.proposal_id,
    )
    emit({"cancelled": True, "proposal_id": args.proposal_id})


def cmd_select_mode(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config, paths = load_project(config_path)
    _, _, _, state, _ = load_runtime(config, paths)
    decision = choose_mode(config, state)
    if args.write:
        commit_event_and_rebuild(
            config,
            paths,
            "mode_selected",
            {"mode": decision["mode"], "reason": decision["reason"]},
        )
    emit(decision)


def cmd_select_parent(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, _, state, _ = load_runtime(config, paths)
    mode = args.mode or state.get("last_mode")
    if mode not in MODES:
        raise EvolutionError("Provide --mode or write a valid last mode with select-mode --write")
    emit({"mode": mode, **choose_parent(mode, config, state, records)})


def cmd_select_context(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, _, state, _ = load_runtime(config, paths)
    mode = args.mode or state.get("last_mode")
    if mode not in MODES:
        raise EvolutionError("Provide --mode or write a valid last mode with select-mode --write")
    parent_id = args.parent
    if not parent_id:
        parent_id = choose_parent(mode, config, state, records)["parent_id"]
    pack = choose_context(mode, parent_id, config, state, records)
    add_supplemental_lessons(pack, getattr(args, "lesson_index", None), config)
    output_path = Path(args.output).resolve() if args.output else paths["context_pack"]
    atomic_write_json(output_path, pack)
    inspiration_ids = [item["candidate_id"] for item in pack["inspirations"]]
    emit(
        {
            "written": str(output_path),
            "binding": "preview_only; use plan-round for a consumable context",
            "mode": mode,
            "parent_id": parent_id,
            "inspiration_ids": inspiration_ids,
            "negative_lesson_ids": [item["candidate_id"] for item in pack["negative_lessons"]],
        }
    )


def cmd_update(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, proposals, state, campaigns = load_runtime(config, paths)
    candidate = load_json(Path(args.result).resolve())
    emit(
        update_archive(
            config, paths, state, records, proposals, campaigns, candidate
        )
    )


def cmd_seed(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, _, _, _ = load_runtime(config, paths)
    candidate = load_json(Path(args.result).resolve())
    emit(seed_archive(config, paths, records, candidate))


def cmd_summarize_failures(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, _, _, _ = load_runtime(config, paths)
    summary = failure_summary(records)
    output_path = Path(args.output).resolve() if args.output else paths["failure_summary"]
    atomic_write_json(output_path, summary)
    emit({"written": str(output_path), **summary})


def cmd_record_evaluation(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, evaluations, _, state, _ = load_runtime(config, paths)
    supplied = load_json(Path(args.result).resolve())
    errors = validate_protocol_evaluation_result(supplied, config)
    if errors:
        raise EvolutionError(
            "Protocol evaluation result is invalid:\n- " + "\n- ".join(errors)
        )
    candidate_id = supplied["candidate_id"]
    candidate = records_by_id(records).get(candidate_id)
    if candidate is None:
        raise EvolutionError(f"Evaluated candidate is absent from archive: {candidate_id}")
    if candidate.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise EvolutionError(
            "Legacy candidate lacks schema-v3 proposal/scope binding; "
            "re-evaluate and ingest it as a schema-v3 proposed candidate before validation or holdout"
        )
    if supplied["evaluation_id"] in {
        record.get("evaluation_id") for record in evaluations
    }:
        raise EvolutionError(
            f"Evaluation ID already exists: {supplied['evaluation_id']}"
        )
    protocol = supplied["protocol"]
    fingerprint = protocol_fingerprint(supplied["evaluation"])
    existing_fingerprint = state.get(f"{protocol}_protocol_fingerprint")
    if existing_fingerprint and fingerprint != existing_fingerprint:
        raise EvolutionError(
            f"{protocol.capitalize()} evaluator/dataset/command protocol changed; "
            "start a new state root"
        )
    if protocol == "holdout":
        if not args.stage_end_confirmed:
            raise EvolutionError(
                "Holdout recording requires --stage-end-confirmed; holdout is sealed until stage end"
            )
        if state.get("holdout_status") == "completed":
            raise EvolutionError("A sealed holdout evaluation is already recorded")
        if config["completion"].get("require_validation"):
            expected_candidate = state.get("verified_best_id")
            reason = "verified validation leader"
        else:
            expected_candidate = state.get("development_best_id")
            reason = "development leader"
        if not expected_candidate:
            raise EvolutionError(f"No {reason} is available for sealed holdout")
        if candidate_id != expected_candidate:
            raise EvolutionError(
                f"Sealed holdout candidate must equal the {reason}: {expected_candidate}"
            )

    record = copy.deepcopy(supplied)
    record["sequence"] = len(evaluations) + 1
    record["recorded_at"] = utc_now()
    previous_verified = state.get("verified_best_id")
    _, runtime = commit_event_and_rebuild(
        config,
        paths,
        "protocol_evaluation_recorded",
        {"record": record},
        entity_kind="evaluation",
        entity_id=record["evaluation_id"],
    )
    _, rebuilt_evaluations, _, rebuilt_state, _ = runtime
    emit(
        {
            "recorded": True,
            "evaluation_id": record["evaluation_id"],
            "candidate_id": candidate_id,
            "protocol": protocol,
            "valid": record["evaluation"]["valid"],
            "verified_promotion": (
                protocol == "validation"
                and rebuilt_state.get("verified_best_id") == candidate_id
                and previous_verified != candidate_id
            ),
            "verified_best_id": rebuilt_state.get("verified_best_id"),
            "verified_best_score": rebuilt_state.get("verified_best_score"),
            "holdout_status": rebuilt_state.get("holdout_status"),
            "evaluation_count": len(rebuilt_evaluations),
        }
    )


def cmd_rebuild_state(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    errors, warnings = validate_config(config, config_path, require_state=False)
    if errors:
        raise EvolutionError("Project adapter is invalid:\n- " + "\n- ".join(errors))
    paths = state_paths(config, config_path)
    integrity = event_store_integrity(paths["events"])
    if integrity != "ok":
        raise EvolutionError(f"event store integrity check failed: {integrity}")
    records, evaluations, proposals, state, campaigns = rebuild_views(config, paths)
    emit(
        {
            "rebuilt": True,
            "warnings": warnings,
            "event_store": str(paths["events"]),
            "candidate_count": len(records),
            "protocol_evaluation_count": len(evaluations),
            "campaign_count": len(campaigns),
            "proposal_count": len(proposals),
            "development_best_id": state.get("development_best_id"),
            "verified_best_id": state.get("verified_best_id"),
            "holdout_status": state.get("holdout_status"),
        }
    )


def cmd_doctor(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    errors, warnings = validate_config(config, config_path, require_state=True)
    paths = state_paths(config, config_path)
    event_count = None
    if event_store_integrity(paths["events"]) == "ok":
        event_count = len(read_events(paths["events"]))
    emit(
        {
            "healthy": not errors,
            "errors": errors,
            "warnings": warnings,
            "event_store_integrity": event_store_integrity(paths["events"]),
            "event_count": event_count,
            "recovery": "run rebuild-state when a materialized view differs",
        }
    )
    if errors:
        raise SystemExit(1)


def cmd_migrate_state(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    errors, _ = validate_config(config, config_path, require_state=False)
    if errors:
        raise EvolutionError("Project adapter is invalid:\n- " + "\n- ".join(errors))
    paths = state_paths(config, config_path)
    legacy_records = read_jsonl(paths["archive"])
    legacy_state = load_json(paths["mode_state"]) if paths["mode_state"].exists() else {}
    if paths["events"].exists() and read_events(paths["events"]):
        raise EvolutionError("Event store is not empty; refusing legacy migration")
    events: list[tuple[str, dict[str, Any], str | None, str | None]] = []
    for record in legacy_records:
        candidate_id = str(record.get("candidate_id", ""))
        if not candidate_id:
            raise EvolutionError("Legacy archive contains a record without candidate_id")
        events.append(
            (
                "candidate_recorded",
                {"record": record},
                "candidate",
                candidate_id,
            )
        )
    if isinstance(legacy_state, dict):
        events.append(
            (
                "legacy_state_imported",
                {
                    "context_use_counts": legacy_state.get("context_use_counts", {}),
                    "last_mode": legacy_state.get("last_mode"),
                    "last_mode_reason": legacy_state.get("last_mode_reason"),
                },
                None,
                None,
            )
        )
    if paths["campaigns"].exists():
        for path in sorted(paths["campaigns"].glob("*.json")):
            campaign = load_json(path)
            campaign_id = str(campaign.get("campaign_id", ""))
            if not campaign_id:
                raise EvolutionError(f"Legacy campaign has no campaign_id: {path}")
            events.append(
                (
                    "campaign_snapshot_imported",
                    {"campaign": campaign},
                    "campaign",
                    campaign_id,
                )
            )
    _, runtime = commit_events_and_rebuild(config, paths, events)
    records, evaluations, proposals, state, campaigns = runtime
    emit(
        {
            "migrated": True,
            "candidate_count": len(records),
            "protocol_evaluation_count": len(evaluations),
            "campaign_count": len(campaigns),
            "proposal_count": len(proposals),
            "legacy_development_best_id": state.get("development_best_id"),
            "note": "Legacy candidates remain historical; re-evaluate through a schema-v3 proposal before validation promotion.",
        }
    )


def cmd_campaign_start(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, _, state, campaigns = load_runtime(config, paths)
    if state.get("active_campaign"):
        raise EvolutionError("An active campaign already exists; close it before starting another")
    supplied = load_json(Path(args.spec).resolve())
    errors = validate_campaign_spec(supplied, config, records)
    if errors:
        raise EvolutionError("Campaign specification is invalid:\n- " + "\n- ".join(errors))
    campaign = copy.deepcopy(supplied)
    head = records_by_id(records)[campaign["head_candidate_id"]]
    started_at = utc_now()
    campaign.update(
        {
            "status": "active",
            "step": 0,
            "current_milestone_index": 0,
            "start_candidate_id": campaign["head_candidate_id"],
            "start_primary_score": head["evaluation"]["primary_score"],
            "milestone_start_candidate_id": campaign["head_candidate_id"],
            "budget_used": {"candidates": 0, "evaluator_runs": 0},
            "milestone_usage": {},
            "started_at": started_at,
            "updated_at": started_at,
            "history": [],
        }
    )
    path = campaign_file(paths, campaign["campaign_id"])
    if campaign["campaign_id"] in campaigns:
        raise EvolutionError(f"Campaign ID already exists: {campaign['campaign_id']}")
    _, runtime = commit_event_and_rebuild(
        config,
        paths,
        "campaign_started",
        {"campaign": campaign},
        entity_kind="campaign",
        entity_id=campaign["campaign_id"],
    )
    _, _, _, rebuilt_state, _ = runtime
    emit(
        {
            "started": True,
            "campaign": str(path),
            "active_campaign": rebuilt_state["active_campaign"],
        }
    )


def cmd_campaign_advance(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, proposals, state, campaigns = load_runtime(config, paths)
    active = state.get("active_campaign")
    if not isinstance(active, dict) or not active.get("campaign_id"):
        raise EvolutionError("No active campaign")
    campaign_id = str(active["campaign_id"])
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise EvolutionError(f"Active campaign is absent from event log: {campaign_id}")
    if campaign.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        raise EvolutionError("Legacy Campaign cannot advance under Campaign v2 rules")
    milestone = campaign_current_milestone(campaign)
    if milestone is None:
        raise EvolutionError("Campaign has no remaining milestone")
    by_id = records_by_id(records)
    candidate = by_id.get(args.candidate_id)
    if candidate is None:
        raise EvolutionError(f"Candidate is absent from archive: {args.candidate_id}")
    if candidate not in valid_records([candidate], config):
        raise EvolutionError("Campaign head must remain valid and descriptor-complete")
    if candidate.get("campaign_id") != campaign_id:
        raise EvolutionError("Candidate campaign_id does not match the active campaign")
    if candidate.get("parent_id") != campaign.get("head_candidate_id"):
        raise EvolutionError("Campaign candidate parent_id must equal the current campaign head")
    proposal = next(
        (
            item
            for item in proposals
            if item.get("proposal_id") == candidate.get("proposal_id")
        ),
        None,
    )
    if proposal is None or proposal.get("status") != "consumed":
        raise EvolutionError("Campaign advance requires a consumed proposal")
    if proposal.get("milestone_id") != milestone.get("milestone_id"):
        raise EvolutionError("Campaign candidate belongs to a different milestone")
    hard = candidate["evaluation"]["hard_constraints"]
    for name in milestone["required_hard_constraints"]:
        if hard.get(name) is not True:
            raise EvolutionError(f"Campaign milestone hard constraint failed: {name}")
    start_candidate = records_by_id(records).get(
        str(campaign.get("milestone_start_candidate_id"))
    )
    if start_candidate is None:
        raise EvolutionError("Campaign milestone start candidate is absent")
    if not score_within_regression(
        float(candidate["evaluation"]["primary_score"]),
        float(start_candidate["evaluation"]["primary_score"]),
        config["evaluation"]["direction"],
        float(milestone["max_primary_regression_fraction"]),
    ):
        raise EvolutionError("Campaign candidate exceeds milestone regression allowance")
    _, runtime = commit_event_and_rebuild(
        config,
        paths,
        "campaign_advanced",
        {
            "campaign_id": campaign_id,
            "candidate_id": args.candidate_id,
            "milestone_id": milestone["milestone_id"],
            "note": args.note or "",
        },
    )
    _, _, _, rebuilt_state, rebuilt_campaigns = runtime
    rebuilt_campaign = rebuilt_campaigns[campaign_id]
    emit(
        {
            "advanced": True,
            "campaign_id": campaign_id,
            "head_candidate_id": args.candidate_id,
            "step": rebuilt_state["active_campaign"]["step"],
            "milestone_id": rebuilt_state["active_campaign"]["milestone_id"],
            "milestone_count": len(rebuilt_campaign["milestones"]),
        }
    )


def cmd_campaign_close(args: argparse.Namespace) -> None:
    config, paths = load_project(Path(args.config))
    records, _, proposals, state, campaigns = load_runtime(config, paths)
    active = state.get("active_campaign")
    if not isinstance(active, dict) or not active.get("campaign_id"):
        raise EvolutionError("No active campaign")
    campaign_id = str(active["campaign_id"])
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise EvolutionError(f"Active campaign is absent from event log: {campaign_id}")
    if (
        campaign.get("schema_version") != CAMPAIGN_SCHEMA_VERSION
        and args.status != "aborted"
    ):
        raise EvolutionError("Legacy Campaign may only be closed as aborted")
    if any(
        proposal.get("status") == "open"
        and proposal.get("campaign_id") == campaign_id
        for proposal in proposals
    ):
        raise EvolutionError("Close or consume the open Campaign proposal first")
    if args.status == "completed":
        gate_errors = campaign_final_gate_errors(campaign, state, records, config)
        if gate_errors:
            raise EvolutionError(
                "Campaign final gate failed:\n- " + "\n- ".join(gate_errors)
            )
    if args.status == "budget_exhausted" and not campaign_is_exhausted(campaign):
        raise EvolutionError("Campaign budget is not exhausted")
    commit_event_and_rebuild(
        config,
        paths,
        "campaign_closed",
        {"campaign_id": campaign_id, "status": args.status, "reason": args.reason},
    )
    emit({"closed": True, "campaign_id": campaign_id, "status": args.status})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage evaluator-driven quality-diversity evolution metadata."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize a project adapter and empty state")
    init_parser.add_argument("--project-root", default=".")
    init_parser.add_argument("--name")
    init_parser.add_argument("--candidate-root", required=True)
    init_parser.add_argument("--state-root")
    init_parser.add_argument(
        "--execution-environment", choices=ENVIRONMENTS, default="native"
    )
    init_parser.set_defaults(func=cmd_init)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=".evolution/project.json")

    project_migrate_parser = subparsers.add_parser(
        "migrate-project", parents=[common]
    )
    project_migrate_parser.add_argument(
        "--isolation-enforcement", choices=ISOLATION_ENFORCEMENTS, required=True
    )
    project_migrate_parser.add_argument("--adapter-path", required=True)
    project_migrate_parser.add_argument("--adapter-sha256")
    project_migrate_parser.add_argument(
        "--trusted-process-acknowledged", action="store_true"
    )
    project_migrate_parser.add_argument("--worker-workspace-root", required=True)
    project_migrate_parser.add_argument(
        "--controller-only-root", action="append", required=True
    )
    project_migrate_parser.add_argument(
        "--isolation-preflight-command", required=True
    )
    project_migrate_parser.set_defaults(func=cmd_migrate_project)

    validate_parser = subparsers.add_parser("validate", parents=[common])
    validate_parser.add_argument("--no-state", action="store_true")
    validate_parser.set_defaults(func=cmd_validate)

    mode_parser = subparsers.add_parser("select-mode", parents=[common])
    mode_parser.add_argument("--write", action="store_true")
    mode_parser.set_defaults(func=cmd_select_mode)

    proposal_parser = subparsers.add_parser("plan-round", parents=[common])
    proposal_parser.add_argument("--proposal-id")
    proposal_parser.add_argument("--workspace-root", required=True)
    proposal_parser.add_argument("--worker-proposal-output", required=True)
    proposal_parser.add_argument("--isolation-proof", required=True)
    proposal_parser.add_argument("--mode", choices=MODES)
    proposal_parser.add_argument("--override-reason")
    proposal_parser.add_argument("--override-evidence", action="append")
    proposal_parser.add_argument("--target-descriptors")
    proposal_parser.add_argument("--lesson-index", help="Controller-owned compact evidence index to bind into context")
    proposal_parser.add_argument(
        "--descriptor-policy", choices=DESCRIPTOR_POLICIES, default="allow_novel"
    )
    proposal_parser.set_defaults(func=cmd_plan_round)

    cancel_parser = subparsers.add_parser("cancel-proposal", parents=[common])
    cancel_parser.add_argument("--proposal-id", required=True)
    cancel_parser.add_argument("--reason", required=True)
    cancel_parser.set_defaults(func=cmd_cancel_proposal)

    parent_parser = subparsers.add_parser("select-parent", parents=[common])
    parent_parser.add_argument("--mode", choices=MODES)
    parent_parser.set_defaults(func=cmd_select_parent)

    context_parser = subparsers.add_parser("select-context", parents=[common])
    context_parser.add_argument("--mode", choices=MODES)
    context_parser.add_argument("--parent")
    context_parser.add_argument("--output")
    context_parser.add_argument("--lesson-index")
    context_parser.set_defaults(func=cmd_select_context)

    update_parser = subparsers.add_parser("update", parents=[common])
    update_parser.add_argument("--result", required=True)
    update_parser.set_defaults(func=cmd_update)

    seed_parser = subparsers.add_parser("seed", parents=[common])
    seed_parser.add_argument("--result", required=True)
    seed_parser.set_defaults(func=cmd_seed)

    protocol_parser = subparsers.add_parser("record-evaluation", parents=[common])
    protocol_parser.add_argument("--result", required=True)
    protocol_parser.add_argument("--stage-end-confirmed", action="store_true")
    protocol_parser.set_defaults(func=cmd_record_evaluation)

    compatibility_parser = subparsers.add_parser("register-compatibility", parents=[common])
    compatibility_parser.add_argument("--record", required=True)
    compatibility_parser.set_defaults(func=cmd_register_compatibility)

    rebuild_parser = subparsers.add_parser("rebuild-state", parents=[common])
    rebuild_parser.set_defaults(func=cmd_rebuild_state)

    doctor_parser = subparsers.add_parser("doctor", parents=[common])
    doctor_parser.set_defaults(func=cmd_doctor)

    migrate_parser = subparsers.add_parser("migrate-state", parents=[common])
    migrate_parser.set_defaults(func=cmd_migrate_state)

    failure_parser = subparsers.add_parser("summarize-failures", parents=[common])
    failure_parser.add_argument("--output")
    failure_parser.set_defaults(func=cmd_summarize_failures)

    campaign_start_parser = subparsers.add_parser("campaign-start", parents=[common])
    campaign_start_parser.add_argument("--spec", required=True)
    campaign_start_parser.set_defaults(func=cmd_campaign_start)

    campaign_advance_parser = subparsers.add_parser("campaign-advance", parents=[common])
    campaign_advance_parser.add_argument("--candidate-id", required=True)
    campaign_advance_parser.add_argument("--note")
    campaign_advance_parser.set_defaults(func=cmd_campaign_advance)

    campaign_budget_parser = subparsers.add_parser("campaign-correct-budget", parents=[common])
    campaign_budget_parser.add_argument("--record", required=True)
    campaign_budget_parser.set_defaults(func=cmd_campaign_correct_budget)

    campaign_close_parser = subparsers.add_parser("campaign-close", parents=[common])
    campaign_close_parser.add_argument("--status", choices=CAMPAIGN_TERMINAL_STATUSES, required=True)
    campaign_close_parser.add_argument("--reason", required=True)
    campaign_close_parser.set_defaults(func=cmd_campaign_close)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except EvolutionError as exc:
        emit({"error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
