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
const approvalRows = new Map();
const approvalBatches = new Map();
const reviews = new Map();
const toolCards = new Map();
const workProgress = new Map();
const TERMINAL = new Set(['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted']);
const EVENT_CARD_KINDS = /^(work|review|tool|approval|execution|file|task|workspace)\./;
const EVENT_CARD_TERMINAL = new Set(['run.failed', 'run.cancelled', 'run.interrupted']);
const TURN_STATES = {
  'message.accepted': '已排队',
  'work.idle': '等待继续', 'work.plan.waiting': '等待继续', 'work.plan.resumed': '已排队',
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
  'work.plan.created': '工作计划已创建', 'work.unit.started': '分组开始',
  'work.unit.completed': '分组完成', 'work.unit.suspended': '分组暂停', 'work.source.changed': '来源已变更', 'work.plan.waiting': '工作计划已暂停',
  'work.plan.resumed': '继续工作计划', 'work.idle': '等待继续',
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
  const turn = { section, request, activity, steps, reply, state, loading, loadingLabel, modelRequest: null };
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
    if (article.classList.contains('timeline-step')) {
      let segment = turn.steps.lastElementChild;
      if (!segment?.classList.contains('timeline-segment')) {
        segment = element('div', undefined, 'timeline-segment');
        const headings = element('div', undefined, 'step-columns');
        headings.setAttribute('aria-hidden', 'true');
        headings.append(...['步骤', '结果', '说明', ''].map(text => element('span', text)));
        segment.append(headings);
        turn.steps.append(segment);
      }
      segment.append(article);
    } else {
      workProgress.delete(run);
      turn.steps.append(article);
    }
  }
}

// A bubble is an entry with a head (label + sub-label) plus body content.
function bubble(kind, label, sub, run, stage, container) {
  const article = element('article', undefined, kind === 'event' ? 'timeline-step' : 'entry entry-' + kind);
  const head = element('header', undefined, 'entry-head');
  const labelEl = element('span', label, 'entry-label');
  const subEl = element('span', sub, 'entry-sub');
  head.append(labelEl, subEl);
  article.append(head);
  if (container) container.append(article);
  else appendEntry(article, run, stage);
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

function addJson(container, payload, label = '详情 JSON') {
  if (!payload || !Object.keys(payload).length) return;
  const details = document.createElement('details');
  details.className = 'event-json';
  details.append(element('summary', label));
  revealJson(details, payload);
  container.append(details);
  return details;
}

function revealJson(details, payload) {
  details.addEventListener('toggle', () => {
    if (details.open && !details.querySelector(':scope > pre')) {
      details.append(element('pre', JSON.stringify(payload, null, 2)));
    }
  });
}

function stepDisclosure(entry, label, payload) {
  const details = element('details', undefined, 'event-json step-details');
  const summary = element('summary', undefined, 'step-summary');
  const link = element('span', label, 'step-link');
  const note = element('span', undefined, 'step-note');
  summary.append(entry.label, entry.sub, note, link);
  details.append(summary);
  entry.head.replaceWith(details);
  if (payload) revealJson(details, payload);
  return { details, note };
}

const STEP_STATUSES = {
  succeeded: '成功', success: '成功', completed: '已完成', delivered: '已交付',
  failed: '失败', error: '失败', timed_out: '超时', cancelled: '已取消',
  interrupted: '已中断', rejected: '已拒绝', partial: '部分完成',
  waiting: '等待继续', waiting_for_approval: '等待审批', awaiting_review: '等待验收',
  blocked: '受阻', running: '进行中', unknown: '结果未知',
};
const EVENT_STATUSES = {
  'task.started': '进行中', 'task.resumed': '已恢复', 'task.delivered': '已交付',
  'task.failed': '失败', 'task.interrupted': '已中断', 'task.needs_replan': '待重规划',
  'task.awaiting_review': '等待验收', 'work.plan.waiting': '等待继续',
  'work.unit.started': '进行中', 'work.unit.completed': '已完成', 'work.unit.suspended': '已暂停',
  'run.failed': '失败', 'run.cancelled': '已取消', 'run.interrupted': '已中断',
};

function setStepStatus(node, status, tone = '') {
  node.textContent = STEP_STATUSES[status] || status || '—';
  node.dataset.tone = tone || (
    ['failed', 'error', 'timed_out', '失败', '超时'].includes(status) ? 'error' :
    ['succeeded', 'success', 'completed', 'delivered', '已完成', '已交付'].includes(status) ? 'success' :
    ['partial', 'blocked', 'waiting', 'awaiting_review', 'unknown'].includes(status) ? 'warning' : ''
  );
}

function setStepNote(node, text, error = false) {
  node.textContent = text || '';
  node.title = node.textContent;
  node.dataset.tone = error ? 'error' : '';
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

function prominentFields(kind, payload) {
  const p = payload || {};
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

function closeApproval(run) {
  for (const control of approvals.get(run) || []) control.remove();
  approvals.delete(run);
}

function approvalEvent(event) {
  const { run, kind, seq, payload } = event;
  const requested = kind === 'approval.requested';
  const batchSeq = requested ? seq : payload.request_seq ?? seq;
  const ids = new Set([
    ...(payload.requests || []).map(request => request.request_id),
    ...(payload.request_ids || []), ...Object.keys(payload.decisions || {}),
  ]);
  ids.delete(undefined);
  ids.delete(null);
  const batchKey = JSON.stringify([run, batchSeq]);
  if (!ids.size) for (const id of approvalBatches.get(batchKey) || [null]) ids.add(id);
  if (requested) approvalBatches.set(batchKey, ids);
  closeApproval(run);
  const actions = [];
  const buttons = [];
  const choices = new Map();
  let submitting = false;

  async function choose(id, choice) {
    if (submitting) return;
    choices.set(id, choice);
    if (ids.size > 1) {
      for (const control of buttons) {
        if (control.id === id) control.button.setAttribute('aria-pressed', String(control.choice === choice));
      }
    }
    if (choices.size < ids.size) return;
    submitting = true;
    for (const { button } of buttons) button.disabled = true;
    try {
      await api(`runs/${run}/decision`, {
        decisions: Object.fromEntries(choices), request_seq: seq,
      });
      for (const action of actions) action.remove();
    } finally {
      submitting = false;
      for (const { button } of buttons) button.disabled = false;
    }
  }

  for (const id of ids) {
    const key = JSON.stringify([run, id ?? batchSeq]);
    const request = (payload.requests || []).find(item => item.request_id === id);
    let row = approvalRows.get(key);
    if (!row) {
      const entry = bubble('event', '审批', '', run);
      entry.article.classList.add('entry-event-approval');
      const history = [event];
      const { details, note } = stepDisclosure(entry, '详情', history);
      row = { ...entry, history, details, note };
      approvalRows.set(key, row);
    } else {
      row.history.push(event);
      const json = row.details.querySelector('pre');
      if (json) json.textContent = JSON.stringify(row.history, null, 2);
    }
    const subject = request?.tool || request?.path || request?.key || request?.kind;
    if (subject && (requested || !row.subject)) row.subject = subject;
    const choice = payload.decisions?.[id] || payload.decision;
    setStepStatus(row.sub, requested ? '等待审批' : kind === 'approval.closed' ? '已关闭' :
      ({approve:'已批准', remember:'已批准并记住', reject:'已拒绝'})[choice] || '已处理',
    requested || choice === 'reject' ? 'warning' : choice ? 'success' : '');
    setStepNote(row.note, kind === 'approval.closed' && payload.reason ?
      [row.subject, payload.reason].filter(Boolean).join(' · ') : row.subject);

    if (!requested || !request || !id) continue;
    const action = element('div', undefined, 'approval-controls');
    if (request.tool && request.kind === 'code_execution') {
      action.append(element('p', [request.execution_location, request.working_directory,
        request.mode, request.notice].filter(Boolean).join(' · ')));
      action.append(element('pre', request.argv ? JSON.stringify(request.argv) : request.code || request.path));
    }
    if (ids.size > 1 && !actions.length) {
      action.append(element('p', '逐项选择，选完自动提交', 'approval-batch-hint'));
    }
    const options = element('div', undefined, 'approval-buttons');
    options.setAttribute('role', 'group');
    options.setAttribute('aria-label', '审批决定：' + (row.subject || row.label.textContent));
    const decisions = [
      ['reject', '拒绝'],
      ['approve', request.approve_scope === 'run' ? '允许本轮' : '允许本次'],
    ];
    if (request.allow_remember) decisions.push(['remember', '在此会话记住']);
    for (const [choice, label] of decisions) {
      const button = element('button', label, choice === 'approve' ? 'approval-allow' : undefined);
      button.type = 'button';
      if (ids.size > 1) button.setAttribute('aria-pressed', 'false');
      button.onclick = handle(() => choose(id, choice));
      buttons.push({ id, choice, button });
      options.append(button);
    }
    action.append(options);
    actions.push(action);
    row.article.append(action);
  }
  if (actions.length) approvals.set(run, actions);
}

function toolEventCard(event) {
  const { kind, run, payload, seq } = event;
  const key = payload.call_id ? JSON.stringify([run, payload.call_id]) : null;
  let card = key ? toolCards.get(key) : null;
  if (!card) {
    const entry = bubble('event', '工具执行', '执行中', run);
    const { article, label, sub } = entry;
    article.classList.add('entry-event-tool');
    const { details, note } = stepDisclosure(entry, '详情');
    details.classList.add('tool-details');
    const identifiers = element('div');
    details.append(identifiers);
    card = { article, label, sub, note, details, identifiers };
    if (key) toolCards.set(key, card);
  }

  const isResult = kind === 'tool.result';
  const section = element('section', undefined, 'event-block');
  section.append(element('h4', EVENT_LABELS[kind] + ' · seq ' + seq, 'event-block-title'));
  addJson(section, event, '完整事件 JSON');
  card.details.append(section);

  card.label.textContent = payload.name || card.name || '工具执行';
  card.name = card.label.textContent;
  card.taskId = payload.task_id || card.taskId;
  card.identifiers.replaceChildren();
  addFields(card.identifiers, [['Task ID', card.taskId], ['工具', card.name], ['Call ID', payload.call_id]]);
  if (!isResult) return;
  setStepStatus(card.sub, payload.execution_status || payload.status || '已返回');
  const error = payload.error || payload.reason ||
    (payload.status === 'error' ? payload.content : '');
  const notes = [];
  if (typeof error === 'string' && error.trim()) notes.push(error.trim().split(/\r?\n/, 1)[0]);
  if (payload.effects === 'workspace_may_have_changed') notes.push('原目录可能已有修改');
  setStepNote(card.note, notes.join(' · '), Boolean(error));
  addArtifactActions(card.article, event);
}

function workProgressCard(event) {
  const { run, kind } = event;
  let progress = workProgress.get(run);
  if (!progress) {
    const details = element('details', undefined, 'timeline-step work-progress');
    const summary = element('summary', undefined, 'step-summary');
    const status = element('span', undefined, 'entry-sub');
    const note = element('span', undefined, 'step-note');
    summary.append(element('span', '工作计划进度', 'entry-label'), status, note,
      element('span', '详情', 'step-link'));
    const items = element('div', undefined, 'work-progress-events');
    details.append(summary, items);
    appendEntry(details, run);
    progress = { status, note, items, count: 0 };
    workProgress.set(run, progress);
  }
  progress.count++;
  setStepStatus(progress.status, EVENT_STATUSES[kind] || EVENT_LABELS[kind]);
  setStepNote(progress.note, progress.count + ' 条记录');
  eventCard(event, progress.items);
}

function taskPlanBubble(event) {
  const goals = (event.payload.items || []).map(item => item.step_spec?.goal).filter(Boolean);
  if (!goals.length) {
    eventCard(event);
    return;
  }
  const { article } = bubble('assistant-stream', 'Artorias', '任务计划', event.run);
  article.classList.add('sealed');
  const list = element('ol', undefined, 'plan-goals');
  for (const goal of goals) list.append(element('li', goal, 'message-text'));
  article.append(list);
  addJson(article, event, '查看计划详情 · seq ' + event.seq);
}

function eventCard(event, container) {
  const { kind, run, payload } = event;
  const label = EVENT_LABELS[kind] || kind;
  const entry = bubble('event', label, kind, run, undefined, container);
  entry.article.classList.add('entry-event-' + eventCategory(kind));
  if (run) entry.article.dataset.run = run;

  setStepStatus(entry.sub, payload.step_outcome || payload.status || EVENT_STATUSES[kind]);
  const fields = kind.startsWith('task.') && !payload.reason ? [] : prominentFields(kind, payload);
  const { details, note } = stepDisclosure(entry, '详情', event);
  const problem = payload.error || (kind.endsWith('.failed') && payload.reason);
  const reason = problem || payload.reason || payload.key;
  setStepNote(note, typeof reason === 'string' ? reason : '', Boolean(problem));
  if (fields.length) addFields(details, fields);

  if (kind === 'work.plan.waiting') {
    const extra = payload.calls >= payload.call_limit || payload.task_calls >= payload.task_limit ? 80 : 0;
    addTextBlock(entry.article, '暂停原因', payload.reason);
    addTextBlock(entry.article, '执行进度', [payload.units_completed + '/' + payload.units_total + ' 个单元已完成',
      '本步模型调用 ' + payload.calls + '/' + payload.call_limit].join('，'));
    const button = element('button', extra ? '追加 80 次模型调用并继续' : '继续');
    button.dataset.workPlan = payload.plan_id;
    const key = crypto.randomUUID();
    button.onclick = handle(async () => {
      await api('sessions/' + event.session + '/work-plans/' + payload.plan_id + '/resume', {
        generation: payload.generation, request_key: key, additional_calls: extra,
      });
      button.disabled = true;
    });
    entry.article.append(button);
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
    if (kind === 'work.plan.waiting' || kind === 'work.idle') runs.set(run, 'waiting');
    if (kind === 'work.plan.resumed' || TERMINAL.has(kind)) {
      turnFor(run).section.querySelectorAll('button[data-work-plan]').forEach(button => {
        if (TERMINAL.has(kind) || button.dataset.workPlan === payload.plan_id) button.disabled = true;
      });
      if (kind === 'work.plan.resumed') {
        userBubble(run, payload.text);
        runs.set(run, 'queued');
      }
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
    if (kind === 'tool.call' || kind === 'tool.result') toolEventCard(event);
    else if (kind.startsWith('approval.')) approvalEvent(event);
    else if (kind === 'task.plan' && payload.kind === 'tasks') taskPlanBubble(event);
    else if (kind.startsWith('work.unit.') || kind === 'work.plan.created' ||
      kind === 'work.source.changed' || kind === 'work.idle') workProgressCard(event);
    else if (EVENT_CARD_KINDS.test(kind) || EVENT_CARD_TERMINAL.has(kind)) eventCard(event);
  }
  if (run && (turns.has(run) || kind === 'model.started')) {
    updateTurnLoading(turnFor(run), kind, payload);
  }
  if (kind === 'run.failed') $('status').textContent = payload.error || '运行失败';
  else if (kind === 'approval.requested') $('status').textContent = '等待审批';
  else if (kind === 'work.plan.waiting') $('status').textContent = payload.reason;
  else if (kind === 'work.idle') $('status').textContent = '工作计划已暂停';
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
  approvalRows.clear();
  approvalBatches.clear();
  reviews.clear();
  toolCards.clear();
  workProgress.clear();
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
