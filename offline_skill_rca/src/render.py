"""诊断报告渲染工具。

优先使用 repair LLM 在最终阶段生成的中文 Markdown 报告；如果最终输出缺少
``diagnosis_report_markdown``，则根据结构化字段生成一个兜底报告，保证每次
运行至少有可读的 ``diagnosis_report.md``。
"""
from __future__ import annotations

from typing import Any


def render_report(data: dict[str, Any]) -> str:
    """把最终分析结果渲染成 Markdown。

    参数 ``data`` 是 Stage 7 或 Stage 7d 归一化后的最终修复包。函数尽量宽容：
    字段缺失时跳过对应章节，而不是让报告生成失败。
    """
    if isinstance(data.get("diagnosis_report_markdown"), str) and data["diagnosis_report_markdown"].strip():
        # LLM 已经给出完整中文报告时，直接采用 LLM 版本，避免本地模板覆盖其诊断叙述。
        return data["diagnosis_report_markdown"].strip() + "\n"
    lines = ["# Offline SkillRCA Diagnosis Report", ""]
    summary = data.get("summary") or data.get("diagnosis_summary") or ""
    if summary:
        lines.extend(["## Summary", str(summary), ""])
    blockers = data.get("non_skill_blockers") or []
    if blockers:
        # blocker 可能是 dict，也可能是历史版本遗留的字符串；这里同时兼容。
        lines.append("## Non-Skill Blockers")
        for blocker in blockers:
            if isinstance(blocker, dict):
                lines.append(f"- {blocker.get('description') or blocker}")
            else:
                lines.append(f"- {blocker}")
        lines.append("")
    hypotheses = data.get("root_cause_hypotheses") or []
    if hypotheses:
        # root cause 是诊断报告最重要的结构化内容，兜底报告中保留分数和影响轨迹。
        lines.append("## Root Causes")
        for item in hypotheses:
            lines.append(f"### {item.get('hypothesis_id', 'H?')}: {item.get('root_cause_type', '')}")
            lines.append(f"- Node: {item.get('node_id', '')}")
            lines.append(f"- Score: {item.get('score', '')}")
            lines.append(f"- Affected trajectories: {', '.join(map(str, item.get('affected_trajectories', [])))}")
            lines.append(f"- Description: {item.get('description', '')}")
            lines.append("")
    node_recommendations = data.get("node_repair_recommendations") or []
    if node_recommendations:
        # 新版 Stage 5 不再生成 LLM root cause，而是用本地公式整理逐 node 修复建议。
        # 兜底报告保留分类、动作、直接失败指标和 bad event 数，方便快速判断优先级。
        lines.append("## Node Repair Recommendations")
        for item in node_recommendations:
            classification = item.get("classification") or {}
            status = item.get("execution_success_analysis") or {}
            lines.append(f"### {item.get('node_id', 'N?')}: {classification.get('category', '')}")
            lines.append(f"- Action: {item.get('recommended_action', '')}")
            lines.append(f"- Attempted success rate: {status.get('attempted_success_rate')}")
            lines.append(f"- Direct failure rate: {status.get('direct_failure_rate')}")
            lines.append(f"- Blocked rate: {status.get('blocked_rate')}")
            lines.append(f"- Bad events: {len(item.get('bad_event_list') or [])}")
            lines.append(f"- Target skills: {', '.join(map(str, item.get('target_skill_ids') or []))}")
            lines.append("")
    patches = data.get("skill_patch_plan") or []
    if patches:
        # patch 摘要只展示目标 skill 和变更说明，完整 patch 内容另写入 patches/。
        lines.append("## Proposed Skill Patches")
        for patch in patches:
            target = patch.get("target_skill_id") or patch.get("new_skill_id") or patch.get("relative_path") or ""
            lines.append(f"- {patch.get('patch_id', 'P?')}: {patch.get('action', '')} `{target}` - {patch.get('proposed_change_summary', '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
