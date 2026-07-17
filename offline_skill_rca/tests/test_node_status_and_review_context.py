"""节点状态计算与 Stage 7 审查上下文的回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.stages import stage_05_node_execution_assessment as stage5
from src.stages import stage_06_skill_repair_suggestions as stage6
from src.stages import stage_08_transactional_skill_repair as stage7


def trace(traj_id: str, status: str) -> dict:
    """构造只包含一个能力节点状态的最小 Stage 5 输出。"""
    return {
        "traj_id": traj_id,
        "success": 0,
        "node_assessments": [{"node_id": "N1", "status": status, "status_calculation": {"rationale": status}}],
    }


class NodeStatusTests(unittest.TestCase):
    def test_blocked_is_not_a_direct_failure_or_repair_trigger(self) -> None:
        analysis = stage6._node_status_analysis([trace("T1", "blocked"), trace("T2", "blocked")], "N1")
        self.assertIsNone(analysis["attempted_success_rate"])
        self.assertEqual(0.0, analysis["direct_failure_rate"])
        self.assertEqual(1.0, analysis["blocked_rate"])

        classification = stage6._classify_node(
            [],
            analysis,
            {"directly_relevant_skill_count": 0},
            total_traces=2,
        )
        self.assertFalse(classification["needs_repair"])
        self.assertEqual(0, classification["affected_trace_count"])
        pressure = stage6._node_pressure(
            {"execution_success_analysis": analysis, "bad_event_list": []},
            total_traces=2,
        )
        self.assertEqual(0.0, pressure)

    def test_events_from_blocked_traces_are_context_only(self) -> None:
        analysis = stage6._node_status_analysis(
            [trace("T1", "blocked"), trace("T2", "blocked"), trace("T3", "fail")],
            "N1",
        )
        partition = stage6._partition_events_by_node_status(
            [
                {"traj_id": "T1", "event_id": "blocked-fatal", "severity": "fatal"},
                {"traj_id": "T2", "event_id": "blocked-major", "severity": "major"},
                {"traj_id": "T3", "event_id": "direct-major", "severity": "major"},
            ],
            analysis,
        )
        self.assertEqual(["direct-major"], [item["event_id"] for item in partition["direct_failure_events"]])
        self.assertEqual(2, partition["context_event_counts_by_status"]["blocked"])
        classification = stage6._classify_node(
            partition["direct_failure_events"],
            analysis,
            {"directly_relevant_skill_count": 1, "best_overall_coverage": 0.4},
            total_traces=3,
            event_partition=partition,
        )
        self.assertTrue(classification["needs_repair"])
        self.assertEqual(1, classification["affected_trace_count"])
        self.assertEqual(0, classification["fatal_bad_event_count"])
        self.assertEqual(2, classification["ignored_context_event_count"])

    def test_legacy_bad_status_is_normalized_to_fail(self) -> None:
        analysis = stage6._node_status_analysis(
            [
                trace("T1", "pass"),
                trace("T2", "bad"),
                trace("T3", "miss"),
                trace("T4", "blocked"),
                trace("T5", "unknown"),
            ],
            "N1",
        )
        self.assertEqual(
            {"pass": 1, "fail": 1, "miss": 1, "blocked": 1, "unknown": 1},
            analysis["status_counts"],
        )
        self.assertEqual(0.5, analysis["attempted_success_rate"])
        self.assertEqual(0.4, analysis["direct_failure_rate"])
        self.assertEqual(0.2, analysis["blocked_rate"])

    def test_repeated_direct_failures_choose_action_from_coverage(self) -> None:
        analysis = stage6._node_status_analysis(
            [trace("T1", "fail"), trace("T2", "fail"), trace("T3", "pass"), trace("T4", "blocked"), trace("T5", "unknown")],
            "N1",
        )
        events = [
            {"traj_id": "T1", "event_id": "E1", "severity": "major"},
            {"traj_id": "T2", "event_id": "E2", "severity": "major"},
        ]
        covered = stage6._classify_node(
            events,
            analysis,
            {"directly_relevant_skill_count": 1, "best_overall_coverage": 0.92, "total_skill_pair_rows": 2},
            total_traces=5,
        )
        absent = stage6._classify_node(
            events,
            analysis,
            {"directly_relevant_skill_count": 0, "best_overall_coverage": None, "total_skill_pair_rows": 2},
            total_traces=5,
        )
        missing_coverage = stage6._classify_node(
            events,
            analysis,
            {"directly_relevant_skill_count": 0, "best_overall_coverage": None, "total_skill_pair_rows": 0},
            total_traces=5,
        )
        self.assertTrue(covered["needs_repair"])
        self.assertTrue(covered["requires_semantic_confirmation"])
        self.assertEqual("revise_existing_skill", stage6._recommended_action(covered))
        self.assertEqual("add_new_skill", stage6._recommended_action(absent))
        self.assertEqual("add_new_skill", stage6._recommended_action(missing_coverage))

    def test_status_is_computed_from_intermediate_judgments(self) -> None:
        cases = [
            ({"capability_presence": {"value": "full"}, "fully_successful": {"value": True}, "prerequisites_satisfied": {"value": True}, "success_judgeable": {"value": True}}, "pass"),
            ({"capability_presence": {"value": "partial"}, "fully_successful": {"value": False}, "prerequisites_satisfied": {"value": True}, "success_judgeable": {"value": True}}, "fail"),
            ({"capability_presence": {"value": "none"}, "fully_successful": {"value": False}, "prerequisites_satisfied": {"value": True}, "success_judgeable": {"value": True}}, "miss"),
            ({"capability_presence": {"value": "none"}, "fully_successful": {"value": None}, "prerequisites_satisfied": {"value": False}, "success_judgeable": {"value": True}}, "blocked"),
            ({"capability_presence": {"value": "unknown"}, "fully_successful": {"value": None}, "prerequisites_satisfied": {"value": None}, "success_judgeable": {"value": False}}, "unknown"),
            ({"capability_presence": {"value": "none"}, "fully_successful": {"value": False}, "prerequisites_satisfied": {"value": None}, "success_judgeable": {"value": True}}, "unknown"),
            ({"capability_presence": {"value": "partial"}, "fully_successful": {"value": True}, "prerequisites_satisfied": {"value": True}, "success_judgeable": {"value": True}}, "unknown"),
            ({"capability_presence": {"value": "full"}, "fully_successful": {"value": True}, "prerequisites_satisfied": {"value": False}, "success_judgeable": {"value": True}}, "unknown"),
        ]
        for raw, expected in cases:
            normalized = stage5.normalize_assessment("N1", raw)
            status, _reason, _warnings = stage5.calculate_node_status(normalized)
            self.assertEqual(expected, status)

    def test_pass_requires_evidence_for_every_node_requirement(self) -> None:
        node = {
            "node_id": "N1",
            "operations": ["Filter the selected filing.", "Classify records as stocks."],
            "checks": ["The stock count is numeric."],
        }
        base = {
            "capability_presence": {"value": "full"},
            "fully_successful": {"value": True},
            "prerequisites_satisfied": {"value": True},
            "success_judgeable": {"value": True},
        }
        complete = {
            **base,
            "requirement_audit": [
                {
                    "kind": "operation",
                    "requirement": requirement,
                    "status": "satisfied",
                    "evidence_refs": [f"step-{index}"],
                }
                for index, requirement in enumerate(node["operations"], start=1)
            ]
            + [
                {
                    "kind": "check",
                    "requirement": node["checks"][0],
                    "status": "satisfied",
                    "evidence_refs": ["step-3"],
                }
            ],
        }
        normalized = stage5.normalize_assessment("N1", complete, node)
        self.assertEqual("pass", stage5.calculate_node_status(normalized)[0])

        missing_classification = {
            **complete,
            "requirement_audit": [
                row
                for row in complete["requirement_audit"]
                if row["requirement"] != "Classify records as stocks."
            ],
        }
        normalized = stage5.normalize_assessment("N1", missing_classification, node)
        status, _reason, warnings = stage5.calculate_node_status(normalized)
        self.assertEqual("unknown", status)
        self.assertTrue(any("pass requires" in warning for warning in warnings))

        violated_classification = {
            **complete,
            "requirement_audit": [
                {
                    **row,
                    "status": "violated"
                    if row["requirement"] == "Classify records as stocks."
                    else row["status"],
                }
                for row in complete["requirement_audit"]
            ],
        }
        normalized = stage5.normalize_assessment("N1", violated_classification, node)
        self.assertEqual("fail", stage5.calculate_node_status(normalized)[0])


class ReviewContextTests(unittest.TestCase):
    def test_review_payload_contains_inventory_and_related_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "source"
            working = root / "working"
            for library in (source, working):
                (library / "alpha").mkdir(parents=True)
                (library / "beta").mkdir(parents=True)
                (library / "alpha" / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
                (library / "beta" / "SKILL.md").write_text("# Beta\n", encoding="utf-8")
            output = root / "run"
            output.mkdir()
            archive = output / "candidate.json"
            archive.write_text(
                json.dumps({"files": [], "change_summary": "candidate"}),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                root=root,
                skills_dir=source,
                output_dir=output,
                output_skills_dir=working,
                skill_word_limit=1200,
                max_prompt_chars=220_000,
            )
            bundle = {
                "skill_library": [
                    {"skill_id": "alpha", "title": "alpha", "path": "source/alpha/SKILL.md"},
                    {"skill_id": "beta", "title": "beta", "path": "source/beta/SKILL.md"},
                ],
                "stage_01b_skill_standardizations": [
                    {"skill_id": "alpha", "title": "Alpha", "intent": "Handle alpha workflows."},
                    {"skill_id": "beta", "title": "Beta", "intent": "Unrelated beta workflows."},
                ],
                # 完整流水线在 Stage 8 运行时尚未写最终 stage_outputs.json，
                # 因此 Review 必须能直接消费内存中的前序阶段输出。
                "review_stage_outputs": {
                    "stage_02_capability_graph": {
                        "capability_graph": {"nodes": [{"node_id": "N1", "goal": "Alpha capability"}]},
                        "coverage_pairs": [{"node_id": "N1", "skill_id": "alpha", "overall_coverage": 0.4}],
                    },
                    "stage_03_failure_events_by_trace": [
                        {"traj_id": "T1", "failure_events": [{"event_id": "E1", "observed": "alpha failed"}]}
                    ],
                    "stage_04_failure_event_alignment": {
                        "alignments": [{"traj_id": "T1", "event_id": "E1", "node_id": "N1", "confidence": 0.9}]
                    },
                    "stage_05_node_execution_assessments": [
                        {"traj_id": "T1", "success": 0, "node_assessments": [{"node_id": "N1", "status": "fail"}]}
                    ],
                },
            }
            suggestion = {
                "repair_unit_id": "A1",
                "suggestion_id": "A1",
                "suggestion_ids": ["A1"],
                "action": "revise_existing_skill",
                "target_skill_id": "alpha",
                "source_suggestion": {"suggestion_id": "A1", "target_skill_id": "alpha"},
            }
            attempt = {"candidate_archive": str(archive), "files_before": [], "local_validation_errors": []}
            # 磁盘文件故意留空，确认内存上下文优先且不会被旧文件覆盖。
            (output / "stage_outputs.json").write_text("{}", encoding="utf-8")
            suggestion["node_ids"] = ["N1"]
            suggestion["source_suggestion"]["evidence_refs"] = [{"traj_id": "T1", "event_id": "E1"}]

            payload = stage7._review_payload(config, bundle, suggestion, attempt)

            self.assertEqual(["alpha", "beta"], payload["current_skill_library_inventory"])
            self.assertEqual("alpha", payload["related_skill_summaries"][0]["skill_id"])
            self.assertTrue(stage7.review_schema()["checks"]["suggestion_capability_correct"])
            self.assertEqual("N1", payload["capability_nodes"][0]["node_id"])
            self.assertEqual("E1", payload["suggestion_evidence"]["events"][0]["event_id"])
            self.assertEqual("fail", payload["node_execution_context"][0]["node_assessments"][0]["status"])
            self.assertEqual("alpha", payload["coverage_context"][0]["skill_id"])
            prompt = stage7.build_review_prompt(config, bundle, suggestion, attempt, payload)
            self.assertIn("# current_skill_library_inventory", prompt)
            self.assertIn("# suggestion_evidence", prompt)
            self.assertIn('"skill_id": "alpha"', prompt)
            self.assertNotIn("<related_skill_summaries>", prompt)
            self.assertNotIn("local_candidate_validation_errors", payload)

    def test_semantic_rejection_advances_without_committing(self) -> None:
        state = {
            "current_suggestion_index": 0,
            "suggestions": [{"suggestion_id": "A1"}, {"suggestion_id": "A2"}],
            "status": "running",
        }
        suggestion = state["suggestions"][0]
        attempt = {"accepted": False}
        stage7._advance_after_semantic_review(
            state,
            suggestion,
            attempt,
            "reject_suggestion",
            "The proposed rule contradicts the capability requirement.",
        )
        self.assertEqual("rejected_suggestion", suggestion["status"])
        self.assertEqual(1, state["current_suggestion_index"])
        self.assertEqual("repair", state["next_operation"])

    def test_review_rejects_invalid_suggestion_without_repair_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "source"
            working = root / "working"
            output = root / "run"
            for directory in (source / "alpha", working / "alpha", output / "stage_outputs_individual"):
                directory.mkdir(parents=True)
            original = "---\nname: alpha\ndescription: Alpha.\n---\n\n# Alpha\n"
            (source / "alpha" / "SKILL.md").write_text(original, encoding="utf-8")
            (working / "alpha" / "SKILL.md").write_text(original, encoding="utf-8")
            candidate = output / "candidate.json"
            candidate.write_text(
                json.dumps({"files": [{"path": "alpha/SKILL.md", "content": original + "Bad rule.\n"}]}),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                root=root,
                skills_dir=source,
                output_dir=output,
                output_skills_dir=working,
                skill_word_limit=1200,
                max_prompt_chars=220_000,
            )
            suggestion = {
                "index": 0,
                "repair_unit_id": "A1",
                "suggestion_id": "A1",
                "suggestion_ids": ["A1"],
                "node_id": "N1",
                "node_ids": ["N1"],
                "action": "revise_existing_skill",
                "target_skill_id": "alpha",
                "source_suggestion": {"suggestion_id": "A1", "target_skill_id": "alpha"},
                "source_suggestions": [{"suggestion_id": "A1", "target_skill_id": "alpha"}],
                "status": "awaiting_review",
                "attempt_count": 1,
                "attempts": [{"attempt_number": 1, "candidate_archive": str(candidate), "files_before": [], "local_validation_errors": []}],
            }
            state = {
                "status": "running",
                "current_suggestion_index": 0,
                "suggestions": [suggestion],
                "interactions": [],
                "interaction_sequence": 0,
                "accepted_count": 0,
                "applied_changes": [],
            }
            review = {
                "repair_unit_id": "A1",
                "suggestion_ids": ["A1"],
                "decision": "reject_suggestion",
                "checks": {
                    "suggestion_evidence_supported": False,
                    "suggestion_capability_correct": False,
                    "suggestion_reusable_skill_scope": True,
                    "candidate_satisfies_valid_suggestions": True,
                    "scope_preserved": True,
                    "files_usable": True,
                    "claude_code_compatible": True,
                    "library_consistent": True,
                },
                "issues": [{"code": "invalid_rule", "message": "The proposed rule contradicts the capability."}],
                "retry_instructions": [],
            }
            with patch.object(stage7, "run_llm_stage", return_value=review):
                stage7._run_review_operation(config, {"skill_library": []}, state, suggestion)

            self.assertEqual("completed", state["status"])
            self.assertEqual("rejected_suggestion", suggestion["status"])
            self.assertEqual(original, (working / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual("reject_suggestion", state["interactions"][0]["review_decision"])


if __name__ == "__main__":
    unittest.main()
