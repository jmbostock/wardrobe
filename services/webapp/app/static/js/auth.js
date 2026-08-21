// auth page — log in / sign up. On success, store the token and go to /suggest.
async function authAction(endpoint) {
  const body = { email: $('auth-email').value.trim(), password: $('auth-pass').value };
  const res = await fetch('/api/auth/' + endpoint, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail;
    $('auth-status').textContent = (typeof detail === 'string' ? detail : 'error') || 'error';
    return;
  }
  const data = await res.json();
  setToken(data.token);
  $('auth-status').textContent = 'signed in as ' + data.user.email;
  location.href = '/suggest';
}
$('auth-login').addEventListener('click', () => authAction('login'));
$('auth-register').addEventListener('click', () => authAction('register'));
$('auth-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') authAction('login'); });

// already signed in? skip the login screen
if (getToken()) location.href = '/suggest';
