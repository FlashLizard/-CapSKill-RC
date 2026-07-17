"""Stage 2：构造能力图，并同时分析能力节点与 Skill 的覆盖关系。

能力图与覆盖矩阵共享同一任务语义上下文。Repair LLM 先给出完成任务所需的
可复用能力节点，再为每个能力节点与每个 Skill 生成一条 node-skill 覆盖记录。
聚合分数、覆盖缺口与标签仍由本地代码确定性计算，避免让 LLM 执行公式。

本阶段只使用任务描述、Stage 1a 的任务标准化结果、原始 Skill 库和 Stage 1b
的 Skill 标准化结果；不读取失败轨迹。这样能力图和初始覆盖判断不会被某次具体
失败路径牵引。
"""
from __future__ import annotations

from typing import Any

from ..calculations import postprocess_skill_coverage, summarize_node_coverage
from .common import json_block, render_prompt_template, run_llm_stage

STAGE_NAME = "stage-02-capability-graph"


def stage_schema() -> dict[str, Any]:
    """返回 Stage 2 的能力图与覆盖矩阵联合 JSON 契约。"""
    return {
        "capability_graph": {
            "nodes": [
                {
                    "node_id": "N1",
                    "goal": "string",
                    "inputs": [],
                    "outputs": [],
                    "operations": [],
                    "checks": [],
                }
            ],
            "edges": [{"from": "N1", "to": "N2", "description": "why N2 depends on N1"}],
        },
        "coverage_pairs": [
            {
                "node_id": "N1",
                "skill_id": "string",
                "directly_relevant": True,
                "relevance_reason": "string",
                "scores": {
                    "requirement_fit": 0.0,
                    "trigger": 0.0,
                    "procedure": 0.0,
                    "verification": 0.0,
                    "recovery": 0.0,
                    "execution_support": 0.0,
                },
                "execution_support_need": "not_needed|helpful|required",
                "evidence": [],
            }
        ],
    }


def build_prompt(
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    max_chars: int,
) -> str:
    """构造 Stage 2 prompt，不包含任何轨迹或 verifier 信息。"""
    from ..pipeline import fit_prompt

    instructions = render_prompt_template(
        "stage-02-capability-graph.txt",
        {"stage2_schema": json_block(stage_schema())},
    )
    payload = {
        "stage_01a_task_description_standardization": task_standardization,
        "stage_01b_skill_standardizations": skill_standardizations,
    }
    return fit_prompt(instructions, payload, max_chars)


def run(
    config: Any,
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
) -> dict[str, Any]:
    """运行 Stage 2，并用本地公式补齐覆盖聚合字段。"""
    result = run_llm_stage(
        config,
        STAGE_NAME,
        build_prompt(bundle, task_standardization, skill_standardizations, config.max_prompt_chars),
    )
    result = postprocess_skill_coverage(result)
    result["node_coverage_summary"] = summarize_node_coverage(result)
    return result
