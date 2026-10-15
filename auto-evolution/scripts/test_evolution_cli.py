#!/usr/bin/env python3
"""Synthetic end-to-end tests for evolution_cli.py."""

from __future__ import annotations

import json
import hashlib
import ntpath
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
CLI = HERE / "evolution_cli.py"


class EvolutionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = self.root / "controller"
        self.candidate = self.root / "candidate"
        self.baseline = self.root / "baseline"
        self.state_root = self.root / "state"
        self.controller.mkdir()
        self.candidate.mkdir()
        self.baseline.mkdir()
        self.run_cli(
            "init",
            "--project-root",
            str(self.controller),
            "--candidate-root",
            str(self.candidate),
            "--state-root",
            str(self.state_root),
            "--name",
            "synthetic",
        )
        self.config_path = self.controller / ".evolution" / "project.json"
        self.adapter_path = self.controller / "synthetic_adapter.py"
        self.adapter_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        config = self.load_json(self.config_path)
        config["adapter"] = {
            "interface_version": 1,
            "path": str(self.adapter_path),
            "sha256": hashlib.sha256(self.adapter_path.read_bytes()).hexdigest(),
        }
        config["project"].update(
            {
                "baseline_root": str(self.baseline),
                "required_branch": "test-branch",
                "protected_roots": [str(self.baseline), str(self.controller)],
            }
        )
        config["commands"].update(
            {
                "isolation_preflight": "DO_NOT_EXECUTE isolation-preflight",
                "build": "DO_NOT_EXECUTE build",
                "smoke": "DO_NOT_EXECUTE smoke",
                "development": "DO_NOT_EXECUTE development",
                "validation": "DO_NOT_EXECUTE validation",
                "holdout": "DO_NOT_EXECUTE holdout",
            }
        )
        config["datasets"].update(
            {
                "smoke": "smoke-manifest",
                "development": "development-manifest",
                "validation": "validation-manifest",
                "holdout": "sealed-holdout-manifest",
            }
        )
        config["evaluation"].update(
            {
                "noise_band": 0.01,
                "descriptor_keys": ["family", "execution"],
                "descriptor_space": {
                    "family": ["a", "b"],
                    "execution": ["serial", "parallel"],
                },
            }
        )
        config["search"].update(
            {
                "bootstrap_valid_candidates": 2,
                "plateau_window": 2,
                "coverage_target": 0.5,
                "target_cells": 4,
                "repeated_bottleneck_threshold": 2,
            }
        )
        self.write_json(self.config_path, config)
        self.candidate_requests: dict[str, dict] = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str, expected: int = 0) -> dict:
        process = subprocess.run(
            [sys.executable, str(CLI), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            process.returncode,
            expected,
            msg=f"stdout={process.stdout}\nstderr={process.stderr}",
        )
        return json.loads(process.stdout)

    @staticmethod
    def load_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")

    def candidate_result(
        self,
        candidate_id: str,
        score: float | None,
        family: str = "a",
        execution: str = "serial",
        *,
        valid: bool = True,
        parent_id: str | None = None,
        mode: str = "explore",
        campaign_id: str | None = None,
        repair_score: float | None = None,
        build_passed: bool | None = None,
    ) -> Path:
        workspace = self.candidate / candidate_id
        workspace.mkdir(parents=True, exist_ok=True)
        result = {
            "schema_version": 3,
            "candidate_id": candidate_id,
            "proposal_id": None,
            "proposal_digest": None,
            "context_digest": None,
            "workspace_root": str(workspace),
            "code_ref": str(workspace),
            "hypothesis": f"synthetic hypothesis for {candidate_id}",
            "changed_files": ["src/example.cpp"],
            "tags": [family, execution],
            "bottlenecks": ["memory"],
            "scope": {
                "workspace_root": str(workspace),
                "manifest_digest": f"sha256:manifest-{candidate_id}",
                "protected_inputs_digest": "sha256:protected",
            },
            "evaluation": {
                "protocol": "development",
                "protocol_id": "synthetic-development-v1",
                "valid": valid,
                "primary_score": score,
                "samples": [score, score, score] if valid else [],
                "aggregation": {"method": "median", "value": score},
                "provenance": {
                    "dataset_id": "development-manifest",
                    "dataset_digest": "sha256:development",
                    "evaluator_id": "synthetic-evaluator-v1",
                    "evaluator_digest": "sha256:evaluator",
                    "command_digest": "sha256:command",
                    "environment": {"hardware": "synthetic"},
                },
                "hard_constraints": {
                    "build_passed": valid if build_passed is None else build_passed,
                    "correctness_passed": valid,
                    "scope_passed": True,
                },
                "auxiliary_metrics": {
                    "time_ms": 10.0,
                    "correctness_points": repair_score,
                },
                "descriptors": {"family": family, "execution": execution},
                "failure": None
                if valid
                else {"category": "build", "message": "synthetic compile failure"},
                "artifacts": {"log": str(self.root / f"{candidate_id}.log")},
            },
        }
        path = self.root / f"{candidate_id}.json"
        self.write_json(path, result)
        self.candidate_requests[str(path)] = {
            "parent_id": parent_id,
            "mode": mode,
            "campaign_id": campaign_id,
        }
        return path

    def protocol_result(
        self,
        evaluation_id: str,
        candidate_id: str,
        protocol: str,
        score: float | None,
        *,
        valid: bool = True,
        command_digest: str | None = None,
    ) -> Path:
        result = {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "candidate_id": candidate_id,
            "protocol": protocol,
            "evaluation": {
                "protocol": protocol,
                "protocol_id": f"synthetic-{protocol}-v1",
                "valid": valid,
                "primary_score": score,
                "samples": [score, score, score] if valid else [],
                "aggregation": {"method": "median", "value": score},
                "provenance": {
                    "dataset_id": f"{protocol}-manifest"
                    if protocol != "holdout"
                    else "sealed-holdout-manifest",
                    "dataset_digest": f"sha256:{protocol}",
                    "evaluator_id": "synthetic-evaluator-v1",
                    "evaluator_digest": "sha256:evaluator",
                    "command_digest": command_digest or f"sha256:{protocol}-command",
                    "environment": {"hardware": "synthetic"},
                },
                "hard_constraints": {
                    "build_passed": valid,
                    "correctness_passed": valid,
                    "scope_passed": True,
                },
                "auxiliary_metrics": {},
                "failure": None
                if valid
                else {"category": "correctness", "message": "synthetic failure"},
                "artifacts": {"log": str(self.root / f"{evaluation_id}.log")},
            },
        }
        path = self.root / f"{evaluation_id}.json"
        self.write_json(path, result)
        return path

    @staticmethod
    def portable_normalize(raw: str) -> str:
        return ntpath.normpath(raw).replace("\\", "/").rstrip("/").casefold()

    def isolation_proof(self, workspace: str, name: str) -> Path:
        config = self.load_json(self.config_path)
        roots = sorted(
            self.portable_normalize(str(root))
            for root in config["isolation"]["controller_only_roots"]
        )
        encoded = json.dumps(
            roots, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        proof = {
            "passed": True,
            "enforcement": config["isolation"]["enforcement"],
            "workspace_root": workspace,
            "controller_only_roots_digest": "sha256:"
            + hashlib.sha256(encoded).hexdigest(),
            "preflight_command_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    config["commands"]["isolation_preflight"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "preflight_artifact_digest": f"sha256:preflight-{name}",
            "protected_inputs_digest": "sha256:protected",
        }
        if config["isolation"]["enforcement"] == "trusted_process":
            proof["risk_acknowledged"] = True
        path = self.root / f"{name}-isolation.json"
        self.write_json(path, proof)
        return path

    def prepare_candidate(self, path: Path) -> tuple[Path, dict]:
        candidate = self.load_json(path)
        request = self.candidate_requests.get(str(path), {})
        workspace = candidate["workspace_root"]
        proposal_id = f"p-{candidate['candidate_id']}"
        default = self.run_cli("select-mode", "--config", str(self.config_path))
        args = [
            "plan-round",
            "--config",
            str(self.config_path),
            "--proposal-id",
            proposal_id,
            "--workspace-root",
            workspace,
            "--worker-proposal-output",
            str(Path(workspace) / "proposal.json"),
            "--isolation-proof",
            str(self.isolation_proof(workspace, candidate["candidate_id"])),
        ]
        requested_mode = request.get("mode")
        if requested_mode:
            args.extend(["--mode", requested_mode])
            if requested_mode != default["mode"]:
                args.extend(
                    [
                        "--override-reason",
                        "synthetic test override",
                        "--override-evidence",
                        "artifact:synthetic",
                    ]
                )
        planned = self.run_cli(*args)
        proposal = self.load_json(Path(planned["proposal_file"]))
        self.assertEqual(
            proposal, self.load_json(Path(planned["worker_proposal_file"]))
        )
        expected_campaign = request.get("campaign_id")
        if expected_campaign is not None:
            self.assertEqual(proposal["campaign_id"], expected_campaign)
        candidate.update(
            {
                "proposal_id": proposal_id,
                "proposal_digest": proposal["proposal_digest"],
                "context_digest": proposal["context_digest"],
            }
        )
        self.write_json(path, candidate)
        return path, proposal

    def ingest(self, path: Path) -> dict:
        archive = self.state_root / "archive.jsonl"
        if not archive.read_text(encoding="utf-8").strip():
            return self.run_cli(
                "seed", "--config", str(self.config_path), "--result", str(path)
            )
        prepared, _ = self.prepare_candidate(path)
        return self.run_cli(
            "update", "--config", str(self.config_path), "--result", str(prepared)
        )

    @staticmethod
    def campaign_spec(
        campaign_id: str,
        head_candidate_id: str,
        milestones: list[tuple[str, dict, float]],
    ) -> dict:
        return {
            "schema_version": 2,
            "campaign_id": campaign_id,
            "objective": "synthetic coupled change",
            "rationale": "intermediate candidates may regress",
            "head_candidate_id": head_candidate_id,
            "target_descriptors": milestones[-1][1],
            "descriptor_policy": "exact",
            "external_contract_digest": "sha256:contract",
            "affected_modules": ["src/example.cpp"],
            "budget": {
                "max_candidates": max(len(milestones) + 1, 2),
                "max_evaluator_runs": 30,
                "max_elapsed_seconds": 3600,
            },
            "milestones": [
                {
                    "milestone_id": milestone_id,
                    "objective": f"complete {milestone_id}",
                    "target_descriptors": descriptors,
                    "descriptor_policy": "exact",
                    "required_hard_constraints": [
                        "build_passed",
                        "correctness_passed",
                        "scope_passed",
                    ],
                    "max_primary_regression_fraction": regression,
                }
                for milestone_id, descriptors, regression in milestones
            ],
            "final_gate": {
                "required_hard_constraints": [
                    "build_passed",
                    "correctness_passed",
                    "scope_passed",
                ],
                "max_regression_from_start_fraction": 0.0,
                "min_primary_score": None,
                "require_validation": False,
            },
            "termination": {"rollback_candidate_id": head_candidate_id},
        }

    def test_quality_diversity_context_failure_and_campaign_flow(self) -> None:
        validation = self.run_cli("validate", "--config", str(self.config_path))
        self.assertTrue(validation["valid"])

        first_mode = self.run_cli("select-mode", "--config", str(self.config_path))
        self.assertEqual(first_mode["mode"], "explore")

        self.ingest(self.candidate_result("c1", 1.0))
        self.ingest(self.candidate_result("c2", 0.9, "b", "parallel", parent_id="c1"))
        invalid = self.candidate_result("c3", None, valid=False, parent_id="c1")
        self.ingest(invalid)
        c4 = self.candidate_result("c4", 1.2, parent_id="c1", mode="exploit")
        self.ingest(c4)

        state = self.load_json(self.state_root / "mode_state.json")
        self.assertEqual(state["attempt_count"], 4)
        self.assertEqual(state["valid_candidate_count"], 3)
        self.assertEqual(state["global_best_id"], "c4")
        self.assertEqual(set(state["cell_elites"].values()), {"c2", "c4"})

        decision = self.run_cli(
            "select-mode", "--config", str(self.config_path), "--write"
        )
        self.assertEqual(decision["mode"], "exploit")
        parent = self.run_cli("select-parent", "--config", str(self.config_path))
        self.assertEqual(parent["parent_id"], "c4")
        context = self.run_cli(
            "select-context",
            "--config",
            str(self.config_path),
            "--mode",
            "explore",
            "--parent",
            "c2",
        )
        self.assertIn("c4", context["inspiration_ids"])
        self.assertIn("c3", context["negative_lesson_ids"])

        failure_summary = self.run_cli(
            "summarize-failures", "--config", str(self.config_path)
        )
        self.assertEqual(failure_summary["groups"]["build"]["count"], 1)

        duplicate = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(c4),
            expected=2,
        )
        self.assertIn("already exists", duplicate["error"])

        campaign_spec = self.campaign_spec(
            "arch-change",
            "c4",
            [
                ("representation", {"family": "a", "execution": "serial"}, 0.1),
                ("integration", {"family": "a", "execution": "parallel"}, 0.1),
            ],
        )
        spec_path = self.root / "campaign.json"
        self.write_json(spec_path, campaign_spec)
        self.run_cli(
            "campaign-start",
            "--config",
            str(self.config_path),
            "--spec",
            str(spec_path),
        )
        active_mode = self.run_cli("select-mode", "--config", str(self.config_path))
        self.assertEqual(active_mode["mode"], "campaign")

        c5 = self.candidate_result(
            "c5",
            1.1,
            parent_id="c4",
            mode="campaign",
            campaign_id="arch-change",
        )
        self.ingest(c5)
        advanced = self.run_cli(
            "campaign-advance",
            "--config",
            str(self.config_path),
            "--candidate-id",
            "c5",
        )
        self.assertEqual(advanced["head_candidate_id"], "c5")
        self.assertEqual(advanced["milestone_id"], "integration")
        c6 = self.candidate_result(
            "c6",
            1.25,
            "a",
            "parallel",
            parent_id="c5",
            mode="campaign",
            campaign_id="arch-change",
        )
        self.ingest(c6)
        final_advance = self.run_cli(
            "campaign-advance",
            "--config",
            str(self.config_path),
            "--candidate-id",
            "c6",
        )
        self.assertIsNone(final_advance["milestone_id"])
        self.run_cli(
            "campaign-close",
            "--config",
            str(self.config_path),
            "--status",
            "completed",
            "--reason",
            "synthetic test complete",
        )
        self.assertIsNone(self.load_json(self.state_root / "mode_state.json")["active_campaign"])

    def test_research_mode_after_plateau_and_repeated_bottleneck(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        self.ingest(self.candidate_result("c2", 1.0, "b", "parallel", parent_id="c1"))
        self.ingest(self.candidate_result("c3", 1.0, parent_id="c1"))
        decision = self.run_cli("select-mode", "--config", str(self.config_path))
        self.assertEqual(decision["mode"], "research")

    def test_rebuilds_materialized_views_from_authoritative_events(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        self.ingest(self.candidate_result("c2", 1.2, parent_id="c1"))
        campaign_spec = self.campaign_spec(
            "recovery-campaign",
            "c2",
            [("recover", {"family": "a", "execution": "serial"}, 0.1)],
        )
        spec_path = self.root / "recovery-campaign.json"
        self.write_json(spec_path, campaign_spec)
        self.run_cli(
            "campaign-start",
            "--config",
            str(self.config_path),
            "--spec",
            str(spec_path),
        )
        self.write_json(self.state_root / "mode_state.json", {"schema_version": 2})
        (self.state_root / "archive.jsonl").write_text("", encoding="utf-8")
        self.write_json(
            self.state_root / "campaigns" / "recovery-campaign.json", {"broken": True}
        )
        proposal_path = next((self.state_root / "proposals").glob("*.json"))
        self.write_json(proposal_path, {"broken": True})

        authoritative_parent = self.run_cli(
            "select-parent",
            "--config",
            str(self.config_path),
            "--mode",
            "exploit",
        )
        self.assertEqual(authoritative_parent["parent_id"], "c2")

        unhealthy = self.run_cli(
            "doctor", "--config", str(self.config_path), expected=1
        )
        self.assertFalse(unhealthy["healthy"])
        self.assertTrue(any("differs" in item for item in unhealthy["errors"]))

        rebuilt = self.run_cli("rebuild-state", "--config", str(self.config_path))
        self.assertEqual(rebuilt["candidate_count"], 2)
        self.assertEqual(rebuilt["development_best_id"], "c2")
        healthy = self.run_cli("doctor", "--config", str(self.config_path))
        self.assertTrue(healthy["healthy"])
        state = self.load_json(self.state_root / "mode_state.json")
        self.assertEqual(state["attempt_count"], 2)
        self.assertEqual(state["global_best_id"], "c2")
        campaign = self.load_json(
            self.state_root / "campaigns" / "recovery-campaign.json"
        )
        self.assertEqual(campaign["status"], "active")
        self.assertEqual(campaign["head_candidate_id"], "c2")
        self.assertNotIn("broken", self.load_json(proposal_path))

    def test_development_validation_and_holdout_are_isolated(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        self.ingest(self.candidate_result("c2", 1.2, parent_id="c1"))

        validation = self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(self.protocol_result("v-c1", "c1", "validation", 1.05)),
        )
        self.assertTrue(validation["verified_promotion"])
        state = self.load_json(self.state_root / "mode_state.json")
        self.assertEqual(state["development_best_id"], "c2")
        self.assertEqual(state["verified_best_id"], "c1")
        self.assertEqual(len((self.state_root / "archive.jsonl").read_text().splitlines()), 2)
        development_parent = self.run_cli(
            "select-parent",
            "--config",
            str(self.config_path),
            "--mode",
            "exploit",
        )
        self.assertEqual(development_parent["parent_id"], "c2")

        wrong_holdout = self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(self.protocol_result("h-c2", "c2", "holdout", 1.3)),
            "--stage-end-confirmed",
            expected=2,
        )
        self.assertIn("verified validation leader", wrong_holdout["error"])

        missing_confirmation = self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(self.protocol_result("h-c1", "c1", "holdout", 1.0)),
            expected=2,
        )
        self.assertIn("stage-end-confirmed", missing_confirmation["error"])

        holdout = self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(self.root / "h-c1.json"),
            "--stage-end-confirmed",
        )
        self.assertEqual(holdout["holdout_status"], "completed")
        state = self.load_json(self.state_root / "mode_state.json")
        self.assertEqual(state["development_best_id"], "c2")
        self.assertEqual(state["verified_best_id"], "c1")
        self.assertEqual(state["holdout_candidate_id"], "c1")
        self.assertEqual(
            len((self.state_root / "archive.jsonl").read_text().splitlines()), 2
        )
        self.assertEqual(
            len((self.state_root / "protocol_evaluations.jsonl").read_text().splitlines()),
            2,
        )
        stopped = self.run_cli("select-mode", "--config", str(self.config_path))
        self.assertEqual(stopped["mode"], "stop")
        self.assertEqual(stopped["reason"], "sealed_holdout_recorded_stage_closed")
        after_holdout = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(self.candidate_result("c3", 1.4, parent_id="c2")),
            expected=2,
        )
        self.assertIn("stage is closed", after_holdout["error"])

    def test_protocol_fingerprint_drift_is_rejected_before_commit(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(self.protocol_result("v1", "c1", "validation", 1.0)),
        )
        drifted = self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(
                self.protocol_result(
                    "v2",
                    "c1",
                    "validation",
                    1.1,
                    command_digest="sha256:different-command",
                )
            ),
            expected=2,
        )
        self.assertIn("protocol changed", drifted["error"])
        evaluations = (self.state_root / "protocol_evaluations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(evaluations), 1)

    def test_legacy_jsonl_state_migrates_but_cannot_claim_verified_status(self) -> None:
        legacy_path = self.candidate_result("legacy-c1", 1.0)
        legacy = self.load_json(legacy_path)
        legacy["schema_version"] = 1
        for key in (
            "protocol",
            "protocol_id",
            "samples",
            "aggregation",
            "provenance",
        ):
            legacy["evaluation"].pop(key, None)
        legacy["sequence"] = 1
        legacy["parent_id"] = None
        legacy["mode"] = "explore"
        legacy["campaign_id"] = None
        legacy["recorded_at"] = "2026-01-01T00:00:00+00:00"
        legacy["cell_key"] = json.dumps(
            {"execution": "serial", "family": "a"}, separators=(",", ":")
        )
        (self.state_root / "archive.jsonl").write_text(
            json.dumps(legacy) + "\n", encoding="utf-8"
        )
        self.write_json(
            self.state_root / "mode_state.json",
            {
                "schema_version": 1,
                "context_use_counts": {"legacy-c1": 2},
                "last_mode": "explore",
                "last_mode_reason": "legacy",
            },
        )
        legacy_campaign = {
            "schema_version": 1,
            "campaign_id": "legacy-campaign",
            "objective": "legacy",
            "rationale": "legacy",
            "head_candidate_id": "legacy-c1",
            "max_steps": 1,
            "milestones": ["legacy-step"],
            "status": "active",
            "step": 0,
            "history": [],
        }
        self.write_json(
            self.state_root / "campaigns" / "legacy-campaign.json",
            legacy_campaign,
        )
        for suffix in ("", "-wal", "-shm"):
            path = self.state_root / f"events.sqlite3{suffix}"
            if path.exists():
                path.unlink()

        migrated = self.run_cli(
            "migrate-state", "--config", str(self.config_path)
        )
        self.assertEqual(migrated["candidate_count"], 1)
        self.assertEqual(migrated["legacy_development_best_id"], "legacy-c1")
        self.assertTrue(
            self.run_cli("doctor", "--config", str(self.config_path))["healthy"]
        )

        cannot_advance = self.run_cli(
            "campaign-advance",
            "--config",
            str(self.config_path),
            "--candidate-id",
            "legacy-c1",
            expected=2,
        )
        self.assertIn("cannot advance", cannot_advance["error"])
        cannot_complete = self.run_cli(
            "campaign-close",
            "--config",
            str(self.config_path),
            "--status",
            "completed",
            "--reason",
            "legacy",
            expected=2,
        )
        self.assertIn("only be closed as aborted", cannot_complete["error"])
        self.run_cli(
            "campaign-close",
            "--config",
            str(self.config_path),
            "--status",
            "aborted",
            "--reason",
            "migrated legacy Campaign",
        )

        validation = self.run_cli(
            "record-evaluation",
            "--config",
            str(self.config_path),
            "--result",
            str(
                self.protocol_result(
                    "legacy-validation", "legacy-c1", "validation", 1.0
                )
            ),
            expected=2,
        )
        self.assertIn("Legacy candidate", validation["error"])

    def test_concurrent_candidate_commits_are_not_lost(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        candidates = [
            self.candidate_result("c2", 1.1, parent_id="c1"),
            self.candidate_result("c3", 1.2, parent_id="c1"),
        ]
        candidates = [self.prepare_candidate(candidate)[0] for candidate in candidates]
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(CLI),
                    "update",
                    "--config",
                    str(self.config_path),
                    "--result",
                    str(candidate),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for candidate in candidates
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertIn(process.returncode, (0, 2), msg=f"{stdout}\n{stderr}")
            payload = json.loads(stdout)
            if process.returncode == 2:
                self.assertIn("was committed", payload["error"])

        rebuilt = self.run_cli("rebuild-state", "--config", str(self.config_path))
        self.assertEqual(rebuilt["candidate_count"], 3)
        archive_ids = {
            json.loads(line)["candidate_id"]
            for line in (self.state_root / "archive.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        }
        self.assertEqual(archive_ids, {"c1", "c2", "c3"})

    def test_proposal_binds_parent_mode_context_and_consumes_once(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        candidate, proposal = self.prepare_candidate(
            self.candidate_result("c2", 1.1, mode="exploit")
        )
        original = self.load_json(candidate)
        attacks = [
            ("parent_id", "wrong-parent", "parent_id conflicts"),
            ("mode", "research", "mode conflicts"),
            ("context_digest", "sha256:tampered", "context_digest does not match"),
        ]
        for key, value, expected_error in attacks:
            payload = dict(original)
            payload[key] = value
            self.write_json(candidate, payload)
            rejected = self.run_cli(
                "update",
                "--config",
                str(self.config_path),
                "--result",
                str(candidate),
                expected=2,
            )
            self.assertIn(expected_error, rejected["error"])

        self.write_json(candidate, original)
        accepted = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(candidate),
        )
        self.assertEqual(accepted["proposal_status"], "consumed")

        duplicate = dict(original)
        duplicate["candidate_id"] = "c3"
        duplicate["code_ref"] = proposal["workspace_root"]
        duplicate_path = self.root / "c3-duplicate-proposal.json"
        self.write_json(duplicate_path, duplicate)
        rejected = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(duplicate_path),
            expected=2,
        )
        self.assertIn("requires an open proposal", rejected["error"])

    def test_target_attempts_use_intent_while_archive_uses_actual_descriptors(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        candidate, proposal = self.prepare_candidate(
            self.candidate_result("c2", 1.1)
        )
        payload = self.load_json(candidate)
        alternatives = [
            {"family": "a", "execution": "serial"},
            {"family": "a", "execution": "parallel"},
            {"family": "b", "execution": "serial"},
            {"family": "b", "execution": "parallel"},
        ]
        actual = next(
            item for item in alternatives if item != proposal["intended_descriptors"]
        )
        payload["evaluation"]["descriptors"] = actual
        self.write_json(candidate, payload)
        result = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(candidate),
        )
        self.assertFalse(result["descriptor_match"])
        state = self.load_json(self.state_root / "mode_state.json")
        self.assertEqual(state["target_cell_attempts"][proposal["target_cell"]], 1)
        archive = [
            json.loads(line)
            for line in (self.state_root / "archive.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        c2 = next(item for item in archive if item["candidate_id"] == "c2")
        self.assertEqual(c2["evaluation"]["descriptors"], actual)
        self.assertNotEqual(c2["cell_key"], proposal["target_cell"])

    def test_exact_descriptor_policy_and_override_evidence_are_enforced(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        candidate = self.candidate_result("c2", 1.1, mode="exploit")
        payload = self.load_json(candidate)
        workspace = payload["workspace_root"]
        proof = self.isolation_proof(workspace, "c2-override")
        missing_evidence = self.run_cli(
            "plan-round",
            "--config",
            str(self.config_path),
            "--proposal-id",
            "p-c2-missing",
            "--workspace-root",
            workspace,
            "--worker-proposal-output",
            str(Path(workspace) / "proposal-missing.json"),
            "--isolation-proof",
            str(proof),
            "--mode",
            "exploit",
            expected=2,
        )
        self.assertIn("requires --override-reason", missing_evidence["error"])

        planned = self.run_cli(
            "plan-round",
            "--config",
            str(self.config_path),
            "--proposal-id",
            "p-c2",
            "--workspace-root",
            workspace,
            "--worker-proposal-output",
            str(Path(workspace) / "proposal.json"),
            "--isolation-proof",
            str(proof),
            "--mode",
            "exploit",
            "--override-reason",
            "measured evidence",
            "--override-evidence",
            "artifact:profile",
            "--descriptor-policy",
            "exact",
        )
        proposal = self.load_json(Path(planned["proposal_file"]))
        payload.update(
            {
                "proposal_id": proposal["proposal_id"],
                "proposal_digest": proposal["proposal_digest"],
                "context_digest": proposal["context_digest"],
            }
        )
        alternatives = [
            {"family": "a", "execution": "serial"},
            {"family": "a", "execution": "parallel"},
            {"family": "b", "execution": "serial"},
            {"family": "b", "execution": "parallel"},
        ]
        payload["evaluation"]["descriptors"] = next(
            item for item in alternatives if item != proposal["intended_descriptors"]
        )
        self.write_json(candidate, payload)
        rejected = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(candidate),
            expected=2,
        )
        self.assertIn("do not exactly match", rejected["error"])
        proposal_view = self.load_json(Path(planned["proposal_file"]))
        self.assertEqual(proposal_view["status"], "open")

    def test_campaign_blocks_bypass_enforces_budget_and_final_gate(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        preexisting, _ = self.prepare_candidate(self.candidate_result("outside", 1.1))
        spec = self.campaign_spec(
            "bounded",
            "c1",
            [("only", {"family": "a", "execution": "serial"}, 0.1)],
        )
        spec["budget"]["max_candidates"] = 1
        spec_path = self.root / "bounded-campaign.json"
        self.write_json(spec_path, spec)
        self.run_cli(
            "campaign-start",
            "--config",
            str(self.config_path),
            "--spec",
            str(spec_path),
        )

        bypass = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(preexisting),
            expected=2,
        )
        self.assertIn("Active Campaign rejects", bypass["error"])
        early = self.run_cli(
            "campaign-close",
            "--config",
            str(self.config_path),
            "--status",
            "completed",
            "--reason",
            "too early",
            expected=2,
        )
        self.assertIn("not all Campaign milestones", early["error"])

        failed = self.candidate_result(
            "failed-step",
            None,
            valid=False,
            mode="campaign",
            campaign_id="bounded",
        )
        self.ingest(failed)
        next_candidate = self.candidate_result(
            "over-budget", 1.1, mode="campaign", campaign_id="bounded"
        )
        payload = self.load_json(next_candidate)
        proof = self.isolation_proof(payload["workspace_root"], "over-budget")
        exhausted = self.run_cli(
            "plan-round",
            "--config",
            str(self.config_path),
            "--workspace-root",
            payload["workspace_root"],
            "--worker-proposal-output",
            str(Path(payload["workspace_root"]) / "proposal.json"),
            "--isolation-proof",
            str(proof),
            expected=2,
        )
        self.assertIn("budget is exhausted", exhausted["error"])
        closed = self.run_cli(
            "campaign-close",
            "--config",
            str(self.config_path),
            "--status",
            "budget_exhausted",
            "--reason",
            "candidate budget used",
        )
        self.assertTrue(closed["closed"])

    def test_isolation_contract_and_preflight_proof_are_required(self) -> None:
        self.ingest(self.candidate_result("c1", 1.0))
        payload = self.load_json(self.candidate_result("c2", 1.1))
        proof_path = self.isolation_proof(payload["workspace_root"], "bad-proof")
        proof = self.load_json(proof_path)
        proof["passed"] = False
        self.write_json(proof_path, proof)
        rejected = self.run_cli(
            "plan-round",
            "--config",
            str(self.config_path),
            "--workspace-root",
            payload["workspace_root"],
            "--worker-proposal-output",
            str(Path(payload["workspace_root"]) / "proposal.json"),
            "--isolation-proof",
            str(proof_path),
            expected=2,
        )
        self.assertIn("isolation_proof.passed", rejected["error"])

        config = self.load_json(self.config_path)
        config["evaluation"]["hard_constraints"].remove("scope_passed")
        self.write_json(self.config_path, config)
        invalid = self.run_cli(
            "validate", "--config", str(self.config_path), expected=1
        )
        self.assertTrue(
            any("must include scope_passed" in item for item in invalid["errors"])
        )

    def test_project_schema_one_migrates_with_explicit_isolation(self) -> None:
        config = self.load_json(self.config_path)
        config["schema_version"] = 1
        config.pop("adapter")
        config.pop("isolation")
        config["commands"].pop("isolation_preflight")
        config["evaluation"]["hard_constraints"].remove("scope_passed")
        config["search"].pop("max_open_proposals")
        self.write_json(self.config_path, config)

        migrated = self.run_cli(
            "migrate-project",
            "--config",
            str(self.config_path),
            "--isolation-enforcement",
            "os_acl",
            "--adapter-path",
            str(self.adapter_path),
            "--worker-workspace-root",
            str(self.candidate),
            "--controller-only-root",
            str(self.controller),
            "--controller-only-root",
            str(self.state_root),
            "--isolation-preflight-command",
            "DO_NOT_EXECUTE isolation-preflight",
        )
        self.assertTrue(migrated["migrated"])
        self.assertTrue(
            (self.config_path.parent / "project.schema1.backup.json").exists()
        )
        updated = self.load_json(self.config_path)
        self.assertEqual(updated["schema_version"], 3)
        self.assertIn("scope_passed", updated["evaluation"]["hard_constraints"])
        self.assertTrue(
            self.run_cli("validate", "--config", str(self.config_path))["valid"]
        )

    def test_trusted_process_requires_explicit_acknowledgment(self) -> None:
        config = self.load_json(self.config_path)
        config["isolation"]["enforcement"] = "trusted_process"
        config["isolation"]["trusted_process_acknowledged"] = False
        self.write_json(self.config_path, config)
        invalid = self.run_cli(
            "validate", "--config", str(self.config_path), expected=1
        )
        self.assertTrue(
            any(
                "trusted_process_acknowledged" in item
                for item in invalid["errors"]
            )
        )
        config["isolation"]["trusted_process_acknowledged"] = True
        self.write_json(self.config_path, config)
        self.ingest(self.candidate_result("c1", 1.0))
        candidate = self.candidate_result("c2", 1.1)
        prepared, proposal = self.prepare_candidate(candidate)
        self.assertTrue(proposal["isolation_proof"]["risk_acknowledged"])
        accepted = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(prepared),
        )
        self.assertTrue(accepted["recorded"])

    def test_invalid_seed_uses_repair_metric_until_a_valid_parent_exists(self) -> None:
        config = self.load_json(self.config_path)
        config["evaluation"]["repair_metric"] = "correctness_points"
        config["evaluation"]["repair_direction"] = "maximize"
        self.write_json(self.config_path, config)

        self.ingest(
            self.candidate_result(
                "invalid-seed",
                None,
                valid=False,
                repair_score=2.0,
                build_passed=True,
            )
        )
        decision = self.run_cli("select-mode", "--config", str(self.config_path))
        self.assertEqual(decision["mode"], "repair")
        parent = self.run_cli(
            "select-parent",
            "--config",
            str(self.config_path),
            "--mode",
            "repair",
        )
        self.assertEqual(parent["parent_id"], "invalid-seed")

        self.ingest(
            self.candidate_result(
                "better-invalid",
                None,
                valid=False,
                mode="repair",
                repair_score=8.0,
                build_passed=True,
            )
        )
        parent = self.run_cli(
            "select-parent",
            "--config",
            str(self.config_path),
            "--mode",
            "repair",
        )
        self.assertEqual(parent["parent_id"], "better-invalid")

        self.ingest(
            self.candidate_result(
                "first-valid", 1.0, mode="repair", repair_score=20.0
            )
        )
        decision = self.run_cli("select-mode", "--config", str(self.config_path))
        self.assertEqual(decision["mode"], "explore")

    def test_validator_rejects_state_inside_mutable_candidate(self) -> None:
        config = self.load_json(self.config_path)
        config["project"]["state_root"] = str(self.candidate / ".state")
        self.write_json(self.config_path, config)
        result = self.run_cli(
            "validate", "--config", str(self.config_path), expected=1
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any("state root must be isolated" in item for item in result["errors"]))

    def test_rejects_unknown_descriptor_and_conflicting_parent(self) -> None:
        unknown = self.candidate_result("c1", 1.0, family="unknown")
        result = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(unknown),
            expected=2,
        )
        self.assertIn("outside descriptor_space", result["error"])

        self.ingest(self.candidate_result("seed", 1.0))
        orphan = self.candidate_result("c2", 1.0, parent_id="seed")
        orphan, _ = self.prepare_candidate(orphan)
        payload = self.load_json(orphan)
        payload["parent_id"] = "not-in-archive"
        self.write_json(orphan, payload)
        result = self.run_cli(
            "update",
            "--config",
            str(self.config_path),
            "--result",
            str(orphan),
            expected=2,
        )
        self.assertIn("self-declared parent_id conflicts", result["error"])


if __name__ == "__main__":
    unittest.main()
