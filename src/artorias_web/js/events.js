import { headers } from './api.js';

// Capture the session and cursor in this subscription; an old stream cannot
// advance a newly selected session's cursor.
export async function watch(sessionId, signal, onEvent, onReconnect, after = 0) {
  let cursor = after;
  while (!signal.aborted) {
    let reader;
    try {
      const response = await fetch(`/api/sessions/${sessionId}/events?after=${cursor}`, {
        headers: headers(), signal,
      });
      if (!response.ok) throw Error(response.status);
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!signal.aborted) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index;
        while ((index = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const line = block.split('\n').find(item => item.startsWith('data: '));
          if (!line || signal.aborted) continue;
          const event = JSON.parse(line.slice(6));
          if (event.seq > cursor) {
            onEvent(event);
            cursor = event.seq;
          }
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      onReconnect(error);
    } finally {
      if (reader) {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    }
    if (!signal.aborted) await new Promise(resolve => setTimeout(resolve, 1000));
  }
}
