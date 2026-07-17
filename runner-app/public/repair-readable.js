const EMPTY_TEXT = "无内容。";

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlightHtml(value, query = "") {
  const text = escapeHtml(value ?? "");
  const needle = String(query || "").trim();
  if (!needle) return text;
  const re = new RegExp(`(${escapeRegExp(needle)})`, "gi");
  return text.replace(re, "<mark>$1</mark>");
}

export function stringifyJson(value) {
  if (value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function stripJsonFence(text) {
  const trimmed = String(text || "").trim();
  const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  return fenced ? fenced[1].trim() : trimmed;
}

function extractJsonCandidate(text) {
  const stripped = stripJsonFence(text);
  if (!stripped) return "";
  if (stripped.startsWith("{") || stripped.startsWith("[")) return stripped;
  const objectStart = stripped.indexOf("{");
  const objectEnd = stripped.lastIndexOf("}");
  const arrayStart = stripped.indexOf("[");
  const arrayEnd = stripped.lastIndexOf("]");
  const candidates = [];
  if (objectStart !== -1 && objectEnd > objectStart) candidates.push(stripped.slice(objectStart, objectEnd + 1));
  if (arrayStart !== -1 && arrayEnd > arrayStart) candidates.push(stripped.slice(arrayStart, arrayEnd + 1));
  return candidates.sort((a, b) => b.length - a.length)[0] || stripped;
}

export function parseJsonFromText(text) {
  const candidate = extractJsonCandidate(text);
  if (!candidate) return null;
  try {
    return JSON.parse(candidate);
  } catch {
    return null;
  }
}

function escapedNewlineCount(text) {
  return (String(text || "").match(/\\r\\n|\\n/g) || []).length;
}

function decodeReadableText(value) {
  const text = String(value ?? "");
  if (escapedNewlineCount(text) < 2 && !/\\n\s*(?:[-*#]|\d+\.|\{|\[|")/.test(text)) {
    return text;
  }
  return text
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "  ");
}

function isScalar(value) {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}

function isObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function labelFromKey(key) {
  return String(key || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function summarizeValue(value) {
  if (Array.isArray(value)) return `${value.length} items`;
  if (isObject(value)) return `${Object.keys(value).length} fields`;
  if (value === null || value === undefined || value === "") return "-";
  const text = decodeReadableText(value);
  return text.length > 90 ? `${text.slice(0, 90)}...` : text;
}

function titleForObject(value, fallback) {
  if (!isObject(value)) return fallback;
  const id = value.patch_id || value.hypothesis_id || value.event_id || value.node_id || value.skill_id || value.traj_id || value.draft_id;
  const title = value.title || value.goal || value.description || value.action || value.root_cause_type;
  return [id, title].filter(Boolean).join(" · ") || fallback;
}

function renderTextLines(text, query) {
  const normalized = decodeReadableText(text).replace(/\r\n/g, "\n");
  if (!normalized.trim()) return `<span class="repair-readable-empty">${EMPTY_TEXT}</span>`;
  const lines = normalized.split("\n");
  const blocks = [];
  let paragraph = [];
  const flushParagraph = () => {
    const content = paragraph.join("\n").trim();
    if (content) blocks.push(`<p>${highlightHtml(content, query)}</p>`);
    paragraph = [];
  };
  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      continue;
    }
    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      blocks.push(`<h4>${highlightHtml(heading[2], query)}</h4>`);
      continue;
    }
    if (/^[-*]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) {
      flushParagraph();
      blocks.push(`<div class="repair-readable-line repair-readable-bullet">${highlightHtml(trimmed, query)}</div>`);
      continue;
    }
    paragraph.push(line);
  }
  flushParagraph();
  return blocks.join("") || `<p>${highlightHtml(normalized, query)}</p>`;
}

function renderScalar(value, options) {
  if (value === null || value === undefined || value === "") {
    return `<span class="repair-readable-empty">-</span>`;
  }
  if (typeof value === "boolean") {
    return `<span class="status-pill ${value ? "status-ok" : "status-warn"}">${value ? "true" : "false"}</span>`;
  }
  if (typeof value === "number") {
    return `<code class="repair-readable-code">${escapeHtml(value)}</code>`;
  }
  const text = decodeReadableText(value);
  const multiline = text.includes("\n") || text.length > 180;
  if (!multiline) return `<span>${highlightHtml(text, options.query)}</span>`;
  return `<div class="repair-readable-text">${renderTextLines(text, options.query)}</div>`;
}

function renderArray(value, options, depth) {
  const items = Array.isArray(value) ? value : [];
  if (!items.length) return `<span class="repair-readable-empty">[]</span>`;
  if (items.every(isScalar)) {
    return `
      <div class="repair-readable-chip-row">
        ${items.slice(0, options.maxArrayItems).map((item) => `<div class="repair-readable-chip">${renderScalar(item, options)}</div>`).join("")}
        ${items.length > options.maxArrayItems ? `<div class="repair-readable-muted">+${items.length - options.maxArrayItems} more</div>` : ""}
      </div>
    `;
  }
  return `
    <div class="repair-readable-list">
      ${items.slice(0, options.maxArrayItems).map((item, index) => {
        const title = titleForObject(item, `Item ${index + 1}`);
        const body = renderReadableContent(item, { ...options, depth: depth + 1 });
        const open = depth < 1 && index < 3 ? " open" : "";
        return `
          <details class="repair-readable-card"${open}>
            <summary>
              <strong>${escapeHtml(title)}</strong>
              <span>${escapeHtml(summarizeValue(item))}</span>
            </summary>
            ${body}
          </details>
        `;
      }).join("")}
      ${items.length > options.maxArrayItems ? `<div class="repair-readable-muted">还有 ${items.length - options.maxArrayItems} 项未展开显示，可查看 Raw JSON。</div>` : ""}
    </div>
  `;
}

function renderObject(value, options, depth) {
  const entries = Object.entries(value || {});
  if (!entries.length) return `<span class="repair-readable-empty">{}</span>`;
  const scalarRows = entries.filter(([, item]) => isScalar(item));
  const complexRows = entries.filter(([, item]) => !isScalar(item));
  return `
    <div class="repair-readable-object">
      ${scalarRows.length ? `
        <div class="repair-readable-kv-grid">
          ${scalarRows.map(([key, item]) => `
            <div class="repair-readable-kv-row">
              <span>${escapeHtml(labelFromKey(key))}</span>
              <strong>${renderScalar(item, options)}</strong>
            </div>
          `).join("")}
        </div>
      ` : ""}
      ${complexRows.map(([key, item], index) => {
        const open = depth < 1 && index < 8 ? " open" : "";
        return `
          <details class="repair-readable-field"${open}>
            <summary>
              <strong>${escapeHtml(labelFromKey(key))}</strong>
              <span>${escapeHtml(summarizeValue(item))}</span>
            </summary>
            ${renderReadableContent(item, { ...options, depth: depth + 1 })}
          </details>
        `;
      }).join("")}
    </div>
  `;
}

export function renderReadableContent(value, options = {}) {
  const normalizedOptions = {
    query: options.query || "",
    depth: Number(options.depth || 0),
    maxDepth: Number(options.maxDepth || 5),
    maxArrayItems: Number(options.maxArrayItems || 80),
    emptyText: options.emptyText || EMPTY_TEXT,
  };
  if (value === undefined || value === null || value === "") {
    return `<div class="detail-empty">${escapeHtml(normalizedOptions.emptyText)}</div>`;
  }
  if (normalizedOptions.depth > normalizedOptions.maxDepth) {
    return renderRawJsonBlock(value, normalizedOptions);
  }
  if (Array.isArray(value)) return renderArray(value, normalizedOptions, normalizedOptions.depth);
  if (isObject(value)) return renderObject(value, normalizedOptions, normalizedOptions.depth);
  return renderScalar(value, normalizedOptions);
}

export function renderReadableLlmText(text, options = {}) {
  const parsed = parseJsonFromText(text);
  if (parsed !== null) {
    return `
      <div class="repair-readable-callout">已将模型文本解析为 JSON，并把字符串字段按可读文本展开。</div>
      ${renderReadableContent(parsed, options)}
      <details class="repair-trace-details">
        <summary>原始 extracted content</summary>
        ${renderRawTextBlock(text, options)}
      </details>
    `;
  }
  return renderReadableContent(text, options);
}

export function renderRawTextBlock(text, options = {}) {
  return `<pre class="repair-trace-pre">${highlightHtml(text ?? "", options.query || "")}</pre>`;
}

export function renderRawJsonBlock(value, options = {}) {
  return `<pre class="repair-trace-pre">${highlightHtml(stringifyJson(value), options.query || "")}</pre>`;
}
