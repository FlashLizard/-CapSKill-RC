# Offline SkillRCA

Offline SkillRCA v2 analyzes a SkillsBench task using only:

- the task description
- a current skills library
- exactly five failed trajectories
- a 0/1 success value, sanitized failure summaries, and archived or trajectory-reconstructed final artifacts for each trajectory

Verifier/test source, rubric, hidden metrics, and local static skill scoring are
not included in any repair LLM prompt.

The repair is an explicit eight-stage repair LLM protocol:

1. input standardization: task description and each Skill are processed in separate rounds
2. capability graph construction and node-Skill coverage analysis
3. per-trajectory failure/cause event extraction, run in parallel
4. failure/cause-event-to-capability-node alignment
5. per-trajectory node execution judgments with deterministic local status calculation
6. per-node Skill repair suggestion generation, run in parallel
7. conditional clustering and merging of excessive `add_new_skill` actions
8. transactional Skill repair by individual suggestion or same-Skill suggestion package, with an LLM review before every commit

The script only orchestrates repair LLM calls, records prompts/transcripts, and
applies the final LLM-produced skill drafts. It does not locally infer repairs,
score skills, inspect verifier output, or write patch reviews.

## Code layout

The repair pipeline is split into stage-specific scripts:

```text
offline_skill_rca/
  prompt_templates/
    stage-01a-task-description-standardization.txt
    stage-01b-skill-standardization.txt
    stage-02-capability-graph.txt
    stage-03-failure-event-extraction.txt
    stage-04-failure-event-alignment.txt
    stage-05-node-execution-assessment.txt
    stage-06-skill-repair-suggestions.txt
    stage-07-repair-action-merge.txt
    stage-08-skill-repair.txt
    stage-08-skill-review.txt
  src/
    pipeline.py                    # orchestration, IO, schemas, compatibility wrappers
    stages/
      common.py                    # template loading and LLM call helpers
      stage_01_input_standardization.py
      stage_02_capability_graph.py
      stage_03_failure_event_extraction.py
      stage_04_failure_event_alignment.py
      stage_05_node_execution_assessment.py
      stage_06_skill_repair_suggestions.py
      stage_07_repair_action_merge.py
      stage_08_transactional_skill_repair.py
```

Edit the `.txt` files under `prompt_templates/` to tune stage instructions.
The Python stage modules inject the relevant JSON schema and visible evidence
after loading the template. Template placeholders use `{{name}}`, for example
`{{stage2_schema}}`, `{{node_execution_assessment_schema}}`, or `{{stage8_review_schema}}`.

The generated per-run prompts are still written to `<output-dir>/prompts/` so
you can inspect exactly what was sent to the repair LLM.

## Web 配置预设

Stage Debug 页面顶部的“配置预设”可以保存和复用当前整套设置，包括任务、轨迹、Skill 库、各阶段限制、Repair LLM 与独立 Review LLM 的 API URL、模型和 API Key。选择已有预设后可加载、覆盖保存或删除。

预设保存在工作区的 `.runner-config/repair-stage-presets.json`。完整设置由 Windows DPAPI 按当前登录用户加密，预设列表接口只返回名称和更新时间；只有显式加载某个预设时才会解密。因此该文件不能在另一个 Windows 用户下直接解密复用，也不应作为跨机器配置导出格式。

## Run repair

```powershell
cd E:\code\skillsbench-test\skillsbench
$env:OFFLINE_SKILL_RCA_API_KEY = '<strong-model-api-key>'
$env:OFFLINE_SKILL_RCA_BASE_URL = 'https://api.camel-hub.com'

.venv\Scripts\python.exe offline_skill_rca\run_offline_skill_rca.py `
  --task-dir tasks/r2r-mpc-control `
  --skills-dir skill-libraries/r2r-mpc-control/drop-state-space-linearization-20260701 `
  --traces jobs/web-runner/critical-skill-ablation-20260701/r2r-mpc-control `
  --output-dir repair-runs/r2r-mpc-control/offline-skill-rca-demo `
  --output-skills-dir skill-libraries/r2r-mpc-control/offline-skill-rca-demo `
  --strong-model gpt-5.5 `
  --trace-analysis-workers 5 `
  --stage7-repair-mode skill_package `
  --stage7-skill-package-size 3 `
  --use-separate-review-llm `
  --review-base-url https://review-api.example `
  --review-model review-model-name `
  --review-api-key '<review-model-api-key>' `
  --force
```

Stage 8 supports two transaction granularities. `per_suggestion` applies one
repair suggestion per repair/review transaction. `skill_package` groups at most
`--stage7-skill-package-size` `revise_existing_skill` suggestions that target
the same Skill. `add_new_skill` is always executed as its own transaction.

The separate review endpoint is optional. Without
`--use-separate-review-llm`, review calls reuse the repair LLM configuration.
Review API keys are never stored in the stage-debug manifest.

Important outputs:

```text
input_bundle.json                    # sanitized visible inputs only
offline_skill_rca_prompt.txt          # stage index and visibility contract
prompts/stage-01-*.prompt.txt         # per-stage prompts
llm_transcript/stage-01-*.request.json
llm_transcript/stage-01-*.response.json
llm_transcript/stage-01-*.parsed.json
stage_outputs.json                    # raw LLM output from every stage
trace_analyses.json                   # Stage 3 per-trajectory outputs
capability_graph.json                 # Stage 2 capability graph and coverage
offline_skill_rca_full.json           # final Stage 7 transactional repair result
```

## Validate repaired skills

```powershell
$env:DEEPSEEK_API_KEY = '<deepseek-api-key>'

.venv\Scripts\python.exe offline_skill_rca\validate_repaired_skills.py `
  --task-dir tmp/easy5-localimages/r2r-mpc-control `
  --skills-dir skill-libraries/r2r-mpc-control/offline-skill-rca-demo `
  --jobs-root jobs/offline-skill-rca-validation/r2r-demo `
  --model deepseek-v4-flash `
  --agent claude-agent-acp `
  --base-url https://api.deepseek.com `
  --repeats 1
```

The validator stores each repeat under `--jobs-root/run-N` and prints a compact
pass-rate summary after all repeats finish.
