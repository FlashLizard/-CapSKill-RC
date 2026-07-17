const $ = (id) => document.getElementById(id);

const elements = {
  subtitle: $("analysisSubtitle"),
  taskSelect: $("analysisTaskSelect"),
  taskPath: $("analysisTaskPath"),
  trajectoryPath: $("analysisTrajectoryPath"),
  artifactDir: $("analysisArtifactDir"),
  rollout: $("analysisRollout"),
  resultPath: $("analysisResultPath"),
  contextStatus: $("contextStatus"),
  judgeEnabled: $("judgeEnabled"),
  judgeProvider: $("judgeProvider"),
  judgeModel: $("judgeModel"),
  judgeBaseUrl: $("judgeBaseUrl"),
  judgeMaxTokens: $("judgeMaxTokens"),
  judgeApiKey: $("judgeApiKey"),
  keywordMode: $("keywordMode"),
  loadContext: $("loadAnalysisContext"),
  run: $("runAnalysis"),
  output: $("analysisOutput"),
};

const query = new URLSearchParams(window.location.search);
const JUDGE_CONFIG_KEY = "skillsbench.runner.judgeConfig.v1";

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function pct(value) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function signedPct(value) {
  const n = Number(value || 0);
  const sign = n > 0 ? "+" : "";
  return `${sign}${Math.round(n * 100)}%`;
}

function contributionClass(value) {
  const n = Number(value || 0);
  if (n >= 0.45) return "positive";
  if (n <= -0.25) return "negative";
  return "neutral";
}

function severityClass(value) {
  if (value === "good" || value === "low") return "pass";
  if (value === "bad" || value === "high") return "fail";
  if (value === "medium" || value === "warn") return "warn";
  return "";
}

function renderJsonDetails(title, value) {
  return `
    <details class="detail-json">
      <summary>${escapeHtml(title)}</summary>
      <pre>${escapeHtml(JSON.stringify(value ?? null, null, 2))}</pre>
    </details>
  `;
}

function setStatus(message) {
  elements.contextStatus.textContent = message;
}

function loadJudgeConfig() {
  try {
    const config = JSON.parse(localStorage.getItem(JUDGE_CONFIG_KEY) || "{}");
    if (config.provider) elements.judgeProvider.value = config.provider;
    if (config.model) elements.judgeModel.value = config.model;
    if (config.baseUrl) elements.judgeBaseUrl.value = config.baseUrl;
    if (config.maxTokens) elements.judgeMaxTokens.value = config.maxTokens;
    if (config.keywordMode) elements.keywordMode.value = config.keywordMode;
  } catch {
    // Ignore broken local config.
  }
}

function saveJudgeConfig() {
  localStorage.setItem(JUDGE_CONFIG_KEY, JSON.stringify({
    provider: elements.judgeProvider.value,
    model: elements.judgeModel.value,
    baseUrl: elements.judgeBaseUrl.value,
    maxTokens: elements.judgeMaxTokens.value,
    keywordMode: elements.keywordMode.value,
  }));
}

function requestPayload() {
  const keywordMode = elements.keywordMode.value;
  return {
    taskPath: elements.taskPath.value.trim(),
    trajectoryPath: elements.trajectoryPath.value.trim(),
    artifactDir: elements.artifactDir.value.trim(),
    rollout: elements.rollout.value.trim(),
    resultPath: elements.resultPath.value.trim(),
    judge: {
      enabled: elements.judgeEnabled.checked,
      provider: elements.judgeProvider.value,
      model: elements.judgeModel.value.trim(),
      baseUrl: elements.judgeBaseUrl.value.trim(),
      apiKey: elements.judgeApiKey.value.trim(),
      maxTokens: Number(elements.judgeMaxTokens.value || 1800),
    },
    keywordExtraction: {
      enabled: keywordMode !== "rules",
      mode: keywordMode,
      reuseJudgeConfig: true,
      provider: elements.judgeProvider.value,
      model: elements.judgeModel.value.trim(),
      baseUrl: elements.judgeBaseUrl.value.trim(),
      apiKey: elements.judgeApiKey.value.trim(),
      maxTokens: Number(elements.judgeMaxTokens.value || 1800),
    },
  };
}

function applyContext(context) {
  if (context.taskPath) elements.taskPath.value = context.taskPath;
  if (context.trajectoryPath) elements.trajectoryPath.value = context.trajectoryPath;
  if (context.resultPath) elements.resultPath.value = context.resultPath;
  if (context.artifactDir) elements.artifactDir.value = context.artifactDir;
  if (context.rolloutPath && !elements.rollout.value) {
    elements.rollout.value = context.rolloutPath.split("/").at(-1) || "";
  }
  elements.subtitle.textContent = `${context.taskPath || "未指定 task"} · ${context.trajectoryPath || "未指定 trajectory"}`;
  for (const option of elements.taskSelect.options) {
    if (option.value === context.taskPath) {
      elements.taskSelect.value = context.taskPath;
      break;
    }
  }
}

async function fetchTasks() {
  const response = await fetch("/api/tasks");
  const data = await response.json();
  const tasks = data.tasks || [];
  elements.taskSelect.innerHTML = [
    `<option value="">自动推断 / 手动填写</option>`,
    ...tasks.map((task) => `<option value="${escapeHtml(task.taskDir)}">${escapeHtml(task.rootLabel)}/${escapeHtml(task.name)} · ${task.skillCount} skills</option>`),
  ].join("");
}

async function loadContext() {
  setStatus("加载中...");
  const params = new URLSearchParams();
  const payload = requestPayload();
  for (const key of ["taskPath", "trajectoryPath", "artifactDir", "rollout", "resultPath"]) {
    if (payload[key]) params.set(key, payload[key]);
  }
  const response = await fetch(`/api/analysis-context?${params.toString()}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "加载上下文失败");
  applyContext(data.context);
  setStatus("上下文已加载");
  return data.context;
}

function renderOverview(analysis) {
  const outcome = analysis.outcome || {};
  const stats = analysis.stats || {};
  return `
    <section class="analysis-card">
      <div class="analysis-card-title">
        <h2>概览</h2>
        <span class="badge ${outcome.passed ? "pass" : "fail"}">${outcome.passed ? "SUCCESS" : "FAILED"}</span>
      </div>
      <div class="trajectory-stats analysis-stats">
        <div><span>reward</span><strong>${escapeHtml(outcome.reward ?? "-")}</strong></div>
        <div><span>events</span><strong>${stats.events || 0}</strong></div>
        <div><span>tool calls</span><strong>${stats.toolCalls || 0}</strong></div>
        <div><span>skills</span><strong>${analysis.task?.skillCount || 0}</strong></div>
        <div><span>parse errors</span><strong>${stats.parseErrors || 0}</strong></div>
        <div><span>error</span><strong>${escapeHtml(outcome.errorCategory || "-")}</strong></div>
      </div>
      ${outcome.error ? `<div class="detail-error">${escapeHtml(outcome.error)}</div>` : ""}
    </section>
  `;
}

function renderReasons(title, items, emptyText) {
  return `
    <section class="analysis-card">
      <div class="analysis-card-title"><h2>${escapeHtml(title)}</h2></div>
      <div class="reason-list">
        ${(items || []).map((item) => `
          <div class="reason-item">
            <span class="badge ${severityClass(item.severity)}">${escapeHtml(item.severity || "info")}</span>
            <div>
              <strong>${escapeHtml(item.label)}</strong>
              <p>${escapeHtml(item.detail || item.evidence || "")}</p>
            </div>
          </div>
        `).join("") || `<div class="detail-empty">${escapeHtml(emptyText)}</div>`}
      </div>
    </section>
  `;
}

function renderSkills(skills) {
  return `
    <section class="analysis-card">
      <div class="analysis-card-title"><h2>Skill 使用与贡献</h2></div>
      <div class="skill-analysis-grid">
        ${(skills || []).map((skill) => {
          const klass = contributionClass(skill.contribution);
          const width = Math.min(100, Math.round(Math.abs(Number(skill.contribution || 0)) * 100));
          return `
            <article class="skill-analysis-card">
              <div class="skill-analysis-head">
                <div>
                  <h3>${escapeHtml(skill.name)}</h3>
                  <p>${escapeHtml(skill.description || skill.path || "")}</p>
                </div>
                <span class="badge ${skill.invoked ? "pass" : skill.expected ? "warn" : ""}">${skill.invoked ? "used" : skill.expected ? "expected" : "optional"}</span>
              </div>
              <div class="skill-metrics">
                <span>相关性 ${pct(skill.relevance)}</span>
                <span>贡献 ${signedPct(skill.contribution)}</span>
                <span>${escapeHtml(skill.correctness)}</span>
                <span>关键词 ${escapeHtml(skill.keywordSource || "rules")}</span>
              </div>
              <div class="contribution-track">
                <div class="contribution-fill ${klass}" style="width:${width}%"></div>
              </div>
              <p class="skill-assessment">${escapeHtml(skill.assessment)}</p>
              <div class="skill-evidence">
                <span>invoke: ${escapeHtml(skill.invocationEvent || "-")}</span>
                <span>mentions: ${escapeHtml((skill.mentionedEvents || []).join(", ") || "-")}</span>
                <span>tools: ${escapeHtml((skill.relatedToolEvents || []).join(", ") || "-")}</span>
              </div>
              ${(skill.matchedKeywords || []).length ? `<p class="skill-keywords">${escapeHtml(skill.matchedKeywords.join(", "))}</p>` : ""}
            </article>
          `;
        }).join("") || `<div class="detail-empty">该 task 没有发现 SKILL.md。</div>`}
      </div>
    </section>
  `;
}

function keywordModeLabel(mode) {
  if (mode === "llm-task") return "LLM 总结 task";
  if (mode === "llm-skills") return "LLM 总结 skills";
  if (mode === "llm-both") return "LLM 总结 task + skills";
  return "本地规则";
}

function renderKeywordList(keywords) {
  return `
    <div class="keyword-list">
      ${(keywords || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("") || `<span>无</span>`}
    </div>
  `;
}

function renderKeywordExtraction(keywordExtraction) {
  if (!keywordExtraction?.enabled) return "";
  const errors = keywordExtraction.errors || [];
  return `
    <section class="analysis-card">
      <div class="analysis-card-title">
        <h2>关键词提取</h2>
        <span class="badge ${keywordExtraction.ok ? "pass" : "warn"}">${keywordExtraction.ok ? "OK" : "FALLBACK"}</span>
      </div>
      <div class="keyword-extraction-meta">
        <span>模式 ${escapeHtml(keywordModeLabel(keywordExtraction.mode))}</span>
        <span>来源 ${escapeHtml(keywordExtraction.source || "rules")}</span>
        ${(keywordExtraction.calls || []).map((call) => `<span>${escapeHtml(call.target)} ${call.ok ? "OK" : "FAILED"}${call.elapsedSec ? ` · ${escapeHtml(call.elapsedSec)}s` : ""}</span>`).join("")}
      </div>
      ${errors.length ? `<div class="detail-error">${escapeHtml(errors.join("\n"))}</div>` : ""}
      ${(keywordExtraction.taskKeywords || []).length ? `
        <div>
          <h3 class="subtle-heading">Task 关键词</h3>
          ${renderKeywordList(keywordExtraction.taskKeywords)}
        </div>
      ` : ""}
      ${(keywordExtraction.skillKeywords || []).length ? `
        <div class="keyword-skill-list">
          ${(keywordExtraction.skillKeywords || []).map((item) => `
            <div>
              <strong>${escapeHtml(item.name)}</strong>
              ${renderKeywordList(item.keywords)}
            </div>
          `).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function renderTimeline(timeline) {
  return `
    <section class="analysis-card">
      <div class="analysis-card-title"><h2>Skill 时间线</h2></div>
      <div class="analysis-timeline">
        ${(timeline || []).map((item) => `
          <div class="timeline-point">
            <span>#${escapeHtml(item.event || "-")}</span>
            <strong>${escapeHtml(item.skill)}</strong>
            <p>${escapeHtml(item.label || item.kind || "")}</p>
          </div>
        `).join("") || `<div class="detail-empty">轨迹中没有明确 skill 启动或引用事件。</div>`}
      </div>
    </section>
  `;
}

function renderJudge(judge) {
  if (!judge?.enabled) return "";
  const parsed = judge.parsed || {};
  const rootCauses = parsed.rootCauses || [];
  return `
    <section class="analysis-card">
      <div class="analysis-card-title">
        <h2>Judge LLM</h2>
        <span class="badge ${judge.ok ? "pass" : "fail"}">${judge.ok ? "OK" : "FAILED"}</span>
      </div>
      ${parsed.overall ? `
        <div class="judge-summary">
          <strong>${escapeHtml(parsed.overall.verdict || "")}</strong>
          <p>${escapeHtml(parsed.overall.summary || "")}</p>
        </div>
      ` : judge.rawPreview ? `<div class="detail-error">${escapeHtml(judge.rawPreview)}</div>` : ""}
      <div class="reason-list">
        ${rootCauses.map((item) => `
          <div class="reason-item">
            <span class="badge ${severityClass(item.severity)}">${escapeHtml(item.severity || "info")}</span>
            <div>
              <strong>${escapeHtml(item.label)}</strong>
              <p>${escapeHtml(item.detail || item.evidence || "")}</p>
            </div>
          </div>
        `).join("") || `<div class="detail-empty">Judge 没有返回根因。</div>`}
      </div>
      ${parsed.skillJudgments ? renderJsonDetails("Judge skill judgments", parsed.skillJudgments) : ""}
      ${parsed.recommendations ? `
        <div class="recommendations">
          ${(parsed.recommendations || []).map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function renderToolStats(stats) {
  return `
    <section class="analysis-card">
      <div class="analysis-card-title"><h2>工具调用统计</h2></div>
      <div class="tool-stat-grid">
        ${Object.entries(stats.toolKindCounts || {}).map(([key, value]) => `<div><span>${escapeHtml(key)}</span><strong>${value}</strong></div>`).join("") || `<div><span>tool</span><strong>0</strong></div>`}
      </div>
      ${renderJsonDetails("event / status counts", {
        eventTypeCounts: stats.eventTypeCounts,
        toolStatusCounts: stats.toolStatusCounts,
      })}
    </section>
  `;
}

function renderAnalysis(analysis) {
  elements.output.innerHTML = [
    renderOverview(analysis),
    renderKeywordExtraction(analysis.keywordExtraction),
    renderReasons(analysis.outcome?.passed ? "成功原因" : "失败原因", analysis.outcome?.passed ? analysis.reasons?.success : analysis.reasons?.failure, "没有发现明显原因。"),
    renderSkills(analysis.skills),
    renderTimeline(analysis.timeline),
    renderToolStats(analysis.stats || {}),
    renderJudge(analysis.judge),
    renderJsonDetails("完整分析 JSON", analysis),
  ].join("");
}

async function runAnalysis() {
  saveJudgeConfig();
  elements.output.innerHTML = `<div class="detail-empty">分析中...</div>`;
  elements.run.disabled = true;
  try {
    const response = await fetch("/api/analyze-trajectory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload()),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "分析失败");
    applyContext(data.context);
    renderAnalysis(data.analysis);
    setStatus("分析完成");
  } finally {
    elements.run.disabled = false;
  }
}

elements.taskSelect.addEventListener("change", () => {
  elements.taskPath.value = elements.taskSelect.value;
});

elements.loadContext.addEventListener("click", () => {
  loadContext().catch((error) => {
    setStatus(error.message);
  });
});

elements.run.addEventListener("click", () => {
  runAnalysis().catch((error) => {
    elements.output.innerHTML = `<div class="detail-error">${escapeHtml(error.message)}</div>`;
    setStatus("分析失败");
  });
});

for (const item of [elements.judgeProvider, elements.judgeModel, elements.judgeBaseUrl, elements.judgeMaxTokens, elements.keywordMode]) {
  item.addEventListener("change", saveJudgeConfig);
}

await fetchTasks();
loadJudgeConfig();

for (const [key, target] of [
  ["taskPath", elements.taskPath],
  ["trajectoryPath", elements.trajectoryPath],
  ["artifactDir", elements.artifactDir],
  ["rollout", elements.rollout],
  ["resultPath", elements.resultPath],
]) {
  if (query.get(key)) target.value = query.get(key);
}

if (elements.artifactDir.value || elements.trajectoryPath.value) {
  loadContext().catch((error) => setStatus(error.message));
}
