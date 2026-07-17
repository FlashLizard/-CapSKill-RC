"""Stage 7 repair unit 与独立 Review LLM 路由的回归测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.stages import stage_08_transactional_skill_repair as stage7
from src.stages.common import make_llm


def stage6_actions() -> dict:
    """构造含已有 Skill 修复和新增 Skill 顺序屏障的最小动作队列。"""
    return {
        "repair_actions": [
            {"suggestion_id": "A1", "action": "revise_existing_skill", "target_skill_id": "alpha", "execution_order": 1},
            {"suggestion_id": "A2", "action": "revise_existing_skill", "target_skill_id": "alpha", "execution_order": 2},
            {"suggestion_id": "B1", "action": "revise_existing_skill", "target_skill_id": "beta", "execution_order": 3},
            {"suggestion_id": "N1", "action": "add_new_skill", "new_skill_id": "gamma", "execution_order": 4},
            {"suggestion_id": "C1", "action": "revise_existing_skill", "target_skill_id": "gamma", "execution_order": 5},
            {"suggestion_id": "C2", "action": "revise_existing_skill", "target_skill_id": "gamma", "execution_order": 6},
            {"suggestion_id": "C3", "action": "revise_existing_skill", "target_skill_id": "gamma", "execution_order": 7},
        ]
    }


class Stage7RepairUnitTests(unittest.TestCase):
    def test_claude_code_skill_validation_accepts_standard_skill(self) -> None:
        content = """---
name: alpha-skill
description: Validates alpha data. Use when Claude needs to inspect or verify alpha datasets.
---

# Alpha validation

1. Inspect the input schema.
2. Validate the required fields.
3. Report actionable failures.
"""
        errors = stage7._claude_code_skill_errors(Path("alpha-skill/SKILL.md"), content)
        self.assertEqual(errors, [])

    def test_claude_code_skill_validation_rejects_missing_or_mismatched_frontmatter(self) -> None:
        missing = stage7._claude_code_skill_errors(Path("alpha-skill/SKILL.md"), "# Alpha\n")
        self.assertTrue(any("frontmatter" in error for error in missing))

        mismatched = """---
name: beta-skill
description: Handles beta work. Use when beta inputs need processing.
---

# Beta

Process the input.
"""
        errors = stage7._claude_code_skill_errors(Path("alpha-skill/SKILL.md"), mismatched)
        self.assertTrue(any("must match" in error for error in errors))

    def test_existing_skill_title_resolves_to_original_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            skills = root / "skills"
            target = skills / "finite-horizon-lqr"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("# Finite Horizon LQR\n", encoding="utf-8")
            config = SimpleNamespace(root=root, skills_dir=skills)
            bundle = {
                "skill_library": [
                    {"skill_id": "finite-horizon-lqr", "title": "finite-horizon-lqr", "path": "skills/finite-horizon-lqr/SKILL.md"}
                ],
                "stage_01b_skill_standardizations": [{"title": "Finite-Horizon LQR for MPC"}],
            }
            self.assertEqual(
                Path("finite-horizon-lqr"),
                stage7._skill_relative_root(config, bundle, "Finite-Horizon LQR for MPC"),
            )

    def test_unknown_existing_skill_does_not_fall_back_to_new_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            skills = root / "skills"
            skills.mkdir()
            config = SimpleNamespace(root=root, skills_dir=skills)
            bundle = {"skill_library": [], "stage_01b_skill_standardizations": []}
            with self.assertRaisesRegex(RuntimeError, "must resolve to exactly one source Skill"):
                stage7._skill_relative_root(config, bundle, "Unknown Display Title")

    def test_committed_new_skill_can_be_revised_by_exact_directory_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            skills = root / "skills"
            repaired = root / "repaired"
            skills.mkdir()
            (repaired / "skill_for_n8").mkdir(parents=True)
            (repaired / "skill_for_n8" / "SKILL.md").write_text("# New skill\n", encoding="utf-8")
            config = SimpleNamespace(root=root, skills_dir=skills, output_skills_dir=repaired)
            bundle = {"skill_library": [], "stage_01b_skill_standardizations": []}
            self.assertEqual(
                Path("skill_for_n8"),
                stage7._skill_relative_root(config, bundle, "skill_for_n8"),
            )

    def test_physical_new_skill_limit_counts_candidate_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "source"
            repaired = root / "repaired"
            for library in (source, repaired):
                (library / "alpha").mkdir(parents=True)
                (library / "alpha" / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            (repaired / "new-one").mkdir()
            (repaired / "new-one" / "SKILL.md").write_text("# New one\n", encoding="utf-8")
            config = SimpleNamespace(skills_dir=source, output_skills_dir=repaired, max_new_skill_count=2)
            errors = stage7._max_new_skill_errors(
                config,
                [{"path": "new-two/SKILL.md", "content": "# New two\n"}],
            )
            self.assertEqual([], errors)
            errors = stage7._max_new_skill_errors(
                config,
                [
                    {"path": "new-two/SKILL.md", "content": "# New two\n"},
                    {"path": "new-three/SKILL.md", "content": "# New three\n"},
                ],
            )
            self.assertEqual(1, len(errors))
            self.assertIn("exceeding max_new_skills 2", errors[0])

    def test_per_suggestion_mode_keeps_every_action_separate(self) -> None:
        config = SimpleNamespace(stage7_repair_mode="per_suggestion", stage7_skill_package_size=2)
        units = stage7.prepare_repair_units(config, stage6_actions())
        self.assertEqual(7, len(units))
        self.assertTrue(all(unit["repair_unit_mode"] == "single_suggestion" for unit in units))
        self.assertTrue(all(len(unit["suggestion_ids"]) == 1 for unit in units))

    def test_package_mode_groups_same_skill_but_never_add_new_skill(self) -> None:
        config = SimpleNamespace(stage7_repair_mode="skill_package", stage7_skill_package_size=2)
        units = stage7.prepare_repair_units(config, stage6_actions())
        self.assertEqual(
            [["A1", "A2"], ["B1"], ["N1"], ["C1", "C2"], ["C3"]],
            [unit["suggestion_ids"] for unit in units],
        )
        add_units = [unit for unit in units if unit["action"] == "add_new_skill"]
        self.assertEqual(1, len(add_units))
        self.assertEqual(["N1"], add_units[0]["suggestion_ids"])
        self.assertTrue(all(len(unit["suggestion_ids"]) <= 2 for unit in units))

    def test_review_client_uses_independent_api_only_when_enabled(self) -> None:
        base = {
            "strong_base_url": "https://repair.example",
            "strong_api_key": "repair-key",
            "strong_model": "repair-model",
            "review_base_url": "https://review.example",
            "review_api_key": "review-key",
            "review_model": "review-model",
            "output_dir": Path("."),
        }
        shared = make_llm(
            SimpleNamespace(**base, use_separate_review_llm=False),
            "stage-07-review-shared",
            llm_role="review",
        )
        separate = make_llm(
            SimpleNamespace(**base, use_separate_review_llm=True),
            "stage-07-review-separate",
            llm_role="review",
        )
        self.assertEqual(("https://repair.example", "repair-model", "repair-key"), (shared.base_url, shared.model, shared.api_key))
        self.assertEqual(("https://review.example", "review-model", "review-key"), (separate.base_url, separate.model, separate.api_key))

    def test_package_state_and_prompt_keep_all_member_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            skills = root / "skills"
            alpha = skills / "alpha"
            alpha.mkdir(parents=True)
            (alpha / "SKILL.md").write_text("# Alpha\n\nOriginal content.\n", encoding="utf-8")
            config = SimpleNamespace(
                root=root,
                skills_dir=skills,
                output_dir=root / "run",
                output_skills_dir=root / "repaired",
                force=False,
                stage7_repair_mode="skill_package",
                stage7_skill_package_size=2,
                skill_word_limit=1200,
                max_prompt_chars=220_000,
            )
            bundle = {
                "skill_library": [
                    {
                        "skill_id": "alpha",
                        "path": "skills/alpha/SKILL.md",
                    }
                ]
            }
            actions = {
                "repair_actions": [
                    {"suggestion_id": "A1", "action": "revise_existing_skill", "target_skill_id": "alpha", "execution_order": 1},
                    {"suggestion_id": "A2", "action": "revise_existing_skill", "target_skill_id": "alpha", "execution_order": 2},
                ]
            }
            state = stage7.initialize_state(config, bundle, actions)
            self.assertEqual(2, state["suggestion_count"])
            self.assertEqual(1, state["repair_unit_count"])
            unit = state["suggestions"][0]
            payload = stage7._repair_payload(config, bundle, unit)
            self.assertEqual(["A1", "A2"], payload["suggestion_ids"])
            self.assertEqual(2, len(payload["selected_stage6_suggestions"]))
            self.assertIn("Original content.", payload["current_related_files"][0]["content"])
            prompt = stage7.build_repair_prompt(config, bundle, unit, payload)
            self.assertIn('"A1"', prompt)
            self.assertIn('"A2"', prompt)
            self.assertNotIn("<selected_stage5_suggestions>", prompt)
            self.assertNotIn("<selected_stage6_suggestions>", prompt)


if __name__ == "__main__":
    unittest.main()
