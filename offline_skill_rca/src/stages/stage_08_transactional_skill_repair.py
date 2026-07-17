"""Stage 8：按建议或建议包执行、带审查和回滚的事务式 Skill 修复。

Stage 6 已经为每个需要处理的 node 生成了具体建议。本阶段不再一次性生成
patch plan，也不再依赖后续阶段，而是把每条建议依次执行成以下状态机：

``repair -> review -> commit``

repair LLM 每次处理一个 repair unit：它可以是一条建议，也可以是同一已有
Skill 的有限建议包。``add_new_skill`` 始终保持单独 unit。review LLM 随后
检查候选文件是否忠实满足 unit 内的全部建议。只有 review 通过且本地路径检查也通过时，
候选文件才会原子提交到复制出的 skill library。审查失败时工作库保持不变，
下一次 repair 会携带审查意见重试。

所有 prompt、request、response、parsed JSON、候选文件和审查结论都会持久化，
因此 Web debug 页面可以逐调用展示，也可以在任意一次 LLM 调用期间暂停后续跑。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..io_utils import copy_tree, ensure_inside, normalize_patch_path, safe_rel, write_json
from .common import json_block, render_prompt_template, run_llm_stage, write_prompt_file

STAGE_NAME = "stage-08-transactional-skill-repair"
REPAIR_TEMPLATE = "stage-08-skill-repair.txt"
REVIEW_TEMPLATE = "stage-08-skill-review.txt"
STATE_FILENAME = "stage_08_transactional_skill_repair.json"
LEGACY_STATE_FILENAMES = (
    "stage_07_transactional_skill_repair.json",
    "stage_06_transactional_skill_repair.json",
)


CLAUDE_CODE_SKILL_SPEC = """Claude Code Skill format requirements:
1. A Skill is one directory containing a required file named exactly `SKILL.md`.
2. `SKILL.md` starts at the first character with YAML frontmatter delimited by `---` lines, followed by a non-empty Markdown body. Do not wrap the frontmatter or file in a code fence.
3. Frontmatter must contain `name` and `description`.
4. `name` must exactly match the Skill directory name; use 1-64 lowercase ASCII letters, digits, and single hyphens only. It must not start or end with a hyphen or contain `--`.
5. `description` must be a non-empty string of at most 1024 characters. State both what the Skill does and the concrete situations in which Claude should invoke it. Put trigger information here because Claude reads metadata before loading the body.
6. Use optional frontmatter fields only when operationally necessary and supported by Claude Code. Do not invent metadata fields to carry repair evidence, task answers, verifier details, or provenance.
7. Write the body as concise, imperative instructions. Include only reusable procedures, decisions, verification, recovery, and resource-loading directions needed after activation. Keep it under 500 lines and within the supplied skill_word_limit.
8. Put detailed or reusable material in supporting files when useful. Reference every supporting file from `SKILL.md` with a relative path and state when Claude should read or run it. Do not create README, changelog, installation guide, or other process documentation.
9. For an existing Skill, preserve its directory and frontmatter `name`. For a new Skill, create only the requested Skill root and make its directory, frontmatter name, scope, and description agree.
10. Return complete final contents for every changed file. A syntactically valid but undiscoverable, ambiguously triggered, empty, or task-answer-specific Skill is invalid."""


def claude_code_skill_spec() -> str:
    """返回 Repair/Review 共用且可由 Web 覆盖的 Claude Code Skill 契约。"""
    return CLAUDE_CODE_SKILL_SPEC


def repair_schema() -> dict[str, Any]:
    """返回单次修复调用的最小 JSON 契约。

    ``files`` 中的 content 必须是完整文件，而不是 diff。这样脚本无需理解 LLM
    的编辑意图，只需在审查通过后把确定的文件内容写入工作副本。
    """
    return {
        "repair_unit_id": "must equal the provided repair_unit_id",
        "suggestion_ids": ["must contain every provided suggestion id in the same order"],
        "files": [
            {
                "path": "path relative to the copied skill-library root",
                "content": "complete final file content; never a diff or omitted fragment",
            }
        ],
    }


def review_schema() -> dict[str, Any]:
    """返回单次候选文件审查的 JSON 契约。"""
    return {
        "repair_unit_id": "must equal the provided repair_unit_id",
        "suggestion_ids": ["must contain every reviewed suggestion id in the same order"],
        "decision": "accept|reject_candidate|reject_suggestion",
        "checks": {
            "suggestion_evidence_supported": True,
            "suggestion_capability_correct": True,
            "suggestion_reusable_skill_scope": True,
            "candidate_satisfies_valid_suggestions": True,
            "scope_preserved": True,
            "files_usable": True,
            "claude_code_compatible": True,
            "library_consistent": True,
        },
        "issues": [{"code": "string", "message": "string"}],
        "retry_instructions": [],
    }


def _now() -> str:
    """生成稳定的 UTC 时间戳，供状态机和审计记录使用。"""
    return datetime.now(UTC).isoformat()


def state_path(config: Any) -> Path:
    """返回 Stage 8 持久化状态文件路径。"""
    return config.output_dir / "stage_outputs_individual" / STATE_FILENAME


def read_state(config: Any) -> dict[str, Any] | None:
    """读取 Stage 8 状态；不存在时兼容读取旧编号的状态文件。"""
    path = state_path(config)
    if not path.exists():
        path = next(
            (
                config.output_dir / "stage_outputs_individual" / name
                for name in LEGACY_STATE_FILENAMES
                if (config.output_dir / "stage_outputs_individual" / name).exists()
            ),
            None,
        )
        if path is None:
            return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_state(config: Any, state: dict[str, Any]) -> None:
    """原子保存状态，避免暂停或进程终止留下半个 JSON 文件。"""
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    # stage_outputs.json 由 debug 编排器刷新；这里额外写一份固定名称，便于独立
    # pipeline 和人工诊断不经过 Web 也能直接定位最终状态。
    write_json(config.output_dir / "stage_08_transactional_skill_repair.json", state)


def _priority_value(value: Any) -> int:
    """把 Stage 5 的优先级转换成稳定排序值。"""
    return {"high": 3, "medium": 2, "low": 1}.get(str(value or "").lower(), 0)


def _unique_evidence_skill_id(raw: dict[str, Any]) -> str | None:
    """从建议证据中确定唯一的目标 skill。

    早期 Stage 5 兼容视图曾把 ``existing_skill_repairs[].skill_id`` 丢失为
    ``target_skill_id: null``，但每条展开建议的 evidence_refs 仍保留了原始
    skill_id。只有所有非空证据都指向同一个 skill 时才自动恢复，存在歧义时
    返回 None，避免 Stage 7 猜错修改目录。
    """
    skill_ids = {
        str(item.get("skill_id") or "").strip()
        for item in raw.get("evidence_refs") or []
        if isinstance(item, dict) and str(item.get("skill_id") or "").strip()
    }
    return next(iter(skill_ids)) if len(skill_ids) == 1 else None


def _resolved_target_skill_id(raw: dict[str, Any]) -> str | None:
    """兼容 Stage 5 新旧字段并确定已有 skill 的目标 id。"""
    explicit = raw.get("target_skill_id") or raw.get("skill_id")
    if explicit:
        return str(explicit).strip() or None
    return _unique_evidence_skill_id(raw)


def prepare_suggestions(stage5: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Stage 5 的逐 node 输出展开成顺序执行的独立建议。

    新旧 Stage 5 输出都可能存在：新版在每个 node 下保存分支式建议，同时保留
    ``skill_repair_suggestions`` 兼容视图；旧版可能只有聚合列表。这里优先使用
    逐 node 视图，并通过 suggestion_id 去重，确保每个建议只被应用一次。
    """
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(raw: Any, node_id: Any = None, fallback_index: int = 0) -> None:
        if not isinstance(raw, dict):
            return
        action = str(raw.get("action") or "").strip()
        if action not in {"revise_existing_skill", "add_new_skill"}:
            return
        target_skill_id = _resolved_target_skill_id(raw)
        new_skill_id = raw.get("new_skill_id") or raw.get("new_skill_id_suggestion")
        suggestion_id = str(
            raw.get("suggestion_id")
            or f"{node_id or 'node'}-{action}-{target_skill_id or new_skill_id or fallback_index + 1}"
        ).strip()
        if not suggestion_id or suggestion_id in seen:
            return
        seen.add(suggestion_id)
        item = dict(raw)
        item.update(
            {
                "suggestion_id": suggestion_id,
                "node_id": raw.get("node_id") or node_id,
                "action": action,
                "target_skill_id": target_skill_id,
                "new_skill_id": new_skill_id,
            }
        )
        suggestions.append(item)

    # Stage 6 已经由代码确定了执行顺序：每个聚类的根 add_new_skill 必须先于
    # 指向它的 revise_existing_skill。这里直接使用该列表，不能再次按优先级
    # 排序，否则后续修复可能在根 skill 创建前执行。
    if "repair_actions" in stage5:
        for index, item in enumerate(stage5.get("repair_actions") or []):
            add(item, (item or {}).get("node_id") if isinstance(item, dict) else None, index)
        suggestions.sort(key=lambda item: int(item.get("execution_order") or 0))
        return suggestions

    node_results = stage5.get("node_skill_repair_suggestions") or stage5.get("node_repair_recommendations") or []
    for node_index, node in enumerate(node_results):
        if not isinstance(node, dict):
            continue
        node_id = node.get("node_id")
        # 优先读取分支式原始建议。兼容视图可能来自更早版本，曾丢失 skill_id 或
        # 把字符串形式的 change_requests 压缩为空对象。
        count_before_node = len(suggestions)
        for index, repair in enumerate(node.get("existing_skill_repairs") or []):
            if not isinstance(repair, dict):
                continue
            add(
                {
                    **repair,
                    "suggestion_id": repair.get("suggestion_id") or f"{node_id}-R{index + 1}",
                    "action": "revise_existing_skill",
                    "target_skill_id": repair.get("target_skill_id") or repair.get("skill_id"),
                    "repair_objective": repair.get("repair_goal"),
                },
                node_id,
                index,
            )
        proposal = node.get("new_skill_proposal")
        proposal_skill_id = (
            proposal.get("new_skill_id") or proposal.get("new_skill_id_suggestion")
            if isinstance(proposal, dict)
            else None
        )
        if isinstance(proposal, dict) and proposal_skill_id:
            add(
                {
                    **proposal,
                    "suggestion_id": proposal.get("suggestion_id") or f"{node_id}-N1",
                    "action": "add_new_skill",
                    "new_skill_id": proposal_skill_id,
                    "repair_objective": proposal.get("skill_goal"),
                },
                node_id,
                0,
            )
        # 只有节点没有分支式建议时才退回统一兼容视图。
        if len(suggestions) == count_before_node:
            for index, raw in enumerate(node.get("skill_repair_suggestions") or []):
                add(raw, node_id, index)

    # 极早期运行没有逐 node 兼容建议时，才读取聚合列表作为兜底。
    if not suggestions:
        for index, item in enumerate(stage5.get("skill_repair_recommendations") or []):
            add(item, (item or {}).get("node_id") if isinstance(item, dict) else None, index)
        for index, item in enumerate(stage5.get("new_skill_recommendations") or []):
            add(item, (item or {}).get("node_id") if isinstance(item, dict) else None, index)

    suggestions.sort(
        key=lambda item: (
            -_priority_value(item.get("priority")),
            -float(item.get("confidence") or item.get("priority_score") or 0),
            str(item.get("node_id") or ""),
            str(item.get("suggestion_id") or ""),
        )
    )
    return suggestions


def _repair_mode(config: Any) -> str:
    """返回规范化的 Stage 7 修复粒度。"""
    return "skill_package" if getattr(config, "stage7_repair_mode", "per_suggestion") == "skill_package" else "per_suggestion"


def _skill_package_size(config: Any) -> int:
    """返回同一 Skill 每个建议包允许包含的最大建议数。"""
    return max(1, min(100, int(getattr(config, "stage7_skill_package_size", 3) or 3)))


def _repair_unit_from_members(members: list[dict[str, Any]], package_ordinal: int = 0) -> dict[str, Any]:
    """把一条或多条原子建议转换成一个事务式 repair unit。"""
    if not members:
        raise ValueError("repair unit must contain at least one suggestion")
    first = members[0]
    suggestion_ids = [str(item.get("suggestion_id") or "") for item in members]
    node_ids = [str(item.get("node_id")) for item in members if item.get("node_id")]
    is_package = len(members) > 1
    target_skill_id = first.get("target_skill_id")
    if is_package:
        safe_skill = re.sub(r"[^A-Za-z0-9._-]+", "-", str(target_skill_id or "skill")).strip("-") or "skill"
        unit_id = f"package-{safe_skill}-{package_ordinal:02d}-{suggestion_ids[0]}"
    else:
        unit_id = suggestion_ids[0]
    return {
        "suggestion_id": unit_id,
        "repair_unit_id": unit_id,
        "suggestion_ids": suggestion_ids,
        "node_id": first.get("node_id"),
        "node_ids": node_ids,
        "action": first.get("action"),
        "target_skill_id": target_skill_id,
        "new_skill_id": first.get("new_skill_id"),
        "repair_unit_mode": "skill_suggestion_package" if is_package else "single_suggestion",
        "member_suggestions": [dict(item) for item in members],
    }


def prepare_repair_units(config: Any, stage5: dict[str, Any]) -> list[dict[str, Any]]:
    """按配置把 Stage 6 建议整理成 Stage 7 的事务执行单元。

    ``add_new_skill`` 是顺序屏障并始终单独执行。两个新增操作之间的
    ``revise_existing_skill`` 建议按 target_skill_id 聚合并按上限切包；同一
    segment 内按 skill 首次出现的顺序生成包，避免无界上下文。
    """
    suggestions = prepare_suggestions(stage5)
    if _repair_mode(config) == "per_suggestion":
        return [_repair_unit_from_members([item]) for item in suggestions]

    units: list[dict[str, Any]] = []
    package_ordinal = 0
    segment: list[dict[str, Any]] = []

    def flush_segment() -> None:
        nonlocal package_ordinal
        if not segment:
            return
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in segment:
            skill_id = str(item.get("target_skill_id") or "").strip()
            # 缺失目标的建议仍作为独立 unit，让后续路径校验给出明确错误。
            key = skill_id or f"__missing__{item.get('suggestion_id')}"
            groups.setdefault(key, []).append(item)
        limit = _skill_package_size(config)
        for members in groups.values():
            for start in range(0, len(members), limit):
                chunk = members[start : start + limit]
                package_ordinal += 1
                units.append(_repair_unit_from_members(chunk, package_ordinal))
        segment.clear()

    for suggestion in suggestions:
        if suggestion.get("action") == "add_new_skill":
            flush_segment()
            units.append(_repair_unit_from_members([suggestion]))
        else:
            segment.append(suggestion)
    flush_segment()
    for index, unit in enumerate(units):
        unit["index"] = index
    return units


def _state_rows_from_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 repair units 转成状态机可持久化的待执行行。"""
    return [
        {
            "index": index,
            "suggestion_id": unit.get("repair_unit_id"),
            "repair_unit_id": unit.get("repair_unit_id"),
            "suggestion_ids": unit.get("suggestion_ids") or [],
            "node_id": unit.get("node_id"),
            "node_ids": unit.get("node_ids") or [],
            "action": unit.get("action"),
            "target_skill_id": unit.get("target_skill_id"),
            "new_skill_id": unit.get("new_skill_id"),
            "repair_unit_mode": unit.get("repair_unit_mode"),
            "status": "pending",
            "attempt_count": 0,
            "source_suggestion": (unit.get("member_suggestions") or [{}])[0],
            "source_suggestions": unit.get("member_suggestions") or [],
            "attempts": [],
        }
        for index, unit in enumerate(units)
    ]


def _migrate_existing_state(config: Any, state: dict[str, Any], stage5: dict[str, Any]) -> dict[str, Any]:
    """用当前 Stage 5 输出修复旧 Stage 7 状态中缺失的 skill 标识。

    Prompt 预览本身会初始化并持久化 Stage 7。修复代码升级后，已有运行目录
    不能要求用户删除重建，因此这里按 suggestion_id 对齐最新建议，并只补全
    缺失字段；已经产生的 attempts、interactions 和提交历史保持不变。
    """
    prepared = prepare_repair_units(config, stage5)
    prepared_by_id = {
        str(item.get("suggestion_id")): item
        for item in prepared
        if item.get("suggestion_id")
    }
    stored_signature = [
        (
            str(item.get("repair_unit_id") or item.get("suggestion_id") or ""),
            tuple(item.get("suggestion_ids") or [item.get("suggestion_id")]),
            str(item.get("action") or ""),
            str(item.get("target_skill_id") or ""),
            str(item.get("new_skill_id") or ""),
        )
        for item in state.get("suggestions") or []
        if isinstance(item, dict)
    ]
    prepared_signature = [
        (
            str(item.get("repair_unit_id") or item.get("suggestion_id") or ""),
            tuple(item.get("suggestion_ids") or [item.get("suggestion_id")]),
            str(item.get("action") or ""),
            str(item.get("target_skill_id") or ""),
            str(item.get("new_skill_id") or ""),
        )
        for item in prepared
    ]
    if stored_signature != prepared_signature:
        if int(state.get("accepted_count") or 0) > 0:
            raise RuntimeError(
                "Stage 6 actions or the Stage 7 repair-unit mode changed after Stage 7 had already committed changes. "
                "Use a new output skill-library copy to preserve transactional ordering."
            )
        # 旧运行可能在 Stage 6 引入前已生成若干被拒绝候选。没有候选被提交时，
        # 可以安全替换待执行队列；原交互仍保存在 superseded 字段和审计文件中。
        state["superseded_pre_merge_suggestions"] = state.get("suggestions") or []
        state["suggestions"] = _state_rows_from_units(prepared)
        state["suggestion_count"] = sum(len(item.get("suggestion_ids") or []) for item in prepared)
        state["repair_unit_count"] = len(prepared)
        state["repair_mode"] = _repair_mode(config)
        state["skill_package_size"] = _skill_package_size(config)
        state["current_suggestion_index"] = 0
        state["next_operation"] = None if not prepared else "repair"
        state["status"] = "completed" if not prepared else "pending"
        state["active_interaction"] = None
        state["pause_reason"] = None
        state["updated_at"] = _now()
        write_state(config, state)
        return state
    changed = False
    for stored in state.get("suggestions") or []:
        if not isinstance(stored, dict):
            continue
        suggestion_id = str(stored.get("repair_unit_id") or stored.get("suggestion_id") or "")
        fresh = prepared_by_id.get(suggestion_id) or {}
        source = stored.get("source_suggestion")
        source = dict(source) if isinstance(source, dict) else {}
        # 尚未产生候选时可以无损升级为更完整的 Stage 5 原始分支建议。已经开始
        # repair/review 的条目保持当时输入不变，保证审计轨迹前后一致。
        if fresh and not (stored.get("attempts") or []):
            fresh_sources = fresh.get("member_suggestions") or []
            fresh_source = fresh_sources[0] if fresh_sources else fresh
            if source != fresh_source:
                source = dict(fresh_source)
                changed = True
            if stored.get("source_suggestions") != fresh_sources:
                stored["source_suggestions"] = [dict(item) for item in fresh_sources]
                changed = True

        target_skill_id = (
            stored.get("target_skill_id")
            or _resolved_target_skill_id(source)
            or fresh.get("target_skill_id")
        )
        new_skill_id = (
            stored.get("new_skill_id")
            or source.get("new_skill_id")
            or source.get("new_skill_id_suggestion")
            or fresh.get("new_skill_id")
        )
        if target_skill_id and not stored.get("target_skill_id"):
            stored["target_skill_id"] = target_skill_id
            changed = True
        if new_skill_id and not stored.get("new_skill_id"):
            stored["new_skill_id"] = new_skill_id
            changed = True
        if target_skill_id and not source.get("target_skill_id"):
            source["target_skill_id"] = target_skill_id
            changed = True
        if new_skill_id and not source.get("new_skill_id"):
            source["new_skill_id"] = new_skill_id
            changed = True
        stored["source_suggestion"] = source

    expected_mode = _repair_mode(config)
    expected_package_size = _skill_package_size(config)
    if state.get("repair_mode") != expected_mode:
        state["repair_mode"] = expected_mode
        changed = True
    if state.get("skill_package_size") != expected_package_size:
        state["skill_package_size"] = expected_package_size
        changed = True
    state["repair_unit_count"] = len(state.get("suggestions") or [])

    if changed:
        state["updated_at"] = _now()
        write_state(config, state)
    return state


def _normalized_skill_alias(value: Any) -> str:
    """把 Skill ID 或展示标题转换为只用于匹配的稳定别名。"""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _skill_relative_root(config: Any, bundle: dict[str, Any], skill_id: str) -> Path:
    """把已有 Skill 的 ID 或标准化标题唯一映射回原始目录。

    Stage 5 偶尔会返回 Stage 1 的展示标题，而不是原始 ``skill_id``。这仍然是
    ``revise_existing_skill``，不能把展示标题当成新目录。映射失败或存在歧义时
    直接停止当前事务，让问题在本地暴露，避免悄悄突破新增 Skill 上限。
    """
    skills = [item for item in bundle.get("skill_library") or [] if isinstance(item, dict)]
    standardizations = [
        item
        for item in bundle.get("stage_01b_skill_standardizations") or []
        if isinstance(item, dict)
    ]
    requested = str(skill_id or "").strip()
    requested_alias = _normalized_skill_alias(requested)
    matches: list[dict[str, Any]] = []
    for index, skill in enumerate(skills):
        aliases = {
            str(skill.get("skill_id") or "").strip(),
            str(skill.get("title") or "").strip(),
        }
        if index < len(standardizations):
            aliases.add(str(standardizations[index].get("title") or "").strip())
        if requested in aliases or (requested_alias and requested_alias in {_normalized_skill_alias(v) for v in aliases}):
            matches.append(skill)
    if not matches:
        # Stage 6 会把同一聚类中的后续 add_new_skill 建议改写成对根新增
        # Skill 的 revise。该目标不在原始 bundle 中，但必须已经由前一事务提交，
        # 并且这里只接受精确目录 ID，绝不使用展示标题或模糊匹配创建路径。
        try:
            working_relative = normalize_patch_path(requested)
        except SystemExit:
            working_relative = Path()
        working_root = getattr(config, "output_skills_dir", None)
        working_skill_file = Path(working_root) / working_relative / "SKILL.md" if working_root else None
        if working_relative.parts and working_skill_file is not None and working_skill_file.is_file():
            return working_relative
    if len(matches) != 1:
        available = [str(item.get("skill_id") or "") for item in skills]
        reason = "ambiguous" if matches else "unknown"
        raise RuntimeError(
            f"Existing Skill target {requested!r} is {reason}; expected one of {available}. "
            "A revise_existing_skill action must resolve to exactly one source Skill."
        )
    raw_path = Path(str(matches[0].get("path") or ""))
    absolute = raw_path if raw_path.is_absolute() else config.root / raw_path
    try:
        return absolute.resolve().relative_to(config.skills_dir.resolve()).parent
    except ValueError as exc:
        raise RuntimeError(f"Existing Skill path is outside skills_dir: {absolute}") from exc


def _target_root(config: Any, bundle: dict[str, Any], suggestion: dict[str, Any]) -> Path:
    """返回当前建议允许修改的 skill 子目录。"""
    if suggestion.get("action") == "revise_existing_skill":
        skill_id = str(suggestion.get("target_skill_id") or "").strip()
        if not skill_id:
            raise RuntimeError(f"Suggestion {suggestion.get('suggestion_id')} has no target_skill_id")
        return _skill_relative_root(config, bundle, skill_id)
    new_skill_id = str(suggestion.get("new_skill_id") or "").strip()
    if not new_skill_id:
        raise RuntimeError(f"Suggestion {suggestion.get('suggestion_id')} has no new_skill_id")
    return normalize_patch_path(new_skill_id)


def _file_type(path: Path) -> str:
    """为 prompt 提供简洁的文件类型标签。"""
    if path.suffix.lower() in {".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".ps1", ".sql", ".css", ".html"}:
        return "code"
    if path.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv"}:
        return "document"
    return "others"


def _skill_word_limit(config: Any) -> int:
    """返回 skill 文档计数上限，和 Stage 6/CLI 使用同一配置。"""
    return max(100, min(20_000, int(getattr(config, "skill_word_limit", 1200) or 1200)))


def _count_skill_word_units(content: str) -> int:
    """按中文单字、英文/数字单词计算跨语言可解释的文档长度。"""
    return len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+", content))


def _skill_roots(root: Path) -> set[str]:
    """返回一个库中所有包含 SKILL.md 的相对 Skill 根目录。"""
    if not root.exists():
        return set()
    return {
        path.parent.resolve().relative_to(root.resolve()).as_posix()
        for path in root.rglob("SKILL.md")
        if path.is_file()
    }


def _max_new_skill_errors(config: Any, candidate_files: list[dict[str, Any]]) -> list[str]:
    """在调用 Review 前按物理目录检查最终新增 Skill 数量。

    Stage 6 限制的是 ``add_new_skill`` 动作数；这里再检查实际文件系统结果，防止
    错误的 revise 路径、标题别名或候选文件路径额外创建 Skill 目录。
    """
    limit = max(0, int(getattr(config, "max_new_skill_count", 0) or 0))
    if not limit:
        return []
    source_roots = _skill_roots(config.skills_dir)
    predicted_roots = _skill_roots(config.output_skills_dir)
    for item in candidate_files:
        relative = normalize_patch_path(str(item.get("path") or ""))
        if relative.name.casefold() == "skill.md":
            predicted_roots.add(relative.parent.as_posix())
    added_roots = sorted(predicted_roots - source_roots)
    if len(added_roots) <= limit:
        return []
    return [
        f"candidate would create {len(added_roots)} new Skill directories, exceeding max_new_skills {limit}: "
        + ", ".join(added_roots)
    ]


def _skill_length_audit(config: Any) -> dict[str, Any]:
    """审计工作副本中所有 SKILL.md 的长度。

    候选文件提交前已经会被本地校验；这里再对最终工作库做一次全量扫描，便于
    Web、manifest 和后续测试脚本确认整套修复后的 skill 文档都处在长度上限内。
    """
    limit = _skill_word_limit(config)
    rows: list[dict[str, Any]] = []
    if not config.output_skills_dir or not config.output_skills_dir.exists():
        return {"limit": limit, "ok": True, "skills": [], "oversized": []}
    for path in sorted(config.output_skills_dir.rglob("SKILL.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            units = None
            ok = False
        else:
            units = _count_skill_word_units(content)
            ok = units <= limit
        item = {
            "path": safe_rel(config.root, path),
            "word_units": units,
            "limit": limit,
            "ok": ok,
        }
        rows.append(item)
    return {
        "limit": limit,
        "ok": all(item.get("ok") for item in rows),
        "skills": rows,
        "oversized": [item for item in rows if not item.get("ok")],
    }


def _read_related_files(config: Any, bundle: dict[str, Any], suggestion: dict[str, Any]) -> list[dict[str, Any]]:
    """完整读取当前工作副本中与建议相关的文件原文。

    文本文件不截断。二进制文件不能由文本 LLM 安全重写，因此只提供存在性、大小
    和不可编辑说明；审查通过的候选也不允许用文本覆盖这些二进制文件。
    """
    root = _target_root(config, bundle, suggestion)
    skill_dir = (config.output_skills_dir / root).resolve()
    ensure_inside(config.output_skills_dir, skill_dir)
    if not skill_dir.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
        relative = path.resolve().relative_to(config.output_skills_dir.resolve()).as_posix()
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
            editable = True
        except UnicodeDecodeError:
            content = f"[binary file omitted; {len(raw)} bytes; this file must not be overwritten as text]"
            editable = False
        files.append(
            {
                "path": relative,
                "type": _file_type(path),
                "editable": editable,
                "content": content,
            }
        )
    return files


def _library_inventory(config: Any) -> list[str]:
    """列出工作副本中的 skill 目录，帮助新增 skill 避免命名冲突。"""
    if not config.output_skills_dir or not config.output_skills_dir.exists():
        return []
    return sorted(path.name for path in config.output_skills_dir.iterdir() if path.is_dir())


def _skill_summary_features(value: Any) -> set[str]:
    """提取用于相关摘要检索的轻量词项，兼容英文和中文。"""
    text = json.dumps(value, ensure_ascii=False).lower() if not isinstance(value, str) else value.lower()
    features = {token for token in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text)}
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(segment) == 1:
            features.add(segment)
        else:
            features.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return features


def _referenced_skill_ids(suggestion: dict[str, Any]) -> set[str]:
    """从 repair unit 及其 Stage 5 建议中收集显式 Skill 引用。"""
    identifiers: set[str] = set()
    singular_keys = {"skill_id", "target_skill_id"}
    plural_keys = {"skill_ids", "target_skill_ids", "suspected_skill_ids"}

    def visit(value: Any, key: str = "") -> None:
        if key in singular_keys and value not in (None, ""):
            identifiers.add(str(value).strip())
            return
        if key in plural_keys and isinstance(value, list):
            identifiers.update(str(item).strip() for item in value if str(item).strip())
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(suggestion)
    return {item for item in identifiers if item}


def _compact_skill_summary(item: dict[str, Any]) -> dict[str, Any]:
    """只保留审查冲突与重复所需的标准化 Skill 字段。"""
    return {
        "skill_id": item.get("skill_id") or item.get("id"),
        "title": item.get("title"),
        "intent": item.get("intent"),
        "triggers": item.get("triggers") or [],
        "inputs": item.get("inputs") or item.get("inputs_outputs") or [],
        "outputs": item.get("outputs") or [],
        "procedure": item.get("procedure") or item.get("procedure_summary") or [],
        "verification": item.get("verification") or item.get("verification_or_recovery") or [],
        "recovery": item.get("recovery") or item.get("verification_or_recovery") or [],
        "tools_or_templates": item.get("tools_or_templates") or [],
        "limits": item.get("limits") or item.get("ambiguities_or_limits") or [],
    }


def _related_skill_summaries(bundle: dict[str, Any], suggestion: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """选择与当前 repair unit 显式相关或语义词项重叠的 Skill 摘要。"""
    summaries = bundle.get("stage_01b_skill_standardizations") or []
    if not isinstance(summaries, list):
        return []
    referenced = _referenced_skill_ids(suggestion)
    query = _skill_summary_features(_unit_source_suggestions(suggestion))
    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for raw in summaries:
        if not isinstance(raw, dict):
            continue
        summary = _compact_skill_summary(raw)
        skill_id = str(summary.get("skill_id") or "").strip()
        direct = skill_id in referenced
        overlap = len(query.intersection(_skill_summary_features(summary)))
        if direct or overlap > 0:
            ranked.append((1 if direct else 0, overlap, skill_id, summary))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [summary for _direct, _overlap, _skill_id, summary in ranked[: max(1, limit)]]


def initialize_state(config: Any, bundle: dict[str, Any], stage5: dict[str, Any]) -> dict[str, Any]:
    """复制原 Skill library，并创建可断点续跑的 Stage 7 状态。"""
    existing = read_state(config)
    if existing is not None:
        state = _migrate_existing_state(config, existing, stage5)
        state["skill_length_audit"] = _skill_length_audit(config)
        write_state(config, state)
        return state
    if not config.output_skills_dir:
        raise RuntimeError("Stage 7 requires output_skills_dir")
    ensure_inside(config.root, config.skills_dir)
    ensure_inside(config.root, config.output_skills_dir)
    if config.skills_dir.resolve() == config.output_skills_dir.resolve():
        raise RuntimeError("output_skills_dir must differ from the source skills_dir")
    copy_tree(config.skills_dir, config.output_skills_dir, bool(config.force))
    prepared = prepare_repair_units(config, stage5)
    atomic_suggestion_count = sum(len(item.get("suggestion_ids") or []) for item in prepared)
    state = {
        "stage_type": "transactional_skill_repair",
        "stage_name": STAGE_NAME,
        "status": "completed" if not prepared else "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "source_skills_dir": safe_rel(config.root, config.skills_dir),
        "working_skills_dir": safe_rel(config.root, config.output_skills_dir),
        "repair_mode": _repair_mode(config),
        "skill_package_size": _skill_package_size(config),
        "suggestion_count": atomic_suggestion_count,
        "repair_unit_count": len(prepared),
        "accepted_count": 0,
        "current_suggestion_index": 0,
        "next_operation": None if not prepared else "repair",
        "active_interaction": None,
        "interaction_sequence": 0,
        "pause_reason": None,
        "suggestions": _state_rows_from_units(prepared),
        "interactions": [],
        "applied_changes": [],
        "skill_length_audit": _skill_length_audit(config),
    }
    write_state(config, state)
    _write_applied_manifest(config, state)
    return state


def _current_suggestion(state: dict[str, Any]) -> dict[str, Any] | None:
    """读取尚未完成的当前建议。"""
    index = int(state.get("current_suggestion_index") or 0)
    suggestions = state.get("suggestions") or []
    return suggestions[index] if 0 <= index < len(suggestions) else None


def _max_attempts_per_suggestion() -> int:
    """读取单个 repair unit 的最大 repair 尝试次数。

    该上限和 run-until-complete 的总 LLM 操作上限互补：总上限防止一次后台
    运行永久占用资源，单建议上限防止同一条建议在审查反复拒绝后阻塞后续建议。
    被跳过的建议不会提交任何候选文件。
    """
    raw = os.getenv("OFFLINE_SKILL_RCA_STAGE7_MAX_ATTEMPTS_PER_SUGGESTION") or os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_ATTEMPTS_PER_SUGGESTION") or "3"
    try:
        return max(1, min(50, int(raw)))
    except ValueError:
        return 3


def _advance_after_attempt_cap(state: dict[str, Any], suggestion: dict[str, Any], reason: str) -> None:
    """超过单建议尝试上限时跳过当前建议并推进到下一条。"""
    suggestion["status"] = "skipped_after_max_attempts"
    suggestion["skip_reason"] = reason
    suggestion["skipped_at"] = _now()
    state.setdefault("skipped_suggestions", []).append(
        {
            "suggestion_id": suggestion.get("suggestion_id"),
            "suggestion_ids": _unit_suggestion_ids(suggestion),
            "node_id": suggestion.get("node_id"),
            "action": suggestion.get("action"),
            "attempt_count": suggestion.get("attempt_count"),
            "reason": reason,
            "skipped_at": suggestion["skipped_at"],
        }
    )
    state["current_suggestion_index"] = int(state.get("current_suggestion_index") or 0) + 1
    if state["current_suggestion_index"] >= len(state.get("suggestions") or []):
        state["status"] = "completed"
        state["next_operation"] = None
        state["completed_at"] = _now()
    else:
        state["status"] = "running"
        state["next_operation"] = "repair"
    state["updated_at"] = _now()


def _advance_after_semantic_review(
    state: dict[str, Any],
    suggestion: dict[str, Any],
    attempt: dict[str, Any],
    decision: str,
    reason: str,
) -> None:
    """建议本身不成立时，不提交候选并直接推进到下一 repair unit。"""
    status = "rejected_suggestion"
    attempt["accepted"] = False
    attempt["status"] = status
    suggestion["status"] = status
    suggestion["semantic_review_reason"] = reason
    suggestion["reviewed_at"] = _now()
    state.setdefault("semantic_review_rejections", []).append(
        {
            "suggestion_id": suggestion.get("suggestion_id"),
            "suggestion_ids": _unit_suggestion_ids(suggestion),
            "node_id": suggestion.get("node_id"),
            "action": suggestion.get("action"),
            "decision": decision,
            "reason": reason,
            "reviewed_at": suggestion["reviewed_at"],
        }
    )
    state["current_suggestion_index"] = int(state.get("current_suggestion_index") or 0) + 1
    if state["current_suggestion_index"] >= len(state.get("suggestions") or []):
        state["status"] = "completed"
        state["next_operation"] = None
        state["completed_at"] = _now()
    else:
        state["status"] = "running"
        state["next_operation"] = "repair"
    state["updated_at"] = _now()


def _recover_interrupted_interaction(state: dict[str, Any]) -> None:
    """续跑前把上次被进程终止的调用标为 interrupted。"""
    active = state.get("active_interaction")
    if not isinstance(active, dict):
        return
    sequence = active.get("sequence")
    for item in state.get("interactions") or []:
        if item.get("sequence") == sequence and item.get("status") == "running":
            item["status"] = "interrupted"
            item["ended_at"] = _now()
            item["error"] = "The process stopped before this LLM interaction completed."
            break
    state["active_interaction"] = None
    state["status"] = "paused"
    state["pause_reason"] = "Recovered an interrupted LLM interaction; the same logical operation can be retried."


def next_operation(state: dict[str, Any]) -> str | None:
    """根据当前建议状态推导下一次应执行 repair 还是 review。"""
    if state.get("status") == "completed":
        return None
    suggestion = _current_suggestion(state)
    if not suggestion:
        return None
    attempts = suggestion.get("attempts") or []
    if not attempts:
        return "repair"
    latest = attempts[-1]
    if latest.get("status") == "candidate_ready":
        return "review"
    if latest.get("status") in {"rejected", "repair_error", "local_validation_failed"}:
        return "repair"
    if latest.get("status") == "review_error":
        return "review"
    return "repair"


def _interaction_name(state: dict[str, Any], suggestion: dict[str, Any], operation: str, attempt_number: int) -> str:
    """生成短而稳定、不会覆盖历史 transcript 的唯一名称。

    Repair LLM 可以返回很长的建议 ID。若把完整 ID 放入文件名，深层的
    ``repair-runs/<task>/<variant>/stage_07_interactions`` 目录在 Windows 上
    很容易超过路径限制。这里保留可读前缀，并追加原始 ID 的稳定哈希；完整
    suggestion_id 仍保存在 interaction JSON 中，因此不会损失审计信息。
    """
    sequence = int(state.get("interaction_sequence") or 0) + 1
    raw_id = str(suggestion.get("suggestion_id") or suggestion.get("index") or "suggestion")
    safe_id = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw_id).strip("-")
    safe_id = safe_id[:48].rstrip("-._") or "suggestion"
    id_hash = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:10]
    return (
        f"stage-08-{sequence:03d}-s{int(suggestion.get('index') or 0) + 1:02d}-"
        f"{safe_id}-{id_hash}-a{attempt_number:02d}-{operation}"
    )


def _latest_review_feedback(suggestion: dict[str, Any]) -> dict[str, Any] | None:
    """把最近一次语义审查或本地校验反馈提供给下一次 repair。"""
    for attempt in reversed(suggestion.get("attempts") or []):
        local_feedback = attempt.get("repair_feedback")
        if isinstance(local_feedback, dict):
            return local_feedback
        review = attempt.get("review_result")
        if isinstance(review, dict) and not attempt.get("accepted"):
            return review
    return None


def _unit_suggestion_ids(suggestion: dict[str, Any]) -> list[str]:
    """返回 repair unit 中的原子建议 id。"""
    values = suggestion.get("suggestion_ids") or [suggestion.get("suggestion_id")]
    return [str(value) for value in values if value]


def _unit_source_suggestions(suggestion: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 repair unit 中提供给 LLM 的完整 Stage 5 建议。"""
    values = suggestion.get("source_suggestions") or [suggestion.get("source_suggestion")]
    return [dict(value) for value in values if isinstance(value, dict)]


def _repair_payload(config: Any, bundle: dict[str, Any], suggestion: dict[str, Any]) -> dict[str, Any]:
    """构造一个 repair unit 的修复调用外部输入。"""
    return {
        "repair_unit_id": suggestion.get("repair_unit_id") or suggestion.get("suggestion_id"),
        "suggestion_ids": _unit_suggestion_ids(suggestion),
        "repair_action": suggestion.get("action"),
        "selected_stage6_suggestions": _unit_source_suggestions(suggestion),
        "allowed_skill_root": _target_root(config, bundle, suggestion).as_posix(),
        "current_related_files": _read_related_files(config, bundle, suggestion),
        "current_skill_library_inventory": _library_inventory(config),
        "previous_review_feedback": _latest_review_feedback(suggestion),
        "skill_word_limit": _skill_word_limit(config),
    }


def build_repair_prompt(
    config: Any,
    bundle: dict[str, Any],
    suggestion: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    """渲染下一次 repair prompt。"""
    from ..pipeline import fit_prompt

    template = render_prompt_template(
        REPAIR_TEMPLATE,
        {
            "stage8_repair_schema": json_block(repair_schema()),
            "claude_code_skill_spec": claude_code_skill_spec(),
        },
    )
    return fit_prompt(template, payload or _repair_payload(config, bundle, suggestion), config.max_prompt_chars)


def _candidate_archive_path(config: Any, suggestion: dict[str, Any], attempt_number: int) -> Path:
    """返回候选文件 JSON 的审计归档路径。"""
    safe_id = "".join(
        ch if ch.isalnum() or ch in "._-" else "-" for ch in str(suggestion.get("suggestion_id") or "suggestion")
    )
    return config.output_dir / "stage_08_candidates" / safe_id / f"attempt-{attempt_number:02d}.json"


def _validate_candidate(
    config: Any,
    bundle: dict[str, Any],
    suggestion: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """确定性检查候选文件路径和完整内容，不替代 LLM 语义审查。"""
    errors: list[str] = []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_root = _target_root(config, bundle, suggestion)
    expected_unit_id = str(suggestion.get("repair_unit_id") or suggestion.get("suggestion_id") or "")
    if str(candidate.get("repair_unit_id") or "") != expected_unit_id:
        errors.append(f"repair_unit_id must equal {expected_unit_id}")
    returned_ids = [str(value) for value in candidate.get("suggestion_ids") or []]
    if returned_ids != _unit_suggestion_ids(suggestion):
        errors.append("suggestion_ids must exactly match the repair unit in the provided order")
    files = candidate.get("files")
    if not isinstance(files, list):
        errors.append("files must be a JSON array")
        files = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            errors.append(f"files[{index}] is not an object")
            continue
        try:
            relative = normalize_patch_path(str(item.get("path") or ""))
        except SystemExit as exc:
            errors.append(str(exc))
            continue
        if not relative.parts:
            errors.append(f"files[{index}].path is empty")
            continue
        try:
            relative.relative_to(allowed_root)
        except ValueError:
            errors.append(f"{relative.as_posix()} is outside allowed_skill_root {allowed_root.as_posix()}")
            continue
        path_key = relative.as_posix()
        if path_key in seen:
            errors.append(f"duplicate candidate path: {path_key}")
            continue
        seen.add(path_key)
        content = item.get("content")
        if not isinstance(content, str):
            errors.append(f"{path_key} has no complete string content")
            continue
        if not content.strip():
            errors.append(f"{path_key} has empty content")
            continue
        incomplete_reason = _incomplete_content_reason(content)
        if incomplete_reason:
            errors.append(f"{path_key} is not a complete final file: {incomplete_reason}")
            continue
        existing = config.output_skills_dir / relative
        if existing.exists():
            try:
                existing.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                errors.append(f"{path_key} is binary and cannot be overwritten by a text response")
                continue
        normalized.append(
            {
                "path": path_key,
                "content": content,
            }
        )
        if relative.name.lower() == "skill.md":
            if relative.name != "SKILL.md":
                errors.append(f"{path_key} must use the exact entrypoint filename SKILL.md")
            errors.extend(_claude_code_skill_errors(relative, content))
            word_units = _count_skill_word_units(content)
            if word_units > _skill_word_limit(config):
                errors.append(
                    f"{path_key} has {word_units} counted word units, exceeding skill_word_limit "
                    f"{_skill_word_limit(config)}"
                )
    if not normalized:
        errors.append("candidate contains no writable files")
    if suggestion.get("action") == "add_new_skill":
        required = (allowed_root / "SKILL.md").as_posix()
        if required not in seen:
            errors.append(f"new skill candidate must include {required}")
    errors.extend(_max_new_skill_errors(config, normalized))
    return normalized, errors


def _incomplete_content_reason(content: str) -> str | None:
    """识别明确的省略占位符或 diff，而不对文件语义做主观判断。"""
    lowered = content.strip().lower()
    if lowered.startswith("diff --git ") or lowered.startswith("*** begin patch"):
        return "a diff/patch was returned instead of final content"
    markers = [
        r"\.\.\.\[truncated \d+ chars\]",
        r"\[(?:content|rest|remainder) omitted\]",
        r"<(?:complete|full) file content>",
        r"(?:rest|remaining content) (?:is )?(?:unchanged|omitted)",
    ]
    for marker in markers:
        if re.search(marker, lowered):
            return "an omission or truncation marker is present"
    return None


def _claude_code_skill_errors(relative: Path, content: str) -> list[str]:
    """确定性校验 Agent Skills 开放标准与 Claude Code 的共同格式要求。

    触发语义是否足够准确仍由 Review LLM 判断；这里仅处理可可靠程序化判断的
    入口文件、YAML、命名、长度和正文完整性，失败时直接退回 Repair。
    """
    path_key = relative.as_posix()
    errors: list[str] = []
    if content.startswith("\ufeff"):
        errors.append(f"{path_key} must not start with a UTF-8 BOM before YAML frontmatter")
        content = content.lstrip("\ufeff")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return [f"{path_key} must start with YAML frontmatter delimited by ---"]
    closing_index = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing_index is None:
        return [f"{path_key} YAML frontmatter has no closing --- delimiter"]
    frontmatter_text = "\n".join(lines[1:closing_index])
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        return [f"{path_key} has invalid YAML frontmatter: {exc}"]
    if not isinstance(frontmatter, dict):
        return [f"{path_key} YAML frontmatter must be a mapping"]

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{path_key} frontmatter.name must be a non-empty string")
    else:
        name = name.strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
            errors.append(
                f"{path_key} frontmatter.name must be 1-64 lowercase letters, digits, or single hyphens"
            )
        if name != relative.parent.name:
            errors.append(
                f"{path_key} frontmatter.name must match its parent Skill directory "
                f"({relative.parent.name})"
            )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{path_key} frontmatter.description must be a non-empty string")
    elif len(description.strip()) > 1024:
        errors.append(f"{path_key} frontmatter.description exceeds 1024 characters")

    compatibility = frontmatter.get("compatibility")
    if compatibility is not None and (not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500):
        errors.append(f"{path_key} frontmatter.compatibility must be a 1-500 character string")
    metadata = frontmatter.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items())
    ):
        errors.append(f"{path_key} frontmatter.metadata must map strings to strings")
    for field in ("disable-model-invocation", "user-invocable"):
        if field in frontmatter and not isinstance(frontmatter[field], bool):
            errors.append(f"{path_key} frontmatter.{field} must be a boolean")

    body_lines = lines[closing_index + 1 :]
    if not "\n".join(body_lines).strip():
        errors.append(f"{path_key} must contain a non-empty Markdown body after frontmatter")
    if len(lines) > 500:
        errors.append(f"{path_key} has {len(lines)} lines; Claude Code Skills must stay at or below 500 lines")
    return errors


def _review_payload(
    config: Any,
    bundle: dict[str, Any],
    suggestion: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    """构造 review 输入，同时提供审查建议正确性所需的最小证据上下文。"""
    archive = Path(str(attempt.get("candidate_archive") or ""))
    if not archive.is_absolute():
        archive = config.root / archive
    candidate = json.loads(archive.read_text(encoding="utf-8")) if archive.exists() else {}
    semantic_context = _review_semantic_context(config, bundle, suggestion)
    return {
        "repair_unit_id": suggestion.get("repair_unit_id") or suggestion.get("suggestion_id"),
        "suggestion_ids": _unit_suggestion_ids(suggestion),
        "selected_stage6_suggestions": _unit_source_suggestions(suggestion),
        "files_before_this_attempt": attempt.get("files_before") or _read_related_files(config, bundle, suggestion),
        "candidate_modified_files": candidate.get("files") or [],
        "current_skill_library_inventory": _library_inventory(config),
        "related_skill_summaries": _related_skill_summaries(bundle, suggestion),
        "capability_nodes": semantic_context["capability_nodes"],
        "suggestion_evidence": semantic_context["suggestion_evidence"],
        "node_execution_context": semantic_context["node_execution_context"],
        "coverage_context": semantic_context["coverage_context"],
        "skill_word_limit": _skill_word_limit(config),
    }


def _review_semantic_context(
    config: Any,
    bundle: dict[str, Any],
    suggestion: dict[str, Any],
) -> dict[str, Any]:
    """从已完成 stages 中解析建议证据，避免 Review 只能机械检查是否照做。

    这里只读取能力图、agent-only failure/cause events、事件对齐、节点执行判断和
    node-Skill coverage。不会读取 verifier 目录、verifier 字段或任何验证器输出。
    """
    # 完整流水线会直接传入前序阶段的内存结果；Stage Debug 与断点恢复则可以使用
    # 已落盘的聚合文件。优先使用内存结果，避免读取上一次运行遗留的旧文件。
    outputs = bundle.get("review_stage_outputs")
    if not isinstance(outputs, dict):
        outputs_path = config.output_dir / "stage_outputs.json"
        try:
            outputs = json.loads(outputs_path.read_text(encoding="utf-8")) if outputs_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            outputs = {}
    if not isinstance(outputs, dict):
        outputs = {}

    node_ids = {str(value) for value in (suggestion.get("node_ids") or [suggestion.get("node_id")]) if value}
    stage2 = outputs.get("stage_02_capability_graph") or {}
    graph = stage2.get("capability_graph") if isinstance(stage2, dict) else {}
    capability_nodes = [
        node for node in (graph or {}).get("nodes") or []
        if isinstance(node, dict) and str(node.get("node_id") or "") in node_ids
    ]

    evidence_refs: set[tuple[str, str]] = set()
    for source in _unit_source_suggestions(suggestion):
        for ref in source.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            traj_id = str(ref.get("traj_id") or "")
            event_id = str(ref.get("event_id") or "")
            if event_id:
                evidence_refs.add((traj_id, event_id))

    suggestion_evidence: list[dict[str, Any]] = []
    stage3 = outputs.get("stage_03_failure_events_by_trace") or []
    for trace in stage3 if isinstance(stage3, list) else []:
        if not isinstance(trace, dict):
            continue
        traj_id = str(trace.get("traj_id") or "")
        for kind, key in (("failure", "failure_events"), ("cause", "cause_events")):
            for event in trace.get(key) or []:
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id") or "")
                if (traj_id, event_id) in evidence_refs or ("", event_id) in evidence_refs:
                    suggestion_evidence.append(
                        {"traj_id": traj_id, "event_kind": kind, **event}
                    )

    alignments = (outputs.get("stage_04_failure_event_alignment") or {}).get("alignments") or []
    alignment_context = [
        row for row in alignments
        if isinstance(row, dict)
        and (str(row.get("traj_id") or ""), str(row.get("event_id") or "")) in evidence_refs
    ]

    evidence_traj_ids = {traj_id for traj_id, _event_id in evidence_refs if traj_id}
    stage5 = outputs.get("stage_05_node_execution_assessments") or []
    node_execution_context = []
    for trace in stage5 if isinstance(stage5, list) else []:
        if not isinstance(trace, dict):
            continue
        traj_id = str(trace.get("traj_id") or "")
        if evidence_traj_ids and traj_id not in evidence_traj_ids:
            continue
        assessments = [
            item for item in trace.get("node_assessments") or []
            if isinstance(item, dict) and str(item.get("node_id") or "") in node_ids
        ]
        if assessments:
            node_execution_context.append(
                {"traj_id": traj_id, "task_success": trace.get("success"), "node_assessments": assessments}
            )

    target_skill_ids = _referenced_skill_ids(suggestion)
    coverage_rows = stage2.get("coverage_pairs") or stage2.get("skill_coverage_matrix") or []
    coverage_context = [
        row for row in coverage_rows
        if isinstance(row, dict)
        and str(row.get("node_id") or "") in node_ids
        and (not target_skill_ids or str(row.get("skill_id") or "") in target_skill_ids)
    ]
    return {
        "capability_nodes": capability_nodes,
        "suggestion_evidence": {
            "events": suggestion_evidence,
            "event_node_alignments": alignment_context,
        },
        "node_execution_context": node_execution_context,
        "coverage_context": coverage_context,
    }


def build_review_prompt(
    config: Any,
    bundle: dict[str, Any],
    suggestion: dict[str, Any],
    attempt: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    """渲染候选文件审查 prompt。"""
    from ..pipeline import fit_prompt

    template = render_prompt_template(
        REVIEW_TEMPLATE,
        {
            "stage8_review_schema": json_block(review_schema()),
            "claude_code_skill_spec": claude_code_skill_spec(),
        },
    )
    return fit_prompt(template, payload or _review_payload(config, bundle, suggestion, attempt), config.max_prompt_chars)


def preview_next_prompt(config: Any, bundle: dict[str, Any], stage5: dict[str, Any]) -> tuple[str | None, str | None]:
    """初始化工作副本并生成下一次调用 prompt，但不推进状态机。"""
    state = initialize_state(config, bundle, stage5)
    _recover_interrupted_interaction(state)
    suggestion = _current_suggestion(state)
    operation = next_operation(state)
    if not suggestion or not operation:
        write_state(config, state)
        return None, None
    if operation == "repair":
        payload = _repair_payload(config, bundle, suggestion)
        prompt = build_repair_prompt(config, bundle, suggestion, payload)
        attempt_number = int(suggestion.get("attempt_count") or 0) + 1
    else:
        attempt = (suggestion.get("attempts") or [])[-1]
        payload = _review_payload(config, bundle, suggestion, attempt)
        prompt = build_review_prompt(config, bundle, suggestion, attempt, payload)
        attempt_number = int(attempt.get("attempt_number") or suggestion.get("attempt_count") or 1)
    name = _interaction_name(state, suggestion, operation, attempt_number) + "-preview"
    evidence_path = config.output_dir / "stage_08_interactions" / "next-operation.evidence.json"
    write_json(evidence_path, payload)
    state["next_evidence_archive"] = safe_rel(config.root, evidence_path)
    write_prompt_file(config, name, prompt)
    from ..pipeline import sanitize

    prompt_path = config.output_dir / "prompts" / f"{sanitize(name)}.prompt.txt"
    state["next_prompt_archive"] = safe_rel(config.root, prompt_path)
    state["next_preview_operation"] = operation
    state["next_operation"] = operation
    state["updated_at"] = _now()
    write_state(config, state)
    return name, prompt


def _start_interaction(
    config: Any,
    state: dict[str, Any],
    suggestion: dict[str, Any],
    operation: str,
    attempt_number: int,
    prompt: str,
    evidence_payload: dict[str, Any],
) -> dict[str, Any]:
    """在调用 LLM 前持久化 interaction，保证强制暂停也有完整 request 线索。"""
    name = _interaction_name(state, suggestion, operation, attempt_number)
    state["interaction_sequence"] = int(state.get("interaction_sequence") or 0) + 1
    interaction = {
        "sequence": state["interaction_sequence"],
        "name": name,
        "suggestion_index": suggestion.get("index"),
        "suggestion_id": suggestion.get("suggestion_id"),
        "repair_unit_id": suggestion.get("repair_unit_id") or suggestion.get("suggestion_id"),
        "suggestion_ids": _unit_suggestion_ids(suggestion),
        "node_id": suggestion.get("node_id"),
        "node_ids": suggestion.get("node_ids") or [],
        "action": suggestion.get("action"),
        "operation": operation,
        "attempt_number": attempt_number,
        "status": "running",
        "started_at": _now(),
        "ended_at": None,
        "template": REPAIR_TEMPLATE if operation == "repair" else REVIEW_TEMPLATE,
    }
    evidence_path = config.output_dir / "stage_08_interactions" / f"{name}.evidence.json"
    write_json(evidence_path, evidence_payload)
    interaction["evidence_archive"] = safe_rel(config.root, evidence_path)
    state.setdefault("interactions", []).append(interaction)
    state["active_interaction"] = {
        "sequence": interaction["sequence"],
        "name": name,
        "operation": operation,
    }
    state["status"] = "running"
    state["pause_reason"] = None
    state["next_operation"] = operation
    state["updated_at"] = _now()
    write_state(config, state)
    # run_llm_stage 也会写 prompt；这里提前写一次，使进程在网络请求期间被暂停时，
    # debug 页面仍能看到本轮实际输入。
    write_prompt_file(config, name, prompt)
    return interaction


def _finish_interaction(config: Any, state: dict[str, Any], interaction: dict[str, Any], status: str, **extra: Any) -> None:
    """结束 interaction 并立即保存状态。"""
    interaction["status"] = status
    interaction["ended_at"] = _now()
    interaction.update(extra)
    state["active_interaction"] = None
    state["updated_at"] = _now()
    write_state(config, state)


def _run_repair_operation(config: Any, bundle: dict[str, Any], state: dict[str, Any], suggestion: dict[str, Any]) -> None:
    """执行一次候选文件生成调用；结果只写审计目录，不修改工作库。"""
    attempt_number = int(suggestion.get("attempt_count") or 0) + 1
    files_before = _read_related_files(config, bundle, suggestion)
    payload = _repair_payload(config, bundle, suggestion)
    prompt = build_repair_prompt(config, bundle, suggestion, payload)
    interaction = _start_interaction(config, state, suggestion, "repair", attempt_number, prompt, payload)
    attempt = {
        "attempt_number": attempt_number,
        "status": "repair_running",
        "files_before": files_before,
        "repair_interaction": interaction.get("name"),
        "candidate_archive": None,
        "local_validation_errors": [],
        "review_result": None,
        "accepted": False,
    }
    suggestion.setdefault("attempts", []).append(attempt)
    suggestion["attempt_count"] = attempt_number
    write_state(config, state)
    try:
        result = run_llm_stage(config, interaction["name"], prompt, max_tokens=int(os.getenv("OFFLINE_SKILL_RCA_STAGE7_REPAIR_MAX_TOKENS") or os.getenv("OFFLINE_SKILL_RCA_STAGE6_REPAIR_MAX_TOKENS") or 24_000))
        normalized_files, validation_errors = _validate_candidate(config, bundle, suggestion, result)
        candidate = {
            "repair_unit_id": suggestion.get("repair_unit_id") or suggestion.get("suggestion_id"),
            "suggestion_ids": _unit_suggestion_ids(suggestion),
            "action": suggestion.get("action"),
            "files": normalized_files,
            "raw_response": result,
            "local_validation_errors": validation_errors,
        }
        archive = _candidate_archive_path(config, suggestion, attempt_number)
        write_json(archive, candidate)
        attempt["candidate_archive"] = safe_rel(config.root, archive)
        attempt["local_validation_errors"] = validation_errors
        if validation_errors:
            # 路径、标识、完整性和字数均可由代码确定。失败时直接把具体错误反馈
            # 给下一轮 Repair，避免浪费一次 Review LLM 调用。
            attempt["status"] = "local_validation_failed"
            attempt["repair_feedback"] = {
                "source": "local_validation",
                "issues": [
                    {"code": "local_validation", "message": message}
                    for message in validation_errors
                ],
                "retry_instructions": validation_errors,
            }
            suggestion["status"] = "pending"
            state["next_operation"] = "repair"
        else:
            attempt["status"] = "candidate_ready"
            suggestion["status"] = "awaiting_review"
            state["next_operation"] = "review"
        _finish_interaction(
            config,
            state,
            interaction,
            "done",
            candidate_archive=attempt["candidate_archive"],
            local_validation_errors=validation_errors,
            next_operation=state["next_operation"],
        )
    except Exception as exc:
        attempt["status"] = "repair_error"
        attempt["error"] = str(exc)
        suggestion["status"] = "pending"
        state["next_operation"] = "repair"
        _finish_interaction(config, state, interaction, "error", error=str(exc))
        raise


def _apply_candidate_transactionally(
    config: Any,
    candidate_files: list[dict[str, Any]],
) -> None:
    """通过整库目录交换原子提交候选文件，并在交换失败时恢复旧工作库。"""
    working = config.output_skills_dir.resolve()
    ensure_inside(config.root, working)
    token = uuid.uuid4().hex
    temporary = working.with_name(f"{working.name}.stage8-next-{token}")
    backup = working.with_name(f"{working.name}.stage8-backup-{token}")
    shutil.copytree(working, temporary)
    try:
        for item in candidate_files:
            relative = normalize_patch_path(str(item.get("path") or ""))
            target = (temporary / relative).resolve()
            ensure_inside(temporary, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(item.get("content") or "").rstrip() + "\n", encoding="utf-8")
        working.rename(backup)
        try:
            temporary.rename(working)
        except Exception:
            backup.rename(working)
            raise
        shutil.rmtree(backup)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and not working.exists():
            backup.rename(working)
        raise


def _advance_after_accept(config: Any, state: dict[str, Any], suggestion: dict[str, Any], attempt: dict[str, Any]) -> None:
    """提交通过审查的候选并移动到下一条建议。"""
    archive = Path(str(attempt.get("candidate_archive") or ""))
    if not archive.is_absolute():
        archive = config.root / archive
    candidate = json.loads(archive.read_text(encoding="utf-8"))
    _apply_candidate_transactionally(config, candidate.get("files") or [])
    attempt["accepted"] = True
    attempt["status"] = "accepted"
    attempt["committed_at"] = _now()
    suggestion["status"] = "accepted"
    state["accepted_count"] = int(state.get("accepted_count") or 0) + len(_unit_suggestion_ids(suggestion))
    state.setdefault("applied_changes", []).append(
        {
            "suggestion_id": suggestion.get("suggestion_id"),
            "suggestion_ids": _unit_suggestion_ids(suggestion),
            "repair_unit_mode": suggestion.get("repair_unit_mode") or "single_suggestion",
            "node_id": suggestion.get("node_id"),
            "action": suggestion.get("action"),
            "attempt_number": attempt.get("attempt_number"),
            "files": [item.get("path") for item in candidate.get("files") or []],
            "committed_at": attempt["committed_at"],
        }
    )
    state["current_suggestion_index"] = int(state.get("current_suggestion_index") or 0) + 1
    if state["current_suggestion_index"] >= len(state.get("suggestions") or []):
        state["status"] = "completed"
        state["next_operation"] = None
        state["completed_at"] = _now()
    else:
        state["status"] = "running"
        state["next_operation"] = "repair"
    state["skill_length_audit"] = _skill_length_audit(config)


def _run_review_operation(config: Any, bundle: dict[str, Any], state: dict[str, Any], suggestion: dict[str, Any]) -> None:
    """执行一次候选审查；通过才提交，拒绝则保持工作库并等待重试。"""
    attempt = (suggestion.get("attempts") or [])[-1]
    if attempt.get("local_validation_errors"):
        # 防御旧状态或手工编辑后的异常：本地失败绝不能进入 Review LLM。
        attempt["status"] = "local_validation_failed"
        attempt["repair_feedback"] = {
            "source": "local_validation",
            "issues": [
                {"code": "local_validation", "message": message}
                for message in attempt.get("local_validation_errors") or []
            ],
            "retry_instructions": list(attempt.get("local_validation_errors") or []),
        }
        suggestion["status"] = "pending"
        state["next_operation"] = "repair"
        write_state(config, state)
        return
    attempt_number = int(attempt.get("attempt_number") or suggestion.get("attempt_count") or 1)
    payload = _review_payload(config, bundle, suggestion, attempt)
    prompt = build_review_prompt(config, bundle, suggestion, attempt, payload)
    interaction = _start_interaction(config, state, suggestion, "review", attempt_number, prompt, payload)
    attempt["review_interactions"] = [*(attempt.get("review_interactions") or []), interaction.get("name")]
    try:
        result = run_llm_stage(
            config,
            interaction["name"],
            prompt,
            max_tokens=int(os.getenv("OFFLINE_SKILL_RCA_STAGE7_REVIEW_MAX_TOKENS") or os.getenv("OFFLINE_SKILL_RCA_STAGE6_REVIEW_MAX_TOKENS") or 8_000),
            llm_role="review",
        )
        checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
        if not checks:
            # 旧 Review 响应兼容读取；新版 prompt 不再生成这些平铺字段。
            checks = {
                "suggestion_evidence_supported": result.get("suggestion_evidence_supported", True),
                "suggestion_capability_correct": result.get("suggestion_capability_correct", True),
                "suggestion_reusable_skill_scope": result.get("suggestion_reusable_skill_scope", True),
                "candidate_satisfies_valid_suggestions": result.get("suggestion_satisfied"),
                "scope_preserved": result.get("scope_preserved"),
                "files_usable": result.get("files_complete_and_usable"),
                "claude_code_compatible": result.get("claude_code_compatible", True),
                "library_consistent": result.get("no_library_conflict_or_duplication"),
            }
        response_shape_errors: list[str] = []
        if not isinstance(result.get("issues"), list):
            response_shape_errors.append("issues must be a JSON array")
        if not isinstance(result.get("retry_instructions"), list):
            response_shape_errors.append("retry_instructions must be a JSON array")
        issues = result.get("issues") if isinstance(result.get("issues"), list) else []
        retry_instructions = result.get("retry_instructions") if isinstance(result.get("retry_instructions"), list) else []
        decision = str(result.get("decision") or "").strip().lower()
        if decision == "reject":
            decision = "reject_candidate"
        if decision not in {"accept", "reject_candidate", "reject_suggestion"}:
            response_shape_errors.append("decision must be accept, reject_candidate, or reject_suggestion")
            decision = "reject_candidate"
        required_check_names = {
            "suggestion_evidence_supported",
            "suggestion_capability_correct",
            "suggestion_reusable_skill_scope",
            "candidate_satisfies_valid_suggestions",
            "scope_preserved",
            "files_usable",
            "claude_code_compatible",
            "library_consistent",
        }
        missing_checks = sorted(required_check_names.difference(checks))
        if missing_checks:
            response_shape_errors.append(f"checks missing required fields: {', '.join(missing_checks)}")
        llm_accept = (
            decision == "accept"
            and checks.get("suggestion_evidence_supported") is True
            and checks.get("suggestion_capability_correct") is True
            and checks.get("suggestion_reusable_skill_scope") is True
            and checks.get("candidate_satisfies_valid_suggestions") is True
            and checks.get("scope_preserved") is True
            and checks.get("files_usable") is True
            and checks.get("claude_code_compatible") is True
            and checks.get("library_consistent") is True
            and not issues
            and not retry_instructions
            and not response_shape_errors
        )
        expected_unit_id = str(suggestion.get("repair_unit_id") or suggestion.get("suggestion_id") or "")
        identity_errors = []
        if str(result.get("repair_unit_id") or "") != expected_unit_id:
            identity_errors.append(f"repair_unit_id must equal {expected_unit_id}")
        if [str(value) for value in result.get("suggestion_ids") or []] != _unit_suggestion_ids(suggestion):
            identity_errors.append("suggestion_ids must exactly match the reviewed repair unit")
        if identity_errors:
            llm_accept = False
        semantic_rejection = (
            decision == "reject_suggestion"
            and not identity_errors
            and not response_shape_errors
            and bool(issues)
            and not retry_instructions
            and (
                checks.get("suggestion_evidence_supported") is False
                or checks.get("suggestion_capability_correct") is False
                or checks.get("suggestion_reusable_skill_scope") is False
            )
        )
        accepted = llm_accept
        review_result = dict(result)
        review_result["checks"] = checks
        review_result["issues"] = list(issues)
        review_result["retry_instructions"] = list(retry_instructions)
        review_result["llm_accepted"] = llm_accept
        review_result["accepted_after_local_validation"] = accepted
        review_result["normalized_decision"] = decision
        review_result["semantic_rejection"] = semantic_rejection
        if identity_errors:
            review_result.setdefault("issues", []).extend(
                {"code": "response_identity", "message": message}
                for message in identity_errors
            )
            review_result.setdefault("retry_instructions", []).append("Return the exact repair_unit_id and suggestion_ids.")
        if response_shape_errors:
            review_result["issues"].extend(
                {"code": "response_schema", "message": message}
                for message in response_shape_errors
            )
            review_result["retry_instructions"].append("Return every required Review schema field with the correct JSON type.")
        attempt["review_result"] = review_result
        if accepted:
            _advance_after_accept(config, state, suggestion, attempt)
        elif semantic_rejection:
            reason = "; ".join(
                str(item.get("message") or item.get("code") or item)
                if isinstance(item, dict) else str(item)
                for item in issues
            ) or "Review found the repair suggestion unsupported or unsafe."
            _advance_after_semantic_review(state, suggestion, attempt, decision, reason)
        else:
            attempt["accepted"] = False
            attempt["status"] = "rejected"
            suggestion["status"] = "pending"
            state["status"] = "running"
            state["next_operation"] = "repair"
        _finish_interaction(
            config,
            state,
            interaction,
            "done",
            review_decision="accept" if accepted else decision or "reject_candidate",
        )
        _write_applied_manifest(config, state)
    except Exception as exc:
        attempt["status"] = "review_error"
        attempt["error"] = str(exc)
        suggestion["status"] = "awaiting_review"
        state["next_operation"] = "review"
        _finish_interaction(config, state, interaction, "error", error=str(exc))
        raise


def run_step(config: Any, bundle: dict[str, Any], stage5: dict[str, Any]) -> dict[str, Any]:
    """只执行一个 repair 或 review 调用，供 Web 单步调试。"""
    state = initialize_state(config, bundle, stage5)
    _recover_interrupted_interaction(state)
    suggestion = _current_suggestion(state)
    operation = next_operation(state)
    if not suggestion or not operation:
        state["status"] = "completed"
        state["next_operation"] = None
        write_state(config, state)
        _write_applied_manifest(config, state)
        return state
    if operation == "repair" and int(suggestion.get("attempt_count") or 0) >= _max_attempts_per_suggestion():
        reason = (
            f"Skipped after {suggestion.get('attempt_count')} repair attempt(s) without an accepted review. "
            "No candidate from this suggestion was applied."
        )
        _advance_after_attempt_cap(state, suggestion, reason)
        _write_applied_manifest(config, state)
        write_state(config, state)
        return state
    if operation == "repair":
        _run_repair_operation(config, bundle, state, suggestion)
    else:
        _run_review_operation(config, bundle, state, suggestion)
    # 为 debug 页预先整理下一次调用的占位符数据。这个步骤只读取已经提交的工作
    # 库和候选归档，不会推进状态机或调用 LLM。
    next_suggestion = _current_suggestion(state)
    following_operation = next_operation(state)
    if next_suggestion and following_operation:
        if following_operation == "repair":
            next_payload = _repair_payload(config, bundle, next_suggestion)
        else:
            next_attempt = (next_suggestion.get("attempts") or [])[-1]
            next_payload = _review_payload(config, bundle, next_suggestion, next_attempt)
        evidence_path = config.output_dir / "stage_08_interactions" / "next-operation.evidence.json"
        write_json(evidence_path, next_payload)
        state["next_evidence_archive"] = safe_rel(config.root, evidence_path)
    else:
        state["next_evidence_archive"] = None
    # 状态推进后，之前生成的 preview prompt 已不再代表下一次调用。用户再次点击
    # “生成 Prompt”时会写入新的路径和 operation。
    state["next_prompt_archive"] = None
    state["next_preview_operation"] = None
    state["updated_at"] = _now()
    write_state(config, state)
    return state


def run_until_complete(
    config: Any,
    bundle: dict[str, Any],
    stage5: dict[str, Any],
    max_operations: int | None = None,
) -> dict[str, Any]:
    """连续执行 Stage 7，直到全部通过或达到单进程安全步数上限。

    安全上限只用于避免异常模型无限拒绝导致单个后台进程永久占用资源；达到上限时
    状态变为 paused，再次点击“运行至完成”即可从原位置继续，不会丢失进度。
    """
    state = initialize_state(config, bundle, stage5)
    configured_limit = (
        max_operations
        or getattr(config, "stage7_max_operations", None)
        or os.getenv("OFFLINE_SKILL_RCA_STAGE7_MAX_OPERATIONS")
        or os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_OPERATIONS")
        # 兼容早期只通过环境变量设置隐藏上限的运行脚本。
        or os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_STEPS_PER_RUN")
        or 30
    )
    operation_limit = max(1, min(1000, int(configured_limit)))
    run_record = {
        "started_at": _now(),
        "ended_at": None,
        "max_operations": operation_limit,
        "operations_executed": 0,
        "limit_reached": False,
        "completed": state.get("status") == "completed",
    }
    state["last_until_complete_run"] = run_record
    write_state(config, state)

    for _ in range(operation_limit):
        if state.get("status") == "completed":
            run_record["ended_at"] = _now()
            run_record["completed"] = True
            write_state(config, state)
            return state
        state = run_step(config, bundle, stage5)
        run_record = state.setdefault("last_until_complete_run", run_record)
        run_record["operations_executed"] = int(run_record.get("operations_executed") or 0) + 1
        if state.get("status") == "completed":
            run_record["ended_at"] = _now()
            run_record["completed"] = True
            run_record["limit_reached"] = False
            write_state(config, state)
            return state
        # run_step 自身已经保存了事务状态；这里再保存本次连续运行计数，保证进程
        # 被暂停或异常终止时，Debug 页仍能看到准确的已执行次数。
        write_state(config, state)

    state["status"] = "paused"
    run_record = state.setdefault("last_until_complete_run", run_record)
    run_record["ended_at"] = _now()
    run_record["completed"] = False
    run_record["limit_reached"] = True
    state["pause_reason"] = (
        f"Reached the configured run-until-complete limit of {operation_limit} LLM operations "
        "before all skill suggestions passed review."
    )
    state["updated_at"] = _now()
    write_state(config, state)
    return state


def run(
    config: Any,
    bundle: dict[str, Any],
    stage5: dict[str, Any],
    mode: str = "until-complete",
    max_operations: int | None = None,
) -> dict[str, Any]:
    """Stage 7 公共入口；debug 用 step，完整 pipeline 用 until-complete。"""
    if mode == "step":
        return run_step(config, bundle, stage5)
    if mode == "until-complete":
        return run_until_complete(config, bundle, stage5, max_operations=max_operations)
    raise ValueError(f"Unknown Stage 7 run mode: {mode}")


def pause(config: Any, reason: str = "Paused from the Web debug page.") -> dict[str, Any]:
    """把 Stage 7 标为 paused，并保留被终止 interaction 的审计记录。"""
    state = read_state(config)
    if state is None:
        raise RuntimeError("Stage 7 has not been initialized")
    _recover_interrupted_interaction(state)
    state["status"] = "paused"
    state["pause_reason"] = reason
    state["updated_at"] = _now()
    write_state(config, state)
    return state


def _write_applied_manifest(config: Any, state: dict[str, Any]) -> None:
    """持续更新可测试 skill library 的应用清单。"""
    state["skill_length_audit"] = _skill_length_audit(config)
    write_json(
        config.output_dir / "applied_repair_manifest.json",
        {
            "sourceSkillsDir": state.get("source_skills_dir"),
            "outputSkillsDir": state.get("working_skills_dir"),
            "status": state.get("status"),
            "repairMode": state.get("repair_mode"),
            "skillPackageSize": state.get("skill_package_size"),
            "suggestionCount": state.get("suggestion_count"),
            "repairUnitCount": state.get("repair_unit_count"),
            "acceptedCount": state.get("accepted_count"),
            "skillLengthAudit": state.get("skill_length_audit"),
            "applied": state.get("applied_changes") or [],
        },
    )


def final_analysis(state: dict[str, Any]) -> dict[str, Any]:
    """把事务状态投影成现有报告/导出代码可消费的精简最终结果。"""
    patch_plan = []
    drafts = []
    for suggestion in state.get("suggestions") or []:
        accepted_attempt = next(
            (attempt for attempt in reversed(suggestion.get("attempts") or []) if attempt.get("accepted")),
            None,
        )
        if not accepted_attempt:
            continue
        sources = _unit_source_suggestions(suggestion)
        member_ids = _unit_suggestion_ids(suggestion)
        for index, patch_id in enumerate(member_ids):
            source = sources[index] if index < len(sources) else {}
            patch_plan.append(
                {
                    "patch_id": patch_id,
                    "repair_unit_id": suggestion.get("repair_unit_id") or suggestion.get("suggestion_id"),
                    "action": source.get("action") or suggestion.get("action"),
                    "target_skill_id": source.get("target_skill_id") or suggestion.get("target_skill_id"),
                    "new_skill_id": source.get("new_skill_id") or suggestion.get("new_skill_id"),
                    "linked_node_ids": [source.get("node_id")] if source.get("node_id") else [],
                    "problem_summary": source.get("problem_diagnosis") or "",
                    "change_summary": ((accepted_attempt.get("review_result") or {}).get("review_summary") or ""),
                    "risk_level": "low",
                }
            )
        archive = Path(str(accepted_attempt.get("candidate_archive") or ""))
        if archive.exists():
            candidate = json.loads(archive.read_text(encoding="utf-8"))
            for item in candidate.get("files") or []:
                drafts.append(
                    {
                        "operation": "add" if suggestion.get("action") == "add_new_skill" else "revise",
                        "skill_id": suggestion.get("new_skill_id") or suggestion.get("target_skill_id"),
                        "relative_path": item.get("path"),
                        "content": item.get("content"),
                        "source_patch_ids": member_ids,
                    }
                )
    return {
        "skill_patch_plan": patch_plan,
        "updated_skill_drafts": drafts,
        "patch_reviews": [
            {
                "patch_id": item.get("suggestion_id"),
                "suggestion_ids": _unit_suggestion_ids(item),
                "status": "accept" if item.get("status") == "accepted" else item.get("status"),
                "attempt_count": item.get("attempt_count"),
            }
            for item in state.get("suggestions") or []
        ],
        "skill_length_audit": state.get("skill_length_audit") or {},
        "stage_08_transactional_skill_repair": state,
    }
