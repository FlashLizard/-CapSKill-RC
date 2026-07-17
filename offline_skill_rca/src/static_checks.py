"""本地静态检查辅助函数。

注意：Offline SkillRCA v2 的正式 repair prompt 不会把这些本地打分作为证据发给
repair LLM。本模块主要用于离线 QA、调试或人工查看 patch 是否缺关键章节。
"""
from __future__ import annotations

import re
from typing import Any

REQUIRED_SECTIONS = [
    # 推荐的 Skill 文档结构；lint 时会检查核心章节是否存在。
    "When to Use",
    "Do Not Use When",
    "Inputs",
    "Outputs",
    "Preconditions",
    "Procedure",
    "Verification Checklist",
    "Common Failure Modes",
    "Recovery",
]


def score_skill_quality(card: dict[str, Any]) -> dict[str, Any]:
    """基于 SkillCard 字段做一个粗粒度质量评分。

    分数不是最终诊断依据，只用于本地观察 skill 是否缺 trigger、procedure、
    verification、recovery 或 tool template。
    """
    trigger_score = score_list(card.get("triggers"), broad_penalty=True)
    procedure_score = score_list(card.get("procedure_steps"))
    verification_score = score_list(card.get("validation_checks"))
    recovery_score = score_list(card.get("recovery_steps"))
    execution_support_score = score_list(card.get("tools_or_code"))
    overall = round((trigger_score + procedure_score + verification_score + recovery_score + execution_support_score) / 5, 2)
    issues = []
    if trigger_score < 2:
        issues.append("missing_trigger")
    if procedure_score < 2:
        issues.append("procedure_too_abstract")
    if verification_score < 2:
        issues.append("missing_verification")
    if recovery_score < 2:
        issues.append("missing_recovery")
    if execution_support_score < 2:
        issues.append("missing_execution_support")
    return {
        "skill_id": card.get("skill_id", ""),
        "trigger_score": trigger_score,
        "procedure_score": procedure_score,
        "verification_score": verification_score,
        "recovery_score": recovery_score,
        "execution_support_score": execution_support_score,
        "overall_score": overall,
        "issues": issues,
    }


def score_list(items: Any, broad_penalty: bool = False) -> float:
    """按条目数量和平均长度估算一个 0-5 分。

    这是启发式分数：条目越多、描述越具体通常越可执行；但 trigger 如果过于泛化
    会被轻微扣分。
    """
    if not items:
        return 0.0
    if not isinstance(items, list):
        items = [str(items)]
    count = len([item for item in items if str(item).strip()])
    avg_len = sum(len(str(item)) for item in items) / max(1, count)
    score = 1.0
    if count >= 2:
        score += 1.0
    if count >= 4:
        score += 1.0
    if avg_len > 40:
        score += 1.0
    if avg_len > 120:
        score += 1.0
    if broad_penalty and any(str(item).strip().lower() in {"always", "use this skill"} for item in items):
        score -= 1.0
    return max(0.0, min(5.0, score))


def lint_patch_content(content: str, task_description: str) -> dict[str, Any]:
    """检查一个 skill patch 内容是否满足基本结构和安全要求。"""
    checks = {
        "has_trigger": "When to Use" in content,
        "has_anti_trigger": "Do Not Use When" in content,
        "has_stepwise_procedure": bool(re.search(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S", section(content, "Procedure"))),
        "has_verification_checklist": "Verification Checklist" in content,
        "has_recovery": "Recovery" in content,
        "has_input_output_contract": "Inputs" in content and "Outputs" in content,
        "no_task_specific_filename": not contains_task_specific_filename(content, task_description),
        "no_answer_leakage": not contains_answer_leakage(content),
        "contains_actionable_steps": len(re.findall(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S", content)) >= 5,
    }
    failed = [key for key, ok in checks.items() if not ok]
    status = "accept" if not failed else "revise"
    return {"status": status, "failed_checks": failed, "checks": checks, "risk_level": "low" if not failed else "medium"}


def section(content: str, name: str) -> str:
    """提取某个二级标题章节内容。"""
    match = re.search(rf"(?ims)^##\s+{re.escape(name)}\s*$([\s\S]*?)(?=^##\s+|\Z)", content)
    return match.group(1) if match else ""


def contains_task_specific_filename(content: str, task_description: str) -> bool:
    """检查 patch 是否泄露任务描述中的具体文件名。

    skill 应该是可复用能力，不应把某个任务专属输入文件写死进去。
    """
    task_names = set(re.findall(r"[\w.-]+\.(?:json|csv|xlsx|md|py|png|txt)", task_description))
    if not task_names:
        return False
    allow = {"SKILL.md"}
    names_in_patch = set(re.findall(r"[\w.-]+\.(?:json|csv|xlsx|md|py|png|txt)", content))
    risky = (task_names & names_in_patch) - allow
    return bool(risky)


def contains_answer_leakage(content: str) -> bool:
    """检查明显的答案硬编码/泄露提示。"""
    suspicious = [
        "exact final answer",
        "copy this value",
        "hard-code the answer",
        "do not compute",
    ]
    lower = content.lower()
    return any(item in lower for item in suspicious)
