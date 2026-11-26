import { api, bootstrap, headers } from './api.js';
import { watch } from './events.js';
import { setupWorkspace, showWorkspace } from './workspace.js';

const $ = id => document.getElementById(id);
const report = error => { $('status').textContent = error.message; };
const handle = action => async (...args) => {
  try { await action(...args); } catch (error) { report(error); }
};

let sessions = [];
let sessionId, controller;
let sending = false;
let composing = false;
const drafts = new Map();
const runs = new Map();
const turns = new Map();
const streams = new Map();
const approvals = new Map();
const reviews = new Map();
const TERMINAL = new Set(['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted']);
const EVENT_CARD_KINDS = /^(review|tool|approval|execution|file|task|workspace)\./;
const EVENT_CARD_TERMINAL = new Set(['run.failed', 'run.cancelled', 'run.interrupted']);
const TURN_STATES = {
  'message.accepted': '已排队',
  'run.started': '处理中',
  'tool.call': '执行工具',
  'execution.started': '执行工具',
  'assistant.final': '正在收尾',
  'approval.requested': '等待审批',
  'approval.decided': '等待继续',
  'run.completed': '已完成',
  'run.failed': '失败',
  'run.cancelled': '已取消',
  'run.interrupted': '已中断',
};
const EVENT_LABELS = {
  'tool.call': '工具调用', 'tool.result': '工具结果',
  'approval.requested': '审批请求', 'approval.decided': '审批决定', 'approval.closed': '审批关闭',
  'run.started': '运行开始', 'run.completed': '运行完成', 'run.failed': '运行失败',
  'run.cancelled': '运行取消', 'run.interrupted': '运行中断',
  'task.plan': '任务计划', 'task.started': '任务开始', 'task.resumed': '任务恢复',
  'task.delivered': '任务交付', 'task.failed': '任务失败', 'task.interrupted': '任务中断',
  'task.revised': '任务修订',
  'workspace.selected': '切换工作空间',
  'execution.started': '执行开始', 'execution.finished': '执行完成',
  'file.changed': '文件变更', 'file.deleted': '文件删除', 'file.reverted': '文件撤销',
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function shortId(value) {
  return value ? String(value).slice(0, 8) : '—';
}

function format(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function turnFor(run) {
  if (turns.has(run)) return turns.get(run);
  const section = element('section', undefined, 'turn');
  const heading = element('h2', `回合 ${turns.size + 1}`, 'turn-title');
  heading.id = `turn-${turns.size + 1}`;
  section.setAttribute('aria-labelledby', heading.id);
  const state = element('span', '', 'turn-state');
  const header = element('header', undefined, 'turn-header');
  header.append(heading, state);
  const request = element('div', undefined, 'turn-request');
  const activity = element('div', undefined, 'turn-activity');
  activity.hidden = true;
  const steps = element('div', undefined, 'turn-steps');
  activity.append(element('h3', '处理过程', 'turn-stage'), steps);
  const reply = element('div', undefined, 'turn-reply');
  const loading = element('div', undefined, 'turn-loading');
  loading.hidden = true;
  const dots = element('span', undefined, 'turn-loading-dots');
  dots.setAttribute('aria-hidden', 'true');
  dots.append(...Array.from({length: 3}, () => element('i')));
  const loadingLabel = element('span');
  loading.append(dots, loadingLabel);
  section.append(header, request, activity, reply, loading);
  $('log').append(section);
  const turn = { request, activity, steps, reply, state, loading, loadingLabel, modelRequest: null };
  turns.set(run, turn);
  return turn;
}

function updateTurnLoading(turn, kind, payload) {
  if (kind === 'model.started') {
    turn.modelRequest = payload.request_id;
    turn.loadingLabel.textContent = payload.stage === 'planner' ? '正在规划' : '思考中';
    turn.loading.hidden = false;
    turn.state.textContent = '处理中';
  } else if (kind === 'model.finished') {
    if (turn.modelRequest !== payload.request_id) return;
    turn.modelRequest = null;
    turn.loading.hidden = true;
  } else if ((kind === 'assistant.delta' && payload.text) || Object.hasOwn(TURN_STATES, kind)) {
    turn.modelRequest = null;
    turn.loading.hidden = true;
    turn.state.textContent = kind === 'assistant.delta' ? '正在回复' : TURN_STATES[kind];
  }
}

function appendEntry(article, run, stage) {
  $('empty').hidden = true;
  if (!run) {
    $('log').append(article);
    return;
  }
  const turn = turnFor(run);
  if (stage === 'request') turn.request.append(article);
  else if (stage === 'reply') turn.reply.append(article);
  else {
    turn.activity.hidden = false;
    turn.steps.append(article);
  }
}

// A bubble is an entry with a head (label + sub-label) plus body content.
function bubble(kind, label, sub, run, stage) {
  const article = element('article', undefined, 'entry entry-' + kind);
  const head = element('header', undefined, 'entry-head');
  const labelEl = element('span', label, 'entry-label');
  const subEl = element('span', sub, 'entry-sub');
  head.append(labelEl, subEl);
  article.append(head);
  appendEntry(article, run, stage);
  return { article, head, label: labelEl, sub: subEl };
}

function userBubble(run, text) {
  const entry = bubble('user', '你', '用户消息', run, 'request');
  entry.article.append(element('div', text, 'message-text'));
  return entry.article;
}

function assistantBubble(run, text) {
  const entry = bubble('assistant', 'Artorias', '最终回复', run, 'reply');
  entry.article.append(element('div', text, 'message-text'));
  return entry.article;
}

// Model output starts as an intermediate streaming bubble; it is promoted to the
// final assistant bubble once the run completes (or assistant.final arrives).
function streamFor(run) {
  let stream = streams.get(run);
  if (!stream) {
    const entry = bubble('assistant-stream', '模型输出', '进行中', run);
    const content = element('div', '', 'message-text');
    entry.article.append(content);
    stream = { article: entry.article, label: entry.label, sub: entry.sub, content, text: '' };
    streams.set(run, stream);
  }
  return stream;
}

// Finalize a streaming bubble as a completed intermediate output (e.g. when the
// model pauses for a tool call or a task step delivers its result).
function sealStream(run) {
  const stream = streams.get(run);
  if (stream) {
    stream.sub.textContent = '中间输出';
    stream.article.classList.add('sealed');
    streams.delete(run);
  }
}

// Mark an interrupted stream without promoting it to the final answer.
function settleStream(run) {
  const stream = streams.get(run);
  if (stream) {
    stream.sub.textContent = '已停止';
    stream.article.classList.add('sealed');
    streams.delete(run);
  }
}

function promoteStream(run, text) {
  const stream = streams.get(run);
  if (stream) {
    streams.delete(run);
    stream.article.classList.remove('entry-assistant-stream');
    stream.article.classList.add('entry-assistant');
    stream.label.textContent = 'Artorias';
    stream.sub.textContent = '最终回复';
    stream.content.textContent = text ?? stream.text;
    if (run) {
      const turn = turnFor(run);
      turn.reply.append(stream.article);
      turn.activity.hidden = !turn.steps.childElementCount;
    }
    return;
  }
  if (text) assistantBubble(run, text);
}

function addFields(container, fields) {
  const list = element('dl', undefined, 'event-fields');
  for (const [label, value] of fields) {
    list.append(element('dt', label), element('dd', format(value)));
  }
  container.append(list);
}

function addTextBlock(container, title, value) {
  if (value === undefined || value === null || value === '') return;
  const block = element('section', undefined, 'event-block');
  block.append(element('h4', title, 'event-block-title'));
  block.append(element('div', value, 'message-text'));
  container.append(block);
}

function addJson(container, payload) {
  if (!payload || !Object.keys(payload).length) return;
  const details = document.createElement('details');
  details.className = 'event-json';
  details.append(element('summary', '详情 JSON'));
  details.addEventListener('toggle', () => {
    if (details.open && details.childElementCount === 1) {
      details.append(element('pre', JSON.stringify(payload, null, 2)));
    }
  });
  container.append(details);
}

function eventCategory(kind) {
  if (kind.startsWith('tool.')) return 'tool';
  if (kind.startsWith('approval.')) return 'approval';
  if (kind.startsWith('run.')) return 'run';
  if (kind.startsWith('task.')) return 'task';
  if (kind.startsWith('workspace.')) return 'workspace';
  if (kind.startsWith('execution.')) return 'execution';
  if (kind.startsWith('file.')) return 'file';
  return 'misc';
}

function taskField(p) { return p.task_id ? [['Task ID', p.task_id]] : []; }

function execFields(p) {
  const fields = [];
  if (p.execution_id) fields.push(['Execution ID', p.execution_id]);
  if (p.execution_status) fields.push(['执行状态', p.execution_status]);
  if (p.publish_status) fields.push(['发布状态', p.publish_status]);
  if (p.exit_code !== undefined && p.exit_code !== null) fields.push(['退出码', p.exit_code]);
  return fields;
}

function prominentFields(kind, payload) {
  const p = payload || {};
  if (kind === 'tool.call') return [['Call ID', p.call_id], ['工具', p.name], ...taskField(p)];
  if (kind === 'tool.result') return [['Call ID', p.call_id], ['工具', p.name], ['状态', p.status], ...taskField(p), ...execFields(p)];
  if (kind.startsWith('approval.')) {
    const fields = [...taskField(p)];
    if (p.decision) fields.push(['决定', ({approve: '批准', remember: '在此会话记住', reject: '拒绝'})[p.decision]]);
    if (p.reason) fields.push(['原因', p.reason]);
    if (p.request_seq !== undefined) fields.push(['Request Seq', p.request_seq]);
    return fields;
  }
  if (kind.startsWith('task.')) {
    const fields = [...taskField(p)];
    if (p.key) fields.push(['Key', p.key]);
    if (p.status) fields.push(['状态', p.status]);
    if (p.reason) fields.push(['原因', p.reason]);
    if (p.scope) fields.push(['Scope', p.scope]);
    if (p.base_revision !== undefined) fields.push(['基础版本', p.base_revision]);
    if (p.applied_revision !== undefined) fields.push(['生效版本', p.applied_revision]);
    return fields;
  }
  if (kind.startsWith('run.')) {
    const fields = [];
    if (p.error) fields.push(['错误', p.error]);
    if (p.reason) fields.push(['原因', p.reason]);
    return fields;
  }
  if (kind === 'workspace.selected') return [['原空间', p.previous_workspace_id], ['新空间', p.workspace_id], ['类型', p.kind]];
  if (kind.startsWith('file.')) {
    const fields = [['Change ID', p.change_id], ['路径', p.path], ['版本', p.version]];
    if (p.reverts) fields.push(['撤销', p.reverts]);
    return fields;
  }
  return [];
}

async function downloadArtifact(event, artifact) {
  const url = `/api/sessions/${encodeURIComponent(event.session)}/executions/${encodeURIComponent(event.payload.execution_id)}/artifacts/${encodeURIComponent(artifact.id)}`;
  const response = await fetch(url, { headers: headers() });
  if (!response.ok) throw Error('产物下载失败：' + response.status);
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = artifact.path.split('/').pop();
  link.click();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function addArtifactActions(container, event) {
  const artifacts = (event.payload.artifacts || []).filter(item => item.action === 'write');
  if (!event.payload.execution_id || !artifacts.length) return;
  const actions = element('div', undefined, 'event-actions');
  for (const artifact of artifacts) {
    const button = element('button', '下载：' + artifact.path);
    button.onclick = handle(() => downloadArtifact(event, artifact));
    actions.append(button);
  }
  container.append(actions);
}

function addApprovalRequests(container, payload) {
  const requests = payload.requests || [];
  for (const request of requests) {
    const { request_id, key, kind, path, ...rest } = request;
    const block = element('section', undefined, 'event-block');
    block.append(element('h4', '审批对象', 'event-block-title'));
    const fields = [['Request ID', request_id]];
    if (kind) fields.push(['类型', kind]);
    if (key && key !== kind) fields.push(['Key', key]);
    if (path) fields.push(['路径', path]);
    addFields(block, fields);
    addJson(block, rest);
    container.append(block);
  }
}

function closeApproval(run) {
  approvals.get(run)?.remove();
  approvals.delete(run);
}

function eventCard(event) {
  const { kind, run, payload } = event;
  const label = EVENT_LABELS[kind] || kind;
  const entry = bubble('event', label, kind, run);
  entry.article.classList.add('entry-event-' + eventCategory(kind));
  if (run) entry.article.dataset.run = run;

  const meta = element('div', undefined, 'entry-meta');
  meta.textContent = run ? `run ${shortId(run)} · seq ${event.seq}` : `会话 · seq ${event.seq}`;
  entry.article.append(meta);

  addFields(entry.article, prominentFields(kind, payload));

  if (kind === 'tool.call' || kind === 'tool.result') {
    if (kind === 'tool.result') {
      const error = payload.error || (payload.status === 'error' ? payload.content : '');
      if (typeof error === 'string' && error.trim()) {
        const firstLine = error.trim().split(/\r?\n/, 1)[0];
        const summary = element('p', firstLine.slice(0, 180) + (firstLine.length > 180 ? '…' : ''), 'event-error-summary');
        entry.article.append(summary);
      }
      addArtifactActions(entry.article, event);
    }
    addJson(entry.article, payload);
  } else if (kind.startsWith('approval.')) {
    addApprovalRequests(entry.article, payload);
  } else if (kind === 'task.delivered') {
    addTextBlock(entry.article, '交付内容', payload.text);
    addJson(entry.article, payload);
  } else {
    addJson(entry.article, payload);
  }

  if (kind.startsWith('review.')) {
    reviews.get(payload.id)?.remove();
    reviews.delete(payload.id);
    if (kind === 'review.requested') {
      const actions = element('div', undefined, 'event-actions');
      const sid = sessionId;
      actions.append(element('p', payload.question), element('p', payload.preview));
      const inspect = element('button','查看完整交付');
      inspect.onclick = handle(async () => {
        const response = await fetch(`/api/sessions/${sid}/reviews/${payload.id}/evidence`, {headers:headers()});
        if (!response.ok) throw Error('读取交付失败');
        if (payload.evidence?.artifact_id === 'response') {
          addTextBlock(entry.article, '交付正文', await response.text());
        } else {
          const url = URL.createObjectURL(await response.blob());
          const link = element('a');
          link.href = url;
          link.download = payload.evidence?.path?.split('/').pop() || 'artifact';
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
      });
      actions.append(inspect);
      const feedback = document.createElement('input');
      feedback.placeholder = '修改意见（可选）';
      actions.append(feedback);
      for (const [decision, label] of [['accept','通过'],['request_changes','需要修改']]) {
        const button = element('button', label);
        const key = crypto.randomUUID();
        button.onclick = handle(async () => {
          await api(`sessions/${sid}/reviews/${payload.id}/decision`, {
            decision, decision_key:key, feedback:feedback.value,
            criterion_version:payload.criterion_version, evidence_version:payload.evidence_version
          });
          actions.remove();
        });
        actions.append(button);
      }
      reviews.set(payload.id, actions);
      entry.article.append(actions);
    }
  }
  if (kind.startsWith('approval.')) closeApproval(run);
  if (kind === 'approval.requested') {
    const actions = element('div', undefined, 'event-actions');
    approvals.set(run, actions);
    const requests = payload.requests || [];
    const controls = new Map();
    for (const request of requests) {
      const label = element('label', request.path || request.kind || '审批');
      const select = element('select');
      const defaultLabel = request.approve_scope === 'run' ? '本轮允许' : '允许此次';
      select.add(new Option('拒绝', 'reject'));
      select.add(new Option(defaultLabel, 'approve'));
      if (request.allow_remember) select.add(new Option('在此会话记住', 'remember'));
      label.append(select);
      actions.append(label);
      controls.set(request.request_id, select);
    }
    const submit = element('button', '提交决定');
    submit.onclick = handle(async () => {
      submit.disabled = true;
      try {
        const choices = Object.fromEntries([...controls].map(([id, select]) => [id, select.value]));
        await api(`runs/${run}/decision`, { decisions: choices, request_seq: event.seq });
        actions.remove();
      } finally {
        submit.disabled = false;
      }
    });
    actions.append(submit);
    entry.article.append(actions);
  }
}

function sessionTitle(session) {
  return session?.title?.trim() || '新会话';
}

function renderSessions() {
  $('sessions').replaceChildren();
  for (const session of sessions) {
    const title = sessionTitle(session);
    const button = element('button', title, 'session');
    button.type = 'button';
    button.title = title + ' · ' + session.id;
    if (session.id === sessionId) button.setAttribute('aria-current', 'true');
    button.onclick = handle(() => selectSession(session.id));
    $('sessions').append(button);
  }
  $('sessionTitle').textContent = sessionTitle(sessions.find(session => session.id === sessionId));
}

function activeRun() {
  for (const [id, state] of runs) if (state === 'running' || state === 'waiting') return id;
  return runs.keys().next().value;
}

function updateComposer() {
  $('send').disabled = !sessionId || sending || !$('input').value.trim();
  $('cancel').hidden = !activeRun();
}

function renderEvent(event) {
  const timeline = $('timeline');
  const follow = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
  const { kind, run, payload } = event;
  if (kind === 'message.accepted') {
    userBubble(run, payload.text);
    if (!runs.has(run)) runs.set(run, 'queued');
    const session = sessions.find(item => item.id === sessionId);
    if (session && !session.title) {
      session.title = payload.text.slice(0, 80);
      renderSessions();
    }
  } else if (kind === 'assistant.delta') {
    if (payload.text) {
      const stream = streamFor(run);
      stream.text += payload.text;
      stream.content.append(document.createTextNode(payload.text));
    }
  } else if (kind === 'assistant.final') {
    promoteStream(run, payload.text);
  } else {
    if (kind === 'run.started') {
      runs.set(run, 'running');
      closeApproval(run);
    }
    if (kind === 'task.awaiting_review') runs.delete(run);
    if (kind === 'approval.requested' || kind === 'analysis.suspended') runs.set(run, 'waiting');
    if (kind === 'approval.decided') runs.set(run, 'queued');
    if (kind === 'run.completed') promoteStream(run, null);
    // A tool call or a new task step finalizes the current model output bubble.
    if (kind === 'tool.call' || kind === 'approval.requested' || kind === 'task.started' || kind === 'task.resumed' || kind === 'task.delivered') {
      sealStream(run);
    }
    if (TERMINAL.has(kind)) {
      if (kind !== 'run.completed') settleStream(run);
      runs.delete(run);
      sealStream(run);
      closeApproval(run);
    }
    if (EVENT_CARD_KINDS.test(kind) || EVENT_CARD_TERMINAL.has(kind)) eventCard(event);
  }
  if (run && (turns.has(run) || kind === 'model.started')) {
    updateTurnLoading(turnFor(run), kind, payload);
  }
  if (kind === 'run.failed') $('status').textContent = payload.error || '运行失败';
  else if (kind === 'approval.requested') $('status').textContent = '等待审批';
  else if (kind === 'analysis.suspended') $('status').textContent = '分析已保存，等待继续…';
  else if (kind === 'run.started') $('status').textContent = '处理中…';
  else if (TERMINAL.has(kind) && !activeRun()) $('status').textContent = '';
  updateComposer();
  if (follow) timeline.scrollTop = timeline.scrollHeight;
}

async function selectSession(id) {
  if (id === sessionId) return;
  controller?.abort();
  sessionId = id;
  input.value = drafts.get(id) || '';
  localStorage.artoriasSession = id;
  streams.clear();
  turns.clear();
  runs.clear();
  approvals.clear();
  reviews.clear();
  $('log').replaceChildren();
  $('empty').hidden = false;
  $('status').textContent = '';
  $('workspace').disabled = true;
  $('workspace').textContent = '选择工作空间';
  renderSessions();
  updateComposer();
  controller = new AbortController();
  void watch(id, controller.signal, renderEvent, () => {
    $('status').textContent = '正在重新连接';
  });
  await showWorkspace(id, () => sessionId);
}

$('new').onclick = handle(async () => {
  $('new').disabled = true;
  try {
    const session = await api('sessions', {});
    sessions.unshift(session);
    await selectSession(session.id);
    $('input').focus();
  } finally {
    $('new').disabled = false;
  }
});

const input = $('input');
input.addEventListener('input', () => {
  if (sessionId) drafts.set(sessionId, input.value);
  updateComposer();
});
input.addEventListener('compositionstart', () => { composing = true; });
input.addEventListener('compositionend', () => { composing = false; });
input.addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || composing || event.keyCode === 229) return;
  event.preventDefault();
  if (!event.repeat) $('composer').requestSubmit();
});
$('composer').onsubmit = handle(async event => {
  event.preventDefault();
  const text = input.value;
  if (!sessionId || sending || composing || !text.trim()) return;
  const selected = sessionId;
  sending = true;
  updateComposer();
  try {
    await api(`sessions/${selected}/messages`, { text, request_key: crypto.randomUUID() });
    if (drafts.get(selected) === text) drafts.delete(selected);
    if (selected === sessionId) {
      if (input.value === text) input.value = '';
      $('timeline').scrollTop = $('timeline').scrollHeight;
    }
  } finally {
    sending = false;
    updateComposer();
  }
});
$('cancel').onclick = handle(async () => {
  const run = activeRun();
  if (run) await api(`runs/${run}/cancel`, {});
});
(async () => {
  const settings = await bootstrap();
  setupWorkspace(() => sessionId, handle, settings.native_folder_picker);
  sessions = await api('sessions');
  const id = sessions.find(session => session.id === localStorage.artoriasSession)?.id ?? sessions[0]?.id;
  if (id) await selectSession(id);
  else await $('new').onclick();
})().catch(report);
