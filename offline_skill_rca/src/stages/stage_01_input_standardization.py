"""Stage 1：输入标准化。

Stage 1 不把 task、skills、trajectory index、constraints 塞进一次对话。
它被拆成两类 repair LLM 调用：

1a. 单独标准化 task_description；
1b. 每个 skill 文件单独标准化，可并行运行；

注意：Stage 1 明确不接收 trajectory，也不接收 constraints。轨迹证据从 Stage 3
开始使用，constraints 由后续需要它的 stage 自己接收。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .common import json_block, render_prompt_template, run_llm_stage

STAGE_NAME = "stage-01-input-standardization"
TASK_STAGE_NAME = "stage-01a-task-description-standardization"


def skill_stage_name(index: int, skill: dict[str, Any]) -> str:
    """生成每个 skill 标准化子轮次的 transcript 名称。"""
    from ..pipeline import sanitize

    return f"stage-01b-skill-{index + 1:02d}-{sanitize(str(skill.get('skill_id') or index + 1))}"


def task_schema() -> dict[str, Any]:
    """Stage 1a task_description 标准化期望 schema。"""
    return {
        "task_id": "string",
        "summary": "string",
        "inputs": [],
        "required_outputs": [],
        "constraints": [],
        "success_criteria": [],
        "ambiguities": [],
    }


def skill_schema() -> dict[str, Any]:
    """Stage 1b 单个 skill 标准化期望 schema。"""
    return {
        "title": "string",
        "intent": "string",
        "triggers": [],
        "inputs": [],
        "outputs": [],
        "procedure": [],
        "verification": [],
        "recovery": [],
        "tools_or_templates": [],
        "limits": [],
        "attached_files": [
            {
                "path": "string",
                "type": "code|document|others",
                "content": "string",
            }
        ],
    }


def aggregate_schema() -> dict[str, Any]:
    """本地组合后的 Stage 1 结构。

    这个结构不再由 repair LLM 生成，只作为脚本保存/兼容展示用；后续 prompt
    会直接接收 Stage 1a 和 Stage 1b 的输出。
    """
    return {
        "stage_01a_task_description_standardization": task_schema(),
        "stage_01b_skill_standardizations": [skill_schema()],
        "input_contract": {
            "allowed_inputs_in_stage1": ["task_description", "skill_library"],
            "excluded_from_stage1": ["failed_trajectories", "trajectory_index", "constraints", "hidden_evaluator_outputs"],
            "hidden_evaluator_outputs_present": False,
            "local_static_skill_scoring_present": False,
            "llm_aggregation_present": False,
        },
        "stage_notes": [],
    }


def build_task_prompt(bundle: dict[str, Any], max_chars: int) -> str:
    """构造 Stage 1a prompt，只包含 task_description。"""
    from ..pipeline import fit_prompt

    instructions = render_prompt_template(
        "stage-01a-task-description-standardization.txt",
        {"task_standardization_schema": json_block(task_schema())},
    )
    payload = {"task_description": bundle.get("task_description")}
    return fit_prompt(instructions, payload, max_chars)


def build_skill_prompt(skill: dict[str, Any], max_chars: int) -> str:
    """构造 Stage 1b prompt，只包含一个 skill 文件。"""
    from ..pipeline import fit_prompt

    instructions = render_prompt_template(
        "stage-01b-skill-standardization.txt",
        {"skill_standardization_schema": json_block(skill_schema())},
    )
    skill_file = {key: value for key, value in skill.items() if key != "attached_files"}
    payload = {
        "skill_file": skill_file,
        "skill_attached_files": skill.get("attached_files") or None,
    }
    return fit_prompt(instructions, payload, max_chars)


def build_prompt(bundle: dict[str, Any], max_chars: int) -> str:
    """兼容聚合调用：返回 Stage 1a 的 prompt。

    完整 Stage 1 需要 1a/1b 多轮 LLM 输出，不能仅靠一个 build_prompt
    完成。调试页会使用 ``build_task_prompt`` 和 ``build_skill_prompt`` 分别
    生成子轮次 prompt。
    """
    return build_task_prompt(bundle, max_chars)


def run_task(config: Any, bundle: dict[str, Any]) -> dict[str, Any]:
    """运行 Stage 1a：标准化 task_description。"""
    return run_llm_stage(config, TASK_STAGE_NAME, build_task_prompt(bundle, config.max_prompt_chars))


def run_skill_one(config: Any, index: int, skill: dict[str, Any]) -> dict[str, Any]:
    """运行 Stage 1b：标准化单个 skill。"""
    name = skill_stage_name(index, skill)
    result = run_llm_stage(config, name, build_skill_prompt(skill, config.max_prompt_chars), max_tokens=int(os.getenv("OFFLINE_SKILL_RCA_STAGE0B_MAX_TOKENS") or 4_000))
    if isinstance(result, dict):
        result.setdefault("skill_id", skill.get("skill_id"))
        result.setdefault("title", skill.get("title"))
        result.setdefault("path", skill.get("path"))
    return result


def run_skills(config: Any, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """并行运行所有 skill 的 Stage 1b 标准化。"""
    skills = list(bundle.get("skill_library") or [])
    if not skills:
        return []
    workers = max(1, min(int(os.getenv("OFFLINE_SKILL_RCA_STAGE0B_WORKERS") or config.trace_analysis_workers or 1), len(skills)))
    results: list[dict[str, Any] | None] = [None] * len(skills)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_skill_one, config, index, skill): index for index, skill in enumerate(skills)}
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
    return [item for item in results if item is not None]


def compose_result(task_result: dict[str, Any], skill_results: list[dict[str, Any]]) -> dict[str, Any]:
    """把 1a/1b 输出组合成一个本地快照。

    该快照不对应额外 LLM 调用，只方便状态页和旧代码读取；后续 stage 的 prompt
    不再依赖这个包装层。
    """
    result = {
        "input_contract": aggregate_schema()["input_contract"],
        "stage_notes": ["Extra Stage 1 aggregation LLM call is disabled; downstream stages use Stage 1a/1b outputs directly."],
    }
    result["stage_01a_task_description_standardization"] = task_result
    result["stage_01b_skill_standardizations"] = skill_results
    return result


def run(config: Any, bundle: dict[str, Any]) -> dict[str, Any]:
    """运行拆分后的 Stage 1，并返回本地组合结果。"""
    task_result = run_task(config, bundle)
    skill_results = run_skills(config, bundle)
    return compose_result(task_result, skill_results)
