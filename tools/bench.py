#!/usr/bin/env python3
"""SkillsBench 本地 Docker/BenchFlow 辅助命令。

Web 控制台适合交互式并行评测；本脚本为 Linux CI、迁移后的 smoke test 和单任务
容器预构建提供稳定的命令行入口。它不保存 API key，也不自行执行 verifier。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parents[1]


def prebuilt_image_config_path() -> Path:
    """返回可提交、可迁移的 task -> Docker image 注册表路径。"""
    configured = os.getenv("SKILLSBENCH_PREBUILT_IMAGES", "").strip()
    return Path(configured).expanduser().resolve() if configured else root() / "runner-app" / "prebuilt-images.json"


def load_prebuilt_images() -> dict[str, str]:
    """读取镜像注册表；文件不存在时从空注册表开始。"""
    path = prebuilt_image_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid prebuilt image registry: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Prebuilt image registry must be a JSON object: {path}")
    return {str(key): str(value).strip() for key, value in data.items() if str(value).strip()}


def save_prebuilt_images(images: dict[str, str]) -> None:
    """以稳定排序写回镜像注册表，避免迁移时产生无意义 diff。"""
    path = prebuilt_image_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(images.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_prebuilt_image(task: str) -> str:
    """为未登记 task 生成合法、稳定且跨机器可复用的镜像标签。"""
    slug = task.strip().lower().replace("_", "-")
    slug = "".join(char if char.isalnum() or char in "-." else "-" for char in slug).strip("-.")
    return f"skillsbench-local-{slug}:latest"


def image_for_task(task: str, requested: str = "") -> str:
    """优先使用显式镜像，其次使用注册表，最后生成稳定默认值。"""
    return requested.strip() or load_prebuilt_images().get(task) or default_prebuilt_image(task)


def task_dir(task: str) -> Path:
    value = Path(task)
    if value.is_absolute():
        path = value
    else:
        path = root() / "tasks" / value
        if not path.exists():
            path = root() / "tasks-extra" / value
    if not (path / "task.md").exists():
        raise SystemExit(f"Task not found or missing task.md: {path}")
    return path.resolve()


def require(command: str) -> str:
    value = shutil.which(command)
    if not value:
        raise SystemExit(f"Required command is not installed or not on PATH: {command}")
    return value


def check(_: argparse.Namespace) -> int:
    for command in ("git", "node", "docker"):
        print(f"{command}: {require(command)}")
    uv = shutil.which("uv")
    print(f"uv: {uv or '(missing; install uv for BenchFlow runs)'}")
    print(f"project: {root()}")
    return 0


def build(args: argparse.Namespace) -> int:
    require("docker")
    directory = task_dir(args.task) / "environment"
    dockerfile = directory / "Dockerfile"
    if not dockerfile.exists():
        raise SystemExit(f"Task has no environment/Dockerfile: {directory}")
    image = image_for_task(args.task, args.image or "")
    command = ["docker", "build", "--tag", image, "--file", str(dockerfile), str(directory)]
    print("+", " ".join(command))
    subprocess.run(command, check=True)
    if args.register:
        images = load_prebuilt_images()
        images[args.task] = image
        save_prebuilt_images(images)
        print(f"Registered {args.task} -> {image} in {prebuilt_image_config_path()}")
    print(f"Built image: {image}")
    return 0


def image_status(args: argparse.Namespace) -> int:
    """检查注册表中的镜像是否已经存在于当前机器。"""
    require("docker")
    images = load_prebuilt_images()
    selected = {args.task: images[args.task]} if args.task and args.task in images else images
    if args.task and args.task not in images:
        print(f"{args.task}: not registered")
        return 1
    missing = 0
    for task, image in sorted(selected.items()):
        present = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        print(f"{task}: {'ready' if present else 'missing'} -> {image}")
        if not present:
            missing += 1
    return 1 if missing else 0


def image_list(_: argparse.Namespace) -> int:
    """列出当前仓库提交的镜像注册表。"""
    for task, image in sorted(load_prebuilt_images().items()):
        print(f"{task}\t{image}")
    return 0


def run_bench(args: argparse.Namespace) -> int:
    require("uv")
    task = task_dir(args.task)
    jobs_dir = Path(args.jobs_dir)
    if not jobs_dir.is_absolute():
        jobs_dir = root() / jobs_dir
    jobs_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "uv", "run", "bench", "eval", "run",
        "--tasks-dir", str(task),
        "--agent", args.agent,
        "--model", args.model,
        "--sandbox", "docker",
        "--jobs-dir", str(jobs_dir),
        "--concurrency", str(max(1, args.concurrency)),
    ]
    if args.skills_dir:
        skills = Path(args.skills_dir)
        if not skills.is_absolute():
            skills = root() / skills
        command.extend(["--skills-dir", str(skills)])
    if args.skill_mode:
        effective_skill_mode = "with-skill" if args.skill_mode == "force-skill" else args.skill_mode
        command.extend(["--skill-mode", effective_skill_mode])
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    env = os.environ.copy()
    if args.base_url:
        env["BENCHFLOW_PROVIDER_BASE_URL"] = args.base_url
        env["ANTHROPIC_BASE_URL"] = args.base_url
    env["BENCHFLOW_PROVIDER_TYPE"] = args.provider
    env["SKILLSBENCH_PROVIDER"] = args.provider
    if args.skill_mode == "force-skill":
        # 命令行工具不能自动改写 task 目录；这个环境变量供支持该约定的
        # BenchFlow/agent 适配器读取，同时明确把实际 CLI skill mode 保持为 with-skill。
        env["SKILLSBENCH_PROMPT_MODE"] = "force-all-skills"
    if args.api_key:
        env["BENCHFLOW_PROVIDER_API_KEY"] = args.api_key
        env["ANTHROPIC_API_KEY"] = args.api_key
    print("+", " ".join(command))
    return subprocess.run(command, cwd=root(), env=env).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build task containers and run local SkillsBench evaluations")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--task", required=True)
    build_parser.add_argument("--image")
    build_parser.add_argument(
        "--no-register",
        dest="register",
        action="store_false",
        help="构建但不更新 runner-app/prebuilt-images.json",
    )
    build_parser.set_defaults(register=True)
    images_parser = sub.add_parser("images", help="查看和检查 task 预构建镜像注册表")
    images_parser.add_argument("action", choices=["list", "status"])
    images_parser.add_argument("--task", help="只查看一个 task")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--task", required=True)
    run_parser.add_argument("--agent", default="claude-agent-acp")
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--skills-dir")
    run_parser.add_argument(
        "--skill-mode",
        choices=["no-skill", "with-skill", "force-skill"],
        default="with-skill",
        help="no-skill 不注入 skill；with-skill 正常提供；force-skill 要求适配器强制调用全部 skill。",
    )
    run_parser.add_argument(
        "--provider",
        choices=["deepseek", "anthropic", "openai", "custom"],
        default=os.getenv("BENCHFLOW_PROVIDER_TYPE", "deepseek"),
        help="供应商标签；实际兼容协议由 --base-url 与 harness 决定。",
    )
    run_parser.add_argument("--jobs-dir", default="jobs/local")
    run_parser.add_argument("--concurrency", type=int, default=1)
    run_parser.add_argument("--base-url", default=os.getenv("BENCHFLOW_PROVIDER_BASE_URL", ""))
    run_parser.add_argument("--api-key", default=os.getenv("BENCHFLOW_PROVIDER_API_KEY", ""))
    run_parser.add_argument("--reasoning-effort", default=os.getenv("BENCHFLOW_REASONING_EFFORT", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "check":
        return check(args)
    if args.command == "build":
        return build(args)
    if args.command == "images":
        return image_list(args) if args.action == "list" else image_status(args)
    return run_bench(args)


if __name__ == "__main__":
    raise SystemExit(main())
