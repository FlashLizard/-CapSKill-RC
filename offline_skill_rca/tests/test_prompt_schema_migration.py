"""新版英文 Prompt 契约与本地短路逻辑的回归测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.calculations import postprocess_skill_coverage
from src.stages import stage_08_transactional_skill_repair as stage7


class CoverageSchemaTests(unittest.TestCase):
    def test_nested_coverage_scores_are_calculated_locally(self) -> None:
        result = postprocess_skill_coverage(
            {
                "capability_graph": {"nodes": [], "edges": []},
                "coverage_pairs": [
                    {
                        "node_id": "N1",
                        "skill_id": "alpha",
                        "directly_relevant": True,
                        "relevance_reason": "Direct support.",
                        "scores": {
                            "requirement_fit": 1.0,
                            "trigger": 0.5,
                            "procedure": 0.5,
                            "verification": 0.0,
                            "recovery": 0.0,
                            "execution_support": None,
                        },
                        "execution_support_need": "not_needed",
                        "evidence": [],
                    }
                ],
            }
        )
        row = result["coverage_pairs"][0]
        self.assertEqual(0.0, row["verification_coverage"])
        self.assertIsNone(row["execution_support_coverage"])
        self.assertEqual(row, result["skill_coverage_matrix"][0])
        self.assertEqual("missing_recovery", row["coverage_labels"][0])


class LocalValidationShortCircuitTests(unittest.TestCase):
    def test_invalid_candidate_returns_to_repair_without_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            skills = root / "skills"
            (skills / "alpha").mkdir(parents=True)
            (skills / "alpha" / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            config = SimpleNamespace(
                root=root,
                skills_dir=skills,
                output_dir=root / "run",
                output_skills_dir=root / "working",
                force=False,
                stage7_repair_mode="per_suggestion",
                stage7_skill_package_size=1,
                skill_word_limit=1200,
                max_prompt_chars=220_000,
            )
            bundle = {
                "skill_library": [{"skill_id": "alpha", "path": "skills/alpha/SKILL.md"}],
                "stage_01b_skill_standardizations": [{"skill_id": "alpha", "title": "Alpha"}],
            }
            actions = {
                "repair_actions": [
                    {
                        "suggestion_id": "A1",
                        "action": "revise_existing_skill",
                        "target_skill_id": "alpha",
                    }
                ]
            }
            invalid = {
                "repair_unit_id": "A1",
                "suggestion_ids": ["A1"],
                "files": [{"path": "alpha/SKILL.md", "content": "[content omitted]"}],
            }
            with patch.object(stage7, "run_llm_stage", return_value=invalid) as llm:
                state = stage7.run_step(config, bundle, actions)

            attempt = state["suggestions"][0]["attempts"][0]
            self.assertEqual("local_validation_failed", attempt["status"])
            self.assertEqual("repair", state["next_operation"])
            self.assertEqual(["repair"], [item["operation"] for item in state["interactions"]])
            self.assertEqual(1, llm.call_count)
            self.assertEqual("local_validation", attempt["repair_feedback"]["source"])


if __name__ == "__main__":
    unittest.main()
