#!/usr/bin/env python3
"""用 BenchFlow 验证修复后的 skills 库。

该脚本会多次运行同一个 task，并统计通过率。它默认使用 claude-agent-acp harness
和 deepseek-v4-flash 模型，但所有关键参数都可以通过 CLI 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class RunResult:
    """一次验证运行的摘要结果。"""
    run_no: int
    jobs_dir: Path
    exit_code: int
    passed: int
    total: int
    score_ratio: float | None
    error: str | None


def resolve(value: str) -> Path:
    """把相对路径解析到仓库根目录下。"""
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def safe_rel(path: Path) -> str:
    """把路径转换为相对仓库根目录的显示形式。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def read_summary(jobs_dir: Path) -> tuple[int, int, float | None, str | None]:
    """读取 BenchFlow 生成的 summary.json。

    如果 run 失败到没有 summary.json，也返回结构化错误字符串，便于总表记录失败。
    """
    summary_path = jobs_dir / "summary.json"
    if not summary_path.exists():
        return 0, 0, None, "missing summary.json"
    try:
        data: dict[str, Any] = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return 0, 0, None, f"could not parse summary.json: {exc}"
    return int(data.get("passed") or data.get("pass") or 0), int(data.get("total") or 0), data.get("score_ratio"), data.get("error")


def env_for_run(args: argparse.Namespace) -> dict[str, str]:
    """为一次 BenchFlow run 构造环境变量。

    同时设置 Anthropic/OpenAI/BenchFlow 常见变量，是为了让 claude code harness、
    BenchFlow provider 和 DeepSeek OpenAI-compatible 接口都能拿到同一组模型配置。
    """
    env = dict(os.environ)
    api_key = args.api_key or env.get("DEEPSEEK_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    if not api_key:
        raise SystemExit("Missing DeepSeek API key. Set DEEPSEEK_API_KEY or pass --api-key.")
    base_url = args.base_url
    env["PYTHONUTF8"] = "1"
    # 关闭 Claude Code 的非必要流量，减少评测时额外网络请求带来的干扰。
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["ANTHROPIC_API_KEY"] = api_key
    env["BENCHFLOW_PROVIDER_BASE_URL"] = base_url
    env["BENCHFLOW_PROVIDER_API_KEY"] = api_key
    env["BENCHFLOW_PROVIDER_MODEL"] = args.model
    env["LLM_BASE_URL"] = base_url
    env["LLM_MODEL"] = args.model
    env["LLM_API_KEY"] = api_key
    if "deepseek" in base_url.lower() or args.model.lower().startswith("deepseek"):
        # DeepSeek 走 OpenAI-compatible API 时，一些底层工具会读取 OPENAI_* 变量。
        env["DEEPSEEK_API_KEY"] = api_key
        env["OPENAI_API_KEY"] = api_key
        env["OPENAI_BASE_URL"] = base_url
    return env


def bench_args(args: argparse.Namespace, jobs_dir: Path) -> list[str]:
    """拼装 ``uv run bench eval run`` 命令行参数。"""
    command = [
        "uv",
        "run",
        "bench",
        "eval",
        "run",
        "--tasks-dir",
        safe_rel(resolve(args.task_dir)),
        "--agent",
        args.agent,
        "--model",
        args.model,
        "--sandbox",
        args.sandbox,
        "--jobs-dir",
        safe_rel(jobs_dir),
        "--skill-mode",
        "with-skill",
        "--skills-dir",
        safe_rel(resolve(args.skills_dir)),
        "--concurrency",
        "1",
        "--agent-idle-timeout",
        args.agent_idle_timeout,
    ]
    if args.usage_tracking:
        command.extend(["--usage-tracking", args.usage_tracking])
    if args.build_concurrency:
        command.extend(["--build-concurrency", str(args.build_concurrency)])
    for key_value in args.agent_env:
        command.extend(["--agent-env", key_value])
    command.extend(["--agent-env", f"BENCHFLOW_PROVIDER_BASE_URL={args.base_url}"])
    command.extend(["--agent-env", f"BENCHFLOW_PROVIDER_MODEL={args.model}"])
    command.extend(["--agent-env", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"])
    if args.agent == "claude-agent-acp":
        # claude-agent-acp 需要 Anthropic 风格的 base/model 环境变量。
        command.extend(["--agent-env", f"ANTHROPIC_BASE_URL={args.base_url}"])
        command.extend(["--agent-env", f"ANTHROPIC_MODEL={args.model}"])
    return command


def run_one(args: argparse.Namespace, run_no: int) -> RunResult:
    """执行一次验证 run，并返回该 run 的结果摘要。"""
    jobs_dir = resolve(args.jobs_root) / f"run-{run_no}"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    log_path = jobs_dir / "validator.log"
    command = bench_args(args, jobs_dir)
    with log_path.open("w", encoding="utf-8") as log:
        # 完整 stdout/stderr 写入 validator.log，终端只打印紧凑摘要。
        log.write(" ".join(command) + "\n")
        log.flush()
        process = subprocess.run(command, cwd=ROOT, env=env_for_run(args), stdout=log, stderr=subprocess.STDOUT, text=True)
    passed, total, score_ratio, error = read_summary(jobs_dir)
    return RunResult(
        run_no=run_no, jobs_dir=jobs_dir, exit_code=process.returncode, passed=passed, total=total, score_ratio=score_ratio, error=error
    )


def parse_args() -> argparse.Namespace:
    """解析验证脚本命令行参数。"""
    parser = argparse.ArgumentParser(description="Validate a repaired skills library with BenchFlow.")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--skills-dir", required=True)
    parser.add_argument("--jobs-root", required=True)
    parser.add_argument("--agent", default="claude-agent-acp")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--sandbox", default="docker")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--agent-idle-timeout", default="0")
    parser.add_argument("--usage-tracking", default="auto")
    parser.add_argument("--build-concurrency", type=int, default=1)
    parser.add_argument("--agent-env", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    """并行或串行执行多次 BenchFlow 验证，并写出 pass-rate 汇总。"""
    args = parse_args()
    jobs_root = resolve(args.jobs_root)
    jobs_root.mkdir(parents=True, exist_ok=True)
    print(f"[validate] task={safe_rel(resolve(args.task_dir))}")
    print(f"[validate] skills={safe_rel(resolve(args.skills_dir))}")
    print(f"[validate] jobs={safe_rel(jobs_root)} repeats={args.repeats} parallel={args.parallel}")

    results: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        # 每个 repeat 使用独立 jobs_dir/run-N，避免多个验证 run 的输出互相覆盖。
        futures = {pool.submit(run_one, args, run_no): run_no for run_no in range(1, args.repeats + 1)}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = "PASS" if result.passed == result.total and result.total else "FAIL"
            print(
                f"[validate] run-{result.run_no}: {status} passed={result.passed}/{result.total} exit={result.exit_code} jobs={safe_rel(result.jobs_dir)}"
            )

    results.sort(key=lambda item: item.run_no)
    total = sum(item.total for item in results)
    passed = sum(item.passed for item in results)
    pass_rate = (passed / total) if total else 0.0
    summary = {
        # validation_summary.json 是后续比较不同 skills 库修复效果的主要入口。
        "taskDir": safe_rel(resolve(args.task_dir)),
        "skillsDir": safe_rel(resolve(args.skills_dir)),
        "jobsRoot": safe_rel(jobs_root),
        "repeats": args.repeats,
        "parallel": args.parallel,
        "passed": passed,
        "total": total,
        "passRate": pass_rate,
        "runs": [result.__dict__ | {"jobs_dir": safe_rel(result.jobs_dir)} for result in results],
    }
    (jobs_root / "validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[validate] pass_rate={passed}/{total} ({pass_rate:.1%})")
    return 0 if total and passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
