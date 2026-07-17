"""Offline SkillRCA 的本地公式计算逻辑。

Repair LLM 负责做证据归纳和定性判断；凡是可以由明确公式得到的字段，都在
这里用代码计算并写回 stage JSON。这样可以避免 LLM 手算权重、归一化或排序时
出现不稳定结果，也方便之后统一调参。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


COVERAGE_BASE_WEIGHTS = {
    "node_requirement_fit": 0.25,
    "trigger_coverage": 0.20,
    "procedure_coverage": 0.25,
    "verification_coverage": 0.20,
    "recovery_coverage": 0.10,
}

EXECUTION_SUPPORT_WEIGHTS = {
    "not_needed": 0.0,
    "helpful": 0.10,
    "required": 0.25,
}

ROOT_CAUSE_WEIGHTS = {
    "F": 0.25,
    "P": 0.20,
    "G": 0.20,
    "D": 0.15,
    "U": 0.10,
    "A": 0.10,
}

SEVERITY_SCORE = {
    "fatal": 1.0,
    "major": 0.75,
    "minor": 0.35,
}

USAGE_SIGNAL_BY_ROOT_CAUSE = {
    "skill_absent": 0.85,
    "skill_under_specified": 0.75,
    "skill_missing_trigger": 0.9,
    "skill_missing_verification": 0.8,
    "skill_missing_recovery": 0.8,
    "skill_conflict": 0.75,
    "skill_too_broad": 0.65,
    "skill_too_task_specific": 0.65,
    "model_needs_execution_support": 0.85,
    "non_skill_issue": 0.2,
}


def clamp01(value: Any) -> float | None:
    """把 LLM 给出的数值规整到 [0, 1]，不可解析时返回 ``None``。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return round(number, 4)


def is_directly_relevant(value: Any) -> bool:
    """兼容 bool 和字符串形式的 directly_relevant。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1", "directly_relevant"}


def stable_unique(values: list[Any]) -> list[str]:
    """保持顺序去重，并把值转换成字符串。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def add_label_candidate(candidates: list[tuple[str, float]], label: str, score: Any, *, threshold: float | None = None) -> None:
    """按维度分数添加标签候选。

    ``coverage_labels`` 表示具体问题或状态，不再保存单独的 ``coverage_label``。
    对有分数的维度，满分 1.0 表示该维度没有问题，因此不产生标签。若传入
    ``threshold``，只有低于阈值才产生对应问题标签。
    """
    value = clamp01(score)
    if value is None:
        value = 0.0
    if value >= 1.0:
        return
    if threshold is not None and value >= threshold:
        return
    candidates.append((label, value))


def compute_coverage_labels(row: dict[str, Any], overall: float | None, missing_slots: list[str]) -> list[str]:
    """根据本地公式和关键维度阈值推断 coverage labels，最多返回 3 个。

    一个 node-skill pair 可能同时存在多个问题，例如 verification、recovery 和
    execution support 都不足。这里返回多标签列表，避免单一 label 掩盖其它关键
    缺口。标签按对应维度分数从低到高排序；满分维度不会产生标签。
    """
    if not is_directly_relevant(row.get("directly_relevant")):
        return ["not_relevant"]
    if overall is None:
        return ["under_specified"]

    lowered_slots = {slot.lower() for slot in missing_slots}
    candidates: list[tuple[str, float]] = []
    if any("conflict" in slot for slot in lowered_slots):
        candidates.append(("conflicting_skills", 0.0))

    need = str(row.get("execution_support_need") or "").strip().lower()
    execution = clamp01(row.get("execution_support_coverage"))
    if need == "required":
        add_label_candidate(candidates, "missing_execution_support", execution, threshold=0.5)
    add_label_candidate(candidates, "missing_verification", row.get("verification_coverage"), threshold=0.35)
    add_label_candidate(candidates, "missing_recovery", row.get("recovery_coverage"), threshold=0.25)

    spec_scores = [
        clamp01(row.get("node_requirement_fit")),
        clamp01(row.get("trigger_coverage")),
        clamp01(row.get("procedure_coverage")),
        clamp01(overall),
    ]
    valid_spec_scores = [score for score in spec_scores if score is not None]
    spec_score = min(valid_spec_scores) if valid_spec_scores else 0.0
    if (
        overall < 0.55
        or (clamp01(row.get("node_requirement_fit")) or 0.0) < 0.45
        or (clamp01(row.get("trigger_coverage")) or 0.0) < 0.45
        or (clamp01(row.get("procedure_coverage")) or 0.0) < 0.45
    ):
        add_label_candidate(candidates, "under_specified", spec_score, threshold=1.0)
    elif overall < 0.80:
        add_label_candidate(candidates, "partially_covered", overall, threshold=1.0)

    candidates.sort(key=lambda item: (item[1], item[0]))
    labels: list[str] = []
    for label, _score in candidates:
        if label not in labels:
            labels.append(label)
        if len(labels) >= 3:
            break
    return labels


def compute_missing_slots(row: dict[str, Any]) -> list[str]:
    """根据低分维度补全 missing_slots，保留 LLM 已经给出的槽位。"""
    slots = list(row.get("missing_slots") or [])
    if not is_directly_relevant(row.get("directly_relevant")):
        return []
    if (clamp01(row.get("node_requirement_fit")) or 0.0) < 0.45:
        slots.append("node_requirement_fit")
    if (clamp01(row.get("trigger_coverage")) or 0.0) < 0.45:
        slots.append("trigger")
    if (clamp01(row.get("procedure_coverage")) or 0.0) < 0.45:
        slots.append("procedure")
    if (clamp01(row.get("verification_coverage")) or 0.0) < 0.35:
        slots.append("verification")
    if (clamp01(row.get("recovery_coverage")) or 0.0) < 0.25:
        slots.append("recovery")
    need = str(row.get("execution_support_need") or "").strip().lower()
    execution = clamp01(row.get("execution_support_coverage"))
    if need == "required" and (execution is None or execution < 0.5):
        slots.append("execution_support")
    return stable_unique(slots)


def compute_coverage_row(row: dict[str, Any]) -> dict[str, Any]:
    """计算单个 node-skill pair 的 aggregate coverage 字段。"""
    out = normalize_coverage_pair(row)
    already_calculated = isinstance(out.get("calculation"), dict) and out["calculation"].get("calculated_by") == "local_code"
    original_overall = out.get("overall_coverage")
    if original_overall not in (None, "") and not already_calculated and "llm_overall_coverage" not in out:
        out["llm_overall_coverage"] = original_overall
    out.pop("coverage_label", None)
    out.pop("llm_coverage_label", None)
    out.pop("llm_coverage_label_suggestion", None)
    out.pop("llm_coverage_labels", None)

    if not is_directly_relevant(out.get("directly_relevant")):
        for key in [
            "node_requirement_fit",
            "node_requirement_fit_rationale",
            "trigger_coverage",
            "procedure_coverage",
            "verification_coverage",
            "recovery_coverage",
            "execution_support_need",
            "execution_support_coverage",
            "execution_support_rationale",
        ]:
            out[key] = None
        out["overall_coverage"] = None
        out["coverage_gap"] = None
        out["coverage_labels"] = ["not_relevant"]
        out["missing_slots"] = []
        out["calculation"] = {
            "calculated_by": "local_code",
            "formula": "directly_relevant=false => aggregate coverage is not applicable",
            "applicable_dimensions": [],
            "weights": {},
            "inputs": {},
        }
        return out

    weighted_sum = 0.0
    weight_sum = 0.0
    applicable_dimensions: list[str] = []
    inputs: dict[str, Any] = {}
    weights: dict[str, float] = {}
    warnings: list[str] = []

    for key, weight in COVERAGE_BASE_WEIGHTS.items():
        score = clamp01(out.get(key))
        inputs[key] = score
        weights[key] = weight
        applicable_dimensions.append(key)
        if score is None:
            warnings.append(f"{key} missing or invalid; treated as 0")
            score = 0.0
        weighted_sum += weight * score
        weight_sum += weight
        out[key] = score

    need = str(out.get("execution_support_need") or "").strip().lower()
    if need not in EXECUTION_SUPPORT_WEIGHTS:
        need = "not_needed"
        out["execution_support_need"] = need
    execution_weight = EXECUTION_SUPPORT_WEIGHTS[need]
    execution_score = clamp01(out.get("execution_support_coverage"))
    if execution_weight > 0:
        applicable_dimensions.append("execution_support_coverage")
        weights["execution_support_coverage"] = execution_weight
        inputs["execution_support_coverage"] = execution_score
        if execution_score is None:
            warnings.append("execution_support_coverage missing or invalid; treated as 0")
            execution_score = 0.0
        weighted_sum += execution_weight * execution_score
        weight_sum += execution_weight
        out["execution_support_coverage"] = execution_score
    else:
        inputs["execution_support_coverage"] = execution_score
        weights["execution_support_coverage"] = 0.0
        out["execution_support_coverage"] = None

    overall = round(weighted_sum / weight_sum, 4) if weight_sum else None
    missing_slots = compute_missing_slots(out)
    out["overall_coverage"] = overall
    out["coverage_gap"] = round(1 - overall, 4) if overall is not None else None
    out["missing_slots"] = missing_slots
    coverage_labels = compute_coverage_labels(out, overall, missing_slots)
    out["coverage_labels"] = coverage_labels
    out["calculation"] = {
        "calculated_by": "local_code",
        "formula": "overall_coverage = sum(applicable_weight_i * score_i) / sum(applicable_weight_i)",
        "applicable_dimensions": applicable_dimensions,
        "weights": weights,
        "inputs": inputs,
        "weighted_sum": round(weighted_sum, 4),
        "weight_sum": round(weight_sum, 4),
        "warnings": warnings,
    }
    return out


def normalize_coverage_pair(row: dict[str, Any]) -> dict[str, Any]:
    """把新版嵌套 coverage pair 映射到本地公式使用的稳定字段。

    新版 LLM 契约把六个语义分数放在 ``scores`` 下，并使用简短字段名；旧运行
    产物则把它们平铺为 ``*_coverage``。本地计算与 Stage 5 继续读取平铺字段，
    同时保留原 ``scores``，因此新旧运行目录都能继续计算和调试。
    """
    out = dict(row or {})
    scores = out.get("scores") if isinstance(out.get("scores"), dict) else {}
    mapping = {
        "node_requirement_fit": "requirement_fit",
        "trigger_coverage": "trigger",
        "procedure_coverage": "procedure",
        "verification_coverage": "verification",
        "recovery_coverage": "recovery",
        "execution_support_coverage": "execution_support",
    }
    for flat_name, nested_name in mapping.items():
        if flat_name not in out:
            out[flat_name] = scores.get(nested_name)
    if "direct_relevance_rationale" not in out:
        out["direct_relevance_rationale"] = out.get("relevance_reason")
    return out


def postprocess_skill_coverage(result: dict[str, Any]) -> dict[str, Any]:
    """给 Stage 2 输出写入本地计算后的 coverage 字段。"""
    out = dict(result or {})
    rows = out.get("coverage_pairs") or out.get("skill_coverage_matrix") or out.get("coverage_matrix") or []
    calculated = [compute_coverage_row(row) for row in rows if isinstance(row, dict)]
    # ``coverage_pairs`` 是新版规范字段；兼容别名供旧报告和既有 Stage 5 读取。
    out["coverage_pairs"] = calculated
    out["skill_coverage_matrix"] = calculated
    out["calculation_notes"] = [
        "overall_coverage, coverage_gap, coverage_labels, and computed missing_slots are calculated by local code.",
        "Repair LLM is only responsible for directly_relevant, dimension scores, rationales, and evidence links.",
    ]
    return out


def trace_lengths(bundle: dict[str, Any]) -> dict[str, int]:
    """读取每条轨迹的 step 数，用于把 step_id 归一成早期失败优先级。"""
    lengths: dict[str, int] = {}
    for traj in bundle.get("failed_trajectories") or []:
        traj_id = str(traj.get("traj_id") or "")
        if traj_id:
            lengths[traj_id] = max(len(traj.get("steps") or []), 1)
    return lengths


def collect_bad_events(stage2: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把 Stage 2 的 bad_events 建成 event_id/traj_id 双键索引。"""
    events: dict[str, dict[str, Any]] = {}
    for trace in stage2 or []:
        traj_id = str(trace.get("traj_id") or "")
        for event in trace.get("bad_events") or trace.get("failure_events") or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            enriched = dict(event)
            enriched["traj_id"] = traj_id
            if event_id:
                events[event_id] = enriched
                events[f"{traj_id}:{event_id}"] = enriched
    return events


def lookup_event(event_index: dict[str, dict[str, Any]], event_id: Any, traj_id: Any = None) -> dict[str, Any] | None:
    """按 event_id 或 traj_id:event_id 查找事件。"""
    event_key = str(event_id or "")
    if traj_id:
        found = event_index.get(f"{traj_id}:{event_key}")
        if found:
            return found
    return event_index.get(event_key)


def coverage_rows(stage4_coverage: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容读取 Stage 4 coverage matrix。"""
    if not isinstance(stage4_coverage, dict):
        return []
    rows = stage4_coverage.get("skill_coverage_matrix") or stage4_coverage.get("coverage_matrix") or []
    return [row for row in rows if isinstance(row, dict)]


def node_gap(node_id: str, target_skill_ids: list[str], rows: list[dict[str, Any]]) -> float:
    """计算一个 hypothesis 所在节点的 skill coverage gap。

    如果 hypothesis 指向具体 skills，则使用这些 pair 的平均 gap；否则使用该 node
    最好的直接相关 skill 作为当前库对该 node 的覆盖水平。没有直接相关行时，gap=1。
    """
    node_rows = [row for row in rows if str(row.get("node_id") or "") == node_id and is_directly_relevant(row.get("directly_relevant"))]
    if not node_rows:
        return 1.0
    targets = {str(skill_id) for skill_id in target_skill_ids}
    if targets:
        selected = [row for row in node_rows if str(row.get("skill_id") or "") in targets]
        if selected:
            gaps = [clamp01(row.get("coverage_gap")) for row in selected]
            valid = [gap for gap in gaps if gap is not None]
            return round(sum(valid) / len(valid), 4) if valid else 1.0
    coverages = [clamp01(row.get("overall_coverage")) for row in node_rows]
    valid_coverages = [value for value in coverages if value is not None]
    if not valid_coverages:
        return 1.0
    return round(1 - max(valid_coverages), 4)


def frequency_score(affected_trajectories: list[str], total_failed: int) -> float:
    """F：受影响失败轨迹占比。"""
    if total_failed <= 0:
        return 0.0
    return round(min(len(set(affected_trajectories)) / total_failed, 1.0), 4)


def priority_score(events: list[dict[str, Any]], lengths: dict[str, int]) -> float:
    """P：越早发生、越像 first actionable fault 的事件优先级越高。"""
    if not events:
        return 0.0
    scores: list[float] = []
    for event in events:
        traj_id = str(event.get("traj_id") or "")
        length = max(lengths.get(traj_id, 50), 1)
        try:
            step_id = max(float(event.get("step_id") or 1), 1.0)
        except (TypeError, ValueError):
            step_id = 1.0
        early = 1.0 - min((step_id - 1) / length, 1.0)
        bonus = 0.15 if event.get("first_actionable_fault_candidate") else 0.0
        scores.append(min(early * 0.85 + bonus, 1.0))
    return round(sum(scores) / len(scores), 4)


def downstream_score(events: list[dict[str, Any]]) -> float:
    """D：用 severity 和 downstream_consequence 是否存在来近似传播解释力。"""
    if not events:
        return 0.0
    scores: list[float] = []
    for event in events:
        severity = str(event.get("severity") or "").strip().lower()
        base = SEVERITY_SCORE.get(severity, 0.5)
        consequence = str(event.get("downstream_consequence") or "").strip()
        if consequence:
            base = min(base + 0.1, 1.0)
        scores.append(base)
    return round(sum(scores) / len(scores), 4)


def usage_signal_score(root_cause_type: str) -> float:
    """U：按根因类型给出本地可复现的 skill usage 信号强度。"""
    return USAGE_SIGNAL_BY_ROOT_CAUSE.get(root_cause_type, 0.5)


def alternative_rejection_score(hypothesis: dict[str, Any], non_skill_blockers: list[dict[str, Any]]) -> float:
    """A：非 skill 原因越少，skill-level hypothesis 的替代解释排除越强。"""
    root_type = str(hypothesis.get("root_cause_type") or "")
    if root_type == "non_skill_issue":
        return 0.2
    blocking = [item for item in non_skill_blockers if isinstance(item, dict) and item.get("blocks_validation")]
    if blocking:
        return 0.35
    return 0.8


def hypothesis_events(hypothesis: dict[str, Any], event_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """读取 hypothesis 支持事件对应的 Stage 2 事件详情。"""
    events: list[dict[str, Any]] = []
    affected = hypothesis.get("affected_trajectories") or []
    for event_id in hypothesis.get("supporting_events") or []:
        found = None
        for traj_id in affected:
            found = lookup_event(event_index, event_id, traj_id)
            if found:
                break
        if not found:
            found = lookup_event(event_index, event_id)
        if found:
            events.append(found)
    return events


def compute_root_cause_score(
    hypothesis: dict[str, Any],
    *,
    total_failed: int,
    lengths: dict[str, int],
    event_index: dict[str, dict[str, Any]],
    coverage: list[dict[str, Any]],
    non_skill_blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算单个 root-cause hypothesis 的 score 和分解项。"""
    out = dict(hypothesis)
    already_calculated = isinstance(out.get("score_calculation"), dict) and out["score_calculation"].get("calculated_by") == "local_code"
    original_score = out.get("score")
    if original_score not in (None, "") and not already_calculated and "llm_score" not in out:
        out["llm_score"] = original_score

    affected = stable_unique(out.get("affected_trajectories") or [])
    target_skills = stable_unique(out.get("target_skill_ids") or [])
    root_type = str(out.get("root_cause_type") or "")
    node_id = str(out.get("node_id") or "")
    events = hypothesis_events(out, event_index)

    factors = {
        "F": frequency_score(affected, total_failed),
        "P": priority_score(events, lengths),
        "G": node_gap(node_id, target_skills, coverage),
        "D": downstream_score(events),
        "U": usage_signal_score(root_type),
        "A": alternative_rejection_score(out, non_skill_blockers),
    }
    weighted_sum = sum(ROOT_CAUSE_WEIGHTS[key] * factors[key] for key in ROOT_CAUSE_WEIGHTS)
    out["affected_trajectories"] = affected
    out["target_skill_ids"] = target_skills
    out["score"] = round(weighted_sum, 4)
    out["score_factors"] = factors
    out["score_calculation"] = {
        "calculated_by": "local_code",
        "formula": "Score(h)=0.25F+0.20P+0.20G+0.15D+0.10U+0.10A",
        "weights": ROOT_CAUSE_WEIGHTS,
        "inputs": factors,
        "weighted_sum": round(weighted_sum, 4),
        "event_refs_used": [event.get("event_id") for event in events if event.get("event_id")],
    }
    return out


def postprocess_root_cause_hypotheses(
    result: dict[str, Any],
    *,
    bundle: dict[str, Any],
    stage2: list[dict[str, Any]],
    stage4_coverage: dict[str, Any],
) -> dict[str, Any]:
    """给 Stage 5 输出写入本地 root-cause score 并排序。"""
    out = dict(result or {})
    blockers = [item for item in out.get("non_skill_blockers") or [] if isinstance(item, dict)]
    failed = [traj for traj in bundle.get("failed_trajectories") or [] if not traj.get("success")]
    if not failed:
        failed = list(bundle.get("failed_trajectories") or [])
    total_failed = max(len(failed), 1)
    lengths = trace_lengths(bundle)
    event_index = collect_bad_events(stage2)
    rows = coverage_rows(stage4_coverage)

    hypotheses = [
        compute_root_cause_score(
            item,
            total_failed=total_failed,
            lengths=lengths,
            event_index=event_index,
            coverage=rows,
            non_skill_blockers=blockers,
        )
        for item in out.get("root_cause_hypotheses") or []
        if isinstance(item, dict)
    ]
    hypotheses.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
    out["root_cause_hypotheses"] = hypotheses
    out["calculation_notes"] = [
        "root_cause_hypothesis.score is calculated by local code from visible stage outputs.",
        "Repair LLM is responsible for candidate hypotheses, evidence links, affected trajectories, and qualitative descriptions.",
    ]
    return out


def summarize_node_coverage(stage4_coverage: dict[str, Any]) -> dict[str, Any]:
    """为 debug/后续阶段提供按节点聚合的可复现覆盖摘要。"""
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in coverage_rows(stage4_coverage):
        by_node[str(row.get("node_id") or "")].append(row)
    summary: dict[str, Any] = {}
    for node_id, rows in by_node.items():
        relevant = [row for row in rows if is_directly_relevant(row.get("directly_relevant"))]
        coverages = [clamp01(row.get("overall_coverage")) for row in relevant]
        valid = [value for value in coverages if value is not None]
        best = max(valid) if valid else None
        summary[node_id] = {
            "directly_relevant_skill_count": len(relevant),
            "best_overall_coverage": best,
            "node_gap": round(1 - best, 4) if best is not None else 1.0,
            "best_skill_ids": [
                row.get("skill_id")
                for row in relevant
                if best is not None and clamp01(row.get("overall_coverage")) == best
            ],
        }
    return summary
