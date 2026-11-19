import { api, bootstrap, headers } from './api.js';
import { watch } from './events.js';
import { setupWorkspace, loadSpaces, showWorkspace } from './workspace.js';

const $ = id => document.getElementById(id);
let sessionId, runId, controller;
let answers = {};
const report = error => { $('status').textContent = error.message; };
const handle = action => async () => { try { await action(); } catch (error) { report(error); } };

function card(text) {
  const article = document.createElement('article');
  article.textContent = text;
  $('log').append(article);
  return article;
}

function renderEvent(event) {
  runId = event.run || runId;
  const { kind, payload } = event;
  if (kind === 'message.accepted') card('你：' + payload.text);
  else if (kind === 'assistant.delta' || kind === 'assistant.final') {
    answers[event.run] ??= card('');
    if (kind === 'assistant.delta') answers[event.run].textContent += payload.text;
    else answers[event.run].textContent = payload.text;
  } else {
    if (kind === 'approval.requested') {
      const article = card('执行审批：' + JSON.stringify(payload));
      for (const decision of ['approve', 'reject']) {
        const button = document.createElement('button');
        button.textContent = decision === 'approve' ? '批准' : '拒绝';
        button.onclick = handle(async () => {
          await api(`runs/${event.run}/decision`, { decision });
          article.remove();
        });
        article.append(button);
      }
    }
    $('status').textContent = kind;
    if (kind.endsWith('failed') || kind === 'run.interrupted') card(JSON.stringify(payload));
    if (kind.startsWith('tool.') || kind.startsWith('file.')) card(kind + ' ' + JSON.stringify(payload));
    if (kind.startsWith('execution.')) card(kind + ' ' + JSON.stringify(payload));
    if (kind === 'tool.result' && payload.execution_id) {
      const article = card(`Python：${payload.execution_status}；文件提交：${payload.publish_status}`);
      for (const artifact of payload.artifacts || []) {
        if (artifact.action !== 'write') continue;
        const button = document.createElement('button');
        button.textContent = '下载产物：' + artifact.path;
        button.onclick = handle(async () => {
          const url = `/api/sessions/${encodeURIComponent(event.session)}/executions/${encodeURIComponent(payload.execution_id)}/artifacts/${encodeURIComponent(artifact.id)}`;
          const response = await fetch(url, { headers: headers() });
          if (!response.ok) throw Error('产物下载失败：' + response.status);
          const objectUrl = URL.createObjectURL(await response.blob());
          const link = document.createElement('a');
          link.href = objectUrl;
          link.download = artifact.path.split('/').pop();
          link.click();
          setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        });
        article.append(button);
      }
    }

  }
}

async function select() {
  controller?.abort();
  sessionId = $('sessions').value;
  localStorage.artoriasSession = sessionId;
  runId = null;
  answers = {};
  $('log').replaceChildren();
  $('panel').replaceChildren();
  controller = new AbortController();
  void watch(sessionId, controller.signal, renderEvent, () => {
    $('status').textContent = '连接恢复中';
  });
  await showWorkspace(sessionId, () => sessionId);
}

async function load() {
  const sessions = await api('sessions');
  $('sessions').replaceChildren();
  for (const session of sessions) $('sessions').add(new Option(session.id.slice(0, 12), session.id));
  if (sessions.some(session => session.id === localStorage.artoriasSession)) {
    $('sessions').value = localStorage.artoriasSession;
  }
  if (sessions.length) await select();
  await loadSpaces(sessionId);
}

$('new').onclick = handle(async () => {
  const session = await api('sessions', {});
  localStorage.artoriasSession = session.id;
  await load();
});
$('sessions').onchange = handle(async () => { await select(); await loadSpaces(sessionId); });
$('send').onclick = handle(async () => {
  const text = $('input').value;
  if (!sessionId || !text.trim()) return;
  const selected = sessionId;
  const run = await api(`sessions/${selected}/messages`, { text, request_key: crypto.randomUUID() });
  if (selected === sessionId) { runId = run.id; $('input').value = ''; }
});
$('cancel').onclick = handle(async () => { if (runId) await api(`runs/${runId}/cancel`, {}); });
setupWorkspace(() => sessionId, handle);

(async () => {
  await bootstrap();
  await load();
  if (!sessionId) await $('new').onclick();
})().catch(report);
