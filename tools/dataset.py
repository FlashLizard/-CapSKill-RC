#!/usr/bin/env python3
"""下载并接入 SkillsBench 数据集。

本脚本把“数据”和“代码”分开管理：数据默认放在 ``.data/skillsbench``，项目根目录
只保留指向数据的链接或复制目录。这样新仓库可以提交到 git，而任务环境中的大文件、
视频、历史 jobs 和 repair 结果不会被误提交。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


DATA_DIR_NAME = ".data/skillsbench"
DATA_DIRS = ("tasks", "tasks-extra", "skill-libraries")


def project_root() -> Path:
    """返回 tools/ 的上一级目录，保证从任意 cwd 执行时路径一致。"""
    return Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path | None = None) -> None:
    """执行外部命令并在失败时保留原始错误码。"""
    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def dataset_checkout(root: Path, requested: str | None) -> Path:
    """解析数据 checkout 路径；允许用户通过环境变量迁移到更大的磁盘。"""
    value = requested or os.getenv("SKILLSBENCH_DATA_CHECKOUT") or DATA_DIR_NAME
    path = Path(value)
    return path if path.is_absolute() else root / path


def clone(args: argparse.Namespace) -> Path:
    """浅克隆数据集；已经存在时只做 fast-forward，不覆盖用户未提交文件。"""
    root = project_root()
    target = dataset_checkout(root, args.checkout)
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        run(["git", "fetch", "--depth", "1", "origin", args.ref], cwd=target)
        # 不使用 reset --hard：数据目录也可能被用户本地改过，迁移工具不应静默
        # 删除这些修改。若无法 fast-forward，git 会给出明确冲突信息供用户处理。
        run(["git", "pull", "--ff-only", "origin", args.ref], cwd=target)
    elif target.exists() and any(target.iterdir()):
        raise SystemExit(f"Checkout directory is not empty and is not a git repo: {target}")
    else:
        if target.exists() and not any(target.iterdir()):
            target.rmdir()
        run(["git", "clone", "--depth", "1", "--branch", args.ref, args.repo, str(target)])
    print(f"Dataset checkout: {target}")
    return target


def remove_existing(path: Path) -> None:
    """删除由本脚本之前创建的链接/目录；调用者已先校验 --force。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def link_directory(source: Path, target: Path) -> None:
    """创建跨平台目录链接；Windows 无权限时给出明确的 copy 替代方案。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # junction 不要求 Windows 开发者模式，适合普通 PowerShell 用户。
        run(["cmd", "/c", "mklink", "/J", str(target), str(source)])
    else:
        target.symlink_to(source, target_is_directory=True)


def sync(args: argparse.Namespace, checkout: Path) -> None:
    """把 checkout 中的三个运行目录接入项目根目录。"""
    root = project_root()
    for name in DATA_DIRS:
        source = checkout / name
        target = root / name
        if not source.exists():
            print(f"skip: dataset has no {name}")
            continue
        if target.exists() or target.is_symlink():
            if not args.force:
                raise SystemExit(f"Target already exists: {target}; use --force to replace it")
            remove_existing(target)
        if args.mode == "copy":
            shutil.copytree(source, target)
        else:
            link_directory(source, target)
        print(f"connected {name}: {target} -> {source}")


def status(args: argparse.Namespace) -> int:
    """展示数据是否已下载、是否已接入以及当前 commit。"""
    root = project_root()
    checkout = dataset_checkout(root, args.checkout)
    print(f"checkout: {checkout}")
    print(f"exists: {checkout.exists()}")
    if (checkout / ".git").exists():
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=checkout, text=True, capture_output=True)
        print(f"commit: {result.stdout.strip() or '(unknown)'}")
    for name in DATA_DIRS:
        path = root / name
        print(f"{name}: {path.exists()} ({'link' if path.is_symlink() else 'directory'})")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and connect SkillsBench dataset directories")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("clone", "install"):
        command = sub.add_parser(name, help="clone/update the dataset; install also connects directories")
        command.add_argument("--repo", default="https://github.com/benchflow-ai/skillsbench.git")
        command.add_argument("--ref", default="main")
        command.add_argument("--checkout", help=f"checkout directory (default: {DATA_DIR_NAME})")
        command.add_argument("--mode", choices=("link", "copy"), default="link")
        command.add_argument("--force", action="store_true")
    check = sub.add_parser("status", help="show dataset status")
    check.add_argument("--checkout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "status":
        return status(args)
    checkout = clone(args)
    if args.command == "install":
        sync(args, checkout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
