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

// ---------- rating widget (0–10, single-line slider) ----------
// Shared by the wardrobe + outfits detail cards. `bindRating(containerId,
// value)` renders a range slider + clear button; `currentRating()` returns the
// slider value. A slider keeps the rating on one line (the old 10-dot widget
// wrapped on narrow screens).
let _ratingValue = 0;
function bindRating(containerId, value) {
  _ratingValue = value || 0;
  const c = $(containerId);
  if (!c) return;
  c.innerHTML = '';
  const row = document.createElement('div'); row.className = 'rate-row';
  const slider = document.createElement('input');
  slider.type = 'range'; slider.min = '0'; slider.max = '10'; slider.step = '1';
  slider.value = String(_ratingValue);
  slider.className = 'rate-slider';
  slider.title = _ratingValue ? _ratingValue + '/10' : '0/10 (unrated)';
  const refresh = () => {
    _ratingValue = Number(slider.value);
    const label = $(containerId + '-val');
    if (label) label.textContent = _ratingValue ? 'rated ' + _ratingValue + '/10' : 'not rated yet';
    slider.title = _ratingValue ? _ratingValue + '/10' : '0/10 (unrated)';
  };
  slider.addEventListener('input', refresh);
  const clr = document.createElement('button');
  clr.type = 'button'; clr.className = 'ghost'; clr.textContent = 'clear';
  clr.addEventListener('click', () => { slider.value = '0'; refresh(); });
  row.appendChild(slider); row.appendChild(clr);
  c.appendChild(row);
  refresh();
}
function currentRating() { return _ratingValue; }

// ---------- boot ----------
if (document.body.dataset.requireAuth === 'true') requireAuth();

// ---------- PWA: register the service worker (secure contexts only) ----------
if ('serviceWorker' in navigator &&
    (location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname))) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* insecure context or blocked */ });
  });
}
