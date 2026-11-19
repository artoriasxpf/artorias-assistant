import { api } from './api.js';

const $ = id => document.getElementById(id);

export async function showWorkspace(sessionId, current) {
  const workspace = await api(`sessions/${sessionId}/workspace`);
  if (sessionId === current()) {
    $('workspace').textContent = `工作空间：${workspace.name} [${workspace.kind}] ${workspace.root}`;
  }
}

export async function loadSpaces(sessionId) {
  const spaces = await api('workspaces');
  $('spaces').replaceChildren();
  for (const workspace of spaces) {
    $('spaces').add(new Option(`${workspace.name} [${workspace.kind}]`, workspace.id));
  }
  if (sessionId) $('spaces').value = (await api(`sessions/${sessionId}/workspace`)).id;
}

export function setupWorkspace(current, handle) {
  $('switchSpace').onclick = handle(async () => {
    const sessionId = current();
    if (!sessionId) return;
    await api(`sessions/${sessionId}/workspace`, { workspace_id: $('spaces').value });
    await showWorkspace(sessionId, current);
  });
  $('addSpace').onclick = handle(async () => {
    const kind = prompt('空间类型：device_fs（本机）或 cloud_storage（云端逻辑空间）', 'device_fs');
    if (!kind) return;
    const root = prompt(kind === 'device_fs' ? '本机绝对目录（添加即持续授权读写）' : '云端逻辑目录，例如 personal', '');
    if (!root) return;
    const name = prompt('空间名称', root);
    if (!name) return;
    await api('workspaces', { kind, root, name });
    await loadSpaces(current());
  });
  $('files').onclick = handle(async () => {
    const sessionId = current();
    if (!sessionId) return;
    const files = await api(`sessions/${sessionId}/files`);
    if (sessionId !== current()) return;
    $('panel').replaceChildren();
    for (const file of files) {
      const button = document.createElement('button');
      button.textContent = file.path;
      button.onclick = handle(async () => {
        const result = await api(`sessions/${sessionId}/file?path=${encodeURIComponent(file.path)}`);
        if (sessionId !== current()) return;
        const pre = document.createElement('pre');
        pre.textContent = result.content;
        $('panel').append(pre);
      });
      $('panel').append(button);
    }
  });
  $('changes').onclick = handle(async () => {
    const sessionId = current();
    if (!sessionId) return;
    const changes = await api(`sessions/${sessionId}/changes`);
    if (sessionId !== current()) return;
    $('panel').replaceChildren();
    for (const change of changes) {
      const paragraph = document.createElement('p');
      paragraph.textContent = `${change.path} ${change.id.slice(0, 8)} `;
      const button = document.createElement('button');
      button.textContent = '撤销';
      button.onclick = handle(async () => {
        await api(`sessions/${sessionId}/changes/${change.id}/revert`, {});
        button.disabled = true;
      });
      paragraph.append(button);
      $('panel').append(paragraph);
    }
  });
}
