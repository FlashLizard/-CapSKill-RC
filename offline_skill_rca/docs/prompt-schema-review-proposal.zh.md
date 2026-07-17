# Offline SkillRCA Prompt 与 Schema 精简提案（中文）

> 已于 2026-07-15 应用到 `prompt_templates/` 与运行流程。旧阶段产物仍可通过本地兼容适配器读取。

## 总体原则

- 每轮只完成一个阶段目标。
- Prompt 只说明任务、边界、输入和输出，不重复解释 pipeline。
- LLM 只做语义判断；计数、聚合、权重、标签和公式全部由代码计算。
- Schema 只保留下游真正读取的字段。
- 不提供 verifier 过程信息，不要求模型声称修复后一定通过。

## System Prompt

```text
你负责仅使用当前 Prompt 明确提供的输入修复 agent Skill library。
当前阶段：{{stage_name}}

下表定义项目概念名。Schema 字段和占位符是接口标识，保留原始 snake_case。

项目术语表：
| 术语 | 定义 |
| --- | --- |
| task contract | 从任务原文提取的输入、输出、约束和成功条件。 |
| Skill | Skill 库中的一个可复用能力单元，包含触发条件、操作、验证、恢复及附加文件。 |
| Skill library | 本次分析的 Skill 集合；Stage 7 只修改其工作副本。 |
| Skill summary | Stage 1b 从一个 Skill 及其附加文件提取的结构化表示，不等同于原始文件全文。 |
| capability node | 完成任务所需的一项具体、可复用能力。 |
| capability graph | capability node 及其依赖边组成的任务能力图。 |
| coverage pair | 一个 capability node 与一个 Skill 的直接相关性和覆盖维度分析。 |
| bad event | 轨迹中有可见证据支持的错误或遗漏行为。 |
| node status | pass、fail、skipped、blocked 或 unknown；blocked 是上游阻塞，不是该节点的直接失败。 |
| repair action | 可执行的 revise_existing_skill 或 add_new_skill。 |
| repair unit | Stage 7 中一次事务处理的单条建议或同一 Skill 的建议包。 |
| candidate files | Repair LLM 生成、尚未提交的完整候选文件。 |
| commit | 本地校验和 Review 均通过后，将候选原子应用到 Skill 库副本。 |

项目流程表：
| Stage | 职责 | 主要输出 |
| --- | --- | --- |
| 1a | 标准化任务原文 | task contract |
| 1b | 逐个标准化 Skill | Skill summaries |
| 2 | 构造 capability graph 并分析 coverage pair | capability graph、coverage pairs |
| 3 | 并行分析每条轨迹 | bad events、node status |
| 4 | 将 bad event 对齐到 capability node | bad event-node mappings |
| 5 | 为需要修复的 capability node 并行生成建议 | repair actions |
| 6 | 必要时合并过多新增操作 | ordered repair actions |
| 7a | 按 repair unit 生成完整候选文件 | candidate files |
| 7b | 审查 candidate files；通过才提交，否则重试 | review decision、commit 或 retry |

规则：
1. 只执行当前阶段，不提前完成后续阶段。
2. 保持已有 ID 不变，除非当前阶段明确要求生成新 ID。
3. 严格按当前阶段 Schema 返回一个 JSON 对象，不输出 Markdown 或额外文本。
```

## Stage 1a：任务标准化

**建议 Prompt**

```text
仅根据 task_description 提取 task contract。不要解题，不要补充未明确给出的要求。
返回严格 JSON，符合：
{{task_standardization_schema}}

# task_description:
<task_description>
```

**建议 Schema**

```json
{
  "task_id": "string",
  "summary": "string",
  "inputs": [],
  "required_outputs": [],
  "constraints": [],
  "success_criteria": [],
  "ambiguities": []
}
```

建议删除 `original_description`、`task_type` 和包装层 `normalized_task/stage_notes`：原文已作为输入保存，任务类型不是后续必要字段。

## Stage 1b：单个 Skill 标准化

**建议 Prompt**

```text
仅根据当前 Skill 文档及其附加文件提取可见能力。不要评价质量，不要推断未写明的行为。
附加文件为空时，attached_files 返回 null。
返回严格 JSON，符合：
{{skill_standardization_schema}}

# skill_file:
<skill_file>

# skill_attached_files:
<skill_attached_files>
```

**建议 Schema**

```json
{
  "title": "string",
  "intent": "string",
  "triggers": [],
  "inputs": [],
  "outputs": [],
  "procedure": [],
  "verification": [],
  "recovery": [],
  "tools_or_templates": [],
  "limits": [],
  "attached_files": [
    {"path": "string", "type": "code|document|others", "content": "string"}
  ]
}
```

建议拆分 `verification_or_recovery`，因为后续覆盖分析分别评价这两个维度；删除 `visible_sections`，其信息已被结构字段覆盖。

## Stage 2：能力图与 Skill 覆盖

**建议 Prompt**

```text
根据 task contract 构造完整 capability graph；Skill summaries 只用于后续覆盖分析，不能限制 capability graph 包含哪些 capability node。
capability graph 描述完成任务所需的能力。每个 capability node 对应一种具体、可复用能力，边 A -> B 表示 B 依赖 A。
再分析每个 coverage pair（capability node × Skill）。即使没有 Skill 覆盖，也必须保留任务所需的 capability node。
每个 coverage pair 必须有一条记录。先判断 directly_relevant：为 false 时 scores 返回 null；为 true 时给出语义评分和证据。若 execution_support_need 为 not_needed，则 scores.execution_support 返回 null。
不要计算 overall、gap、labels 或聚合结果。
返回严格 JSON，符合：
{{stage2_schema}}

# task_contract
<stage_01a_task_description_standardization>

# skill_summaries:
<stage_01b_skill_standardizations>
```

**建议 Schema**

```json
{
  "capability_graph": {
    "nodes": [
      {
        "node_id": "N1",
        "goal": "string",
        "inputs": [],
        "outputs": [],
        "operations": [],
        "checks": []
      }
    ],
    "edges": [{"from": "N1", "to": "N2","description":"why does N2 rely on N1."}]
  },
  "coverage_pairs": [
    {
      "node_id": "N1",
      "skill_id": "string",
      "directly_relevant": true,
      "relevance_reason": "string",
      "scores": {
        "requirement_fit": 0.0,
        "trigger": 0.0,
        "procedure": 0.0,
        "verification": 0.0,
        "recovery": 0.0,
        "execution_support": 0.0
      },
      "execution_support_need": "not_needed|helpful|required",
      "evidence": []
    }
  ]
}
```

建议删除 `overall_description`、`common_failure_modes`、`stage_notes`、`overall_coverage`、`coverage_gap`、`coverage_labels`、`missing_slots` 和 `calculation`；后五项应由代码生成。

## Stage 3：逐轨迹失败事件抽取

**建议 Prompt**

```text
仅根据当前格式化轨迹、task contract、Skill summaries 和 capability graph，抽取有证据支持的 bad event，并标注每个 capability node 的 node status。
状态只能是 pass、fail、skipped、blocked、unknown。
错误策略归入 fail。
blocked 仅表示上游失败或缺少前置条件导致无法尝试，不是该节点的直接失败。
不要把推测写成事实。
success 只复制输入中给定的 0/1 结果，不根据轨迹重新判断。
返回严格 JSON，符合：
{{trace_analysis_schema}}

# task_contract
<stage_01a_task_description_standardization>

# skill_summaries
<stage_01b_skill_standardizations>

# capability_graph
<stage_02_capability_graph>

# trajectory
<trajectory>
```

**建议 Schema**

```json
{
  "traj_id": "string",
  "success": 0,
  "bad_events": [
    {
      "event_id": "string",
      "step_id": 1,
      "observed": "string",
      "expected": "string",
      "consequence": "string",
      "severity": "minor|major|fatal",
      "first_actionable": true,
      "skill_usage": [
          {"skill_id": "string", "status": "used|ignored|misused|unclear", "evidence": "string"}
      ],
      "evidence": "string"
    }
  ],
  "node_status": {
    "N1": {"status": "pass|fail|skipped|blocked|unknown", "reason": "string"}
  },
  "evidence_limits": []
}
```

每个 `bad_event.skill_usage` 只记录与该事件直接相关的 Skill 使用情况。建议删除 `trajectory_summary` 和 `candidate_skill_gaps`：前者可由事件汇总生成，后者会提前进行 Stage 5 的归因工作。

## Stage 4：失败事件对齐

**建议 Prompt**

```text
根据 capability graph 分析 failure_events_by_trace，将每个 bad event 对齐到一个最相关的 capability node；若无关则 node_id 返回 null。
每个 bad event 必须且只能出现一次。理由只引用 bad event 与 capability node 定义。
返回严格 JSON，符合：
{{failure_event_alignment_schema}}

# capability_graph
<stage_02_capability_graph>

# failure_events_by_trace
<stage_03_failure_events_by_trace>
```

**建议 Schema**

```json
{
  "alignments": [
    {
      "traj_id": "string",
      "event_id": "string",
      "node_id": "N1|null",
      "confidence": 0.0,
      "reason": "string"
    }
  ]
}
```

建议把无关事件的分数统一为 `0`，不要使用范围外的 `-1`。

## Stage 5：逐节点修复建议

**建议 Prompt**

```text
针对所选 capability node 和给定 repair action，生成最小、可执行、可泛化的 Skill 修复建议。
只处理该 capability node，并按指定动作返回可执行建议。
根据 repair action（接口字段 node_repair_action）使用对应返回结构：
{{stage6_schema}}

# capability_graph
<stage_02_capability_graph>

# node_id
<node_id>

# node_repair_action
<node_repair_action>

# node_repair_bound_evidence
<node_bound_evidence>

# related_skill
<node_related_skill_library>

# skill_summaries
<stage_01b_skill_standardizations>
```

**建议 Schema：修复已有 Skill**

```json
{
  "node_id": "N1",
  "action": "revise_existing_skill",
  "issue": "string",
  "repairs": [
    {
      "suggestion_id": "string",
      "skill_id": "string",
      "goal": "string",
      "changes": [{"area": "trigger|procedure|verification|recovery|execution_support", "instruction": "string"}],
      "evidence_refs": [{"traj_id": "string", "event_id": "string"}],
      "constraints": []
    }
  ]
}
```

**建议 Schema：新增 Skill**

```json
{
  "node_id": "N1",
  "action": "add_new_skill",
  "issue": "string",
  "new_skill": {
    "suggestion_id": "string",
    "skill_id": "string",
    "goal": "string",
    "triggers": [],
    "procedure": [],
    "verification": [],
    "recovery": [],
    "attached_files": [],
    "evidence_refs": [{"traj_id": "string", "event_id": "string"}],
    "constraints": []
  }
}
```

证据权重、节点压力和执行顺序由代码确定。

## Stage 6：新增操作合并

**建议 Prompt**

```text
把所有 add_new_skill repair action 完整划分为 merge_config.resolved_target_cluster_count 个语义簇。仅当 max_new_skill_count > 0 时，簇数不得超过该硬上限。
每条建议恰好出现一次。每簇选择一个现有建议作为 root；不要新增、删除或改写 suggestion_id。
只返回聚类，不写 Skill 文件。
返回严格 JSON，符合：
{{repair_action_merge_schema}}

# merge_config
<merge_config>

# max_new_skill_count
<max_new_skill_count>

# add_new_skill_actions
<add_new_skill_actions>
```

**建议 Schema**

```json
{
  "clusters": [
    {
      "root_suggestion_id": "string",
      "member_suggestion_ids": [],
      "unified_scope": "string"
    }
  ]
}
```

建议删除 `cluster_id` 和重复的 rationale；簇序号可由代码生成。

## Stage 7a：生成候选文件

**建议 Prompt**

```text
根据当前 repair unit 修改或创建目标 Skill，并返回 candidate files。
candidate files 必须包含所有修改文件的完整最终内容，不返回 diff。只允许写入 allowed_skill_root。
实现每条建议并遵守 anti-overfit、上一轮反馈和字数限制。不要加入任务答案、verifier 信息或轨迹常量。
返回严格 JSON，符合：
{{stage8_repair_schema}}

# repair_unit_id
<repair_unit_id>

# suggestion_ids
<suggestion_ids>

# selected_suggestions
<selected_stage6_suggestions>

# repair_action
<repair_action>

# allowed_skill_root
<allowed_skill_root>

# current_related_files
<current_related_files>

# current_skill_library_inventory
<current_skill_library_inventory>

# previous_review_feedback
<previous_review_feedback>

# skill_word_limit
<skill_word_limit>
```

**建议 Schema**

```json
{
  "repair_unit_id": "string",
  "suggestion_ids": [],
  "files": [
    {"path": "string", "content": "complete file content"}
  ]
}
```

建议删除返回字段 `action`、`summary` 和逐文件 `change_reason`：输入中已有 action，审查应依据建议和文件正文，而不是模型自述。

## Stage 7b：审查候选文件

**建议 Prompt**

```text
审查 candidate files 是否完整实现当前 repair unit，且范围正确、文件完整可用、未与现有 Skill 重复或冲突。
本地路径、标识、完整性和字数校验已经通过；本调用只进行语义审查。
不要改写文件；reject 时只给可直接执行的重试指令。
仅当四项 checks 全为 true 且 issues 为空时才能 accept；accept 时 retry_instructions 必须为空。
返回严格 JSON，符合：
{{stage8_review_schema}}

# repair_unit_id
<repair_unit_id>

# suggestion_ids
<suggestion_ids>

# selected_suggestions
<selected_stage6_suggestions>

# files_before_this_attempt
<files_before_this_attempt>

# candidate_modified_files
<candidate_modified_files>

# current_skill_library_inventory
<current_skill_library_inventory>

# related_skill_summaries
<related_skill_summaries>

# skill_word_limit
<skill_word_limit>
```

**建议 Schema**

```json
{
  "repair_unit_id": "string",
  "suggestion_ids": [],
  "decision": "accept|reject",
  "checks": {
    "suggestions_satisfied": true,
    "scope_preserved": true,
    "files_usable": true,
    "library_consistent": true
  },
  "issues": [{"code": "string", "message": "string"}],
  "retry_instructions": []
}
```

建议删除 `review_summary` 和 `candidate_change_summary`：结论已由 decision/checks/issues 完整表达，候选摘要不是可靠审查证据。本地校验失败时，代码记录错误并直接返回 Stage 7a Repair，不调用 Review。
