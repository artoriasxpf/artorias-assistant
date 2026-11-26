"use strict";

const collection = "tasks_sandbox";
const partition = "demo-session";
const projectionVersion = "admin-demo-v1";
const scope = {collection, projection_version: projectionVersion};
const documentId = "demo-task-1";
const endpoints = {
  status: {method: "POST", path: "v1/status", help: "查看集合是否就绪，以及生产者身份和最近消费的序号。"},
  search: {method: "POST", path: "v1/search", help: "在指定 collection 和 partition 中查询关键词。"},
  events: {method: "POST", path: "v1/events", help: "snapshot 替换整个集合；upsert 新增或更新文档；delete 删除文档。增量写入需保持 source_id 一致且 seq 连续。"},
  health: {method: "GET", path: "healthz", help: "检查服务能否响应请求。"},
  ready: {method: "GET", path: "readyz", help: "检查服务能否读取索引表。"},
};

function event(operation, seq, document) {
  return {
    ...scope, source_id: "admin-demo", seq, operation,
    entries: document ? [{partition, document}] : [],
  };
}

const templates = {
  status: [["索引状态", scope]],
  search: [["搜索测试 Task", {...scope, partition, query: "搜索", mode: "any", limit: 10}]],
  events: [
    ["初始化快照", event("snapshot", 1)],
    ["新增文档", event("upsert", 2, {id: documentId, title: "实现搜索服务", keywords: "任务 索引", body: "验证 Task 搜索。"})],
    ["更新文档", event("upsert", 3, {id: documentId, title: "完善搜索管理台", keywords: "搜索 管理台", body: "验证修改后的索引。"})],
    ["删除文档", event("delete", 4, {id: documentId})],
  ],
};
const form = document.getElementById("request-form");
const token = document.getElementById("token");
const endpoint = document.getElementById("endpoint");
const preset = document.getElementById("preset");
const editor = document.getElementById("payload");
const error = document.getElementById("json-error");
const send = document.getElementById("send");
const response = document.getElementById("response");
const status = document.getElementById("response-status");
const drafts = new Map();
let activeEndpoint;

function showError(message = "") {
  error.textContent = message;
  error.hidden = !message;
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function loadPreset() {
  editor.value = pretty(templates[activeEndpoint][Number(preset.value)][1]);
  showError();
}

function selectEndpoint() {
  if (activeEndpoint && templates[activeEndpoint]) {
    drafts.set(activeEndpoint, {text: editor.value, preset: preset.value});
  }
  activeEndpoint = endpoint.value;
  const {method, path, help} = endpoints[activeEndpoint];
  const options = templates[activeEndpoint] || [];
  document.getElementById("request-path").textContent = `${method} /${path}`;
  document.getElementById("request-help").textContent = help;
  document.getElementById("json-controls").hidden = !options.length;
  preset.replaceChildren(...options.map(([label], i) => new Option(label, String(i))));
  if (!options.length) {
    editor.value = "";
  } else if (drafts.has(activeEndpoint)) {
    const draft = drafts.get(activeEndpoint);
    editor.value = draft.text;
    preset.value = draft.preset;
  } else {
    loadPreset();
  }
  showError();
}

function parsePayload() {
  try {
    const value = JSON.parse(editor.value);
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      showError("请求正文必须是 JSON 对象。");
      editor.focus();
      return null;
    }
    showError();
    return value;
  } catch (err) {
    showError(`JSON 格式错误：${err.message}`);
    editor.focus();
    return null;
  }
}

function showStatus(label, tone = "") {
  status.textContent = label;
  status.dataset.tone = tone;
}

endpoint.addEventListener("change", selectEndpoint);
editor.addEventListener("input", () => showError());
document.getElementById("load-preset").addEventListener("click", loadPreset);
document.getElementById("format-json").addEventListener("click", () => {
  const payload = parsePayload();
  if (payload) editor.value = pretty(payload);
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (send.disabled) return;
  const credential = token.value.trim();
  if (!credential) {
    token.focus();
    return;
  }

  const selected = activeEndpoint;
  const {method, path} = endpoints[selected];
  const headers = {Authorization: `Bearer ${credential}`};
  const options = {method, headers, redirect: "error", cache: "no-store"};
  if (method === "POST") {
    const payload = parsePayload();
    if (!payload) return;
    if (selected === "events" && payload.collection !== collection &&
        !window.confirm(`将写入集合 ${payload.collection ?? "（未指定）"}。快照会替换整个集合，手工写入可能影响同步游标。确定发送？`)) {
      return;
    }
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }

  send.disabled = true;
  showStatus("请求中");
  response.textContent = `${method} /${path}`;
  const start = performance.now();
  try {
    const result = await fetch(new URL(path, window.location.href), options);
    const body = await result.text();
    let text = body;
    try {
      text = pretty(JSON.parse(body));
    } catch {
      // Preserve non-JSON error bodies for troubleshooting.
    }
    showStatus(`HTTP ${result.status} · ${Math.round(performance.now() - start)} ms`, result.ok ? "ok" : "error");
    response.textContent = text || "（无响应正文）";
  } catch (err) {
    showStatus("请求失败", "error");
    response.textContent = err.message;
  } finally {
    send.disabled = false;
  }
});

selectEndpoint();
