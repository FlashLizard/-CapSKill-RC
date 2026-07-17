import {
  renderRawJsonBlock,
  renderReadableContent,
  renderReadableLlmText,
} from "./repair-readable.js";

const TABS = [
  { id: "overview", label: "概览" },
  { id: "exchange", label: "请求+响应" },
  { id: "request", label: "请求" },
  { id: "response", label: "响应" },
  { id: "parsed", label: "解析结果" },
  { id: "inputs", label: "输入证据" },
  { id: "files", label: "产物文件" },
];

const state = {
  runs: [],
  selectedRunDir: "",
  detail: null,
  tab: "overview",
  runQuery: "",
  textQuery: "",
};

const els = {
  subtitle: document.getElementById("traceSubtitle"),
  runSearch: document.getElementById("traceRunSearch"),
  textSearch: document.getElementById("traceTextSearch"),
  refresh: document.getElementById("refreshTraceRuns"),
  runCount: document.getElementById("traceRunCount"),
  runList: document.getElementById("traceRunList"),
  title: document.getElementById("traceDetailTitle"),
  meta: document.getElementById("traceDetailMeta"),
  tabs: document.getElementById("traceTabs"),
  detail: document.getElementById("traceDetail"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlight(value) {
  const text = escapeHtml(value ?? "");
  const query = state.textQuery.trim();
  if (!query) return text;
  const re = new RegExp(`(${escapeRegExp(query)})`, "gi");
  return text.replace(re, "<mark>$1</mark>");
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let n = size;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function scoreText(value) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : String(value);
}

function isJsonProblem(value) {
  return value && (value.__tooLarge || value.__parseError);
}

function stringifyJson(value) {
  if (value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

async function fetchJson(url) {
  const res = await fetch(url);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function loadRuns() {
  els.subtitle.textContent = "正在扫描 repair-runs 下的 Repair LLM transcript...";
  try {
    const data = await fetchJson("/api/repair-traces");
    state.runs = data.runs || [];
    if (!state.selectedRunDir && state.runs[0]) {
      state.selectedRunDir = state.runs[0].runDir;
    }
    renderRuns();
    if (state.selectedRunDir) {
      await loadDetail(state.selectedRunDir);
    } else {
      els.subtitle.textContent = "没有找到包含 llm_transcript/*.request.json 的修复运行。";
      renderDetail();
    }
  } catch (error) {
    els.subtitle.textContent = `加载失败：${error.message}`;
    els.detail.innerHTML = `<div class="detail-empty">加载失败：${escapeHtml(error.message)}</div>`;
  }
}

async function loadDetail(runDir) {
  state.selectedRunDir = runDir;
  state.detail = null;
  renderRuns();
  els.title.textContent = "正在加载...";
  els.meta.textContent = runDir;
  els.detail.innerHTML = `<div class="detail-empty">正在读取 ${escapeHtml(runDir)}...</div>`;
  try {
    const params = new URLSearchParams({ runDir });
    const data = await fetchJson(`/api/repair-trace?${params.toString()}`);
    state.detail = data.trace;
    renderDetail();
  } catch (error) {
    els.detail.innerHTML = `<div class="detail-empty">读取失败：${escapeHtml(error.message)}</div>`;
  }
}

function filteredRuns() {
  const query = state.runQuery.trim().toLowerCase();
  if (!query) return state.runs;
  return state.runs.filter((run) => {
    const haystack = [
      run.taskName,
      run.variant,
      run.runDir,
      run.model,
      run.endpoint,
      run.taskDir,
      run.sourceSkillsDir,
      run.outputSkillsDir,
    ].join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function renderRuns() {
  const runs = filteredRuns();
  els.runCount.textContent = `${runs.length}/${state.runs.length}`;
  if (!runs.length) {
    els.runList.innerHTML = `<div class="detail-empty">没有匹配的修复运行。</div>`;
    return;
  }
  els.runList.innerHTML = runs.map((run) => {
    const selected = run.runDir === state.selectedRunDir ? " selected" : "";
    const counts = run.counts || {};
    const statusClass = run.statusCode && run.statusCode >= 400 ? "status-error" : "status-ok";
    return `
      <button class="repair-trace-run${selected}" data-run-dir="${escapeAttr(run.runDir)}">
        <div class="repair-trace-run-main">
          <strong title="${escapeAttr(run.taskName)}">${escapeHtml(run.taskName || "(unknown task)")}</strong>
          <span title="${escapeAttr(run.variant)}">${escapeHtml(run.variant || run.runDir)}</span>
        </div>
        <div class="repair-trace-run-meta">
          <span class="status-pill ${statusClass}">HTTP ${escapeHtml(run.statusCode ?? "-")}</span>
          <span>${escapeHtml(run.model || "-")}</span>
          <span>${formatDate(run.modifiedAt)}</span>
        </div>
        <div class="repair-trace-run-counts">
          <span>${counts.failedTrajectories || 0} traces</span>
          <span>${counts.hypotheses || 0} causes</span>
          <span>${counts.patches || 0} patches</span>
        </div>
      </button>
    `;
  }).join("");
}

function activeResult() {
  const detail = state.detail || {};
  if (detail.full && !isJsonProblem(detail.full)) return detail.full;
  if (detail.parsed && !isJsonProblem(detail.parsed)) return detail.parsed;
  return {};
}

function renderTabs() {
  els.tabs.innerHTML = TABS.map((tab) => `
    <button class="${tab.id === state.tab ? "active" : ""}" data-tab="${tab.id}">${tab.label}</button>
  `).join("");
}

function renderDetail() {
  const detail = state.detail;
  renderTabs();
  if (!detail) {
    els.title.textContent = "详情";
    els.meta.textContent = "";
    els.detail.innerHTML = `<div class="detail-empty">选择一个 Repair LLM 轨迹。</div>`;
    return;
  }
  const summary = detail.summary || {};
  els.title.textContent = summary.taskName || "Repair LLM 轨迹";
  els.meta.textContent = [summary.variant, summary.model, formatDate(summary.modifiedAt)].filter(Boolean).join(" · ");
  const renderers = {
    overview: renderOverview,
    exchange: renderExchange,
    request: renderRequest,
    response: renderResponse,
    parsed: renderParsed,
    inputs: renderInputs,
    files: renderFiles,
  };
  els.detail.innerHTML = (renderers[state.tab] || renderOverview)(detail);
}

function renderStat(label, value, note = "") {
  return `
    <div class="repair-trace-stat">
      <strong>${escapeHtml(value)}</strong>
      <span>${escapeHtml(label)}</span>
      ${note ? `<small>${escapeHtml(note)}</small>` : ""}
    </div>
  `;
}

function renderKvTable(rows) {
  return `
    <div class="repair-trace-kv">
      ${rows.map(([key, value]) => `
        <div><span>${escapeHtml(key)}</span><strong title="${escapeAttr(value ?? "")}">${escapeHtml(value ?? "-")}</strong></div>
      `).join("")}
    </div>
  `;
}

function renderOverview(detail) {
  const summary = detail.summary || {};
  const counts = summary.counts || {};
  const usage = summary.usage || {};
  const result = activeResult();
  const hypotheses = asArray(result.root_cause_hypotheses);
  const patches = asArray(result.skill_patch_plan);
  const blockers = asArray(result.non_skill_blockers);
  return `
    <div class="repair-trace-stat-grid">
      ${renderStat("失败轨迹", counts.failedTrajectories || 0, "输入证据")}
      ${renderStat("能力节点", counts.capabilityNodes || counts.s0Nodes || 0, "能力图")}
      ${renderStat("Failure Events", counts.faultCards || 0, "故障证据")}
      ${renderStat("Root Causes", counts.hypotheses || 0, "归因假设")}
      ${renderStat("Patches", counts.patches || 0, "修复计划")}
      ${renderStat("Drafts", counts.drafts || 0, "技能草稿")}
    </div>

    <div class="repair-trace-two-col">
      <section class="repair-trace-section">
        <h3>运行信息</h3>
        ${renderKvTable([
          ["Run Dir", summary.runDir],
          ["Task", summary.taskName],
          ["Variant", summary.variant],
          ["Task Dir", summary.taskDir],
          ["源 Skills", summary.sourceSkillsDir],
          ["输出 Skills", summary.outputSkillsDir],
          ["Model", summary.model],
          ["Endpoint", summary.endpoint],
        ])}
      </section>
      <section class="repair-trace-section">
        <h3>LLM 调用</h3>
        ${renderKvTable([
          ["HTTP", summary.statusCode ?? "-"],
          ["Interactions", summary.interactionCount ?? asArray(detail.interactions).length],
          ["Prompt Tokens", usage.prompt_tokens ?? usage.input_tokens ?? "-"],
          ["Completion Tokens", usage.completion_tokens ?? usage.output_tokens ?? "-"],
          ["Total Tokens", usage.total_tokens ?? "-"],
          ["Request", formatBytes(summary.files?.request?.size)],
          ["Response", formatBytes(summary.files?.response?.size)],
          ["Parsed", formatBytes(summary.files?.parsed?.size)],
          ["Input Bundle", formatBytes(summary.files?.inputBundle?.size)],
        ])}
      </section>
    </div>

    <section class="repair-trace-section">
      <h3>主要归因</h3>
      ${renderHypotheses(hypotheses.slice(0, 5))}
    </section>

    <section class="repair-trace-section">
      <h3>修复计划</h3>
      ${renderPatches(patches.slice(0, 6))}
    </section>

    ${blockers.length ? `
      <section class="repair-trace-section">
        <h3>非 Skill 阻塞</h3>
        ${renderJsonBlock(blockers)}
      </section>
    ` : ""}
  `;
}

function renderExchange(detail) {
  const interactions = asArray(detail.interactions);
  if (interactions.length) {
    return `
      <section class="repair-trace-section">
        <h3>多轮请求与响应对照</h3>
        ${interactions.map((interaction, index) => renderInteractionExchange(interaction, index)).join("")}
      </section>
    `;
  }
  return `
    <section class="repair-trace-section">
      <h3>请求与响应对照</h3>
      ${renderInteractionExchange({
        stage: "final-interaction",
        request: detail.request,
        response: detail.response,
        parsed: detail.parsed,
        error: null,
        files: detail.summary?.files || {},
        modifiedAt: detail.summary?.modifiedAt,
      }, 0)}
    </section>
  `;
}

function renderInteractionExchange(interaction, index) {
  const request = interaction.request || {};
  const response = interaction.response || {};
  const error = interaction.error || null;
  const requestProblem = isJsonProblem(request);
  const responseProblem = isJsonProblem(response);
  const open = index === 0 || String(interaction.stage || "").includes("stage-08") ? " open" : "";
  return `
    <details class="repair-trace-details repair-trace-exchange"${open}>
      <summary>
        <strong>${escapeHtml(interaction.stage || `interaction-${index + 1}`)}</strong>
        <span>
          ${escapeHtml(request?.model || response?.raw_response?.model || "-")}
          · req ${formatBytes(interaction.files?.request?.size)}
          · res ${formatBytes(interaction.files?.response?.size || interaction.files?.error?.size)}
        </span>
      </summary>
      <div class="repair-trace-side-by-side">
        <article class="repair-trace-pane repair-trace-pane-request">
          <div class="repair-trace-pane-head">
            <h4>Request</h4>
            <span>${formatBytes(interaction.files?.request?.size)}</span>
          </div>
          ${requestProblem ? renderJsonProblem(request) : renderRequestPane(request, interaction)}
        </article>
        <article class="repair-trace-pane repair-trace-pane-response">
          <div class="repair-trace-pane-head">
            <h4>Response</h4>
            <span>${formatBytes(interaction.files?.response?.size || interaction.files?.error?.size)}</span>
          </div>
          ${responseProblem ? renderJsonProblem(response) : renderResponsePane(response, interaction, error)}
        </article>
      </div>
    </details>
  `;
}

function renderRequestPane(request, interaction) {
  const messages = asArray(request.messages);
  return `
    ${renderKvTable([
      ["Endpoint", request.endpoint],
      ["Model", request.model],
      ["Temperature", request.temperature],
      ["Max Tokens", request.max_tokens],
      ["Messages", messages.length],
      ["Modified", formatDate(interaction.modifiedAt)],
    ])}
    <div class="repair-trace-message-stack">
      ${messages.map((message, index) => renderCompactMessage(message, index)).join("") || `<div class="detail-empty">没有 messages。</div>`}
    </div>
  `;
}

function renderResponsePane(response, interaction, error) {
  const raw = response.raw_response || {};
  const usage = raw.usage || response.usage || {};
  return `
    ${error ? `<div class="detail-error">${renderJsonBlock(error)}</div>` : ""}
    ${renderKvTable([
      ["HTTP", response.status_code ?? "-"],
      ["Response ID", raw.id || "-"],
      ["Model", raw.model || "-"],
      ["Choices", asArray(raw.choices).length],
      ["Prompt Tokens", usage.prompt_tokens ?? usage.input_tokens ?? "-"],
      ["Completion Tokens", usage.completion_tokens ?? usage.output_tokens ?? "-"],
      ["Total Tokens", usage.total_tokens ?? "-"],
    ])}
    <details class="repair-trace-details" open>
      <summary>Extracted Content · 可读视图</summary>
      ${renderReadableLlmText(response.extracted_content || "", { query: state.textQuery })}
    </details>
    <details class="repair-trace-details">
      <summary>Parsed JSON · 可读视图</summary>
      ${renderReadableContent(interaction.parsed || {}, { query: state.textQuery })}
    </details>
  `;
}

function renderCompactMessage(message, index) {
  const content = typeof message?.content === "string" ? message.content : stringifyJson(message?.content);
  const open = index < 2 ? " open" : "";
  return `
    <details class="repair-trace-details"${open}>
      <summary>
        <strong>${index + 1}. ${escapeHtml(message?.role || "message")}</strong>
        <span>${formatBytes(new Blob([content]).size)}</span>
      </summary>
      <pre class="repair-trace-pre">${highlight(content)}</pre>
    </details>
  `;
}

function renderRequest(detail) {
  const interactions = asArray(detail.interactions);
  if (interactions.length) {
    return `
      <section class="repair-trace-section">
        <h3>多轮请求</h3>
        ${interactions.map((interaction, index) => renderInteractionRequest(interaction, index)).join("")}
      </section>
      ${renderTextArtifact(detail.artifacts?.prompt, "Stage Index", false)}
    `;
  }
  const request = detail.request || {};
  if (isJsonProblem(request)) return renderJsonProblem(request);
  const messages = asArray(request.messages);
  return `
    <section class="repair-trace-section">
      <h3>请求元信息</h3>
      ${renderKvTable([
        ["Endpoint", request.endpoint],
        ["Model", request.model],
        ["Temperature", request.temperature],
        ["Max Tokens", request.max_tokens],
        ["Messages", messages.length],
      ])}
    </section>
    <section class="repair-trace-section">
      <h3>Messages</h3>
      ${messages.map((message, index) => renderMessage(message, index)).join("") || `<div class="detail-empty">没有 messages。</div>`}
    </section>
    ${renderTextArtifact(detail.artifacts?.prompt, "落盘 Prompt", false)}
  `;
}

function renderInteractionRequest(interaction, index) {
  const request = interaction.request || {};
  if (isJsonProblem(request)) return renderJsonProblem(request);
  const messages = asArray(request.messages);
  const open = index === 0 || String(interaction.stage || "").includes("stage-08") ? " open" : "";
  return `
    <details class="repair-trace-details"${open}>
      <summary>
        <strong>${escapeHtml(interaction.stage || `interaction-${index + 1}`)}</strong>
        <span>${escapeHtml(request.model || "-")} · ${formatBytes(interaction.files?.request?.size)}</span>
      </summary>
      ${renderKvTable([
        ["Endpoint", request.endpoint],
        ["Model", request.model],
        ["Temperature", request.temperature],
        ["Max Tokens", request.max_tokens],
        ["Messages", messages.length],
        ["Modified", formatDate(interaction.modifiedAt)],
      ])}
      ${messages.map((message, messageIndex) => renderMessage(message, messageIndex)).join("") || `<div class="detail-empty">没有 messages。</div>`}
    </details>
  `;
}

function renderMessage(message, index) {
  const content = typeof message?.content === "string" ? message.content : stringifyJson(message?.content);
  const open = index < 2 ? " open" : "";
  return `
    <details class="repair-trace-details"${open}>
      <summary>
        <strong>${index + 1}. ${escapeHtml(message?.role || "message")}</strong>
        <span>${formatBytes(new Blob([content]).size)}</span>
      </summary>
      <pre class="repair-trace-pre">${highlight(content)}</pre>
    </details>
  `;
}

function renderResponse(detail) {
  const interactions = asArray(detail.interactions);
  if (interactions.length) {
    return `
      <section class="repair-trace-section">
        <h3>多轮响应</h3>
        ${interactions.map((interaction, index) => renderInteractionResponse(interaction, index)).join("")}
      </section>
    `;
  }
  const response = detail.response || {};
  if (isJsonProblem(response)) return renderJsonProblem(response);
  const raw = response.raw_response || {};
  const usage = raw.usage || response.usage || {};
  return `
    <section class="repair-trace-section">
      <h3>响应元信息</h3>
      ${renderKvTable([
        ["HTTP", response.status_code ?? "-"],
        ["Response ID", raw.id || "-"],
        ["Model", raw.model || "-"],
        ["Choices", asArray(raw.choices).length],
        ["Prompt Tokens", usage.prompt_tokens ?? usage.input_tokens ?? "-"],
        ["Completion Tokens", usage.completion_tokens ?? usage.output_tokens ?? "-"],
        ["Total Tokens", usage.total_tokens ?? "-"],
      ])}
    </section>
    <section class="repair-trace-section">
      <h3>Extracted Content · 可读视图</h3>
      ${renderReadableLlmText(response.extracted_content || "", { query: state.textQuery })}
    </section>
    <section class="repair-trace-section">
      <h3>Raw Response</h3>
      ${renderJsonBlock(raw)}
    </section>
  `;
}

function renderInteractionResponse(interaction, index) {
  const response = interaction.response || {};
  const error = interaction.error || null;
  const open = index === 0 || String(interaction.stage || "").includes("stage-08") ? " open" : "";
  if (isJsonProblem(response)) return renderJsonProblem(response);
  const raw = response.raw_response || {};
  const usage = raw.usage || response.usage || {};
  return `
    <details class="repair-trace-details"${open}>
      <summary>
        <strong>${escapeHtml(interaction.stage || `interaction-${index + 1}`)}</strong>
        <span>${escapeHtml(response.status_code ? `HTTP ${response.status_code}` : error ? "error" : "pending")} · ${formatBytes(interaction.files?.response?.size || interaction.files?.error?.size)}</span>
      </summary>
      ${error ? `<section class="repair-trace-section">${renderJsonBlock(error)}</section>` : ""}
      ${renderKvTable([
        ["HTTP", response.status_code ?? "-"],
        ["Response ID", raw.id || "-"],
        ["Model", raw.model || "-"],
        ["Choices", asArray(raw.choices).length],
        ["Prompt Tokens", usage.prompt_tokens ?? usage.input_tokens ?? "-"],
        ["Completion Tokens", usage.completion_tokens ?? usage.output_tokens ?? "-"],
        ["Total Tokens", usage.total_tokens ?? "-"],
      ])}
      <details class="repair-trace-details" open>
        <summary>Extracted Content · 可读视图</summary>
        ${renderReadableLlmText(response.extracted_content || "", { query: state.textQuery })}
      </details>
      <details class="repair-trace-details">
        <summary>Parsed JSON · 可读视图</summary>
        ${renderReadableContent(interaction.parsed || {}, { query: state.textQuery })}
      </details>
      <details class="repair-trace-details">
        <summary>Raw Response</summary>
        ${renderJsonBlock(raw)}
      </details>
    </details>
  `;
}

function renderParsed(detail) {
  const result = activeResult();
  if (!Object.keys(result).length) return `<div class="detail-empty">没有 parsed 结果。</div>`;
  return `
    <section class="repair-trace-section">
      <h3>能力图</h3>
      ${renderDag(result.capability_graph || result.S0_capability_dag)}
    </section>
    <section class="repair-trace-section">
      <h3>Coverage Matrix</h3>
      ${renderCoverage(result.skill_coverage_matrix)}
    </section>
    <section class="repair-trace-section">
      <h3>Failure Events</h3>
      ${renderFaultCards(result.fault_cards)}
    </section>
    <section class="repair-trace-section">
      <h3>Root Cause Hypotheses</h3>
      ${renderHypotheses(result.root_cause_hypotheses)}
    </section>
    <section class="repair-trace-section">
      <h3>Skill Patch Plan</h3>
      ${renderPatches(result.skill_patch_plan)}
    </section>
    <section class="repair-trace-section">
      <h3>Updated Skill Drafts</h3>
      ${renderDrafts(result.updated_skill_drafts)}
    </section>
    <section class="repair-trace-section">
      <h3>Patch Reviews</h3>
      ${renderJsonBlock(detail.jsonArtifacts?.patchReviews || result.patch_reviews || [])}
    </section>
  `;
}

function renderDag(dag) {
  if (!dag) return `<div class="detail-empty">无能力图。</div>`;
  const nodes = asArray(dag.nodes);
  const edges = asArray(dag.edges);
  return `
    <div class="repair-trace-node-list">
      ${nodes.map((node) => `
        <article class="repair-trace-node">
          <div>
            <strong>${escapeHtml(node.node_id || "-")}</strong>
            <span>${escapeHtml(node.goal || "")}</span>
          </div>
          ${renderMiniList("Required Ops", node.required_operations)}
          ${renderMiniList("Checks", node.required_checks)}
          ${renderMiniList("Failure Modes", node.common_failure_modes)}
        </article>
      `).join("")}
    </div>
    <details class="repair-trace-details">
      <summary>Edges (${edges.length})</summary>
      ${renderJsonBlock(edges)}
    </details>
  `;
}

function renderCoverage(rows) {
  const items = asArray(rows);
  if (!items.length) return `<div class="detail-empty">无 coverage matrix。</div>`;
  return `
    <div class="repair-trace-table-wrap">
      <table class="repair-trace-table">
        <thead>
          <tr>
            <th>Node</th><th>Skill</th><th>Relevant</th><th>Fit</th><th>Trigger</th><th>Procedure</th><th>Verify</th><th>Recovery</th><th>Exec</th><th>Overall</th><th>Gap</th><th>Labels / Missing</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((row) => `
            <tr>
              <td>${escapeHtml(row.node_id || "")}</td>
              <td>${escapeHtml(row.skill_id || asArray(row.matched_skills).join(", ") || "-")}</td>
              <td>${escapeHtml(row.directly_relevant === true ? "yes" : row.directly_relevant === false ? "no" : "-")}</td>
              <td>${scoreText(row.node_requirement_fit)}</td>
              <td>${scoreText(row.trigger_coverage)}</td>
              <td>${scoreText(row.procedure_coverage)}</td>
              <td>${scoreText(row.verification_coverage)}</td>
              <td>${scoreText(row.recovery_coverage)}</td>
              <td>${escapeHtml(row.execution_support_need || "-")} ${scoreText(row.execution_support_coverage)}</td>
              <td><strong>${scoreText(row.overall_coverage)}</strong></td>
              <td>${scoreText(row.coverage_gap)}</td>
              <td>${escapeHtml([asArray(row.coverage_labels).filter(Boolean).join(", "), asArray(row.missing_slots).join(", ")].filter(Boolean).join(" · ") || "-")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderFaultCards(cards) {
  const items = asArray(cards);
  if (!items.length) return `<div class="detail-empty">无 failure events。</div>`;
  return `
    <div class="repair-trace-card-list">
      ${items.map((event) => `
        <article class="repair-trace-data-card">
          <div class="repair-trace-card-head">
            <strong>${escapeHtml(event.event_id || "-")}</strong>
            <span>${escapeHtml([event.traj_id, event.suspected_capability_node, event.severity].filter(Boolean).join(" · "))}</span>
          </div>
          <p><b>Observed:</b> ${highlight(event.observed_behavior || "")}</p>
          <p><b>Expected:</b> ${highlight(event.expected_behavior || "")}</p>
          <p><b>Consequence:</b> ${highlight(event.downstream_consequence || "")}</p>
          ${event.evidence_span ? `<pre class="repair-trace-mini-pre">${highlight(event.evidence_span)}</pre>` : ""}
        </article>
      `).join("")}
    </div>
  `;
}

function renderHypotheses(hypotheses) {
  const items = asArray(hypotheses);
  if (!items.length) return `<div class="detail-empty">无 root cause hypotheses。</div>`;
  return `
    <div class="repair-trace-card-list">
      ${items.map((h) => `
        <article class="repair-trace-data-card">
          <div class="repair-trace-card-head">
            <strong>${escapeHtml(h.hypothesis_id || "-")} · ${escapeHtml(h.root_cause_type || "")}</strong>
            <span>score ${scoreText(h.score)} · ${escapeHtml(h.node_id || "-")}</span>
          </div>
          ${h.score_factors ? `<p><b>Local factors:</b> ${Object.entries(h.score_factors).map(([key, value]) => `${escapeHtml(key)}=${scoreText(value)}`).join(" · ")}</p>` : ""}
          <p>${highlight(h.description || "")}</p>
          ${renderMiniList("Affected", h.affected_trajectories)}
          ${renderMiniList("Target Skills", h.target_skill_ids)}
          <p><b>Evidence:</b> ${highlight(h.evidence_summary || "")}</p>
          <p><b>Action:</b> ${highlight(h.proposed_action || "")}</p>
        </article>
      `).join("")}
    </div>
  `;
}

function renderPatches(patches) {
  const items = asArray(patches);
  if (!items.length) return `<div class="detail-empty">无 patch plan。</div>`;
  return `
    <div class="repair-trace-card-list">
      ${items.map((patch) => `
        <article class="repair-trace-data-card">
          <div class="repair-trace-card-head">
            <strong>${escapeHtml(patch.patch_id || "-")} · ${escapeHtml(patch.action || "")}</strong>
            <span>${escapeHtml(patch.target_skill_id || patch.new_skill_id || "-")} · ${escapeHtml(patch.risk_level || "")}</span>
          </div>
          <p><b>Problem:</b> ${highlight(patch.problem_summary || "")}</p>
          <p><b>Change:</b> ${highlight(patch.proposed_change_summary || "")}</p>
          ${renderMiniList("Nodes", patch.linked_node_ids)}
          ${renderMiniList("Hypotheses", patch.linked_hypotheses)}
          ${patch.patch_content ? `
            <details class="repair-trace-details">
              <summary>Patch Content</summary>
              <pre class="repair-trace-pre">${highlight(patch.patch_content)}</pre>
            </details>
          ` : ""}
        </article>
      `).join("")}
    </div>
  `;
}

function renderDrafts(drafts) {
  const items = asArray(drafts);
  if (!items.length) return `<div class="detail-empty">无 updated skill drafts。</div>`;
  return `
    <div class="repair-trace-card-list">
      ${items.map((draft) => `
        <article class="repair-trace-data-card">
          <div class="repair-trace-card-head">
            <strong>${escapeHtml(draft.title || draft.skill_id || draft.draft_id || "-")}</strong>
            <span>${escapeHtml(draft.operation || "")} · ${escapeHtml(draft.relative_path || "")}</span>
          </div>
          ${renderMiniList("Patch IDs", draft.source_patch_ids)}
          <details class="repair-trace-details">
            <summary>Skill Draft</summary>
            <pre class="repair-trace-pre">${highlight(draft.content || "")}</pre>
          </details>
        </article>
      `).join("")}
    </div>
  `;
}

function renderInputs(detail) {
  const input = detail.inputBundle || {};
  if (isJsonProblem(input)) return renderJsonProblem(input);
  const constraints = input.constraints || {};
  return `
    <section class="repair-trace-section">
      <h3>Task Description</h3>
      <pre class="repair-trace-pre">${highlight(input.task_description || "")}</pre>
    </section>
    <section class="repair-trace-section">
      <h3>输入约束</h3>
      ${renderKvTable(Object.entries(constraints))}
    </section>
    <section class="repair-trace-section">
      <h3>Skill Library</h3>
      ${renderSkills(input.skill_library, input.skill_quality_static)}
    </section>
    <section class="repair-trace-section">
      <h3>Failed Trajectories</h3>
      ${renderTrajectories(input.failed_trajectories)}
    </section>
  `;
}

function renderSkills(skills, quality) {
  const qualityById = new Map(asArray(quality).map((item) => [item.skill_id, item]));
  const items = asArray(skills);
  if (!items.length) return `<div class="detail-empty">无 skill 输入。</div>`;
  return `
    <div class="repair-trace-card-list">
      ${items.map((skill) => {
        const q = qualityById.get(skill.skill_id) || {};
        return `
          <article class="repair-trace-data-card">
            <div class="repair-trace-card-head">
              <strong>${escapeHtml(skill.title || skill.skill_id || "-")}</strong>
              <span>${escapeHtml(skill.path || "")}</span>
            </div>
            ${renderMiniList("Static Issues", q.issues)}
            <p>Quality: trigger ${scoreText(q.trigger_score)}, procedure ${scoreText(q.procedure_score)}, verification ${scoreText(q.verification_score)}, recovery ${scoreText(q.recovery_score)}, template ${scoreText(q.tool_template_score)}</p>
            <details class="repair-trace-details">
              <summary>Skill Content</summary>
              <pre class="repair-trace-pre">${highlight(skill.content || "")}</pre>
            </details>
            ${renderAttachedFiles(skill.attached_files)}
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function renderAttachedFiles(files) {
  const items = asArray(files);
  if (!items.length) return "";
  return `
    <details class="repair-trace-details" open>
      <summary>Attached Files (${items.length})</summary>
      <div class="repair-trace-card-list">
        ${items.map((file) => `
          <article class="repair-trace-data-card">
            <div class="repair-trace-card-head">
              <strong>${escapeHtml(file.path || "-")}</strong>
              <span>${escapeHtml(file.type || "others")}</span>
            </div>
            <pre class="repair-trace-mini-pre">${highlight(file.content || "")}</pre>
          </article>
        `).join("")}
      </div>
    </details>
  `;
}

function renderTrajectories(trajectories) {
  const items = asArray(trajectories);
  if (!items.length) return `<div class="detail-empty">无失败轨迹。</div>`;
  return `
    <div class="repair-trace-card-list">
      ${items.map((traj) => `
        <article class="repair-trace-data-card">
          <div class="repair-trace-card-head">
            <strong>${escapeHtml(traj.traj_id || "-")}</strong>
            <span>${escapeHtml(traj.rollout_dir || traj.task_id || "")}</span>
          </div>
          ${renderKvTable([
            ["Tool Calls", asArray(traj.tool_calls).length],
            ["Observations", asArray(traj.observations).length],
            ["Steps", asArray(traj.steps).length],
            ["Verifier", traj.verifier_result || "-"],
            ["Error", traj.error || "-"],
          ])}
          ${traj.final_output ? `
            <details class="repair-trace-details">
              <summary>Final Output</summary>
              <pre class="repair-trace-pre">${highlight(traj.final_output)}</pre>
            </details>
          ` : ""}
          ${renderTraceSteps(traj.steps)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderTraceSteps(steps) {
  const items = asArray(steps).slice(0, 200);
  if (!items.length) return "";
  return `
    <details class="repair-trace-details">
      <summary>Step Summaries (${items.length})</summary>
      <div class="repair-trace-table-wrap">
        <table class="repair-trace-table">
          <thead><tr><th>ID</th><th>Type</th><th>Tool</th><th>Action</th><th>Observation / Error</th></tr></thead>
          <tbody>
            ${items.map((step) => `
              <tr>
                <td>${escapeHtml(step.step_id ?? "")}</td>
                <td>${escapeHtml(step.action_type || step.role || "")}</td>
                <td>${escapeHtml(step.tool_name || "")}</td>
                <td>${highlight(step.action_summary || "")}</td>
                <td>${highlight(step.error_signal || step.observation_summary || "")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </details>
  `;
}

function renderFiles(detail) {
  const result = activeResult();
  return `
    ${renderTextArtifact(detail.artifacts?.repairProcess, "修复过程记录", true)}
    ${renderTextArtifact(detail.artifacts?.diagnosisReport, "诊断报告", true)}
    ${renderTextArtifact(detail.artifacts?.coverageCsv, "Coverage CSV", false)}
    <section class="repair-trace-section">
      <h3>Patch Files</h3>
      ${renderTextFileList(detail.patchFiles)}
    </section>
    <section class="repair-trace-section">
      <h3>Draft Files</h3>
      ${renderTextFileList(detail.draftFiles)}
    </section>
    <section class="repair-trace-section">
      <h3>JSON Artifacts</h3>
      ${renderJsonBlock({
        capabilityGraph: detail.jsonArtifacts?.capabilityGraph || result.capability_graph || result.S0_capability_dag,
        faultCards: detail.jsonArtifacts?.faultCards || result.fault_cards,
        coverageMatrix: detail.jsonArtifacts?.coverageMatrix || result.skill_coverage_matrix,
        hypotheses: detail.jsonArtifacts?.hypotheses || result.root_cause_hypotheses,
        patchPlan: detail.jsonArtifacts?.patchPlan || result.skill_patch_plan,
        patchReviews: detail.jsonArtifacts?.patchReviews || result.patch_reviews,
        appliedManifest: detail.jsonArtifacts?.appliedManifest,
        traceAnalyses: detail.jsonArtifacts?.traceAnalyses,
        stageOutputs: detail.jsonArtifacts?.stageOutputs,
      })}
    </section>
  `;
}

function renderTextFileList(files) {
  const items = asArray(files);
  if (!items.length) return `<div class="detail-empty">无文件。</div>`;
  return items.map((file, index) => renderTextArtifact(file, file.relativeName || file.name || `file-${index + 1}`, index < 2)).join("");
}

function renderTextArtifact(file, title, open = false) {
  if (!file) return "";
  const marker = file.truncated ? ` · 已截断，原始 ${formatBytes(file.size)}` : ` · ${formatBytes(file.size)}`;
  return `
    <section class="repair-trace-section">
      <details class="repair-trace-details"${open ? " open" : ""}>
        <summary>
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(file.path || "")}${marker}</span>
        </summary>
        ${file.error ? `<div class="detail-empty">读取失败：${escapeHtml(file.error)}</div>` : `<pre class="repair-trace-pre">${highlight(file.text || "")}</pre>`}
      </details>
    </section>
  `;
}

function renderMiniList(label, value) {
  const items = asArray(value).filter((item) => item !== null && item !== undefined && item !== "");
  if (!items.length) return "";
  return `
    <div class="repair-trace-mini-list">
      <span>${escapeHtml(label)}:</span>
      ${items.map((item) => `<code>${escapeHtml(item)}</code>`).join("")}
    </div>
  `;
}

function renderJsonBlock(value) {
  return renderRawJsonBlock(value, { query: state.textQuery });
}

function renderJsonProblem(value) {
  if (!value) return `<div class="detail-empty">无数据。</div>`;
  if (value.__tooLarge) {
    return `<div class="detail-empty">JSON 文件过大：${formatBytes(value.size)}，超过读取上限 ${formatBytes(value.maxBytes)}。</div>`;
  }
  if (value.__parseError) {
    return `<div class="detail-empty">JSON 解析失败：${escapeHtml(value.message)}</div>`;
  }
  return `<div class="detail-empty">无数据。</div>`;
}

els.runSearch.addEventListener("input", (event) => {
  state.runQuery = event.target.value;
  renderRuns();
});

els.textSearch.addEventListener("input", (event) => {
  state.textQuery = event.target.value;
  renderDetail();
});

els.refresh.addEventListener("click", () => loadRuns());

els.runList.addEventListener("click", (event) => {
  const row = event.target.closest("[data-run-dir]");
  if (!row) return;
  loadDetail(row.dataset.runDir);
});

els.tabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (!button) return;
  state.tab = button.dataset.tab;
  renderDetail();
});

loadRuns();
