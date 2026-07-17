# SkillsBench Portable

这是从实验工作区整理出的可迁移版本，包含：

- `runner-app/`：本地 Web 控制台，支持任务运行、harness/skills 库切换、jobs 扫描、轨迹查看和 Offline SkillRCA 调试。
- `offline_skill_rca/`：多阶段、可审计的 skills 修复流程，以及外置 prompt/schema。
- `skill-libraries/`：运行时接入或生成的技能库目录；仓库中故意不包含任何内容。
- `tools/`：数据集下载、容器构建、BenchFlow 运行和 OpenAI 兼容 LLM 探针。
- `scripts/`：Windows/Linux 启动脚本。

## 快速启动

要求：Node.js 18+、Python 3.12+、Git；实际运行 Docker sandbox 任务还需要 Docker 和 uv。
首次使用建议先安装 uv：<https://docs.astral.sh/uv/getting-started/installation/>。

```bash
cp .env.example .env.local
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python tools/dataset.py install --repo https://github.com/benchflow-ai/skillsbench.git
./scripts/start-web.sh
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env.local
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
py -3.12 tools\dataset.py install --repo https://github.com/benchflow-ai/skillsbench.git
.\scripts\start-web.ps1
```

浏览器打开 `http://localhost:5198/`。没有下载数据集时 web 仍可启动，但任务列表为空。

## 数据集管理

数据默认下载到 `.data/skillsbench`，然后以目录链接接入项目根目录；因此不会把数 GB 的任务环境、视频或 jobs 写进 git：

```bash
python3 tools/dataset.py status
python3 tools/dataset.py install --repo https://github.com/benchflow-ai/skillsbench.git --ref main
python3 tools/dataset.py install --mode copy --force
```

`--mode link` 在 Linux 使用符号链接，在 Windows 使用 junction；遇到权限策略时改用 `--mode copy`。
数据安装完成后，根目录下的 `tasks/`、`tasks-extra/` 与 `skill-libraries/` 才会出现；它们仍被 `.gitignore` 排除。

## 本地构建任务容器

```bash
python3 tools/bench.py check
python3 tools/bench.py build --task r2r-mpc-control
python3 tools/bench.py images status --task r2r-mpc-control
```

`build` 只构建指定 task 的 `environment/Dockerfile`，不会扫描和构建整个数据集。它会优先复用
`runner-app/prebuilt-images.json` 中已经登记的镜像名；如果 task 尚未登记，则生成稳定的
`skillsbench-local-<task>:latest` 标签，并自动登记。Web 控制台启动评测时会读取这个注册表，
把已构建镜像注入 task overlay，然后在运行期单独传入 skill mode 和 skills library，因此切换
`no-skill`、`with-skill`、`force-skill` 或不同 skills 库不需要重新构建镜像。

常用命令：

```bash
# 查看仓库中登记的 task -> image 映射
python3 tools/bench.py images list

# 检查当前机器哪些登记镜像已经存在
python3 tools/bench.py images status

# 只为某个 task 构建一次，并把镜像名写入注册表
python3 tools/bench.py build --task r2r-mpc-control

# 使用自定义镜像名构建并登记
python3 tools/bench.py build --task r2r-mpc-control --image my-registry/skillsbench/r2r:2026-07
```

迁移到另一台机器时，注册表文件会随 Git 一起迁移，但 Docker 镜像本身不会进入 Git。
在新机器安装数据集并完成 Docker 登录后，执行 `tools/bench.py images status`；对显示为
`missing` 的 task 再运行一次 `tools/bench.py build --task <task>` 即可。构建完成后，Web
会自动使用同一个镜像标签。若需要把注册表放到仓库外，可设置
`SKILLSBENCH_PREBUILT_IMAGES` 指向一个 JSON 文件，Windows 和 Linux 均支持。

## 用自定义 OpenAI 兼容供应商探针测试

`tools/llm_probe.py` 不绑定某个厂商，只调用 `/v1/chat/completions`，适合先验证 URL、模型和 key，再启动 repair：

```bash
export LLM_API_KEY='...'
python3 tools/llm_probe.py \
  --provider openai \
  --base-url https://api.example.com \
  --model example-model \
  --prompt 'Return JSON only: {"ok": true}'
```

`offline_skill_rca` 使用同一类 OpenAI-compatible 请求。正式 repair 请在 Web 的 Stage Debug 页面填写 repair LLM 配置，并把审查 transcript 保存在 `repair-runs/<task>/<variant>/llm_transcript/`。

## 运行评测

一次运行示例：

```bash
python3 tools/bench.py run \
  --task r2r-mpc-control \
  --agent claude-agent-acp \
  --provider deepseek \
  --model deepseek-v4-flash \
  --skills-dir skill-libraries/r2r-mpc-control/initial \
  --skill-mode with-skill \
  --reasoning-effort low \
  --jobs-dir jobs/local-r2r
```

评测 Web 页支持 `deepseek`、`anthropic`、`openai`、`custom` 四类供应商标签，以及
`no-skill`、`with-skill`、`force-skill` 三种模式。Claude Code 使用自定义供应商时，
`Base URL` 必须提供 Anthropic Messages 兼容入口；API key 可以只在当前表单使用，也可以
选择保存到浏览器配置。思维强度可选 `off`、`minimal`、`low`、`medium`、`high`、`max`、`xhigh`。
`force-skill` 会自动启用全部 skill 的 prompt overlay，并向 BenchFlow 传递 `with-skill`。

重复运行、并行度、force skills、轨迹查看和 group 管理优先使用 Web 控制台；CLI 适合 Linux CI 或脚本化实验。

## 迁移注意事项

1. API key 只放 `.env.local` 或环境变量，不放 prompt、git、URL 查询参数或 jobs 日志。
2. Docker 在 Linux 下通常要求当前用户属于 `docker` 组；否则先用 `sudo docker` 验证权限，再调整 daemon 权限。
3. Windows 的目录链接可能被策略阻止，数据安装脚本会提示改用 `--mode copy`。
4. DeepSeek 对 `reasoning_effort` 的取值与部分网关不同；便携版默认使用 `low`，可通过 `OFFLINE_SKILL_RCA_REASONING_EFFORT=off` 关闭。
5. Repair 默认尊重 `HTTP_PROXY` / `HTTPS_PROXY`，并使用兼容网关常见的 `OpenAI/1.0` User-Agent；不需要代理时设置 `OFFLINE_SKILL_RCA_TRUST_ENV=0`。
6. `.data/`、`tasks/`、`jobs/`、`repair-runs/` 都是运行态数据，故意不进入新仓库。
