let token;

export async function bootstrap() {
  const response = await fetch('/bootstrap');
  if (!response.ok) throw Error(await response.text());
  const settings = await response.json();
  token = settings.token;
  return settings;
}

export function headers() {
  return { 'x-artorias-token': token, 'Content-Type': 'application/json' };
}

export async function api(path, body, signal) {
  const response = await fetch('/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: headers(), signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const message = await response.text();
    let detail;
    try { detail = JSON.parse(message).detail; } catch { /* Non-JSON response */ }
    throw Error(typeof detail === 'string' ? detail : message);
  }
  return response.json();
}
