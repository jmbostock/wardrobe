// common.js — shared helpers for every page (loaded before page scripts).
const $ = (id) => document.getElementById(id);
const TOKEN_KEY = 'altacloset_token';
const RECO_KEY = 'altacloset_last_reco';

// ---------- token helpers ----------
function getToken() { return localStorage.getItem(TOKEN_KEY); }
function setToken(t) { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); }

// ---------- toast ----------
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => { t.style.display = 'none'; }, 2500);
}

// ---------- api ----------
async function api(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const token = getToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    setToken(null);
    if (location.pathname !== '/login') location.href = '/login';
    throw new Error('session expired');
  }
  return res;
}
async function errMsg(res) {
  let msg = 'http ' + res.status;
  try {
    const j = await res.json();
    const d = j.detail;
    msg = (typeof d === 'string') ? d : JSON.stringify(d ?? j ?? msg);
  } catch (e) {
    msg = (await res.text().catch(() => '')) || msg;
  }
  return msg;
}
async function apiJson(path, opts = {}) {
  const res = await api(path, opts);
  if (!res.ok) throw new Error(await errMsg(res));
  return res.json();
}
async function authImageUrl(path) {
  const res = await api(path);
  return URL.createObjectURL(await res.blob());
}

// ---------- auth guard (runs on every authenticated page) ----------
async function requireAuth() {
  try { await apiJson('/api/auth/me'); }
  catch (e) { /* api() already redirected on 401 */ }
}

// ---------- lightbox ----------
function openLightbox(url) {
  $('lightbox-img').src = url;
  $('lightbox').hidden = false;
}
$('lightbox').addEventListener('click', () => {
  $('lightbox').hidden = true;
  $('lightbox-img').src = '';
});

// ---------- rating widget (0–10, tap-friendly dots) ----------
// Shared by the wardrobe + outfits edit modals. `bindRating(containerId, value)`
// re-renders the dots; `currentRating()` returns the tapped value.
let _ratingValue = 0;
function bindRating(containerId, value) {
  _ratingValue = value || 0;
  const c = $(containerId);
  if (!c) return;
  c.innerHTML = '';
  for (let i = 1; i <= 10; i++) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'rate-dot' + (i <= _ratingValue ? ' on' : '');
    b.textContent = i;
    b.title = i + '/10';
    b.addEventListener('click', () => bindRating(containerId, i));
    c.appendChild(b);
  }
  const clr = document.createElement('button');
  clr.type = 'button';
  clr.className = 'ghost rate-clear';
  clr.textContent = 'clear';
  clr.addEventListener('click', () => bindRating(containerId, 0));
  c.appendChild(clr);
  const label = $(containerId + '-val');
  if (label) label.textContent = _ratingValue ? `rated ${_ratingValue}/10` : 'not rated yet';
}
function currentRating() { return _ratingValue; }

// ---------- boot ----------
if (document.body.dataset.requireAuth === 'true') requireAuth();
