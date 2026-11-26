import { api } from './api.js';

const $ = id => document.getElementById(id);

export async function showWorkspace(sessionId, current) {
  const workspace = await api(`sessions/${sessionId}/workspace`);
  if (sessionId !== current()) return;
  const button = $('workspace');
  const location = workspace.kind === 'device_fs' ? '本机' : '云端';
  button.textContent = `工作空间 · ${location} · ${workspace.root}`;
  button.title = button.textContent;
  button.disabled = false;
}

export function setupWorkspace(current, handle, nativePicker) {
  const dialog = $('workspaceDialog');
  const kind = $('spaceKind');
  const local = $('localRoot');
  const manual = $('manualRoot');
  const cloud = $('cloudRoot');
  const localInput = nativePicker ? local : manual;
  let spaces, sessionId;
  let busy = false;

  function updateDialog() {
    const isLocal = kind.value === 'device_fs';
    $('localDirectory').hidden = !isLocal || !nativePicker;
    manual.hidden = !isLocal || nativePicker;
    cloud.hidden = isLocal;
    $('spaceRootLabel').htmlFor = isLocal ? localInput.id : 'cloudRoot';
    $('workspaceHint').textContent = isLocal
      ? (nativePicker ? '选择或新建本机文件夹。' : '输入本机文件夹的绝对路径。')
      : '输入云端逻辑目录，例如 personal/reports。';
    $('saveWorkspace').disabled = busy || !(isLocal ? localInput.value.trim() : cloud.value.trim());
  }

  function showError(message) {
    $('workspaceError').textContent = message;
    $('workspaceError').hidden = !message;
  }

  async function withDialogAction(action) {
    if (busy) return;
    busy = true;
    const controls = [kind, localInput, cloud, $('pickFolder'), $('closeWorkspace')];
    for (const control of controls) control.disabled = true;
    updateDialog();
    showError('');
    try {
      await action();
    } catch (error) {
      showError(error.message);
    } finally {
      busy = false;
      for (const control of controls) control.disabled = false;
      updateDialog();
    }
  }

  $('workspace').onclick = handle(async () => {
    const id = current();
    const [available, selected] = await Promise.all([api('workspaces'), api(`sessions/${id}/workspace`)]);
    if (id !== current() || dialog.open) return;
    sessionId = id;
    spaces = available;
    local.replaceChildren(new Option('请选择文件夹', ''));
    $('localRoots').replaceChildren();
    $('cloudRoots').replaceChildren();
    for (const workspace of spaces) {
      if (workspace.kind === 'device_fs') {
        local.add(new Option(workspace.root, workspace.root));
        $('localRoots').append(new Option(workspace.root, workspace.root));
      }
      else $('cloudRoots').append(new Option(workspace.root, workspace.root));
    }
    kind.value = selected.kind;
    localInput.value = selected.kind === 'device_fs' ? selected.root : '';
    cloud.value = selected.kind === 'cloud_storage' ? selected.root : '';
    showError('');
    updateDialog();
    dialog.showModal();
  });

  kind.onchange = local.onchange = manual.oninput = cloud.oninput = updateDialog;
  $('closeWorkspace').onclick = () => dialog.close();
  dialog.oncancel = event => { if (busy) event.preventDefault(); };
  $('pickFolder').onclick = () => withDialogAction(async () => {
    const { root } = await api('workspaces/pick-folder', {});
    if (!root) return;
    if (![...local.options].some(option => option.value === root)) local.add(new Option(root, root));
    local.value = root;
  });
  $('workspaceForm').onsubmit = event => {
    event.preventDefault();
    void withDialogAction(async () => {
      const isLocal = kind.value === 'device_fs';
      const root = isLocal ? (nativePicker ? local.value : manual.value.trim()) : cloud.value.trim().replace(/^\/+|\/+$/g, '');
      if (isLocal && !root.startsWith('/')) throw Error('请选择或输入本机绝对路径');
      if (!root) throw Error('请选择或输入文件夹');
      const existing = spaces.find(space => space.kind === kind.value && space.root === root);
      const workspace = existing ?? await api('workspaces', {
        kind: kind.value, root, persistent: false, name: root.split('/').filter(Boolean).pop() || root,
      });
      await api(`sessions/${sessionId}/permissions/workspace`, {
        workspace_id: workspace.id, access: $('workspaceAccess').value,
      });
      await api(`sessions/${sessionId}/workspace`, { workspace_id: workspace.id });
      await showWorkspace(sessionId, current);
      dialog.close();
    });
  };

  function openPanel(title, empty) {
    $('projectTitle').textContent = title;
    $('panel').replaceChildren();
    if (empty) $('panel').textContent = empty;
    if (!$('projectDialog').open) $('projectDialog').showModal();
  }

  $('files').onclick = handle(async () => {
    const id = current();
    if (!id) return;
    const files = await api(`sessions/${id}/files`);
    if (id !== current()) return;
    openPanel('项目文件', files.length ? '' : '工作空间内暂无文件。');
    const pre = document.createElement('pre');
    for (const file of files) {
      const button = document.createElement('button');
      button.textContent = file.path;
      button.onclick = handle(async () => {
        const result = await api(`sessions/${id}/file?path=${encodeURIComponent(file.path)}`);
        if (id === current()) pre.textContent = result.content;
      });
      $('panel').append(button);
    }
    $('panel').append(pre);
  });
  $('permissions').onclick = handle(async () => {
    const id = current();
    if (!id) return;
    const grants = await api(`sessions/${id}/permissions`);
    if (id !== current()) return;
    openPanel('会话权限', grants.length ? '' : '此会话暂无单独授权。');
    for (const grant of grants) {
      const row = document.createElement('p');
      const command = grant.rule_json ? JSON.parse(grant.rule_json).request : null;
      const target = command ? [command.path, ...command.args].join(' ') : grant.root;
      row.textContent = `${grant.action} · ${target} `;
      const button = document.createElement('button');
      button.textContent = '撤销';
      button.onclick = handle(async () => {
        await api(`sessions/${id}/permissions/${grant.id}/revoke`, {});
        row.remove();
      });
      row.append(button);
      $('panel').append(row);
    }
  });

  $('changes').onclick = handle(async () => {
    const id = current();
    if (!id) return;
    const changes = await api(`sessions/${id}/changes`);
    if (id !== current()) return;
    openPanel('变更记录', changes.length ? '' : '暂无变更。');
    for (const change of changes) {
      const paragraph = document.createElement('p');
      paragraph.textContent = `${change.path} ${change.id.slice(0, 8)} `;
      const button = document.createElement('button');
      button.textContent = '撤销';
      button.onclick = handle(async () => {
        await api(`sessions/${id}/changes/${change.id}/revert`, {});
        button.disabled = true;
      });
      paragraph.append(button);
      $('panel').append(paragraph);
    }
  });
}
