"""Stage 7：在事务式修复前合并过多的 ``add_new_skill`` 操作。

本阶段把“是否需要合并”和“如何改写 action”分开处理：代码根据数量阈值决定
是否触发，repair LLM 只对新增建议做语义聚类并在每簇选择根建议。聚类通过本地
完整性校验后，代码保留每簇根建议的 ``add_new_skill``，并把其余成员转换成
指向根 skill 的 ``revise_existing_skill``。因此任何原建议都不会被 LLM 静默
删除，Stage 8 也能按根新增、后续增量修复的顺序逐 repair unit 审查。
"""
from __future__ import annotations

import math
from typing import Any

from .common import json_block, render_prompt_template, run_llm_stage

STAGE_NAME = "stage-07-repair-action-merge"
TEMPLATE = "stage-07-repair-action-merge.txt"


def merge_schema() -> dict[str, Any]:
    """返回 LLM 只负责聚类所需的最小 JSON 契约。"""
    return {
        "clusters": [
            {
                "root_suggestion_id": "one member suggestion id retained as add_new_skill",
                "member_suggestion_ids": ["every member id, including the root"],
                "unified_scope": "concise reusable scope shared by this cluster",
            }
        ]
    }


def _prepared_actions(stage6: dict[str, Any]) -> list[dict[str, Any]]:
    """复用 Stage 8 的建议展开逻辑，保证 Stage 6 输出能进入本阶段。"""
    from .stage_08_transactional_skill_repair import prepare_suggestions

    return prepare_suggestions(stage6)


def resolve_merge_config(config: Any, actions: list[dict[str, Any]]) -> dict[str, Any]:
    """解析固定/自动参数，并返回可记录、可预览的最终判定值。

    ``add_skill_merge_threshold`` 是软触发阈值：新增建议明显膨胀时会触发语义
    聚类。``max_new_skill_count`` 是硬上限：只要新增建议数超过该值，即使没有
    超过软阈值，也必须进入合并阶段。这样可以稳定保证 Stage 6 最终最多只创建
    指定数量的新 skill，其余相关新增建议会被转换成对根新 skill 的修复。
    """
    add_count = sum(1 for item in actions if item.get("action") == "add_new_skill")
    configured_threshold = int(getattr(config, "add_skill_merge_threshold", 0) or 0)
    threshold = configured_threshold or max(3, math.ceil(math.sqrt(max(1, len(actions)))))
    configured_target = int(getattr(config, "add_skill_target_count", 0) or 0)
    configured_max_new = int(getattr(config, "max_new_skill_count", 2) or 0)
    max_new_skill_count = max(0, configured_max_new)
    automatic_target = max(1, min(threshold, math.ceil(max(1, add_count) / 2)))
    target = configured_target or automatic_target
    if max_new_skill_count > 0:
        target = min(target, max_new_skill_count)
    target = max(1, min(target, max(1, add_count)))
    merge_required = add_count > 0 and target < add_count and (
        add_count > threshold or (max_new_skill_count > 0 and add_count > max_new_skill_count)
    )
    return {
        "total_repair_action_count": len(actions),
        "add_new_skill_count": add_count,
        "configured_merge_threshold": configured_threshold,
        "resolved_merge_threshold": threshold,
        "configured_target_cluster_count": configured_target,
        "configured_max_new_skill_count": max_new_skill_count,
        "resolved_target_cluster_count": target,
        "skill_word_limit": int(getattr(config, "skill_word_limit", 1200) or 1200),
        "merge_required": merge_required,
        "automatic_threshold_formula": "max(3, ceil(sqrt(total_repair_action_count)))",
        "automatic_target_formula": "min(resolved_merge_threshold, ceil(add_new_skill_count / 2), max_new_skill_count if enabled)",
        "hard_cap_rule": "merge_required when add_new_skill_count > configured_max_new_skill_count and configured_max_new_skill_count > 0",
    }


def build_prompt(config: Any, stage6: dict[str, Any], max_chars: int | None = None) -> str:
    """生成聚类 prompt；即使未触发合并也允许 Debug 页预览完整输入。"""
    from ..pipeline import fit_prompt

    actions = _prepared_actions(stage6)
    add_actions = [item for item in actions if item.get("action") == "add_new_skill"]
    merge_config = resolve_merge_config(config, actions)
    template = render_prompt_template(TEMPLATE, {"repair_action_merge_schema": json_block(merge_schema())})
    payload = {
        "add_new_skill_actions": add_actions,
        "merge_config": merge_config,
        "max_new_skill_count": merge_config["configured_max_new_skill_count"],
        "skill_word_limit": merge_config["skill_word_limit"],
    }
    return fit_prompt(template, payload, max_chars or config.max_prompt_chars)


def _validated_clusters(result: dict[str, Any], add_actions: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """确保 LLM 输出恰好构成原新增建议的无重复完整分区。"""
    action_ids = {str(item.get("suggestion_id") or "") for item in add_actions}
    clusters = result.get("clusters")
    if not isinstance(clusters, list) or len(clusters) != target:
        raise RuntimeError(f"Repair-action merge must return exactly {target} clusters")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise RuntimeError(f"clusters[{index}] is not an object")
        root_id = str(cluster.get("root_suggestion_id") or "").strip()
        member_ids = [str(item).strip() for item in cluster.get("member_suggestion_ids") or [] if str(item).strip()]
        if not root_id or root_id not in member_ids:
            raise RuntimeError(f"clusters[{index}] root_suggestion_id must be one of its members")
        if len(member_ids) != len(set(member_ids)):
            raise RuntimeError(f"clusters[{index}] contains duplicate member ids")
        unknown = set(member_ids) - action_ids
        if unknown:
            raise RuntimeError(f"clusters[{index}] contains unknown suggestion ids: {sorted(unknown)}")
        overlap = seen.intersection(member_ids)
        if overlap:
            raise RuntimeError(f"Suggestions occur in more than one cluster: {sorted(overlap)}")
        seen.update(member_ids)
        normalized.append(
            {
                "cluster_id": f"C{index + 1}",
                "root_suggestion_id": root_id,
                "member_suggestion_ids": member_ids,
                "unified_scope": str(cluster.get("unified_scope") or cluster.get("root_skill_scope") or ""),
            }
        )
    if seen != action_ids:
        raise RuntimeError(f"Clusters do not cover all add_new_skill suggestions; missing={sorted(action_ids - seen)}")
    return normalized


def _ordered_actions(
    original_actions: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    skill_word_limit: int,
) -> list[dict[str, Any]]:
    """按“已有 skill 修复 -> 每簇根新增 -> 簇内增量修复”生成 Stage 8 输入。"""
    by_id = {str(item.get("suggestion_id")): dict(item) for item in original_actions}
    ordered = [
        dict(item, skill_word_limit=skill_word_limit)
        for item in original_actions
        if item.get("action") != "add_new_skill"
    ]
    for cluster in clusters:
        root_id = cluster["root_suggestion_id"]
        root = dict(by_id[root_id])
        root_skill_id = str(root.get("new_skill_id") or "").strip()
        if not root_skill_id:
            raise RuntimeError(f"Root suggestion {root_id} has no new_skill_id")
        root.update(
            {
                "merge_cluster_id": cluster["cluster_id"],
                "merged_member_suggestion_ids": cluster["member_suggestion_ids"],
                "merged_root_skill_scope": cluster["unified_scope"],
                "skill_word_limit": skill_word_limit,
            }
        )
        ordered.append(root)
        for member_id in cluster["member_suggestion_ids"]:
            if member_id == root_id:
                continue
            converted = dict(by_id[member_id])
            converted.update(
                {
                    "action": "revise_existing_skill",
                    "target_skill_id": root_skill_id,
                    "new_skill_id": None,
                    "merged_from_action": "add_new_skill",
                    "merge_cluster_id": cluster["cluster_id"],
                    "merge_root_suggestion_id": root_id,
                    "merge_root_skill_id": root_skill_id,
                    "skill_word_limit": skill_word_limit,
                }
            )
            ordered.append(converted)
    for index, item in enumerate(ordered):
        item["execution_order"] = index
    return ordered


def compose_result(config: Any, stage6: dict[str, Any], llm_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """组合可直接供 Stage 8 使用的完整 action 列表。"""
    actions = _prepared_actions(stage6)
    add_actions = [item for item in actions if item.get("action") == "add_new_skill"]
    merge_config = resolve_merge_config(config, actions)
    if not merge_config["merge_required"]:
        passthrough = [dict(item, execution_order=index, skill_word_limit=merge_config["skill_word_limit"]) for index, item in enumerate(actions)]
        return {
            "stage_type": "repair_action_merge",
            "stage_name": STAGE_NAME,
            "merge_applied": False,
            "merge_config": merge_config,
            "clusters": [],
            "repair_actions": passthrough,
            "original_action_count": len(actions),
            "final_action_count": len(passthrough),
            "original_add_new_skill_count": len(add_actions),
            "final_add_new_skill_count": len(add_actions),
        }
    if not isinstance(llm_result, dict):
        raise RuntimeError("Repair-action merge requires a repair LLM clustering result")
    clusters = _validated_clusters(llm_result, add_actions, merge_config["resolved_target_cluster_count"])
    ordered = _ordered_actions(actions, clusters, merge_config["skill_word_limit"])
    return {
        "stage_type": "repair_action_merge",
        "stage_name": STAGE_NAME,
        "merge_applied": True,
        "merge_config": merge_config,
        "clusters": clusters,
        "repair_actions": ordered,
        "original_action_count": len(actions),
        "final_action_count": len(ordered),
        "original_add_new_skill_count": len(add_actions),
        "final_add_new_skill_count": sum(1 for item in ordered if item.get("action") == "add_new_skill"),
    }


def run(config: Any, stage6: dict[str, Any]) -> dict[str, Any]:
    """按需调用 repair LLM；未超过阈值时完全本地直通。"""
    actions = _prepared_actions(stage6)
    merge_config = resolve_merge_config(config, actions)
    if not merge_config["merge_required"]:
        return compose_result(config, stage6)
    prompt = build_prompt(config, stage6)
    result = run_llm_stage(config, STAGE_NAME, prompt)
    return compose_result(config, stage6, result)
