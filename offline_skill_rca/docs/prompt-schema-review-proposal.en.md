# Offline SkillRCA Prompt and Schema Simplification Proposal (English)

> Applied to `prompt_templates/` and the runtime pipeline on 2026-07-15. Legacy stage outputs remain readable through local compatibility adapters.

## Principles

- Each call has one stage objective.
- Prompts state the task, boundaries, inputs, and output contract only.
- The LLM makes semantic judgments; code computes counts, aggregates, weights, labels, and formulas.
- Schemas contain only fields consumed downstream.
- Never expose verifier internals or ask the model to claim that a repair will pass.

## System Prompt

```text
You repair an agent Skill library using only inputs explicitly supplied in the current prompt.
Current stage: {{stage_name}}

The table below defines project concept names. Schema fields and placeholders are interface identifiers and retain their original snake_case.

Project glossary:
| Term | Definition |
| --- | --- |
| task contract | Inputs, outputs, constraints, and success criteria extracted from the task text. |
| Skill | A reusable library unit containing triggers, procedures, verification, recovery, and optional attached files. |
| Skill library | The Skill collection under analysis; Stage 7 modifies only its working copy. |
| Skill summary | A structured representation extracted by Stage 1b from one Skill and its attached files; it is not the complete raw file text. |
| capability node | One concrete reusable capability required by the task. |
| capability graph | Capability nodes and the dependency edges between them. |
| coverage pair | Direct relevance and coverage dimensions for one capability node and one Skill. |
| bad event | An erroneous or omitted trajectory behavior supported by visible evidence. |
| node status | pass, fail, skipped, blocked, or unknown; blocked is an upstream consequence, not a direct failure of that node. |
| repair action | An executable revise_existing_skill or add_new_skill. |
| repair unit | One suggestion or a same-Skill suggestion package processed transactionally in Stage 7. |
| candidate files | Complete files produced by the Repair LLM but not yet committed. |
| commit | Atomic application of candidate files to the copied Skill library after local validation and Review pass. |

Project workflow:
| Stage | Responsibility | Main output |
| --- | --- | --- |
| 1a | Standardize the task text | task contract |
| 1b | Standardize each Skill | Skill summaries |
| 2 | Build the capability graph and analyze coverage pairs | capability graph, coverage pairs |
| 3 | Analyze each trajectory in parallel | bad events, node status |
| 4 | Align bad events to capability nodes | bad event-node mappings |
| 5 | Generate suggestions in parallel for capability nodes requiring repair | repair actions |
| 6 | Merge excessive new-Skill actions when required | ordered repair actions |
| 7a | Generate complete candidate files for one repair unit | candidate files |
| 7b | Review locally valid candidate files; commit on acceptance or retry on rejection | review decision, commit or retry |

Rules:
1. Perform the current stage only; do not perform later-stage work early.
2. Preserve existing IDs unless the current stage explicitly requires a new ID.
3. Return exactly one JSON object matching the current stage schema, with no Markdown or extra text.
```

## Stage 1a: Task Standardization

**Proposed prompt**

```text
Extract the task contract from task_description only. Do not solve the task or add unstated requirements.
Return strict JSON matching:
{{task_standardization_schema}}

# task_description:
<task_description>
```

**Proposed schema**

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

Remove `original_description`, `task_type`, and the `normalized_task/stage_notes` wrapper. The source text is already archived and task type is not required downstream.

## Stage 1b: Skill Standardization

**Proposed prompt**

```text
Extract visible capabilities from this Skill document and its attached files only. Do not score quality or infer undocumented behavior.
Return attached_files as null when no attached files are supplied.
Return strict JSON matching:
{{skill_standardization_schema}}

# skill_file:
<skill_file>

# skill_attached_files:
<skill_attached_files>
```

**Proposed schema**

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

Split `verification_or_recovery` because later coverage analysis scores them separately. Remove `visible_sections`, whose useful content is represented by the structured fields.

## Stage 2: Capability Graph and Skill Coverage

**Proposed prompt**

```text
Build the complete capability graph from the task contract. Use Skill summaries only for subsequent coverage analysis; they must not limit which capability nodes appear in the graph.
The capability graph describes the capabilities required to complete the task. Each capability node is one concrete reusable capability, and an edge A -> B means B depends on A.
Then analyze every coverage pair (capability node × Skill). Keep every task-required capability node even when no Skill covers it.
Return one record for every coverage pair. Judge directly_relevant first: return null scores when it is false, and semantic scores plus evidence when it is true. If execution_support_need is not_needed, return null for scores.execution_support.
Do not calculate overall coverage, gaps, labels, or aggregates.
Return strict JSON matching:
{{stage2_schema}}

# task_contract
<stage_01a_task_description_standardization>

# skill_summaries:
<stage_01b_skill_standardizations>
```

**Proposed schema**

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
    "edges": [{"from": "N1", "to": "N2", "description": "why N2 depends on N1"}]
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

Remove `overall_description`, `common_failure_modes`, `stage_notes`, `overall_coverage`, `coverage_gap`, `coverage_labels`, `missing_slots`, and `calculation`. Code should produce the last five.

## Stage 3: Per-Trajectory Failure Events

**Proposed prompt**

```text
Using only the formatted trajectory, task contract, Skill summaries, and capability graph, extract evidence-supported bad events and assign node status to every capability node.
Statuses are pass, fail, skipped, blocked, or unknown.
Classify a wrong strategy as fail.
Use blocked only when an upstream failure or missing prerequisite prevented a meaningful attempt; blocked is not a direct failure of that node.
Do not present speculation as fact.
Copy the supplied 0/1 success value exactly; do not reassess it from the trajectory.
Return strict JSON matching:
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

**Proposed schema**

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

Each `bad_event.skill_usage` records only Skill usage directly related to that event. Remove `trajectory_summary` and `candidate_skill_gaps`. The former can be derived from events; the latter prematurely performs Stage 5 attribution.

## Stage 4: Failure-Event Alignment

**Proposed prompt**

```text
Using the capability graph, analyze failure_events_by_trace and align every bad event to its single most relevant capability node; return null when it is unrelated.
Each bad event must appear exactly once. Base the reason only on the bad event and capability node definition.
Return strict JSON matching:
{{failure_event_alignment_schema}}

# capability_graph
<stage_02_capability_graph>

# failure_events_by_trace
<stage_03_failure_events_by_trace>
```

**Proposed schema**

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

Use confidence `0` for unrelated events instead of the out-of-range sentinel `-1`.

## Stage 5: Per-Node Repair Suggestions

**Proposed prompt**

```text
For the selected capability node and repair action, produce minimal, actionable, reusable Skill repair guidance.
Handle this capability node only and return the requested repair action with an executable suggestion.
Use the response structure corresponding to the repair action (interface field node_repair_action):
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

**Proposed schema: revise an existing Skill**

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

**Proposed schema: add a Skill**

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

Evidence weights, node pressure, and execution order are determined by code.

## Stage 6: New-Skill Action Merge

**Proposed prompt**

```text
Partition all add_new_skill repair actions into exactly merge_config.resolved_target_cluster_count semantic clusters. Enforce max_new_skill_count as a hard cap only when it is greater than zero.
Each suggestion must appear exactly once. Select one existing suggestion as each root; do not add, remove, or rewrite suggestion IDs.
Return clustering only; do not write Skill files.
Return strict JSON matching:
{{repair_action_merge_schema}}

# merge_config
<merge_config>

# max_new_skill_count
<max_new_skill_count>

# add_new_skill_actions
<add_new_skill_actions>
```

**Proposed schema**

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

Remove `cluster_id` and the duplicate rationale. Code can assign cluster ordinals.

## Stage 7a: Candidate File Generation

**Proposed prompt**

```text
Modify or create the target Skill according to the current repair unit and return candidate files.
Candidate files must contain complete final content for every modified file, never a diff. Write only under allowed_skill_root.
Implement every suggestion and obey anti-overfit constraints, previous feedback, and the size limit. Do not add task answers, verifier information, or trajectory constants.
Return strict JSON matching:
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

**Proposed schema**

```json
{
  "repair_unit_id": "string",
  "suggestion_ids": [],
  "files": [
    {"path": "string", "content": "complete file content"}
  ]
}
```

Remove returned `action`, `summary`, and per-file `change_reason`. Action is an input, and review should rely on suggestions plus file content rather than the generator's self-report.

## Stage 7b: Candidate Review

**Proposed prompt**

```text
Review whether the candidate files fully implement the repair unit, preserve scope, contain complete usable files, and do not duplicate or conflict with the current Skill library.
Local path, identity, completeness, and size validation has already passed; this call performs semantic review only.
Do not rewrite files. On rejection, return only directly actionable retry instructions.
Accept only when all four checks are true and issues is empty. retry_instructions must be empty on acceptance.
Return strict JSON matching:
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

**Proposed schema**

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

Remove `review_summary` and `candidate_change_summary`. Decision, checks, and issues already express the conclusion; a generator-authored candidate summary is not review evidence. When local validation fails, code records the errors and returns directly to Stage 7a Repair without calling Review.
