let token;

export async function bootstrap() {
  const response = await fetch('/bootstrap');
  if (!response.ok) throw Error(await response.text());
  token = (await response.json()).token;
}

export function headers() {
  return { 'x-artorias-token': token, 'Content-Type': 'application/json' };
}

export async function api(path, body) {
  const response = await fetch('/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw Error(await response.text());
  return response.json();
}
