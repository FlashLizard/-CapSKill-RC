"""Stage 6：逐 node 并行生成 Skill 修复建议。

本阶段的公开名称直接描述其职责，不再沿用历史的 root-cause-hypotheses 名称：

1. 本地代码只负责筛选“需要修复或需要新增 skill 的 node”，并整理绑定到该
   node 的可见证据；
2. 每个需修复 node 单独生成一个 prompt，并行调用 repair LLM；
3. repair LLM 在每个 node prompt 中直接输出针对 skill 的修复建议；
4. Stage 6 聚合逐 node 响应，先交给 Stage 7 合并新增操作，再由 Stage 8 执行修复。

不需要修复/新增的 node 不会生成 prompt，也不会出现在 Stage 6 的主输出列表中。
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
from typing import Any

from ..calculations import clamp01, is_directly_relevant, postprocess_skill_coverage, stable_unique
from .common import json_block, make_llm, render_prompt_template, stage_system_prompt, write_prompt_file

STAGE_NAME = "stage-06-skill-repair-suggestions"

# 这些阈值都写入最终 JSON，方便 Web 页面和人工诊断时知道分类从何而来。
# 如果后续你想调整“什么时候认为 node 需要修复”，优先改这里。
BAD_EVENT_COUNT_THRESHOLD = 1
SUCCESS_RATE_THRESHOLD = 0.80
BAD_EVENT_RATIO_THRESHOLD = 0.20
ADEQUATE_COVERAGE_THRESHOLD = 0.85

# 节点状态只回答两个关键问题：是否执行、执行结果是否正确。
# ``bad`` 与 ``fail`` 都表示已执行但不正确，保留两者只会让标注不稳定；读取
# 历史运行时会把 bad 兼容归一为 fail。blocked 是上游传播结果，不是本节点
# 的直接失败；miss 表示节点可执行但被遗漏，仍属于本节点直接责任。
STATUS_VALUES = ["pass", "fail", "miss", "blocked", "unknown"]
DIRECT_FAILURE_STATUSES = {"fail", "miss"}

SEVERITY_SCORE = {
    "fatal": 1.0,
    "major": 0.75,
    "minor": 0.35,
}

DIMENSION_FIELDS = [
    "node_requirement_fit",
    "trigger_coverage",
    "procedure_coverage",
    "verification_coverage",
    "recovery_coverage",
    "execution_support_coverage",
]

DIMENSION_TO_REPAIR_NOTE = {
    "node_requirement_fit": "补充该 skill 对当前能力节点所需输入、输出、操作和检查点的显式覆盖。",
    "trigger_coverage": "补充更明确的 When to Use / trigger，使弱模型在该 node 场景中会主动调用 skill。",
    "procedure_coverage": "把抽象建议改成可执行的步骤，尤其覆盖 bad events 暴露出的错误路径。",
    "verification_coverage": "补充完成该 node 后必须执行的检查项，避免错误中间结果继续传播。",
    "recovery_coverage": "补充失败恢复策略，说明出现空结果、异常、错配或低置信判断时如何回退重做。",
    "execution_support_coverage": "在确有执行支持需求时补充命令、代码片段、模板或检查脚本。",
}


def stage6_schema() -> dict[str, Any]:
    """返回三种 action 对应的最小响应结构。

    模板会明确要求只返回与 ``node_repair_action`` 对应的内部对象，而不是返回
    这个三分支展示包装。这样 Debug 页仍能一次编辑全部契约，每次 LLM 调用又只
    需要生成当前 action 真正需要的字段。
    """
    return {
        "revise_existing_skill": {
            "node_id": "N1",
            "action": "revise_existing_skill",
            "issue": "string",
            "repairs": [
                {
                    "suggestion_id": "string",
                    "skill_id": "string",
                    "goal": "string",
                    "changes": [
                        {
                            "area": "trigger|procedure|verification|recovery|execution_support",
                            "instruction": "string",
                        }
                    ],
                    "evidence_refs": [{"traj_id": "string", "event_id": "string"}],
                    "constraints": [],
                }
            ],
        },
        "add_new_skill": {
            "node_id": "N1",
            "action": "add_new_skill",
            "issue": "string",
            "new_skill": {
                "suggestion_id": "string",
                "skill_id": "string",
                "goal": "string",
                "triggers": [],
                "procedure": [],
                "verification": [],
                "recovery": [],
                "attached_files": [],
                "evidence_refs": [{"traj_id": "string", "event_id": "string"}],
                "constraints": [],
            },
        },
    }


def build_prompt(
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage3: list[dict[str, Any]],
    stage4: dict[str, Any],
    stage5: list[dict[str, Any]],
    max_chars: int,
) -> str:
    """生成 Stage 6 的整体预览文本。

    真正运行时会调用 ``build_node_prompt`` 为每个需修复 node 单独生成 prompt。
    这个 wrapper 主要用于历史接口和 Web 未选择子 node 时的预览。
    """
    from ..pipeline import fit_prompt

    node_inputs = prepare_node_inputs(bundle, stage2, skill_standardizations, stage3, stage4, stage5)
    preview = {
        "note": "Stage 6 runs one repair-LLM prompt per repair-needed node. Select a Stage 6 node child to view the exact prompt.",
        "repair_needed_node_count": len(node_inputs),
        "node_prompt_jobs": [
            {
                "index": item["index"],
                "node_id": item["node_id"],
                "local_recommended_action": item["local_recommended_action"],
                "stage_name": stage_name(item["index"], item),
            }
            for item in node_inputs
        ],
    }
    instructions = render_prompt_template(
        "stage-06-skill-repair-suggestions.txt",
        {"stage6_schema": json_block(stage6_schema())},
    )
    payload = {
        "stage_02_capability_graph": stage2.get("capability_graph") or stage2,
        "node_id": "<select a Stage 6 node child>",
        "node_repair_action": "<select a Stage 6 node child>",
        "node_bound_evidence": preview,
        "node_related_skill_library": [],
        "stage_01b_skill_standardizations": skill_standardizations,
    }
    return fit_prompt(instructions, payload, max_chars)


def stage_name(index: int, node_input: dict[str, Any]) -> str:
    """生成逐 node Stage 6 transcript/prompt 文件名。"""
    from ..pipeline import sanitize

    return f"stage-06-node-{index + 1:02d}-{sanitize(str(node_input.get('node_id') or index + 1))}"


def prepare_node_inputs(
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage3: list[dict[str, Any]],
    stage4: dict[str, Any],
    stage5: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """本地筛选需要进入 Stage 6 LLM 的 node，并整理 node-bound evidence。

    不需要修复/新增 skill 的 node 不会进入返回列表，因此不会在 Web 子阶段中展示，
    也不会生成 LLM prompt。
    """
    nodes = _graph_nodes(stage2)
    coverage_rows = _coverage_rows(stage2)
    coverage_by_node = _coverage_by_node(coverage_rows)
    alignment_by_event = _alignment_by_event(stage4)
    events_by_node, _unaligned_events = _events_by_node(stage3, alignment_by_event)
    total_traces = max(len(stage5), 1)

    node_inputs: list[dict[str, Any]] = []
    for node_ordinal, node in enumerate(nodes):
        node_id = str(node.get("node_id") or "").strip()
        if not node_id:
            continue
        all_node_events = events_by_node.get(node_id, [])
        status_analysis = _node_status_analysis(stage5, node_id)
        event_partition = _partition_events_by_node_status(all_node_events, status_analysis)
        # 只有节点自身为 fail/miss 的轨迹事件可以触发本节点修复。blocked 是上游
        # 失败传播，pass/unknown 也不能作为本节点已失败的确定证据。
        node_events = event_partition["direct_failure_events"]
        coverage_summary = _node_coverage_summary(node_id, coverage_by_node.get(node_id, []))
        classification = _classify_node(
            node_events,
            status_analysis,
            coverage_summary,
            total_traces,
            event_partition=event_partition,
        )
        if not classification.get("needs_repair"):
            continue

        node_record = {
            "node_id": node_id,
            "node_goal": _node_goal(node),
            "skill_coverage": coverage_summary,
            "execution_success_analysis": status_analysis,
            "bad_event_list": node_events,
            "context_events_not_used_for_repair_trigger": event_partition["context_events"],
            "classification": classification,
            "recommended_action": _recommended_action(classification),
            "target_skill_ids": [
                row.get("skill_id")
                for row in coverage_summary.get("directly_relevant_rows") or []
                if row.get("skill_id")
            ],
        }
        node_inputs.append(
            {
                "index": len(node_inputs),
                "node_ordinal": node_ordinal,
                "node_id": node_id,
                "local_recommended_action": node_record["recommended_action"],
                "classification": classification,
                "node_bound_evidence": _node_bound_evidence(node_record, total_traces),
                "node_related_skill_library": _node_related_skill_library(bundle, node_record["target_skill_ids"]),
                "target_skill_ids": node_record["target_skill_ids"],
            }
        )
    return node_inputs


def build_node_prompt(
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    node_input: dict[str, Any],
    max_chars: int,
) -> str:
    """构造单个 node 的 Stage 6 prompt。"""
    from ..pipeline import fit_prompt

    instructions = render_prompt_template(
        "stage-06-skill-repair-suggestions.txt",
        {"stage6_schema": json_block(stage6_schema())},
    )
    payload = {
        "stage_02_capability_graph": stage2.get("capability_graph") or stage2,
        "node_id": node_input.get("node_id"),
        "node_repair_action": node_input.get("local_recommended_action"),
        "node_bound_evidence": node_input.get("node_bound_evidence"),
        "node_related_skill_library": node_input.get("node_related_skill_library") or [],
        "stage_01b_skill_standardizations": skill_standardizations,
    }
    stage_max_chars = int(os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_PROMPT_CHARS") or max_chars)
    return fit_prompt(instructions, payload, stage_max_chars)


def run_one(
    config: Any,
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    index: int,
    node_input: dict[str, Any],
) -> dict[str, Any]:
    """运行单个 node 的 Stage 6 repair-suggestion prompt。"""
    name = stage_name(index, node_input)
    prompt = build_node_prompt(bundle, stage2, skill_standardizations, node_input, config.max_prompt_chars)
    write_prompt_file(config, name, prompt)
    max_tokens = int(os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_TOKENS") or 8_000)
    for attempt in range(2):
        attempt_name = name if attempt == 0 else f"{name}-forced-retry"
        attempt_prompt = prompt
        if attempt:
            attempt_prompt += (
                "\n\n# required correction\n"
                "Return the requested repair action with at least one complete executable suggestion."
            )
            write_prompt_file(config, attempt_name, attempt_prompt)
        result = make_llm(config, attempt_name).chat_json(
            stage_system_prompt(STAGE_NAME), attempt_prompt, max_tokens=max_tokens
        )
        normalized = _normalize_node_response(result, node_input)
        if normalized.get("skill_repair_suggestions"):
            return normalized
    raise RuntimeError(f"Stage 6 returned no executable repair suggestion for {node_input.get('node_id')}")


def run(
    config: Any,
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage3: list[dict[str, Any]],
    stage4: dict[str, Any],
    stage5: list[dict[str, Any]],
) -> dict[str, Any]:
    """并行运行所有需修复 node 的 Stage 6 LLM 修复建议生成。"""
    node_inputs = prepare_node_inputs(bundle, stage2, skill_standardizations, stage3, stage4, stage5)
    if not node_inputs:
        return compose_result([], [])

    workers = max(1, min(int(config.trace_analysis_workers or 1), len(node_inputs)))
    results: list[dict[str, Any] | None] = [None] * len(node_inputs)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_one, config, bundle, stage2, skill_standardizations, index, node_input): index
            for index, node_input in enumerate(node_inputs)
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
    return compose_result(node_inputs, [item for item in results if item is not None])


def compose_result(node_inputs: list[dict[str, Any]], node_results: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐 node LLM 响应聚合成 Stage 7 可消费的结构。"""
    normalized = []
    for index, result in enumerate(node_results):
        node_input = node_inputs[index] if index < len(node_inputs) else {}
        normalized.append(_normalize_node_response(result, node_input))

    return {
        "stage_type": "node_parallel_skill_repair_suggestions",
        "stage_name": STAGE_NAME,
        # 旧字段保留为空，避免下游旧代码误把 Stage 5 当 root-cause stage。
        "root_cause_hypotheses": [],
        "thresholds": {
            "bad_event_count_threshold": BAD_EVENT_COUNT_THRESHOLD,
            "bad_event_ratio_threshold": BAD_EVENT_RATIO_THRESHOLD,
            "attempted_success_rate_threshold": SUCCESS_RATE_THRESHOLD,
        },
        "node_prompt_jobs": [
            {
                "index": item.get("index"),
                "node_id": item.get("node_id"),
                "stage_name": stage_name(int(item.get("index") or 0), item),
                "local_recommended_action": item.get("local_recommended_action"),
            }
            for item in node_inputs
        ],
        "node_repair_recommendations": normalized,
        "node_skill_repair_suggestions": normalized,
        "skill_repair_recommendations": _aggregate_existing_skill_suggestions(normalized),
        "new_skill_recommendations": _aggregate_new_skill_suggestions(normalized),
        "stage_notes": [
            "Stage 5 called the repair LLM once per repair-needed node.",
            "Nodes that did not require repair or new skill creation are intentionally omitted from this output.",
            "Stage 5 suggestions are repair-planning material. Stage 6 remains responsible for drafting concrete skill patches.",
        ],
    }


def _node_bound_evidence(node_record: dict[str, Any], total_traces: int) -> dict[str, Any]:
    """构造绑定到单个 node 的 evidence payload。

    注意：这里刻意不放入 DAG node 的 goal、purpose、required operations 等定义性
    内容；prompt 中会直接提供完整 DAG，用户可以在模板里控制 DAG 展示方式。
    """
    node_pressure = _node_pressure(node_record, total_traces)
    events = node_record.get("bad_event_list") or []
    coverage_summary = node_record.get("skill_coverage") or {}
    existing_candidates = []
    for row in coverage_summary.get("directly_relevant_rows") or []:
        evidence_items = [
            _event_evidence_item(event, _event_weight_for_skill(event, row, node_pressure))
            for event in events
        ]
        evidence_items.append(_status_evidence_item(node_record, node_pressure, row=row))
        evidence_items.sort(key=lambda item: item.get("weight") or 0.0, reverse=True)
        existing_candidates.append(
            {
                "skill_id": row.get("skill_id"),
                "skill_title": row.get("skill_title"),
                "coverage": {
                    "overall_coverage": row.get("overall_coverage"),
                    "coverage_gap": row.get("coverage_gap"),
                    "coverage_labels": row.get("coverage_labels"),
                    "missing_slots": row.get("missing_slots"),
                    "dimension_scores": row.get("dimension_scores"),
                    "low_score_dimensions": row.get("low_score_dimensions"),
                    "direct_relevance_rationale": row.get("direct_relevance_rationale"),
                },
                "local_repair_basis_summary": _repair_target_summary(node_record, row),
                "evidence_items": evidence_items,
                "priority_score": _average_weight(evidence_items, fallback=node_pressure * _coverage_gap(row)),
            }
        )

    new_skill_candidate = None
    if node_record.get("recommended_action") == "add_new_skill":
        evidence_items = [
            _event_evidence_item(event, _event_weight_for_new_skill(event, node_pressure))
            for event in events
        ]
        evidence_items.append(_status_evidence_item(node_record, node_pressure, row=None))
        evidence_items.sort(key=lambda item: item.get("weight") or 0.0, reverse=True)
        new_skill_candidate = {
            "suggested_new_skill_id_seed": f"skill_for_{str(node_record.get('node_id') or 'node').lower()}",
            "coverage_absence": 1.0,
            "evidence_items": evidence_items,
            "priority_score": _average_weight(evidence_items, fallback=node_pressure),
        }

    return {
        "node_id": node_record.get("node_id"),
        "local_triage": {
            "recommended_action": node_record.get("recommended_action"),
            "classification": node_record.get("classification"),
            "node_pressure": node_pressure,
        },
        "execution_success_analysis": node_record.get("execution_success_analysis"),
        "skill_coverage_for_node": {
            "directly_relevant_skill_count": coverage_summary.get("directly_relevant_skill_count"),
            "best_overall_coverage": coverage_summary.get("best_overall_coverage"),
            "node_gap": coverage_summary.get("node_gap"),
            "best_skill_ids": coverage_summary.get("best_skill_ids"),
            "not_relevant_skill_count": coverage_summary.get("not_relevant_skill_count"),
        },
        "bad_events_aligned_to_node": events,
        "candidate_existing_skill_repairs": existing_candidates,
        "candidate_new_skill": new_skill_candidate,
    }


def _node_related_skill_library(bundle: dict[str, Any], target_skill_ids: list[Any]) -> list[dict[str, Any]]:
    """只给 node prompt 提供直接相关的原始 skill 文件。

    新增 skill 场景没有 target_skill_ids，此时返回空列表；prompt 仍会提供 Stage 1b
    的所有 skill summaries，足够 repair LLM 判断是否真的需要新增。
    """
    targets = {str(item) for item in target_skill_ids if item}
    if not targets:
        return []
    related = []
    for skill in bundle.get("skill_library") or []:
        if not isinstance(skill, dict):
            continue
        skill_id = str(skill.get("skill_id") or skill.get("id") or "").strip()
        if skill_id in targets:
            related.append(skill)
    return related


def _normalize_node_response(result: dict[str, Any], node_input: dict[str, Any]) -> dict[str, Any]:
    """补齐 repair LLM 单 node 响应中的必要字段。"""
    out = dict(result or {})
    node_id = str(node_input.get("node_id") or out.get("node_id") or "").strip()
    out["node_id"] = node_id
    requested_action = str(node_input.get("local_recommended_action") or "add_new_skill")
    response_action = str(out.get("action") or out.get("node_repair_action") or requested_action)
    if response_action != requested_action:
        response_action = requested_action
    out["action"] = response_action
    out["node_repair_action"] = response_action
    out["node_issue_summary"] = str(out.get("issue") or out.get("node_issue_summary") or "")

    # 把新版三分支响应转换为旧兼容字段；规范字段仍原样保留在 out 中供审阅。
    if response_action == "revise_existing_skill" and isinstance(out.get("repairs"), list):
        converted_repairs = []
        for repair in out.get("repairs") or []:
            if not isinstance(repair, dict):
                continue
            change_requests: dict[str, list[str]] = {
                "triggers": [],
                "procedure": [],
                "verification": [],
                "recovery": [],
                "execution_support": [],
            }
            area_map = {"trigger": "triggers"}
            for change in repair.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                area = area_map.get(str(change.get("area") or ""), str(change.get("area") or ""))
                instruction = str(change.get("instruction") or "").strip()
                if area in change_requests and instruction:
                    change_requests[area].append(instruction)
            converted_repairs.append(
                {
                    "suggestion_id": repair.get("suggestion_id"),
                    "skill_id": repair.get("skill_id"),
                    "repair_goal": repair.get("goal"),
                    "change_requests": change_requests,
                    "evidence_refs": repair.get("evidence_refs") or [],
                    "anti_overfit_constraints": repair.get("constraints") or [],
                }
            )
        out["existing_skill_repairs"] = converted_repairs
    if response_action == "add_new_skill" and isinstance(out.get("new_skill"), dict):
        new_skill = out["new_skill"]
        out["new_skill_proposal"] = {
            "suggestion_id": new_skill.get("suggestion_id"),
            "new_skill_id": new_skill.get("skill_id"),
            "skill_goal": new_skill.get("goal"),
            "when_to_use": new_skill.get("triggers") or [],
            "inputs_outputs": [],
            "procedure_requirements": new_skill.get("procedure") or [],
            "verification_requirements": new_skill.get("verification") or [],
            "recovery_requirements": new_skill.get("recovery") or [],
            "attached_files": new_skill.get("attached_files") or [],
            "evidence_refs": new_skill.get("evidence_refs") or [],
            "anti_overfit_constraints": new_skill.get("constraints") or [],
        }
    out["existing_skill_repairs"] = [
        item for item in out.get("existing_skill_repairs") or []
        if isinstance(item, dict)
    ]
    new_skill_proposal = out.get("new_skill_proposal")
    out["new_skill_proposal"] = new_skill_proposal if isinstance(new_skill_proposal, dict) else None
    out.pop("manual" + "_review", None)

    # Stage 6 仍消费统一的 skill_repair_suggestions。这里把新的分支式输出
    # 规整成兼容结构，避免让 repair LLM 在 prompt 中维护两套 JSON。
    suggestions = _compat_suggestions_from_branch_schema(out, node_id)

    # 兼容旧 run 或旧模板：如果 LLM 仍返回旧字段，也照常纳入。
    seen_suggestion_ids = {
        str(item.get("suggestion_id"))
        for item in suggestions
        if isinstance(item, dict) and item.get("suggestion_id")
    }
    for legacy in out.get("skill_repair_suggestions") or []:
        if not isinstance(legacy, dict):
            continue
        normalized = dict(legacy)
        normalized.setdefault("suggestion_id", f"{node_id}-S{len(suggestions) + 1}")
        if str(normalized.get("suggestion_id")) in seen_suggestion_ids:
            continue
        normalized.setdefault("action", out.get("node_repair_action") or requested_action)
        if not normalized.get("target_skill_id"):
            normalized["target_skill_id"] = normalized.get("skill_id") or _unique_evidence_skill_id(normalized)
        normalized.setdefault("new_skill_id", None)
        normalized.setdefault("priority", "medium")
        normalized.setdefault("confidence", 0.5)
        normalized.setdefault("evidence_refs", [])
        normalized.setdefault("coverage_gaps_to_address", [])
        normalized.setdefault("sections_to_change", [])
        normalized.setdefault("concrete_change_requirements", [])
        normalized["node_id"] = node_id
        suggestions.append(normalized)
        seen_suggestion_ids.add(str(normalized.get("suggestion_id")))
    out["skill_repair_suggestions"] = suggestions
    out["local_stage6_context"] = {
        "node_index": node_input.get("index"),
        "local_recommended_action": node_input.get("local_recommended_action"),
        "target_skill_ids": node_input.get("target_skill_ids") or [],
        "classification": node_input.get("classification") or {},
    }
    return out


def _compat_suggestions_from_branch_schema(out: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """把 Stage 5 精简分支 schema 转换成 Stage 6 兼容建议列表。"""
    suggestions: list[dict[str, Any]] = []
    action = str(out.get("node_repair_action") or "add_new_skill")
    for index, repair in enumerate(out.get("existing_skill_repairs") or []):
        change_requests = repair.get("change_requests") if isinstance(repair.get("change_requests"), dict) else {}
        concrete_requirements = _flatten_change_requests(change_requests)
        suggestions.append(
            {
                "suggestion_id": repair.get("suggestion_id") or f"{node_id}-R{index + 1}",
                "node_id": node_id,
                "action": "revise_existing_skill",
                # 新版 Stage 5 schema 使用更直观的 skill_id；旧运行可能仍返回
                # target_skill_id。统一后 Stage 6 才能精确定位要复制和修改的目录。
                "target_skill_id": repair.get("target_skill_id") or repair.get("skill_id"),
                "new_skill_id": None,
                "problem_diagnosis": out.get("node_issue_summary") or "",
                "repair_objective": repair.get("repair_goal") or "",
                "evidence_refs": repair.get("evidence_refs") or [],
                "coverage_gaps_to_address": [
                    key for key, value in change_requests.items()
                    if value and key in {"triggers", "procedure", "verification", "recovery", "execution_support"}
                ],
                "sections_to_change": _sections_from_change_requests(change_requests),
                "concrete_change_requirements": concrete_requirements,
                "change_requests": change_requests,
                "anti_overfit_constraints": repair.get("anti_overfit_constraints") or [],
            }
        )

    proposal = out.get("new_skill_proposal")
    if isinstance(proposal, dict) and _has_meaningful_new_skill_proposal(proposal, action):
        new_skill_id = proposal.get("new_skill_id") or proposal.get("new_skill_id_suggestion")
        concrete_requirements = []
        for key in ["when_to_use", "inputs_outputs", "procedure_requirements", "verification_requirements", "recovery_requirements"]:
            for item in _as_list(proposal.get(key)):
                concrete_requirements.append(f"{key}: {item}")
        suggestions.append(
            {
                "suggestion_id": proposal.get("suggestion_id") or f"{node_id}-N1",
                "node_id": node_id,
                "action": "add_new_skill",
                "target_skill_id": None,
                "new_skill_id": new_skill_id,
                "new_skill_id_suggestion": new_skill_id,
                "problem_diagnosis": out.get("node_issue_summary") or "",
                "repair_objective": proposal.get("skill_goal") or "",
                "evidence_refs": proposal.get("evidence_refs") or [],
                "coverage_gaps_to_address": ["absent_skill"],
                "sections_to_change": ["new_skill"],
                "concrete_change_requirements": concrete_requirements,
                "new_skill_proposal": proposal,
                "anti_overfit_constraints": proposal.get("anti_overfit_constraints") or [],
            }
        )

    return suggestions


def _unique_evidence_skill_id(suggestion: dict[str, Any]) -> str | None:
    """仅当证据一致指向一个 skill 时，用它恢复兼容建议的目标 id。"""
    skill_ids = {
        str(item.get("skill_id") or "").strip()
        for item in suggestion.get("evidence_refs") or []
        if isinstance(item, dict) and str(item.get("skill_id") or "").strip()
    }
    return next(iter(skill_ids)) if len(skill_ids) == 1 else None


def _flatten_change_requests(change_requests: dict[str, Any]) -> list[str]:
    """把分栏 change_requests 展平成 Stage 6 便于阅读的要求列表。"""
    out: list[str] = []
    for key in ["triggers", "procedure", "verification", "recovery", "execution_support"]:
        for item in _as_list(change_requests.get(key)):
            out.append(f"{key}: {item}")
    return out


def _has_meaningful_new_skill_proposal(proposal: dict[str, Any], action: str) -> bool:
    """判断 new_skill_proposal 是否真的是新增 skill 分支，而不是空对象。"""
    if action != "add_new_skill":
        return False
    return any(
        proposal.get(key)
        for key in [
            "new_skill_id",
            "new_skill_id_suggestion",
            "skill_goal",
            "when_to_use",
            "procedure_requirements",
            "verification_requirements",
            "recovery_requirements",
        ]
    )


def _sections_from_change_requests(change_requests: dict[str, Any]) -> list[str]:
    """把 change_requests 键名映射到 SKILL.md 中常见章节名。"""
    mapping = {
        "triggers": "When to Use",
        "procedure": "Procedure",
        "verification": "Verification Checklist",
        "recovery": "Recovery",
        "execution_support": "Minimal Template / Code Snippet",
    }
    return [
        section for key, section in mapping.items()
        if change_requests.get(key)
    ]


def _aggregate_existing_skill_suggestions(node_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐 node 的 revise_existing_skill 建议按 target_skill_id 聚合。"""
    by_skill: dict[str, dict[str, Any]] = {}
    for node_result in node_results:
        node_id = node_result.get("node_id")
        for suggestion in node_result.get("skill_repair_suggestions") or []:
            if suggestion.get("action") != "revise_existing_skill":
                continue
            skill_id = str(suggestion.get("target_skill_id") or "").strip()
            if not skill_id:
                continue
            item = by_skill.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "action": "revise_existing_skill",
                    "affected_node_ids": [],
                    "node_suggestions": [],
                    "priority_score": 0.0,
                },
            )
            if node_id and node_id not in item["affected_node_ids"]:
                item["affected_node_ids"].append(node_id)
            item["node_suggestions"].append(suggestion)
            item["priority_score"] = max(item["priority_score"], _suggestion_priority_score(suggestion))
    out = list(by_skill.values())
    for item in out:
        item["priority_score"] = round(item.get("priority_score") or 0.0, 4)
        item["recommendation_summary"] = (
            f"Repair {item['skill_id']} for nodes {', '.join(map(str, item.get('affected_node_ids') or []))} "
            "using Stage 5 per-node repair suggestions."
        )
    out.sort(key=lambda item: item.get("priority_score") or 0.0, reverse=True)
    return out


def _aggregate_new_skill_suggestions(node_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """收集逐 node 的 add_new_skill 建议。"""
    out = []
    for node_result in node_results:
        for suggestion in node_result.get("skill_repair_suggestions") or []:
            if suggestion.get("action") != "add_new_skill":
                continue
            item = dict(suggestion)
            item["node_id"] = node_result.get("node_id")
            item["action"] = "add_new_skill"
            item["new_skill_id_suggestion"] = item.get("new_skill_id_suggestion") or item.get("new_skill_id")
            proposal = item.get("new_skill_proposal") if isinstance(item.get("new_skill_proposal"), dict) else {}
            item["definition_basis"] = {
                "skill_goal": proposal.get("skill_goal") or item.get("repair_objective"),
                "when_to_use": proposal.get("when_to_use") or [],
                "inputs_outputs": proposal.get("inputs_outputs") or [],
                "procedure_requirements": proposal.get("procedure_requirements") or [],
                "verification_requirements": proposal.get("verification_requirements") or [],
                "recovery_requirements": proposal.get("recovery_requirements") or [],
                "anti_overfit_constraints": proposal.get("anti_overfit_constraints") or item.get("anti_overfit_constraints") or [],
            }
            item["recommendation_summary"] = (
                f"Add {item.get('new_skill_id_suggestion') or 'a new skill'} for node {item.get('node_id')} "
                f"to support: {item['definition_basis'].get('skill_goal') or item.get('repair_objective') or 'the missing capability'}."
            )
            item["evidence_items"] = item.get("evidence_items") or item.get("evidence_refs") or []
            item["priority_score"] = _suggestion_priority_score(suggestion)
            out.append(item)
    out.sort(key=lambda item: item.get("priority_score") or 0.0, reverse=True)
    return out


def _suggestion_priority_score(suggestion: dict[str, Any]) -> float:
    """把 LLM 的 priority/confidence 转成可排序数值。"""
    priority = str(suggestion.get("priority") or "").strip().lower()
    base = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(priority, 0.5)
    confidence = clamp01(suggestion.get("confidence"))
    if confidence is None:
        confidence = 0.5
    return round(0.6 * base + 0.4 * confidence, 4)


def _as_list(value: Any) -> list[Any]:
    """把可能为空、单值或列表的字段安全规整为列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _graph_nodes(stage2: dict[str, Any]) -> list[dict[str, Any]]:
    """从 Stage 2 输出中读取能力图节点。"""
    graph = stage2.get("capability_graph") or stage2
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    return [node for node in nodes or [] if isinstance(node, dict)]


def _node_goal(node: dict[str, Any]) -> str:
    """读取 node 的自然语言目标，兼容 goal/purpose/description。"""
    return str(node.get("goal") or node.get("purpose") or node.get("description") or "").strip()


def _node_definition(node: dict[str, Any]) -> dict[str, Any]:
    """保留 Stage 6 定义新 skill 时需要的 node 语义信息。"""
    return {
        "node_id": node.get("node_id"),
        "goal": _node_goal(node),
        "required_inputs": _as_list(node.get("inputs") or node.get("required_inputs")),
        "expected_outputs": _as_list(node.get("outputs") or node.get("expected_outputs")),
        "required_operations": _as_list(node.get("operations") or node.get("required_operations")),
        "required_checks": _as_list(node.get("checks") or node.get("required_checks")),
        "common_failure_modes": _as_list(node.get("common_failure_modes")),
        "dependencies": _as_list(node.get("dependencies")),
    }


def _coverage_rows(stage2: dict[str, Any]) -> list[dict[str, Any]]:
    """读取 Stage 2 与能力图同时产出的 node-skill coverage rows。"""
    rows = []
    if isinstance(stage2, dict):
        # Debug 页可能展示 LLM 原始 response；这里再跑一次幂等 postprocess，确保
        # Stage 5 总能拿到本地计算的 overall_coverage、coverage_gap 和 labels。
        stage2 = postprocess_skill_coverage(stage2)
        rows = stage2.get("coverage_pairs") or stage2.get("skill_coverage_matrix") or stage2.get("coverage_matrix") or []
    return [row for row in rows if isinstance(row, dict)]


def _coverage_by_node(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按 node_id 聚合 coverage rows。"""
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        node_id = str(row.get("node_id") or "").strip()
        if node_id:
            by_node[node_id].append(row)
    return by_node


def _alignment_by_event(stage4: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """读取 Stage 3 event-to-node alignment，并建立可快速查询的索引。"""
    alignments = []
    if isinstance(stage4, dict):
        alignments = stage4.get("event_node_alignments") or stage4.get("alignments") or []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in alignments:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("event_id") or "").strip()
        traj_id = str(item.get("traj_id") or "").strip()
        if event_id:
            index[(traj_id, event_id)] = item
            # 有些早期输出没有 traj_id；保留一个弱匹配索引兜底。
            index[("", event_id)] = item
    return index


def _valid_node_id(value: Any) -> str | None:
    """过滤掉 LLM alignment 里表示“无关/未知”的 node_id。"""
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"none", "null", "unknown", "irrelevant", "not_relevant", "n/a", "na"}:
        return None
    return text


def _events_by_node(
    stage3: list[dict[str, Any]],
    alignment_by_event: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """把 Stage 2 bad events 合并 Stage 3 alignment 后按 node 聚合。"""
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unaligned: list[dict[str, Any]] = []

    for trace in stage3:
        if not isinstance(trace, dict):
            continue
        traj_id = str(trace.get("traj_id") or "").strip()
        events = list(trace.get("failure_events") or trace.get("bad_events") or [])
        events.extend(trace.get("cause_events") or [])
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "").strip()
            alignment = alignment_by_event.get((traj_id, event_id)) or alignment_by_event.get(("", event_id)) or {}
            node_id = _valid_node_id(alignment.get("node_id")) or _valid_node_id(
                event.get("suspected_capability_node") or event.get("node_id")
            )
            event_record = _event_record(traj_id, event, alignment, node_id)
            if node_id:
                by_node[node_id].append(event_record)
            else:
                unaligned.append(event_record)

    for events in by_node.values():
        events.sort(key=lambda item: (item.get("traj_id") or "", item.get("step_id") or 0, item.get("event_id") or ""))
    return by_node, unaligned


def _event_record(
    traj_id: str,
    event: dict[str, Any],
    alignment: dict[str, Any],
    node_id: str | None,
) -> dict[str, Any]:
    """压缩 bad event，只保留 Stage 6 需要的可见证据字段。"""
    alignment_score = clamp01(alignment.get("confidence") if "confidence" in alignment else alignment.get("alignment_score"))
    skill_usage = [item for item in event.get("skill_usage") or [] if isinstance(item, dict)]
    suspected_skill_ids = _as_list(event.get("suspected_skill_ids"))
    if not suspected_skill_ids:
        suspected_skill_ids = stable_unique([item.get("skill_id") for item in skill_usage if item.get("skill_id")])
    return {
        "event_id": event.get("event_id"),
        "traj_id": traj_id,
        "step_id": event.get("step_id"),
        "step_ids": event.get("step_ids") or ([event.get("step_id")] if event.get("step_id") is not None else []),
        "event_kind": alignment.get("event_kind") or ("cause" if event.get("role") else "failure"),
        "aligned_node_id": node_id,
        "alignment_score": alignment_score,
        "alignment_reason": alignment.get("reason"),
        "intent": event.get("intent"),
        "observed_behavior": event.get("observed") or event.get("observed_behavior"),
        "expected_behavior_from_task_and_skills": event.get("expected_behavior_from_task_and_skills")
        or event.get("expected") or event.get("expected_behavior"),
        "downstream_consequence": event.get("consequence") or event.get("downstream_consequence"),
        "suspected_skill_ids": suspected_skill_ids,
        "skill_usage": skill_usage,
        "severity": event.get("severity"),
        "first_actionable_fault_candidate": bool(
            event.get("first_actionable")
            if "first_actionable" in event
            else event.get("first_actionable_fault_candidate")
        ),
        "evidence_span": event.get("evidence") or event.get("evidence_span"),
        "evidence_refs": event.get("evidence_refs") or [],
        "causal_role": event.get("role"),
    }


def _status_value(raw: Any) -> str:
    """兼容 string 或 dict 形式的 capability_node_status。"""
    if isinstance(raw, dict):
        raw = raw.get("status")
    text = str(raw or "unknown").strip().lower()
    if text == "bad":
        return "fail"
    if text == "skipped":
        return "miss"
    return text if text in STATUS_VALUES else "unknown"


def _status_reason(raw: Any) -> str | None:
    """读取 capability_node_status 的 reason，供 Web 和 Stage 7 审阅。"""
    if isinstance(raw, dict):
        reason = raw.get("reason") or (raw.get("status_calculation") or {}).get("rationale")
        return str(reason) if reason not in (None, "") else None
    return None


def _node_status_analysis(stage5: list[dict[str, Any]], node_id: str) -> dict[str, Any]:
    """计算节点的直接失败、尝试成功和阻塞分布。

    blocked 与 unknown 都不进入本节点的成功/失败分母。miss 是节点前置条件已满足
    却没有出现，因此计入直接失败，但不伪装成一次执行尝试。
    """
    counts = {status: 0 for status in STATUS_VALUES}
    status_by_trace: list[dict[str, Any]] = []

    for trace in stage5:
        if not isinstance(trace, dict):
            continue
        traj_id = str(trace.get("traj_id") or "").strip()
        assessment = next(
            (item for item in trace.get("node_assessments") or [] if str(item.get("node_id") or "") == node_id),
            None,
        )
        raw = assessment or {}
        status = _status_value(raw)
        counts[status] += 1
        status_by_trace.append(
            {
                "traj_id": traj_id,
                "status": status,
                "reason": _status_reason(raw),
                "task_success": trace.get("success"),
            }
        )

    total = len(stage5)
    attempted = counts["pass"] + counts["fail"]
    direct_failures = sum(counts[status] for status in DIRECT_FAILURE_STATUSES)
    attempted_success_rate = round(counts["pass"] / attempted, 4) if attempted else None
    direct_failure_rate = round(direct_failures / total, 4) if total else 0.0
    blocked_rate = round(counts["blocked"] / total, 4) if total else 0.0

    return {
        "total_traces": total,
        "status_counts": counts,
        "attempted_count": attempted,
        "direct_failure_count": direct_failures,
        "attempted_success_rate": attempted_success_rate,
        "direct_failure_rate": direct_failure_rate,
        "blocked_rate": blocked_rate,
        "status_by_trace": status_by_trace,
        "formula": {
            "attempted_success_rate": "pass_count / (pass_count + fail_count)",
            "direct_failure_rate": "(fail_count + miss_count) / total_traces",
            "blocked_rate": "blocked_count / total_traces",
        },
    }


def _partition_events_by_node_status(
    node_events: list[dict[str, Any]],
    status_analysis: dict[str, Any],
) -> dict[str, Any]:
    """按逐轨迹 node status 分离可触发修复的事件与仅供解释的事件。

    Stage 4 只负责事件到能力节点的语义对齐，因此一个事件可以对齐到因上游失败而
    blocked 的下游节点。若在 Stage 6 不再次结合逐轨迹状态门控，这类传播事件会
    错误抬高 bad-event/fatal 计数。这里把 fail/miss 视为节点直接责任，其余状态
    的事件保留为可审计上下文，但不参与任何自动修复阈值或证据权重。
    """
    status_by_trace = {
        str(item.get("traj_id") or ""): _status_value(item.get("status"))
        for item in status_analysis.get("status_by_trace") or []
        if isinstance(item, dict)
    }
    direct: list[dict[str, Any]] = []
    context: list[dict[str, Any]] = []
    context_counts = {status: 0 for status in STATUS_VALUES}
    for event in node_events:
        traj_id = str(event.get("traj_id") or "")
        status = status_by_trace.get(traj_id, "unknown")
        enriched = {**event, "node_status_for_trace": status}
        if status in DIRECT_FAILURE_STATUSES:
            direct.append(enriched)
        else:
            context.append(enriched)
            context_counts[status] = context_counts.get(status, 0) + 1
    return {
        "all_event_count": len(node_events),
        "direct_failure_events": direct,
        "direct_failure_event_count": len(direct),
        "context_events": context,
        "context_event_count": len(context),
        "context_event_counts_by_status": context_counts,
    }


def _brief_coverage_row(row: dict[str, Any]) -> dict[str, Any]:
    """抽取单个 node-skill coverage row 中 Stage 6 真正需要的字段。"""
    dimension_scores = {key: clamp01(row.get(key)) for key in DIMENSION_FIELDS}
    return {
        "skill_id": row.get("skill_id"),
        "skill_title": row.get("skill_title"),
        "directly_relevant": is_directly_relevant(row.get("directly_relevant")),
        "direct_relevance_rationale": row.get("direct_relevance_rationale"),
        "overall_coverage": clamp01(row.get("overall_coverage")),
        "coverage_gap": clamp01(row.get("coverage_gap")),
        "coverage_labels": _as_list(row.get("coverage_labels")),
        "missing_slots": _as_list(row.get("missing_slots")),
        "dimension_scores": dimension_scores,
        "low_score_dimensions": _low_score_dimensions(row),
        "evidence": row.get("evidence"),
    }


def _node_coverage_summary(node_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总一个 node 的 skill coverage。

    Stage 2 已经进行 node-skill pair 逐对分析；这里不会重新判断“一个 node 和多少
    skill 相关”，只读取 directly_relevant=true 的 pair 作为可修复 skill 候选。
    """
    relevant = [_brief_coverage_row(row) for row in rows if is_directly_relevant(row.get("directly_relevant"))]
    relevant.sort(key=lambda item: item.get("overall_coverage") if item.get("overall_coverage") is not None else -1, reverse=True)

    valid_coverages = [row["overall_coverage"] for row in relevant if row.get("overall_coverage") is not None]
    best = max(valid_coverages) if valid_coverages else None
    best_skill_ids = [
        row.get("skill_id")
        for row in relevant
        if best is not None and row.get("overall_coverage") == best and row.get("skill_id")
    ]

    return {
        "node_id": node_id,
        "total_skill_pair_rows": len(rows),
        "directly_relevant_skill_count": len(relevant),
        "best_overall_coverage": best,
        "node_gap": round(1 - best, 4) if best is not None else 1.0,
        "best_skill_ids": best_skill_ids,
        "directly_relevant_rows": relevant,
        "not_relevant_skill_count": len(rows) - len(relevant),
    }


def _low_score_dimensions(row: dict[str, Any]) -> list[dict[str, Any]]:
    """按分数从低到高列出明显薄弱的 coverage 维度。"""
    dims: list[dict[str, Any]] = []
    for field in DIMENSION_FIELDS:
        score = clamp01(row.get(field))
        if score is None:
            continue
        threshold = 0.50
        if field == "verification_coverage":
            threshold = 0.35
        elif field == "recovery_coverage":
            threshold = 0.25
        elif field == "execution_support_coverage":
            need = str(row.get("execution_support_need") or "").strip().lower()
            if need != "required":
                continue
        if score < threshold:
            dims.append(
                {
                    "dimension": field,
                    "score": score,
                    "suggestion": DIMENSION_TO_REPAIR_NOTE.get(field),
                }
            )
    dims.sort(key=lambda item: (item["score"], item["dimension"]))
    return dims


def _classify_node(
    node_events: list[dict[str, Any]],
    status_analysis: dict[str, Any],
    coverage_summary: dict[str, Any],
    total_traces: int,
    event_partition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据直接失败与覆盖缺口把 node 分为无需修复、修复、新增或人工复核。"""
    event_count = len(node_events)
    affected_trace_count = len({str(event.get("traj_id") or "") for event in node_events if event.get("traj_id")})
    event_ratio = round(affected_trace_count / max(total_traces, 1), 4)
    attempted_success_rate = status_analysis.get("attempted_success_rate")
    attempted_count = int(status_analysis.get("attempted_count") or 0)
    direct_failure_count = int(status_analysis.get("direct_failure_count") or 0)
    fatal_count = sum(1 for event in node_events if str(event.get("severity") or "").lower() == "fatal")
    direct_failure_rate = status_analysis.get("direct_failure_rate") or 0.0
    minimum_repeated_trace_count = 1

    reasons: list[str] = []
    if event_count >= BAD_EVENT_COUNT_THRESHOLD and affected_trace_count >= minimum_repeated_trace_count:
        reasons.append(
            f"direct_bad_event_count {event_count} across {affected_trace_count} direct-failure traces"
        )
    if affected_trace_count >= minimum_repeated_trace_count and event_ratio >= BAD_EVENT_RATIO_THRESHOLD:
        reasons.append(f"direct_bad_event_ratio {event_ratio} >= {BAD_EVENT_RATIO_THRESHOLD}")
    if (
        direct_failure_count >= minimum_repeated_trace_count
        and attempted_count >= minimum_repeated_trace_count
        and attempted_success_rate is not None
        and attempted_success_rate < SUCCESS_RATE_THRESHOLD
    ):
        reasons.append(f"attempted_success_rate {attempted_success_rate} < {SUCCESS_RATE_THRESHOLD}")
    if fatal_count and affected_trace_count >= minimum_repeated_trace_count:
        reasons.append(f"fatal_direct_bad_event_count {fatal_count} > 0 with repeated direct failure")
    if direct_failure_count >= minimum_repeated_trace_count and direct_failure_rate >= BAD_EVENT_RATIO_THRESHOLD:
        reasons.append(f"direct_failure_rate {direct_failure_rate} >= {BAD_EVENT_RATIO_THRESHOLD}")

    needs_repair = bool(reasons)
    covered = (coverage_summary.get("directly_relevant_skill_count") or 0) > 0
    coverage_analysis_available = int(coverage_summary.get("total_skill_pair_rows") or 0) > 0
    best_coverage = coverage_summary.get("best_overall_coverage")
    adequately_covered = (
        covered
        and isinstance(best_coverage, (int, float))
        and float(best_coverage) >= ADEQUATE_COVERAGE_THRESHOLD
    )
    if not needs_repair:
        category = "no_repair_needed"
    elif covered:
        category = "needs_skill_repair"
    else:
        category = "needs_new_skill"

    return {
        "needs_repair": needs_repair,
        "category": category,
        "reasons": reasons or ["No threshold was triggered."],
        "bad_event_count": event_count,
        "direct_bad_event_count": event_count,
        "affected_trace_count": affected_trace_count,
        "bad_event_ratio": event_ratio,
        "attempted_success_rate": attempted_success_rate,
        "direct_failure_rate": direct_failure_rate,
        "blocked_rate": status_analysis.get("blocked_rate") or 0.0,
        "fatal_bad_event_count": fatal_count,
        "covered_by_existing_skill": covered,
        "coverage_analysis_available": coverage_analysis_available,
        "adequately_covered": adequately_covered,
        "requires_semantic_confirmation": adequately_covered,
        "best_overall_coverage": best_coverage,
        "ignored_context_event_count": int((event_partition or {}).get("context_event_count") or 0),
        "ignored_context_event_counts_by_status": (event_partition or {}).get("context_event_counts_by_status") or {},
        "thresholds": {
            "bad_event_count_threshold": BAD_EVENT_COUNT_THRESHOLD,
            "bad_event_ratio_threshold": BAD_EVENT_RATIO_THRESHOLD,
            "attempted_success_rate_threshold": SUCCESS_RATE_THRESHOLD,
            "adequate_coverage_threshold": ADEQUATE_COVERAGE_THRESHOLD,
            "minimum_repeated_trace_count": minimum_repeated_trace_count,
        },
    }


def _recommended_action(classification: dict[str, Any]) -> str:
    """把 node 分类映射成 Stage 6 的建议动作。"""
    category = classification.get("category")
    if category == "needs_skill_repair":
        return "revise_existing_skill"
    if category == "needs_new_skill":
        return "add_new_skill"
    return "monitor"


def _skill_title_map(skill_standardizations: list[dict[str, Any]], bundle: dict[str, Any]) -> dict[str, str]:
    """从标准化 skill 和原始 skill library 中尽量恢复 skill title。"""
    titles: dict[str, str] = {}
    for item in skill_standardizations or []:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or item.get("id") or "").strip()
        title = str(item.get("title") or item.get("name") or skill_id).strip()
        if skill_id:
            titles[skill_id] = title
    for item in bundle.get("skill_library") or []:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or item.get("id") or "").strip()
        title = str(item.get("title") or item.get("path") or skill_id).strip()
        if skill_id and skill_id not in titles:
            titles[skill_id] = title
    return titles


def _severity_score(event: dict[str, Any]) -> float:
    """把 bad event severity 映射成 [0, 1]。"""
    return SEVERITY_SCORE.get(str(event.get("severity") or "").lower(), 0.50)


def _node_pressure(node_record: dict[str, Any], total_traces: int) -> float:
    """计算 node 修复压力；blocked 不产生本节点压力。"""
    status = node_record.get("execution_success_analysis", {})
    attempted_success_rate = status.get("attempted_success_rate")
    attempted_failure_pressure = 1 - attempted_success_rate if attempted_success_rate is not None else 0.0
    direct_failure_rate = status.get("direct_failure_rate") or 0.0
    affected_traces = {
        str(event.get("traj_id") or "")
        for event in node_record.get("bad_event_list") or []
        if event.get("traj_id")
    }
    event_ratio = len(affected_traces) / max(total_traces, 1)
    return round(max(attempted_failure_pressure, direct_failure_rate, min(1.0, event_ratio)), 4)


def _label_signal(row: dict[str, Any]) -> float:
    """coverage_labels 越多，表示该 node-skill pair 的结构性缺口越多。"""
    labels = [label for label in _as_list(row.get("coverage_labels")) if label and label != "not_relevant"]
    return round(min(1.0, len(labels) / 3), 4)


def _coverage_gap(row: dict[str, Any]) -> float:
    """读取 coverage_gap；缺失时用 overall_coverage 反推。"""
    gap = clamp01(row.get("coverage_gap"))
    if gap is not None:
        return gap
    overall = clamp01(row.get("overall_coverage"))
    if overall is not None:
        return round(1 - overall, 4)
    return 0.50


def _event_weight_for_skill(event: dict[str, Any], row: dict[str, Any], node_pressure: float) -> dict[str, Any]:
    """计算一个 bad event 支持某个 existing-skill 修复建议的权重。

    权重包含 node-skill 分数信息：coverage_gap 和 coverage_labels 会直接影响分数。
    若 event 自己也怀疑该 skill，则额外给一个轻量 usage bonus。
    """
    coverage_gap = _coverage_gap(row)
    severity = _severity_score(event)
    alignment = clamp01(event.get("alignment_score")) or 0.50
    first_fault = 1.0 if event.get("first_actionable_fault_candidate") else 0.0
    label_signal = _label_signal(row)
    skill_id = str(row.get("skill_id") or "")
    suspected = skill_id in {str(item) for item in _as_list(event.get("suspected_skill_ids"))}
    usage_signal = 1.0 if suspected else 0.0

    weight = (
        0.25 * coverage_gap
        + 0.20 * severity
        + 0.20 * node_pressure
        + 0.15 * alignment
        + 0.10 * first_fault
        + 0.05 * label_signal
        + 0.05 * usage_signal
    )
    return {
        "weight": round(max(0.0, min(1.0, weight)), 4),
        "weight_basis": {
            "formula": "0.25*coverage_gap + 0.20*severity + 0.20*node_pressure + 0.15*alignment + 0.10*first_fault + 0.05*label_signal + 0.05*usage_signal",
            "coverage_gap": coverage_gap,
            "severity": severity,
            "node_pressure": node_pressure,
            "alignment": alignment,
            "first_fault": first_fault,
            "label_signal": label_signal,
            "usage_signal": usage_signal,
        },
    }


def _event_weight_for_new_skill(event: dict[str, Any], node_pressure: float) -> dict[str, Any]:
    """计算一个 bad event 支持新增 skill 的权重。

    新增 skill 没有 node-skill coverage row，因此覆盖缺失固定为 1.0，并更多依赖
    node pressure、事件严重性和 event-to-node alignment。
    """
    coverage_absence = 1.0
    severity = _severity_score(event)
    alignment = clamp01(event.get("alignment_score")) or 0.50
    first_fault = 1.0 if event.get("first_actionable_fault_candidate") else 0.0
    weight = (
        0.30 * coverage_absence
        + 0.25 * severity
        + 0.20 * node_pressure
        + 0.15 * alignment
        + 0.10 * first_fault
    )
    return {
        "weight": round(max(0.0, min(1.0, weight)), 4),
        "weight_basis": {
            "formula": "0.30*coverage_absence + 0.25*severity + 0.20*node_pressure + 0.15*alignment + 0.10*first_fault",
            "coverage_absence": coverage_absence,
            "severity": severity,
            "node_pressure": node_pressure,
            "alignment": alignment,
            "first_fault": first_fault,
        },
    }


def _event_evidence_item(event: dict[str, Any], weight_info: dict[str, Any]) -> dict[str, Any]:
    """把 bad event 和权重组合成给 Stage 6 使用的证据项。"""
    return {
        "event_id": event.get("event_id"),
        "traj_id": event.get("traj_id"),
        "step_id": event.get("step_id"),
        "severity": event.get("severity"),
        "first_actionable_fault_candidate": event.get("first_actionable_fault_candidate"),
        "observed_behavior": event.get("observed_behavior"),
        "expected_behavior_from_task_and_skills": event.get("expected_behavior_from_task_and_skills"),
        "downstream_consequence": event.get("downstream_consequence"),
        "evidence_span": event.get("evidence_span"),
        "weight": weight_info["weight"],
        "weight_basis": weight_info["weight_basis"],
    }


def _repair_target_summary(node_record: dict[str, Any], row: dict[str, Any]) -> str:
    """根据 coverage_labels、missing_slots 和低分维度生成确定性的修复摘要。"""
    notes: list[str] = []
    slots = {str(slot) for slot in _as_list(row.get("missing_slots"))}
    labels = {str(label) for label in _as_list(row.get("coverage_labels"))}
    dimensions = {item["dimension"] for item in row.get("low_score_dimensions") or []}

    if "trigger" in slots or "trigger_coverage" in dimensions:
        notes.append(DIMENSION_TO_REPAIR_NOTE["trigger_coverage"])
    if "procedure" in slots or "procedure_coverage" in dimensions or "under_specified" in labels:
        notes.append(DIMENSION_TO_REPAIR_NOTE["procedure_coverage"])
    if "node_requirement_fit" in slots or "node_requirement_fit" in dimensions:
        notes.append(DIMENSION_TO_REPAIR_NOTE["node_requirement_fit"])
    if "verification" in slots or "verification_coverage" in dimensions or "missing_verification" in labels:
        notes.append(DIMENSION_TO_REPAIR_NOTE["verification_coverage"])
    if "recovery" in slots or "recovery_coverage" in dimensions or "missing_recovery" in labels:
        notes.append(DIMENSION_TO_REPAIR_NOTE["recovery_coverage"])
    if "execution_support" in slots or "execution_support_coverage" in dimensions or "missing_execution_support" in labels:
        notes.append(DIMENSION_TO_REPAIR_NOTE["execution_support_coverage"])

    if not notes:
        notes.append("结合该 node 的 bad events 检查 skill 是否需要更强的显式执行约束或边界条件。")
    return " ".join(stable_unique(notes))


def _accumulate_skill_repairs(
    accumulator: dict[str, dict[str, Any]],
    *,
    node: dict[str, Any],
    node_record: dict[str, Any],
    skill_titles: dict[str, str],
    total_traces: int,
) -> None:
    """把一个需修复 node 的证据分配到每个 directly_relevant skill。"""
    node_pressure = _node_pressure(node_record, total_traces)
    events = node_record.get("bad_event_list") or []
    for row in node_record.get("skill_coverage", {}).get("directly_relevant_rows") or []:
        skill_id = str(row.get("skill_id") or "").strip()
        if not skill_id:
            continue
        skill_record = accumulator.setdefault(
            skill_id,
            {
                "skill_id": skill_id,
                "skill_title": skill_titles.get(skill_id) or row.get("skill_title") or skill_id,
                "action": "revise_existing_skill",
                "affected_node_ids": [],
                "repair_targets": [],
                "priority_score": 0.0,
            },
        )
        if node_record["node_id"] not in skill_record["affected_node_ids"]:
            skill_record["affected_node_ids"].append(node_record["node_id"])

        evidence_items = [
            _event_evidence_item(event, _event_weight_for_skill(event, row, node_pressure))
            for event in events
        ]
        evidence_items.append(_status_evidence_item(node_record, node_pressure, row=row))
        evidence_items.sort(key=lambda item: item.get("weight") or 0.0, reverse=True)
        target_priority = _average_weight(evidence_items, fallback=node_pressure * _coverage_gap(row))
        skill_record["repair_targets"].append(
            {
                "node_id": node_record["node_id"],
                "node_goal": node_record["node_goal"],
                "node_definition": _node_definition(node),
                "node_pressure": node_pressure,
                "coverage_row": row,
                "repair_dimensions": row.get("low_score_dimensions") or [],
                "repair_basis_summary": _repair_target_summary(node_record, row),
                "classification_reasons": node_record.get("classification", {}).get("reasons") or [],
                "evidence_items": evidence_items,
                "priority_score": target_priority,
            }
        )
        skill_record["priority_score"] = max(skill_record["priority_score"], target_priority)


def _finalize_skill_repairs(accumulator: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """对 skill 级建议排序并补充聚合摘要。"""
    out: list[dict[str, Any]] = []
    for record in accumulator.values():
        record["affected_node_ids"] = stable_unique(record.get("affected_node_ids") or [])
        record["repair_targets"].sort(key=lambda item: item.get("priority_score") or 0.0, reverse=True)
        record["priority_score"] = round(record.get("priority_score") or 0.0, 4)
        record["recommendation_summary"] = _skill_repair_summary(record)
        out.append(record)
    out.sort(key=lambda item: item.get("priority_score") or 0.0, reverse=True)
    return out


def _skill_repair_summary(record: dict[str, Any]) -> str:
    """生成面向 Stage 6 的 existing-skill 修复摘要。"""
    target_bits: list[str] = []
    for target in record.get("repair_targets") or []:
        labels = target.get("coverage_row", {}).get("coverage_labels") or []
        slots = target.get("coverage_row", {}).get("missing_slots") or []
        target_bits.append(
            f"{target.get('node_id')} labels={','.join(map(str, labels)) or '-'} missing={','.join(map(str, slots)) or '-'}"
        )
    return (
        f"Revise {record.get('skill_id')} for nodes {', '.join(record.get('affected_node_ids') or [])}. "
        f"Primary local bases: {'; '.join(target_bits)}."
    )


def _new_skill_id_suggestion(node: dict[str, Any]) -> str:
    """根据 node goal 生成稳定、可读的新 skill id 建议。"""
    text = _node_goal(node).lower()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        words = [str(node.get("node_id") or "node").lower()]
    return "skill_for_" + "_".join(words[:6])


def _new_skill_recommendation(
    *,
    node: dict[str, Any],
    node_record: dict[str, Any],
    total_traces: int,
) -> dict[str, Any]:
    """为完全无覆盖的需修复 node 生成新增 skill 建议。"""
    node_pressure = _node_pressure(node_record, total_traces)
    events = node_record.get("bad_event_list") or []
    evidence_items = [
        _event_evidence_item(event, _event_weight_for_new_skill(event, node_pressure))
        for event in events
    ]
    evidence_items.append(_status_evidence_item(node_record, node_pressure, row=None))
    evidence_items.sort(key=lambda item: item.get("weight") or 0.0, reverse=True)
    priority = _average_weight(evidence_items, fallback=node_pressure)
    node_definition = _node_definition(node)
    return {
        "action": "add_new_skill",
        "node_id": node_record["node_id"],
        "node_goal": node_record["node_goal"],
        "new_skill_id_suggestion": _new_skill_id_suggestion(node),
        "definition_basis": {
            "basis_type": "node_definition_plus_bad_events",
            "node_definition": node_definition,
            "classification_reasons": node_record.get("classification", {}).get("reasons") or [],
            "required_skill_capability": _new_skill_capability_summary(node_definition),
        },
        "evidence_items": evidence_items,
        "priority_score": priority,
        "recommendation_summary": (
            f"Add a new skill for {node_record['node_id']} because the node needs repair "
            "and Stage 2 found no directly_relevant skill coverage."
        ),
    }


def _new_skill_capability_summary(node_definition: dict[str, Any]) -> str:
    """把 node 定义压成新增 skill 应覆盖的能力摘要。"""
    operations = "; ".join(map(str, node_definition.get("required_operations") or []))
    checks = "; ".join(map(str, node_definition.get("required_checks") or []))
    return f"Procedure should cover: {operations or 'the node required operations'}. Verification should cover: {checks or 'the node required checks'}."


def _status_evidence_item(node_record: dict[str, Any], node_pressure: float, row: dict[str, Any] | None) -> dict[str, Any]:
    """把 capability_node_status 聚合成一条带权证据。

    bad event 是最直接的行为证据，但有些 Stage 2 输出会把失败压到
    ``capability_node_status`` 中，而没有抽成单独 bad event。为了避免这类 node 的
    修复建议缺证据，这里把成功率、负面状态率和 coverage 缺口转成一条状态证据。
    """
    status = node_record.get("execution_success_analysis") or {}
    direct_failure_rate = status.get("direct_failure_rate") or 0.0
    if row is None:
        coverage_factor = 1.0
        formula = "0.45*node_pressure + 0.35*coverage_absence + 0.20*direct_failure_rate"
        weight = 0.45 * node_pressure + 0.35 * coverage_factor + 0.20 * direct_failure_rate
        basis_name = "coverage_absence"
    else:
        coverage_factor = _coverage_gap(row)
        formula = "0.45*node_pressure + 0.35*coverage_gap + 0.20*direct_failure_rate"
        weight = 0.45 * node_pressure + 0.35 * coverage_factor + 0.20 * direct_failure_rate
        basis_name = "coverage_gap"
    return {
        "evidence_type": "node_status_analysis",
        "node_id": node_record.get("node_id"),
        "status_counts": status.get("status_counts") or {},
        "attempted_success_rate": status.get("attempted_success_rate"),
        "direct_failure_rate": direct_failure_rate,
        "blocked_rate": status.get("blocked_rate"),
        "status_by_trace": status.get("status_by_trace") or [],
        "weight": round(max(0.0, min(1.0, weight)), 4),
        "weight_basis": {
            "formula": formula,
            "node_pressure": node_pressure,
            basis_name: coverage_factor,
            "direct_failure_rate": direct_failure_rate,
        },
    }


def _average_weight(items: list[dict[str, Any]], fallback: float = 0.0) -> float:
    """计算证据平均权重，缺证据时使用 fallback。"""
    weights = [item.get("weight") for item in items if isinstance(item.get("weight"), (int, float))]
    if not weights:
        return round(max(0.0, min(1.0, fallback)), 4)
    return round(sum(weights) / len(weights), 4)
