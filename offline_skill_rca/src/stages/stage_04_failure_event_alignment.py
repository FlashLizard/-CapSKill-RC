"""Stage 4：把失败事件和原因事件分别对齐到能力图节点。

Stage 3 不知道能力节点。Stage 4 只负责事件分类：每个 failure/cause event 映射
到一个最相关节点，或明确返回 null。原因事件可以映射到失败事件的上游节点。
"""
from __future__ import annotations

from typing import Any

from .common import json_block, render_prompt_template, run_llm_stage

STAGE_NAME = "stage-04-failure-event-alignment"


def build_prompt(bundle: dict[str, Any], stage2: dict[str, Any], stage3: list[dict[str, Any]], max_chars: int) -> str:
    """构造 Stage 4 prompt。

    这里原样提供 Stage 3 的逐轨迹结构化输出。Web 预览和真实生成 prompt 共用
    同一语义，不加入轨迹之外的本地推断。
    """
    from ..pipeline import failure_event_alignment_schema, fit_prompt

    instructions = render_prompt_template(
        "stage-04-failure-event-alignment.txt",
        {"failure_event_alignment_schema": json_block(failure_event_alignment_schema())},
    )
    payload = {
        "stage_02_capability_graph": stage2.get("capability_graph") or stage2,
        "stage_03_failure_events_by_trace": stage3,
    }
    return fit_prompt(instructions, payload, max_chars)


def run(config: Any, bundle: dict[str, Any], stage2: dict[str, Any], stage3: list[dict[str, Any]]) -> dict[str, Any]:
    """运行 Stage 4，生成 event-node alignment。"""
    result = run_llm_stage(config, STAGE_NAME, build_prompt(bundle, stage2, stage3, config.max_prompt_chars))
    cause_keys = {
        (str(trace.get("traj_id") or ""), str(event.get("event_id") or ""))
        for trace in stage3
        if isinstance(trace, dict)
        for event in trace.get("cause_events") or []
        if isinstance(event, dict)
    }
    alignments = result.get("alignments") or result.get("event_node_alignments") or []
    normalized = []
    for item in alignments:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if row.get("event_kind") not in {"failure", "cause"}:
            key = (str(row.get("traj_id") or ""), str(row.get("event_id") or ""))
            row["event_kind"] = "cause" if key in cause_keys else "failure"
        if "confidence" not in row:
            row["confidence"] = row.get("alignment_score", 0.0)
        row.pop("alignment_score", None)
        normalized.append(row)
    return {"alignments": normalized}
