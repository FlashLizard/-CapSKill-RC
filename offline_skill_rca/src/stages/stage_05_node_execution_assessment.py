"""Stage 5：逐轨迹判断能力节点执行事实，并由本地公式计算状态。

Repair LLM 只回答四个可审计问题：能力是否出现、是否完整成功、前置条件是否
满足、现有证据是否足以判断。最终 ``pass/fail/miss/blocked/unknown`` 不由 LLM
直接输出，而由本模块的确定性公式计算。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .common import json_block, make_llm, render_prompt_template, stage_system_prompt, write_prompt_file

STAGE_NAME = "stage-05-node-execution-assessment"
PRESENCE_VALUES = {"none", "partial", "full", "unknown"}
STATUS_VALUES = {"pass", "fail", "miss", "blocked", "unknown"}
AUDIT_STATUS_VALUES = {"satisfied", "violated", "unverified"}


def assessment_schema() -> dict[str, Any]:
    """返回 LLM 响应契约；有意不包含最终 status。"""
    evidence = [{"source": "trajectory_step|failure_result|final_artifact|stage3_event|stage4_alignment", "ref": "string", "excerpt": "string"}]
    return {
        "traj_id": "string",
        "node_assessments": [
            {
                "node_id": "N1",
                "capability_presence": {"value": "none|partial|full|unknown", "reason": "string", "evidence_refs": evidence},
                "fully_successful": {"value": "true|false|null", "reason": "string", "evidence_refs": evidence},
                "prerequisites_satisfied": {"value": "true|false|null", "reason": "string", "evidence_refs": evidence},
                "success_judgeable": {"value": "true|false", "reason": "string", "evidence_refs": evidence},
                "requirement_audit": [
                    {
                        "kind": "operation|check",
                        "requirement": "exact string copied from the capability node",
                        "status": "satisfied|violated|unverified",
                        "reason": "string",
                        "evidence_refs": evidence,
                    }
                ],
            }
        ],
        "evidence_limits": [],
    }


def stage_name(index: int, trajectory: dict[str, Any]) -> str:
    """生成逐轨迹 prompt/transcript 名称。"""
    from ..pipeline import sanitize

    return f"stage-05-traj-{index + 1:02d}-{sanitize(str(trajectory.get('traj_id') or index + 1))}"


def _trace_analysis(stage3: list[dict[str, Any]], traj_id: Any) -> dict[str, Any]:
    """按 traj_id 取 Stage 3 输出。"""
    wanted = str(traj_id or "")
    return next((item for item in stage3 if str(item.get("traj_id") or "") == wanted), {})


def _trace_alignments(stage4: dict[str, Any], traj_id: Any) -> list[dict[str, Any]]:
    """只保留当前轨迹的 Stage 4 对齐结果。"""
    wanted = str(traj_id or "")
    return [row for row in stage4.get("alignments") or [] if str(row.get("traj_id") or "") == wanted]


def build_prompt(
    stage2: dict[str, Any],
    trajectory: dict[str, Any],
    stage3_trace: dict[str, Any],
    stage4_alignments: list[dict[str, Any]],
    max_chars: int,
) -> str:
    """构造单条轨迹的节点执行事实判断 prompt。"""
    from ..io_utils import sanitize_agent_artifacts, sanitize_agent_only_visible_result
    from ..pipeline import fit_prompt

    instructions = render_prompt_template(
        "stage-05-node-execution-assessment.txt",
        {"node_execution_assessment_schema": json_block(assessment_schema())},
    )
    # Stage 5 需要查看轨迹事实，但仍执行与 Stage 3 相同的 agent-only 边界。
    safe_trajectory = {
        key: trajectory.get(key)
        for key in ("traj_id", "task_id", "success", "step_formatting_provenance", "steps")
    }
    safe_trajectory["visible_failure_result"] = sanitize_agent_only_visible_result(
        trajectory.get("visible_failure_result") or {"success": trajectory.get("success", 0)}
    )
    safe_trajectory["final_artifacts"] = sanitize_agent_artifacts(trajectory.get("final_artifacts"))
    payload = {
        "stage_02_capability_graph": stage2.get("capability_graph") or stage2,
        "trajectory": safe_trajectory,
        "stage_03_failure_causality": stage3_trace,
        "stage_04_event_node_alignments": stage4_alignments,
    }
    stage_limit = int(os.getenv("OFFLINE_SKILL_RCA_STAGE5_MAX_PROMPT_CHARS") or max_chars)
    return fit_prompt(instructions, payload, stage_limit)


def run_one(
    config: Any,
    stage2: dict[str, Any],
    stage3: list[dict[str, Any]],
    stage4: dict[str, Any],
    index: int,
    trajectory: dict[str, Any],
) -> dict[str, Any]:
    """调用一次 LLM，并立即执行本地状态计算。"""
    name = stage_name(index, trajectory)
    trace_analysis = _trace_analysis(stage3, trajectory.get("traj_id"))
    alignments = _trace_alignments(stage4, trajectory.get("traj_id"))
    prompt = build_prompt(stage2, trajectory, trace_analysis, alignments, config.max_prompt_chars)
    write_prompt_file(config, name, prompt)
    max_tokens = int(os.getenv("OFFLINE_SKILL_RCA_STAGE5_MAX_TOKENS") or 12_000)
    raw = make_llm(config, name).chat_json(stage_system_prompt(STAGE_NAME), prompt, max_tokens=max_tokens)
    return postprocess_trace_assessment(raw, trajectory, stage2)


def run(
    config: Any,
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    stage3: list[dict[str, Any]],
    stage4: dict[str, Any],
) -> list[dict[str, Any]]:
    """并行判断所有轨迹的节点执行事实。"""
    trajectories = list(bundle.get("failed_trajectories") or [])
    if not trajectories:
        return []
    workers = max(1, min(int(config.trace_analysis_workers or 1), len(trajectories)))
    results: list[dict[str, Any] | None] = [None] * len(trajectories)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_one, config, stage2, stage3, stage4, index, trajectory): index
            for index, trajectory in enumerate(trajectories)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [item for item in results if item is not None]


def postprocess_trace_assessment(
    raw: dict[str, Any], trajectory: dict[str, Any], stage2: dict[str, Any]
) -> dict[str, Any]:
    """规范 LLM 判断并为能力图中的每个节点计算 status。"""
    graph_nodes = [node for node in _graph_nodes(stage2) if node.get("node_id")]
    by_node = {
        str(item.get("node_id")): item
        for item in raw.get("node_assessments") or []
        if isinstance(item, dict) and item.get("node_id")
    }
    assessments = []
    for node in graph_nodes:
        node_id = str(node.get("node_id"))
        normalized = normalize_assessment(node_id, by_node.get(node_id) or {}, node)
        status, rationale, warnings = calculate_node_status(normalized)
        normalized["status"] = status
        normalized["status_calculation"] = {
            "method": "deterministic_v3_strict_pass",
            "rationale": rationale,
            "warnings": warnings,
        }
        assessments.append(normalized)
    return {
        "traj_id": trajectory.get("traj_id"),
        "success": 1 if int(trajectory.get("success") or 0) else 0,
        "node_assessments": assessments,
        "evidence_limits": raw.get("evidence_limits") or [],
        "status_formula_version": "deterministic_v3_strict_pass",
    }


def normalize_assessment(
    node_id: str,
    item: dict[str, Any],
    node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把四个判断统一为稳定的 value/reason/evidence_refs 结构。"""
    return {
        "node_id": node_id,
        "capability_presence": _judgment(item.get("capability_presence"), "unknown", PRESENCE_VALUES),
        "fully_successful": _nullable_bool_judgment(item.get("fully_successful")),
        "prerequisites_satisfied": _nullable_bool_judgment(item.get("prerequisites_satisfied")),
        "success_judgeable": _bool_judgment(item.get("success_judgeable"), False),
        "requirement_audit": _normalize_requirement_audit(item.get("requirement_audit"), node),
    }


def calculate_node_status(item: dict[str, Any]) -> tuple[str, str, list[str]]:
    """根据四个中间判断计算节点状态。

    优先级体现了语义边界：前置条件未满足且节点未完整执行为 blocked；证据不足
    为 unknown；可执行却完全未出现为 miss；完整且成功为 pass；已出现但未完整
    成功为 fail。blocked 不属于节点直接失败。
    """
    presence = item["capability_presence"]["value"]
    successful = item["fully_successful"]["value"]
    prerequisites = item["prerequisites_satisfied"]["value"]
    judgeable = item["success_judgeable"]["value"]
    warnings: list[str] = []
    audit = item.get("requirement_audit") or []
    violated_requirements = [entry for entry in audit if entry.get("status") == "violated"]
    unsupported_requirements = [
        entry
        for entry in audit
        if entry.get("status") != "satisfied" or not entry.get("evidence_refs")
    ]
    if successful is True and presence != "full":
        warnings.append("fully_successful=true is inconsistent with capability_presence other than full")
        return "unknown", "Conflicting intermediate judgments prevent a reliable status.", warnings
    if successful is True and prerequisites is False:
        warnings.append("fully_successful=true is inconsistent with prerequisites_satisfied=false")
        return "unknown", "Conflicting intermediate judgments prevent a reliable status.", warnings
    if prerequisites is False and presence != "full" and successful is not True:
        return "blocked", "Prerequisites were not satisfied and the capability was not fully executed.", warnings
    if violated_requirements and presence in {"partial", "full"}:
        if successful is True:
            warnings.append("fully_successful=true conflicts with a violated node operation or check")
        return "fail", "At least one required node operation or check was visibly violated.", warnings
    if judgeable is not True:
        return "unknown", "The supplied evidence is insufficient to judge execution success.", warnings
    if presence == "none":
        if prerequisites is True:
            return "miss", "The capability was executable but did not appear in the trajectory.", warnings
        return "unknown", "The capability did not appear, but its prerequisites were not established.", warnings
    if presence == "full" and successful is True:
        if unsupported_requirements:
            warnings.append("pass requires satisfied evidence for every node operation and check")
            return "unknown", "At least one required node operation or check lacks pass-grade evidence.", warnings
        return "pass", "The capability fully appeared and completed successfully.", warnings
    if presence in {"partial", "full"} and successful is False:
        return "fail", "The capability appeared but was not fully successful.", warnings
    return "unknown", "The intermediate judgments do not determine a consistent status.", warnings


def _judgment(value: Any, default: str, allowed: set[str]) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {"value": value}
    normalized = str(raw.get("value") or default).strip().lower()
    if normalized not in allowed:
        normalized = default
    return {"value": normalized, "reason": str(raw.get("reason") or ""), "evidence_refs": raw.get("evidence_refs") or []}


def _coerce_nullable_bool(value: Any) -> bool | None:
    if value is None or str(value).strip().lower() in {"", "null", "none", "unknown"}:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _nullable_bool_judgment(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {"value": value}
    return {
        "value": _coerce_nullable_bool(raw.get("value")),
        "reason": str(raw.get("reason") or ""),
        "evidence_refs": raw.get("evidence_refs") or [],
    }


def _bool_judgment(value: Any, default: bool) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {"value": value}
    parsed = _coerce_nullable_bool(raw.get("value"))
    return {
        "value": default if parsed is None else parsed,
        "reason": str(raw.get("reason") or ""),
        "evidence_refs": raw.get("evidence_refs") or [],
    }


def _normalize_requirement_audit(
    value: Any,
    node: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """按能力节点原文重组逐操作/逐检查审计。

    LLM 必须逐字返回每个 operation/check。缺项或无法匹配的项会被本地代码标为
    ``unverified``，从而不能支持 ``pass``。这不会在本地推断语义正确性，只负责
    防止响应漏审节点契约。
    """
    raw_rows = [row for row in (value or []) if isinstance(row, dict)]
    by_requirement = {
        (str(row.get("kind") or "").strip().lower(), str(row.get("requirement") or "").strip()): row
        for row in raw_rows
    }
    requirements: list[tuple[str, str]] = []
    if isinstance(node, dict):
        requirements.extend(("operation", str(text).strip()) for text in node.get("operations") or [] if str(text).strip())
        requirements.extend(("check", str(text).strip()) for text in node.get("checks") or [] if str(text).strip())
    elif raw_rows:
        requirements.extend(by_requirement)

    normalized = []
    for kind, requirement in requirements:
        raw = by_requirement.get((kind, requirement)) or {}
        status = str(raw.get("status") or "unverified").strip().lower()
        if status not in AUDIT_STATUS_VALUES:
            status = "unverified"
        normalized.append(
            {
                "kind": kind,
                "requirement": requirement,
                "status": status,
                "reason": str(raw.get("reason") or ""),
                "evidence_refs": raw.get("evidence_refs") or [],
            }
        )
    return normalized


def _graph_nodes(stage2: dict[str, Any]) -> list[dict[str, Any]]:
    graph = stage2.get("capability_graph") if isinstance(stage2.get("capability_graph"), dict) else stage2
    return [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
