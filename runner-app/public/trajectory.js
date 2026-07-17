const $ = (id) => document.getElementById(id);

const elements = {
  path: $("trajectoryPath"),
  stats: $("trajectoryStats"),
  search: $("trajectorySearch"),
  type: $("trajectoryType"),
  status: $("trajectoryStatus"),
  list: $("eventList"),
  detail: $("eventDetail"),
  reload: $("reloadTrajectory"),
  analyze: $("analyzeTrajectory"),
};

const query = new URLSearchParams(window.location.search);
const state = {
  trajectory: null,
  filtered: [],
  selectedIndex: null,
};

if (elements.analyze) {
  elements.analyze.href = `/analysis.html?${query.toString()}`;
}

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function badge(type, text) {
  const klass = type ? ` ${type}` : "";
  return `<span class="badge${klass}">${escapeHtml(text || "-")}</span>`;
}

function eventBadge(event) {
  if (event.type === "tool_call") return badge(event.status === "completed" ? "pass" : "warn", event.kind || "tool");
  if (event.type === "agent_thought") return badge("run", "agent");
  if (event.type === "user_message") return badge("", "user");
  return badge("", event.type || "event");
}

function hydrateSelect(select, values, placeholder) {
  const current = select.value;
  select.innerHTML = `<option value="">${placeholder}</option>${values
    .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
    .join("")}`;
  if (values.includes(current)) select.value = current;
}

function eventHaystack(event) {
  return [
    event.type,
    event.kind,
    event.status,
    event.title,
    event.toolCallId,
    event.preview,
    event.text?.value,
    JSON.stringify(event.content || ""),
  ].join(" ").toLowerCase();
}

function filterEvents() {
  const needle = elements.search.value.trim().toLowerCase();
  const type = elements.type.value;
  const status = elements.status.value;
  state.filtered = (state.trajectory?.events || []).filter((event) => {
    if (type && event.type !== type) return false;
    if (status && event.status !== status) return false;
    if (needle && !eventHaystack(event).includes(needle)) return false;
    return true;
  });
  if (!state.filtered.some((event) => event.index === state.selectedIndex)) {
    state.selectedIndex = state.filtered[0]?.index ?? null;
  }
}

function renderStats() {
  const data = state.trajectory;
  if (!data) {
    elements.stats.innerHTML = "";
    return;
  }
  const counts = Object.entries(data.counts || {})
    .sort((a, b) => b[1] - a[1])
    .map(([key, value]) => `<div><span>${escapeHtml(key)}</span><strong>${value}</strong></div>`)
    .join("");

  elements.path.textContent = `${data.trajectoryPath} · ${formatBytes(data.size)}`;
  elements.stats.innerHTML = `
    <div><span>rollout</span><strong>${escapeHtml(data.rollout)}</strong></div>
    <div><span>events</span><strong>${data.totalEvents}</strong></div>
    <div><span>parse errors</span><strong>${data.parseErrors || 0}</strong></div>
    <div><span>modified</span><strong>${escapeHtml(data.modifiedAt || "-")}</strong></div>
    ${counts}
    ${data.truncated ? `<div><span>读取限制</span><strong>${formatBytes(data.maxBytes)} / ${data.maxEvents} events</strong></div>` : ""}
  `;
}

function renderList() {
  elements.list.innerHTML = state.filtered.map((event) => {
    const selected = event.index === state.selectedIndex ? " selected" : "";
    return `
      <button class="event-card${selected}" type="button" data-event-index="${event.index}">
        <span class="event-index">#${event.index}</span>
        <span class="event-type">${eventBadge(event)}</span>
        <span class="event-title">${escapeHtml(event.title || event.preview || "(empty event)")}</span>
        <span class="event-preview">${escapeHtml(event.title ? event.preview : "")}</span>
      </button>
    `;
  }).join("") || `<div class="detail-empty">没有匹配的轨迹事件。</div>`;

  elements.list.querySelectorAll(".event-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedIndex = Number(card.dataset.eventIndex);
      render();
    });
  });
}

function renderTextBlock(title, value) {
  if (value === undefined || value === null || value === "") return "";
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return `
    <section class="event-content-block">
      <h3>${escapeHtml(title)}</h3>
      <pre class="event-text">${escapeHtml(text)}</pre>
    </section>
  `;
}

function renderContent(value) {
  if (value === undefined || value === null) return "";
  if (!Array.isArray(value)) return renderTextBlock("content", value);
  return value.map((item, index) => {
    const title = item && typeof item === "object"
      ? item.type || item.kind || item.name || `content ${index + 1}`
      : `content ${index + 1}`;
    return renderTextBlock(title, item);
  }).join("");
}

function renderDetail() {
  const event = (state.trajectory?.events || []).find((item) => item.index === state.selectedIndex);
  if (!event) {
    elements.detail.innerHTML = "选择左侧事件查看详情。";
    return;
  }

  elements.detail.innerHTML = `
    <div class="event-detail-head">
      <div>
        <h2>#${event.index} ${escapeHtml(event.type || "event")}</h2>
        <p>line ${event.line}${event.toolCallId ? ` · ${escapeHtml(event.toolCallId)}` : ""}</p>
      </div>
      ${eventBadge(event)}
    </div>
    <div class="detail-grid compact">
      <div><span>kind</span><strong>${escapeHtml(event.kind || "-")}</strong></div>
      <div><span>status</span><strong>${escapeHtml(event.status || "-")}</strong></div>
      <div><span>title</span><strong>${escapeHtml(event.title || "-")}</strong></div>
    </div>
    ${event.text ? renderTextBlock("text", event.text.value) : ""}
    ${renderContent(event.content)}
    <details class="detail-json">
      <summary>结构化 payload</summary>
      <pre>${escapeHtml(JSON.stringify(event.payload || {}, null, 2))}</pre>
    </details>
  `;
}

function render() {
  filterEvents();
  renderStats();
  renderList();
  renderDetail();
}

async function loadTrajectory() {
  const artifactDir = query.get("artifactDir");
  if (!artifactDir) {
    elements.path.textContent = "缺少 artifactDir 参数。";
    return;
  }

  elements.path.textContent = "正在读取轨迹...";
  elements.list.innerHTML = `<div class="detail-empty">加载中...</div>`;
  elements.detail.textContent = "加载中...";

  const params = new URLSearchParams({ artifactDir });
  const rollout = query.get("rollout");
  if (rollout) params.set("rollout", rollout);

  const response = await fetch(`/api/trajectory?${params.toString()}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取轨迹失败");

  state.trajectory = data.trajectory;
  state.selectedIndex = state.trajectory.events[0]?.index ?? null;

  const types = [...new Set(state.trajectory.events.map((event) => event.type).filter(Boolean))].sort();
  const statuses = [...new Set(state.trajectory.events.map((event) => event.status).filter(Boolean))].sort();
  hydrateSelect(elements.type, types, "全部类型");
  hydrateSelect(elements.status, statuses, "全部状态");
  render();
}

for (const element of [elements.search, elements.type, elements.status]) {
  element.addEventListener("input", render);
  element.addEventListener("change", render);
}

elements.reload.addEventListener("click", () => {
  loadTrajectory().catch((error) => {
    elements.path.textContent = error.message;
    elements.list.innerHTML = "";
    elements.detail.innerHTML = `<div class="detail-error">${escapeHtml(error.message)}</div>`;
  });
});

loadTrajectory().catch((error) => {
  elements.path.textContent = error.message;
  elements.list.innerHTML = "";
  elements.detail.innerHTML = `<div class="detail-error">${escapeHtml(error.message)}</div>`;
});
