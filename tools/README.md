# 本地工具

所有工具都只使用 Python 标准库，除了 `bench.py` 调用外部的 uv/Docker，以及需要 `httpx` 的 repair pipeline。命令都从项目根目录解析路径，Linux 和 Windows 使用同一套参数。

- `dataset.py`：下载/更新 SkillsBench，并把任务、附加任务和技能库接入当前项目。
- `bench.py`：检查 Docker/uv，构建单个任务容器，或启动一次 BenchFlow 评测。
- `llm_probe.py`：用 OpenAI chat-completions 协议测试任意供应商。
