#!/usr/bin/env python3
"""重新修复并测试历史高通过率任务的批处理脚本。

脚本不保存任何 API key；repair LLM 和 DeepSeek 的凭据都从环境变量读取。
每个任务会先运行当前的 Offline SkillRCA v2 pipeline，再对生成的技能库做
5 次 BenchFlow 验证。验证失败属于实验结果，repair 阶段失败才会中止批处理。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = "offline-skill-rca-v2-20260708"


@dataclass(frozen=True)
class TaskSpec:
    """一个需要重新修复和测试的任务配置。"""

    task: str
    repair_task_dir: str
    repair_skills_dir: str
    traces_dir: str
    validation_task_dir: str
    repeats: int = 5


TASKS = [
    TaskSpec(
        task="ada-bathroom-plan-repair",
        repair_task_dir="tasks/ada-bathroom-plan-repair",
        repair_skills_dir="tasks/ada-bathroom-plan-repair/environment/skills",
        traces_dir="jobs/web-runner/p3rem-20260629/ada/ws",
        validation_task_dir="tmp/ada-bathroom-rca-force-workflow/ada-bathroom-plan-repair",
    ),
    TaskSpec(
        task="energy-unit-commitment",
        repair_task_dir="tasks/energy-unit-commitment",
        repair_skills_dir="tasks/energy-unit-commitment/environment/skills",
        traces_dir="jobs/web-runner/p3rem-20260629/energy/ws-p1",
        validation_task_dir="tmp/energy-unit-rca-force-workflow-20260703/energy-unit-commitment",
    ),
    TaskSpec(
        task="fix-build-google-auto",
        repair_task_dir="tasks/fix-build-google-auto",
        repair_skills_dir="tasks/fix-build-google-auto/environment/skills",
        traces_dir="jobs/web-runner/force-all-skills-20260702-batch/fix-build-google-auto",
        validation_task_dir="tmp/fix-build-google-auto-rca-force-workflow-20260703/fix-build-google-auto",
    ),
    TaskSpec(
        task="video-silence-remover",
        repair_task_dir="tasks/video-silence-remover",
        repair_skills_dir="tasks/video-silence-remover/environment/skills",
        traces_dir="jobs/web-runner/video-silence-remover-2026-06-26T09-07-57-768Z",
        validation_task_dir="tmp/video-silence-agentready-force-workflow/video-silence-remover",
    ),
    TaskSpec(
        task="setup-fuzzing-py",
        repair_task_dir="tasks/setup-fuzzing-py",
        repair_skills_dir="tasks/setup-fuzzing-py/environment/skills",
        traces_dir="jobs/web-runner/force-all-skills-20260702-batch/setup-fuzzing-py",
        validation_task_dir="tmp/setup-fuzzing-py-rca-20260704/force-helper-overlay-20260704-v3/setup-fuzzing-py",
    ),
]


def rel(path: Path) -> str:
    """把路径转换成相对仓库根目录的字符串。"""

    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def run_command(name: str, command: list[str], log_path: Path, allow_failure: bool = False) -> int:
    """运行一个子命令，并把 stdout/stderr 追加写入日志。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n===== {datetime.now(UTC).isoformat()} {name} =====\n")
        log.write(" ".join(command) + "\n")
        log.flush()
        process = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT, text=True)
        log.write(f"===== exit {process.returncode} =====\n")
    if process.returncode and not allow_failure:
        raise SystemExit(f"{name} failed with exit code {process.returncode}. See {log_path}")
    return process.returncode


def write_log_line(log_path: Path, text: str) -> None:
    """向任务日志写入一段普通说明。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(text.rstrip() + "\n")


def remove_dir(path: Path) -> None:
    """删除本批次会重新生成的目录。

    这里的删除只用于固定的 repair/test 输出路径，不会触碰原始 tasks 或 jobs。
    调用前会确认目标路径仍在仓库根目录下，避免路径拼接错误造成误删。
    """

    full = path.resolve()
    root = ROOT.resolve()
    if full == root or root not in full.parents:
        raise SystemExit(f"Refusing to remove path outside repository: {full}")
    if full.exists():
        shutil.rmtree(full)


def read_validation_summary(jobs_root: Path) -> dict:
    """读取验证脚本生成的 validation_summary.json。"""

    path = jobs_root / "validation_summary.json"
    if not path.exists():
        return {"missing": True, "path": rel(path)}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def main() -> int:
    """顺序执行所有任务的 repair 和 validation。"""

    if not os.getenv("OFFLINE_SKILL_RCA_API_KEY"):
        raise SystemExit("Missing OFFLINE_SKILL_RCA_API_KEY")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit("Missing DEEPSEEK_API_KEY")

    batch_root = ROOT / "repair-runs" / "_batch" / "high-success-v2-20260708"
    remove_dir(batch_root)
    batch_root.mkdir(parents=True, exist_ok=True)
    # 这些默认值只影响 repair LLM 的请求控制，不改变任何 repair 决策内容。
    # 用户可在启动脚本前设置同名环境变量覆盖。
    os.environ.setdefault("OFFLINE_SKILL_RCA_TIMEOUT_SEC", "600")
    os.environ.setdefault("OFFLINE_SKILL_RCA_MAX_RETRIES", "2")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE2_MAX_PROMPT_CHARS", "100000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE2_FALLBACK_PROMPT_CHARS", "85000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE2_MAX_TOKENS", "10000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE7_MAX_PROMPT_CHARS", "90000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE7_MAX_TOKENS", "12000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE8A_MAX_TOKENS", "8000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE8B_MAX_PROMPT_CHARS", "60000")
    os.environ.setdefault("OFFLINE_SKILL_RCA_STAGE8B_MAX_TOKENS", "12000")
    summary_path = batch_root / "summary.json"
    summary: dict = {
        "tag": TAG,
        "startedAt": datetime.now(UTC).isoformat(),
        "tasks": [],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    for spec in TASKS:
        repair_output = ROOT / "repair-runs" / spec.task / TAG
        repaired_skills = ROOT / "skill-libraries" / spec.task / TAG
        validation_jobs = ROOT / "jobs" / "offline-skill-rca-validation" / f"{spec.task}-v2-20260708"
        remove_dir(validation_jobs)
        task_record = {
            "task": spec.task,
            "repairOutput": rel(repair_output),
            "repairedSkills": rel(repaired_skills),
            "validationJobs": rel(validation_jobs),
            "status": "repairing",
        }
        summary["tasks"].append(task_record)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        log_path = batch_root / f"{spec.task}.log"
        repair_cmd = [
            sys.executable,
            "offline_skill_rca/run_offline_skill_rca.py",
            "--task-dir",
            spec.repair_task_dir,
            "--skills-dir",
            spec.repair_skills_dir,
            "--traces",
            spec.traces_dir,
            "--output-dir",
            rel(repair_output),
            "--output-skills-dir",
            rel(repaired_skills),
            "--strong-base-url",
            os.getenv("OFFLINE_SKILL_RCA_BASE_URL", "https://api.camel-hub.com"),
            "--strong-model",
            os.getenv("OFFLINE_SKILL_RCA_MODEL", "gpt-5.5"),
            "--max-prompt-chars",
            "150000",
            "--trace-analysis-workers",
            "3",
            "--force",
        ]
        repair_manifest = repair_output / "applied_repair_manifest.json"
        if os.getenv("OFFLINE_SKILL_RCA_REUSE_REPAIR") == "1" and repair_manifest.exists() and repaired_skills.exists():
            write_log_line(
                log_path,
                f"\n===== {datetime.now(UTC).isoformat()} reuse repair {spec.task} =====\n"
                f"Reusing existing repair output: {rel(repair_output)}\n"
                f"Reusing existing repaired skills: {rel(repaired_skills)}",
            )
        else:
            run_command(f"repair {spec.task}", repair_cmd, log_path)
        task_record["status"] = "validating"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        validate_cmd = [
            sys.executable,
            "offline_skill_rca/validate_repaired_skills.py",
            "--task-dir",
            spec.validation_task_dir,
            "--skills-dir",
            rel(repaired_skills),
            "--jobs-root",
            rel(validation_jobs),
            "--model",
            "deepseek-v4-flash",
            "--agent",
            "claude-agent-acp",
            "--base-url",
            "https://api.deepseek.com",
            "--repeats",
            str(spec.repeats),
            "--parallel",
            "1",
            "--agent-idle-timeout",
            "1800",
            "--build-concurrency",
            "1",
        ]
        exit_code = run_command(f"validate {spec.task}", validate_cmd, log_path, allow_failure=True)
        task_record["status"] = "validated"
        task_record["validationExitCode"] = exit_code
        task_record["validationSummary"] = read_validation_summary(validation_jobs)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    summary["finishedAt"] = datetime.now(UTC).isoformat()
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
