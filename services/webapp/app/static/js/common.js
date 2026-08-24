// common.js — shared helpers for every page (loaded before page scripts).
const $ = (id) => document.getElementById(id);
const TOKEN_KEY = 'altacloset_token';
const ADMIN_TOKEN_KEY = 'altacloset_admin_token';
const RECO_KEY = 'altacloset_last_reco';

// ---------- token helpers ----------
function getToken() { return localStorage.getItem(TOKEN_KEY); }
function setToken(t) { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); }
function getAdminToken() { return localStorage.getItem(ADMIN_TOKEN_KEY); }

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
  // cache:'default' (NOT no-store) — the backend serves images with
  // `Cache-Control: private, max-age=86400, immutable`, so repeat loads hit the
  // browser HTTP cache instead of re-downloading. Edits change the ?v= version
  // in the URL -> cache miss -> fresh fetch. Callers that create a blob with
  // this helper should revoke it via URL.revokeObjectURL when done (or use
  // setAuthImage, which revokes automatically).
  const res = await api(path, { cache: 'default' });
  return URL.createObjectURL(await res.blob());
}

// Load an authed image into an <img> element, cache-friendly and leak-free:
// fetches with the default cache policy (repeat loads are instant), then revokes
// the blob URL after the browser has decoded the image.
async function setAuthImage(img, path) {
  if (!img || !path) return;
  let url = null;
  try {
    const res = await api(path, { cache: 'default' });
    if (!res.ok) return;
    url = URL.createObjectURL(await res.blob());
  } catch (e) { return; }
  const done = () => { if (url) URL.revokeObjectURL(url); };
  img.onload = done; img.onerror = done;
  img.src = url;
}

// Versioned, size-aware URL for a garment object (from /api/wardrobe, which now
// carries id + image_version). size: 'thumb' (grids/pickers) | 'detail' |
// 'full'. ?v=<mtime> is the cache-buster: an edit bumps it -> new URL -> fresh.
function garmentImg(g, size) {
  return '/api/wardrobe/' + g.id + '/image?size=' + (size || 'thumb') + '&v=' + (g.image_version || 0);
}

// ---------- auth guard (runs on every authenticated page) ----------
async function requireAuth() {
  let me = null;
  try { me = await apiJson('/api/auth/me'); }
  catch (e) { /* api() already redirected on 401 */ }
  if (me && me.admin) {
    // dev `admin` tokens have no personal account — park them on /account
    // where the switch-user console lives
    if (location.pathname !== '/account') location.href = '/account';
    return;
  }
  if (me && me.dev) showDevBanner(me.dev);
}

// ---------- DEV banner (dev accounts only) ----------
//   acting_as  → the admin is acting AS a real user (dev instance; changes apply
//                to that user's data on this dev box, never production)
//   test_copy  → the `test` sandbox, whose data is a copy of a real user
function showDevBanner(dev) {
  const b = $('dev-banner');
  if (!b) return;
  const msg = $('dev-banner-msg');
  const btn = $('dev-banner-exit');
  if (dev.acting_as) {
    msg.textContent = 'Acting as ' + (dev.acting_as.email || 'a user') +
      ' (MASTER admin) — changes apply LIVE to their data on this dev instance, never production.';
    btn.hidden = false;
  } else if (dev.test_copy) {
    msg.textContent = 'Sandbox — data is a copy; changes never affect real accounts.';
    btn.hidden = true;  // test can't "exit to admin"
  } else {
    return;
  }
  b.hidden = false;
}
(function wireDevBannerExit() {
  const btn = $('dev-banner-exit');
  if (!btn) return;
  btn.addEventListener('click', () => {
    setToken(getAdminToken());   // drop the acting-as token, back to the admin token
    location.href = '/account';  // Account page, where the switch console lives
  });
})();

// ---------- full-page sheets: scroll-lock while open + swipe-down to close ----------
// Every overlay ("pick a top" / garment / outfit / lightbox) is a `.sheet`: it
// covers the whole page. While open, the body can't scroll (so swiping never
// drags the page underneath), and swiping DOWN dismisses it. Page scripts call
// openSheet/closeSheet so state cleanup stays where it already is.
let _sheetScrollY = null;
function lockPageScroll() {
  if (_sheetScrollY !== null) return;
  _sheetScrollY = window.scrollY;
  document.body.style.overflow = 'hidden';
}
function unlockPageScroll() {
  if (_sheetScrollY === null) return;
  document.body.style.overflow = '';
  _sheetScrollY = null;
}
function openSheet(el) { el.hidden = false; lockPageScroll(); }
function closeSheet(el) { el.hidden = true; unlockPageScroll(); }

// Swipe DOWN from the top of a sheet to dismiss it. Native scrolling inside the
// panel is left alone — once you've scrolled down, that gesture scrolls instead
// of closing (pull-from-top only).
document.addEventListener('touchstart', (e) => {
  const sheet = e.target.closest('.sheet');
  if (!sheet) return;
  const panel = sheet.querySelector('.panel');
  sheet._sy = e.touches[0].clientY;
  sheet._atTop = !panel || panel.scrollTop <= 0;
}, { passive: true });
document.addEventListener('touchmove', (e) => {
  const sheet = e.target.closest('.sheet');
  if (!sheet || sheet._sy === undefined) return;
  const panel = sheet.querySelector('.panel');
  if (panel && panel.scrollTop > 0) sheet._atTop = false;
  const dy = e.touches[0].clientY - sheet._sy;
  if (sheet._atTop && dy > 80) closeSheet(sheet);
}, { passive: true });
// Escape closes any open sheet (desktop convenience).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  for (const el of document.querySelectorAll('.sheet')) {
    if (!el.hidden) closeSheet(el);
  }
});

// ---------- lightbox ----------
function openLightbox(url) {
  $('lightbox-img').src = url;
  openSheet($('lightbox'));
}
function closeLightbox() {
  closeSheet($('lightbox'));
  $('lightbox-img').src = '';
}
$('lightbox').addEventListener('click', closeLightbox);
$('lightbox-close').addEventListener('click', closeLightbox);

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
