#!/usr/bin/env python3
"""Summarize SkillsBench job evidence for repair planning."""

from __future__ import annotations

import argparse
import collections
import json
import re
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except Exception:
        return str(path)


def read_json(path: Path) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(5):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                data = json.loads(text)
                return data if isinstance(data, dict) else {"value": data}
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.1)
    if last_error:
        return {"_read_error": str(last_error)}
    return {}


def read_jsonl(path: Path, limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except json.JSONDecodeError:
                rows.append({"_raw": line[:500]})
            if len(rows) >= limit:
                break
    except OSError as exc:
        rows.append({"_read_error": str(exc)})
    return rows


def read_text(path: Path, max_chars: int = 600_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def discover_result_files(paths: list[Path], task: str) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_file() and path.name == "result.json":
            files.add(path)
        elif path.is_dir():
            for item in path.rglob("result.json"):
                if task in str(item):
                    files.add(item)
    return sorted(files)


def discover_summary_files(paths: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_file() and path.name == "summary.json":
            files.add(path)
        elif path.is_dir():
            files.update(path.rglob("summary.json"))
    return sorted(files)


def result_outcome(result: dict[str, Any]) -> str:
    error = result.get("error")
    if error:
        return "error"
    rewards = result.get("rewards")
    if isinstance(rewards, dict):
        reward = rewards.get("reward")
        if reward == 1 or reward == 1.0:
            return "pass"
        if reward == 0 or reward == 0.0:
            return "fail"
    metrics = result.get("final_metrics")
    if isinstance(metrics, dict):
        score = metrics.get("score") or metrics.get("score_ratio")
        if score in (1, 1.0, "100.0%"):
            return "pass"
    return "unknown"


def error_label(result: dict[str, Any]) -> str:
    error = result.get("error")
    if not error:
        return ""
    if isinstance(error, dict):
        return str(error.get("category") or error.get("type") or error.get("message") or error)[:200]
    return str(error)[:200]


def ctrf_failures(result_file: Path) -> list[dict[str, Any]]:
    rollout_dir = result_file.parent
    ctrf_files = list((rollout_dir / "verifier").glob("ctrf.json"))
    failures: list[dict[str, Any]] = []
    for ctrf in ctrf_files:
        data = read_json(ctrf)
        tests = ((data.get("results") or {}).get("tests") or [])
        for test in tests:
            if test.get("status") != "passed":
                failures.append(
                    {
                        "name": test.get("name"),
                        "status": test.get("status"),
                        "message": str(test.get("message") or test.get("failure") or "")[:500],
                    }
                )
    return failures


def verifier_file_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": rel(path), "exists": False}
    data = read_json(path)
    if path.name == "calibration_log.json":
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        return {
            "path": rel(path),
            "exists": True,
            "phase": data.get("phase"),
            "heater_power_test": data.get("heater_power_test"),
            "points": len(rows),
            "duration": (
                round(float(rows[-1].get("time", 0)) - float(rows[0].get("time", 0)), 3)
                if len(rows) >= 2
                else None
            ),
        }
    if path.name == "control_log.json":
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        return {
            "path": rel(path),
            "exists": True,
            "phase": data.get("phase"),
            "setpoint": data.get("setpoint"),
            "points": len(rows),
            "duration": (
                round(float(rows[-1].get("time", 0)) - float(rows[0].get("time", 0)), 3)
                if len(rows) >= 2
                else None
            ),
        }
    keep = [
        "K",
        "tau",
        "r_squared",
        "fitting_error",
        "Kp",
        "Ki",
        "Kd",
        "lambda",
        "rise_time",
        "overshoot",
        "settling_time",
        "steady_state_error",
        "duration",
        "max_temp",
    ]
    return {"path": rel(path), "exists": True, **{key: data.get(key) for key in keep if key in data}}


def parse_period_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    patterns = [
        r"Period written to\s+/root/period\.txt:\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?:Best|Refined best|Narrow BLS best)\s+(?:BLS |TLS |transit )?period:?\s*(?:P=)?\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?:BLS|TLS)\s+best\s+period:?\s*([0-9]+(?:\.[0-9]+)?)",
        r"Best P\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = float(match.group(1))
            if not (1.0 < value < 30.0):
                continue
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 140)
            snippet = " ".join(text[start:end].split())
            mentions.append({"value": value, "evidence": snippet[:300]})
    unique: dict[tuple[float, str], dict[str, Any]] = {}
    for item in mentions:
        unique[(round(float(item["value"]), 6), item["evidence"][:80])] = item
    return list(unique.values())[:20]


def assertion_findings(result_file: Path) -> list[dict[str, Any]]:
    stdout = result_file.parent / "verifier" / "test-stdout.txt"
    text = read_text(stdout)
    if not text:
        return []
    findings: list[dict[str, Any]] = []
    exoplanet_match = re.search(
        r"AssertionError:\s*Period\s+([0-9]+(?:\.[0-9]+)?)\s+does not match expected\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+\(tolerance:\s*[^\d]*([0-9]+(?:\.[0-9]+)?)",
        text,
        flags=re.I | re.S,
    )
    if exoplanet_match:
        findings.append(
            {
                "path": rel(stdout),
                "kind": "period_mismatch",
                "actual": float(exoplanet_match.group(1)),
                "expected": float(exoplanet_match.group(2)),
                "tolerance": float(exoplanet_match.group(3)),
            }
        )
    hvac_match = re.search(
        r"AssertionError:\s*overshoot\s+\(([0-9]+(?:\.[0-9]+)?)\)\s+doesn't match computed\s+"
        r"\(([0-9]+(?:\.[0-9]+)?)\)",
        text,
        flags=re.I,
    )
    if hvac_match:
        findings.append(
            {
                "path": rel(stdout),
                "kind": "metric_mismatch",
                "metric": "overshoot",
                "reported": float(hvac_match.group(1)),
                "computed": float(hvac_match.group(2)),
                "absoluteDiff": abs(float(hvac_match.group(1)) - float(hvac_match.group(2))),
            }
        )
    if findings:
        return findings
    lines = [
        line.strip()
        for line in text.splitlines()
        if "AssertionError" in line or line.strip().startswith("E       ")
    ]
    return [{"path": rel(stdout), "kind": "assertion_line", "text": line[:300]} for line in lines[:8]]


def exoplanet_artifacts(result_file: Path) -> dict[str, Any]:
    rollout_dir = result_file.parent
    period_files = []
    for path in sorted(rollout_dir.rglob("period.txt")):
        text = read_text(path, max_chars=200).strip()
        try:
            value = float(text)
        except ValueError:
            value = None
        period_files.append({"path": rel(path), "text": text[:80], "value": value})

    mention_sources = [
        rollout_dir / "agent" / "acp_trajectory.jsonl",
        rollout_dir / "trajectory" / "acp_trajectory.jsonl",
        rollout_dir / "trainer" / "adp.jsonl",
        rollout_dir.parent / "adp.jsonl",
    ]
    mentions = []
    for path in mention_sources:
        if not path.exists():
            continue
        for item in parse_period_mentions(read_text(path)):
            mentions.append({"path": rel(path), **item})

    reward_path = rollout_dir / "verifier" / "reward.txt"
    return {
        "periodFiles": period_files,
        "periodMentions": mentions[:20],
        "rewardText": read_text(reward_path, max_chars=200).strip() if reward_path.exists() else "",
        "assertionFindings": assertion_findings(result_file),
    }


def hvac_artifacts(result_file: Path) -> dict[str, Any]:
    verifier_dir = result_file.parent / "verifier"
    expected = [
        "calibration_log.json",
        "estimated_params.json",
        "tuned_gains.json",
        "control_log.json",
        "metrics.json",
    ]
    files = [verifier_file_summary(verifier_dir / name) for name in expected]
    metrics = next((item for item in files if item["path"].endswith("/metrics.json") and item.get("exists")), {})
    checks = {
        "steadyStateErrorOk": metrics.get("steady_state_error") is not None and float(metrics["steady_state_error"]) < 0.5,
        "settlingTimeOk": metrics.get("settling_time") is not None and float(metrics["settling_time"]) < 120,
        "overshootOk": metrics.get("overshoot") is not None and float(metrics["overshoot"]) < 0.10,
        "durationOk": metrics.get("duration") is None or float(metrics["duration"]) >= 150,
        "maxTempOk": metrics.get("max_temp") is not None and float(metrics["max_temp"]) < 30,
    } if metrics else {}
    return {"files": files, "metricChecks": checks, "assertionFindings": assertion_findings(result_file)}


def task_artifacts(task: str, result_file: Path) -> dict[str, Any]:
    if task == "exoplanet-detection-period":
        return exoplanet_artifacts(result_file)
    if task == "hvac-control":
        return hvac_artifacts(result_file)
    return {}


def artifact_summary(task: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if task == "exoplanet-detection-period":
        values_by_outcome: dict[str, list[float]] = collections.defaultdict(list)
        verifier_mismatches = []
        missing_period_files = 0
        for row in rows:
            artifacts = row.get("taskArtifacts") or {}
            if not artifacts.get("periodFiles"):
                missing_period_files += 1
            for item in artifacts.get("periodFiles") or []:
                if isinstance(item.get("value"), (int, float)):
                    values_by_outcome[row["outcome"]].append(float(item["value"]))
            for item in artifacts.get("periodMentions") or []:
                if isinstance(item.get("value"), (int, float)):
                    values_by_outcome[row["outcome"]].append(float(item["value"]))
            for item in artifacts.get("assertionFindings") or []:
                if item.get("kind") == "period_mismatch":
                    verifier_mismatches.append({"outcome": row["outcome"], "rollout": row.get("rollout"), **item})
        return {
            "missingPeriodFiles": missing_period_files,
            "periodValuesByOutcome": {key: sorted(set(round(value, 6) for value in values)) for key, values in values_by_outcome.items()},
            "verifierMismatches": verifier_mismatches,
        }
    if task == "hvac-control":
        metric_rows = []
        metric_mismatches = []
        for row in rows:
            artifacts = row.get("taskArtifacts") or {}
            metrics = next(
                (
                    item
                    for item in artifacts.get("files") or []
                    if item.get("exists") and item.get("path", "").endswith("/metrics.json")
                ),
                {},
            )
            if metrics:
                metric_rows.append(
                    {
                        "outcome": row["outcome"],
                        "rollout": row.get("rollout"),
                        "rise_time": metrics.get("rise_time"),
                        "overshoot": metrics.get("overshoot"),
                        "settling_time": metrics.get("settling_time"),
                        "steady_state_error": metrics.get("steady_state_error"),
                        "duration": metrics.get("duration"),
                        "max_temp": metrics.get("max_temp"),
                    }
                )
            for item in artifacts.get("assertionFindings") or []:
                if item.get("kind") == "metric_mismatch":
                    metric_mismatches.append({"outcome": row["outcome"], "rollout": row.get("rollout"), **item})
        return {"metrics": metric_rows, "metricMismatches": metric_mismatches}
    return {}


def verifier_samples(result_file: Path) -> list[dict[str, Any]]:
    rollout_dir = result_file.parent
    candidates = [rollout_dir / "trainer" / "verifiers.jsonl", *rollout_dir.parent.glob("verifiers.jsonl")]
    samples = []
    for path in candidates:
        if path.exists():
            for row in read_jsonl(path, limit=2):
                compact = {
                    "path": rel(path),
                    "reward": row.get("reward"),
                    "score": row.get("score"),
                    "passed": row.get("passed"),
                    "error": str(row.get("error") or row.get("exception") or "")[:500],
                }
                samples.append(compact)
    return samples[:3]


def summarize(task: str, paths: list[Path]) -> dict[str, Any]:
    result_files = discover_result_files(paths, task)
    summary_files = discover_summary_files(paths)
    rows = []
    counters = {
        "outcome": collections.Counter(),
        "skillMode": collections.Counter(),
        "skillSource": collections.Counter(),
        "error": collections.Counter(),
    }
    total_tool_calls = 0
    total_skill_invocations = 0
    for result_file in result_files:
        result = read_json(result_file)
        outcome = result_outcome(result)
        skill_mode = str(result.get("skill_mode") or "")
        skill_source = str(result.get("skill_source") or "")
        error = error_label(result)
        tool_calls = int(result.get("n_tool_calls") or 0)
        skill_invocations = int(result.get("n_skill_invocations") or 0)
        total_tool_calls += tool_calls
        total_skill_invocations += skill_invocations
        counters["outcome"][outcome] += 1
        counters["skillMode"][skill_mode] += 1
        counters["skillSource"][skill_source] += 1
        if error:
            counters["error"][error] += 1
        rows.append(
            {
                "path": rel(result_file),
                "rollout": result.get("rollout_name"),
                "outcome": outcome,
                "reward": (result.get("rewards") or {}).get("reward") if isinstance(result.get("rewards"), dict) else None,
                "skillMode": skill_mode,
                "skillSource": skill_source,
                "toolCalls": tool_calls,
                "skillInvocations": skill_invocations,
                "error": error,
                "failedTests": ctrf_failures(result_file)[:5],
                "verifierSamples": verifier_samples(result_file),
                "taskArtifacts": task_artifacts(task, result_file),
            }
        )
    summaries = []
    for summary_file in summary_files:
        data = read_json(summary_file)
        if data.get("total") is None:
            continue
        summaries.append(
            {
                "path": rel(summary_file),
                "total": data.get("total"),
                "passed": data.get("passed"),
                "failed": data.get("failed"),
                "errored": data.get("errored"),
                "score": data.get("score"),
                "errorCategories": data.get("error_categories"),
                "elapsedSec": data.get("elapsed_sec"),
            }
        )
    return {
        "task": task,
        "inputs": [rel(path) for path in paths],
        "resultFiles": len(result_files),
        "summaryFiles": len(summaries),
        "outcomes": dict(counters["outcome"]),
        "skillModes": dict(counters["skillMode"]),
        "skillSources": dict(counters["skillSource"]),
        "errors": dict(counters["error"].most_common(10)),
        "totalToolCalls": total_tool_calls,
        "totalSkillInvocations": total_skill_invocations,
        "avgToolCalls": total_tool_calls / len(result_files) if result_files else 0,
        "avgSkillInvocations": total_skill_invocations / len(result_files) if result_files else 0,
        "artifactSummary": artifact_summary(task, rows),
        "summaries": summaries[:20],
        "rollouts": rows,
    }


def markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# Job Evidence Digest: {data['task']}",
        "",
        f"Result files: {data['resultFiles']}",
        f"Summary files: {data['summaryFiles']}",
        f"Outcomes: `{data['outcomes']}`",
        f"Skill modes: `{data['skillModes']}`",
        f"Total tool calls: {data['totalToolCalls']}",
        f"Total skill invocations: {data['totalSkillInvocations']}",
        "",
        "## Errors",
        "",
    ]
    if data["errors"]:
        for label, count in data["errors"].items():
            lines.append(f"- {count}x `{label}`")
    else:
        lines.append("- None recorded in result.json")
    lines.extend(["", "## Summary Files", "", "| Path | Total | Passed | Failed | Errored | Score |", "|---|---:|---:|---:|---:|---|"])
    for item in data["summaries"][:12]:
        lines.append(f"| {item['path']} | {item['total']} | {item['passed']} | {item['failed']} | {item['errored']} | {item['score']} |")
    if data.get("artifactSummary"):
        lines.extend(["", "## Task Artifacts", ""])
        if data["task"] == "exoplanet-detection-period":
            summary = data["artifactSummary"]
            lines.append(f"- Missing period files in rollout dirs: {summary.get('missingPeriodFiles')}")
            lines.append(f"- Period values mentioned by outcome: `{summary.get('periodValuesByOutcome')}`")
            if summary.get("verifierMismatches"):
                lines.append(f"- Verifier period mismatches: `{summary.get('verifierMismatches')}`")
        elif data["task"] == "hvac-control":
            lines.append("| Outcome | Rise | Overshoot | Settling | SSE | Duration | Max temp |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for row in data["artifactSummary"].get("metrics", [])[:20]:
                lines.append(
                    f"| {row.get('outcome')} | {row.get('rise_time')} | {row.get('overshoot')} | "
                    f"{row.get('settling_time')} | {row.get('steady_state_error')} | {row.get('duration')} | {row.get('max_temp')} |"
                )
            if data["artifactSummary"].get("metricMismatches"):
                lines.append("")
                lines.append(f"- Metric mismatches: `{data['artifactSummary'].get('metricMismatches')}`")
    lines.extend(["", "## Rollout Samples", "", "| Outcome | Skill mode | Tools | Skills | Path |", "|---|---|---:|---:|---|"])
    for item in data["rollouts"][:20]:
        lines.append(f"| {item['outcome']} | {item['skillMode']} | {item['toolCalls']} | {item['skillInvocations']} | {item['path']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--job-path", action="append", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    paths = [(ROOT / item).resolve() if not Path(item).is_absolute() else Path(item) for item in args.job_path]
    data = summarize(args.task, paths)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n" if args.json else markdown(data)
    if args.output:
        output = ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
