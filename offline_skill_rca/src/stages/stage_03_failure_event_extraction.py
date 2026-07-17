"""Stage 3：逐条轨迹抽取失败事件、原因事件和因果链。

与其它阶段不同，Stage 3 会对每条轨迹单独发起一次 repair LLM 调用，并行执行。
这样可以避免把 5 条长轨迹塞进同一个上下文导致分析质量下降，也方便之后查看
每条轨迹独立的 request/response。
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from .common import json_block, render_prompt_template, write_prompt_file, make_llm, stage_system_prompt
from ..llm_client import log_progress
from ..io_utils import (
    AGENT_ONLY_EVIDENCE_POLICY_VERSION,
    sanitize_agent_artifacts,
    sanitize_agent_only_visible_result,
    write_json,
)


def stage_name(index: int, traj: dict[str, Any]) -> str:
    """生成稳定的 transcript/prompt 文件名。

    文件名包含序号和 traj_id，既能保持输出顺序，又便于在可视化页面中定位某条
    轨迹对应的 LLM 交互。
    """
    from ..pipeline import sanitize

    return f"stage-03-traj-{index + 1:02d}-{sanitize(str(traj.get('traj_id') or index + 1))}"


def stage3_trajectory_input(config: Any, traj: dict[str, Any]) -> dict[str, Any]:
    """构造真正发给 Stage 3 LLM 的 trajectory 对象。

    优先从 rollout_dir 找回原始 JSONL 并用本地代码重新格式化 steps；这样旧
    bundle 中即使存在缓存 steps，也不会被盲目复用。新 bundle 通常不含
    rollout_dir，此时使用 init 阶段已经由本地代码生成的 steps。返回对象仍然
    不包含 rollout_dir、raw_trajectory_jsonl、messages 或 tool_calls。
    """
    rollout_trajectory = load_trajectory_from_rollout(config, traj)
    steps = []
    if rollout_trajectory is not None:
        from ..schemas import to_plain

        steps = [to_plain(step) for step in rollout_trajectory.steps]
    if not steps:
        steps = list(traj.get("steps") or [])
    if steps_contain_truncation_marker(steps):
        raise SystemExit(
            "Stage 3 trajectory steps contain '[truncated ... chars]' markers and could not be regenerated from raw rollout. "
            "Re-initialize this repair run from the original jobs directory so full steps can be generated."
        )
    visible_failure_result = sanitize_agent_only_visible_result(traj.get("visible_failure_result"))
    final_artifacts = sanitize_agent_artifacts(traj.get("final_artifacts"))
    if rollout_trajectory is not None:
        # 始终覆盖旧缓存，防止历史 bundle 中的 verifier 摘要经兼容路径回流。
        visible_failure_result = rollout_trajectory.result
        final_artifacts = sanitize_agent_artifacts(rollout_trajectory.final_artifacts)
    return {
        "traj_id": traj.get("traj_id"),
        "task_id": traj.get("task_id"),
        "success": success_scalar(traj.get("success")),
        "step_formatting_provenance": "generated_by_local_code_from_acp_trajectory_jsonl; no LLM summarization",
        "steps": steps,
        "visible_failure_result": visible_failure_result or {"success": success_scalar(traj.get("success"))},
        "final_artifacts": final_artifacts or [],
    }


def steps_contain_truncation_marker(steps: list[Any]) -> bool:
    """检查旧缓存 steps 中是否残留本地脚本生成的截断标记。

    有些 ACP 工具输出本身会包含 ``... [truncated from N total chars]``，
    这是原始可见轨迹的一部分，脚本无法从 trajectory 中还原被工具省略的内容。
    这里仅拒绝本地 ``truncate(...)`` 生成的 ``...[truncated N chars]``，
    避免把原始终端文本误判成 pipeline 静默截断。
    """
    return re.search(r"\.\.\.\[truncated \d+ chars\]", str(steps)) is not None


def success_scalar(value: Any) -> int:
    """把旧/新 bundle 中的 success 值统一成 0/1。"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value > 0 else 0
    return 1 if str(value or "").strip().lower() in {"1", "true", "yes", "success"} else 0


def regenerate_steps_from_rollout(config: Any, traj: dict[str, Any]) -> list[dict[str, Any]]:
    """从 rollout_dir 补读原始 ACP JSONL，并用本地代码重新格式化 steps。

    这个函数只用于兼容旧 debug 输出目录或调试场景；它不会把路径或 raw JSONL
    暴露给 LLM。
    """
    rollout_trajectory = load_trajectory_from_rollout(config, traj)
    if rollout_trajectory is None:
        return []
    from ..schemas import to_plain

    return [to_plain(step) for step in rollout_trajectory.steps]


def load_trajectory_from_rollout(config: Any, traj: dict[str, Any]) -> Any | None:
    """安全地重读旧 bundle 指向的 rollout，并应用当前证据收集策略。"""
    rel = traj.get("rollout_dir") or traj.get("rolloutDir")
    if not rel:
        return None
    try:
        root = Path(config.root).resolve()
        rollout_dir = (root / str(rel)).resolve()
        rollout_dir.relative_to(root)
    except Exception:
        return None
    from ..io_utils import load_trajectory

    try:
        return load_trajectory(root, rollout_dir)
    except SystemExit:
        return None


def build_prompt(
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage2: dict[str, Any],
    trajectory: dict[str, Any],
    max_chars: int,
) -> str:
    """构造单条轨迹的 Stage 3 prompt。

    prompt 只包含 task contract、当前轨迹、可见错误结果和失败最终产物。它不看
    能力图，避免在抽取事实时提前把事件强塞进某个能力节点。
    """
    from ..pipeline import fit_prompt, trace_analysis_schema

    instructions = render_prompt_template(
        "stage-03-failure-event-extraction.txt",
        {"trace_analysis_schema": json_block(trace_analysis_schema())},
    )
    # 使用正向白名单，避免手工修改的 bundle 把额外结果字段带入 prompt。
    trajectory_core = {
        key: trajectory.get(key)
        for key in ("traj_id", "task_id", "success", "step_formatting_provenance", "steps")
    }
    payload = {
        "stage_01a_task_description_standardization": task_standardization,
        # 保留函数签名以兼容现有调用方，但本阶段不消费 Skills 或能力图。
        "trajectory": trajectory_core,
        "visible_failure_result": sanitize_agent_only_visible_result(
            trajectory.get("visible_failure_result") or {"success": trajectory.get("success", 0)}
        ),
        "final_artifacts": sanitize_agent_artifacts(trajectory.get("final_artifacts")),
    }
    env_limit = os.getenv("OFFLINE_SKILL_RCA_STAGE3_MAX_PROMPT_CHARS") or os.getenv("OFFLINE_SKILL_RCA_STAGE2_MAX_PROMPT_CHARS")
    stage_max_chars = min(max_chars, int(env_limit)) if env_limit else max_chars
    return fit_prompt(instructions, payload, stage_max_chars)


def run_one(
    config: Any,
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage2: dict[str, Any],
    index: int,
    traj: dict[str, Any],
) -> dict[str, Any]:
    """分析一条轨迹并补齐基本标识字段。

    repair LLM 理论上会返回 traj_id/success，但这里做一次 setdefault，保证并行
    汇总时即使模型漏填字段也能回溯到原始轨迹。
    """
    stage_traj = stage3_trajectory_input(config, traj)
    name = stage_name(index, stage_traj)
    prompt = build_prompt(bundle, task_standardization, skill_standardizations, stage2, stage_traj, config.max_prompt_chars)
    write_prompt_file(config, name, prompt)
    max_tokens = int(os.getenv("OFFLINE_SKILL_RCA_STAGE3_MAX_TOKENS") or os.getenv("OFFLINE_SKILL_RCA_STAGE2_MAX_TOKENS") or 10_000)
    try:
        result = make_llm(config, name).chat_json(stage_system_prompt(name), prompt, max_tokens=max_tokens)
    except Exception as exc:
        # 如果单条长轨迹在网关侧超时或返回不可解析内容，重新请求 repair LLM。
        # fallback 仍必须保留完整格式化轨迹；把上限硬降到 60K 会导致较长轨迹在
        # fit_prompt 中直接失败，也违背“Stage 3 不截断轨迹证据”的输入约束。
        enable_fallback = str(os.getenv("OFFLINE_SKILL_RCA_STAGE3_ENABLE_FALLBACK") or os.getenv("OFFLINE_SKILL_RCA_STAGE2_ENABLE_FALLBACK") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        fallback_chars = int(
            os.getenv("OFFLINE_SKILL_RCA_STAGE3_FALLBACK_PROMPT_CHARS")
            or os.getenv("OFFLINE_SKILL_RCA_STAGE2_FALLBACK_PROMPT_CHARS")
            or config.max_prompt_chars
        )
        if not enable_fallback and fallback_chars >= config.max_prompt_chars:
            # 默认情况下不再用完全相同的 prompt 自动重试一遍；对 timeout 来说，
            # 这只会把等待时间翻倍。HTTP/网络重试已经由 LLMClient 负责。
            raise RuntimeError(f"Stage 3 LLM call failed for {name}: {exc}") from exc
        fallback_name = f"{name}-fallback"
        fallback_prompt = build_prompt(bundle, task_standardization, skill_standardizations, stage2, stage_traj, fallback_chars)
        write_prompt_file(config, fallback_name, fallback_prompt)
        result = make_llm(config, fallback_name).chat_json(stage_system_prompt(fallback_name), fallback_prompt, max_tokens=max_tokens)
    if isinstance(result, dict):
        # 轨迹标识和 0/1 success 是调用输入事实，不允许模型重新判断或改写。
        result["traj_id"] = stage_traj.get("traj_id")
        result["success"] = stage_traj.get("success", 0)
        result["evidence_policy_version"] = AGENT_ONLY_EVIDENCE_POLICY_VERSION
        # Stage 3 只提取事件与因果关系，不允许输出节点映射或节点状态。
        result.pop("node_status", None)
        result.pop("capability_node_status", None)
        result.pop("DAG_node_status", None)
        # LLMClient 在返回前已写 parsed.json；把本地锁定的标识和证据策略同步
        # 回规范文件，保证 Web 单步模式从 transcript 汇总时不会丢失门禁标记。
        write_json(config.output_dir / "llm_transcript" / f"{name}.parsed.json", result)
    return result


def run(
    config: Any,
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage2: dict[str, Any],
) -> list[dict[str, Any]]:
    """并行运行所有轨迹的 Stage 3 分析。

    ``results`` 预先按轨迹数量占位，然后按 future 完成结果回填原索引，保证最终
    输出顺序仍然与输入轨迹顺序一致。
    """
    trajectories = list(bundle.get("failed_trajectories") or [])
    if not trajectories:
        return []
    workers = max(1, min(int(config.trace_analysis_workers or 1), len(trajectories)))
    results: list[dict[str, Any] | None] = [None] * len(trajectories)
    heartbeat_sec = int(os.getenv("OFFLINE_SKILL_RCA_STAGE3_HEARTBEAT_SEC") or os.getenv("OFFLINE_SKILL_RCA_STAGE2_HEARTBEAT_SEC") or 30)
    started_at = time.monotonic()
    log_progress(f"Stage 3 start: {len(trajectories)} trajectories, workers={workers}")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_one, config, bundle, task_standardization, skill_standardizations, stage2, index, traj): index
            for index, traj in enumerate(trajectories)
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=heartbeat_sec, return_when=FIRST_COMPLETED)
            if not done:
                elapsed = time.monotonic() - started_at
                pending_names = [stage_name(futures[future], trajectories[futures[future]]) for future in sorted(pending, key=lambda item: futures[item])]
                log_progress(
                    f"Stage 3 waiting: done={len(trajectories) - len(pending)}/{len(trajectories)} "
                    f"elapsed={elapsed:.1f}s pending={', '.join(pending_names)}"
                )
                continue
            for future in done:
                index = futures[future]
                name = stage_name(index, trajectories[index])
                try:
                    results[index] = future.result()
                    log_progress(f"Stage 3 done: {name}")
                except Exception as exc:
                    log_progress(f"Stage 3 failed: {name}: {exc}")
                    raise
    return [item for item in results if item is not None]
