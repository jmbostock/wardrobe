// account page — profile, location, password, person photos, sign out,
// plus the DEV console for the `admin` account (act as any user) and the
// `test` sandbox (a copy of a user's data that never touches real accounts).
const ADMIN_TOKEN_KEY = 'altacloset_admin_token';
function getAdminToken() { return localStorage.getItem(ADMIN_TOKEN_KEY); }
function setAdminToken(t) { t ? localStorage.setItem(ADMIN_TOKEN_KEY, t) : localStorage.removeItem(ADMIN_TOKEN_KEY); }

async function adminApi(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const t = getAdminToken();
  if (t) headers.set('Authorization', 'Bearer ' + t);
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(typeof j.detail === 'string' ? j.detail : 'http ' + res.status);
  }
  return res.json();
}

// ---------- DEV console (admin account only) ----------
async function loadDevUsers() {
  let data;
  try { data = await adminApi('/api/admin/users'); }
  catch (e) { $('dev-status').textContent = e.message; return; }
  const sel = $('dev-user'); sel.innerHTML = '';
  for (const u of data.users || []) {
    const o = document.createElement('option');
    o.value = u.id;
    o.textContent = `${u.email}  ·  ${u.garment_count ?? 0} garments, ${u.outfit_count ?? 0} outfits`;
    sel.appendChild(o);
  }
  loadTestInfo();
}

async function loadTestInfo() {
  let info;
  try { info = await adminApi('/api/admin/test'); }
  catch (e) { $('dev-test-info').textContent = ''; return; }
  const c = info.counts || {};
  $('dev-test-info').textContent =
    `test sandbox (${info.email}) — ${c.garments ?? 0} garments, ${c.outfits ?? 0} outfits copied.`;
}

async function devAct() {
  const userId = Number($('dev-user').value);
  if (!userId) return;
  setAdminToken(getToken());   // stash the admin token so "Exit to admin" can return
  try {
    const data = await adminApi('/api/admin/impersonate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId }),
    });
    setToken(data.token);      // active token now acts AS that user
    location.href = '/suggest';
  } catch (e) { setAdminToken(null); $('dev-status').textContent = e.message; }
}

async function refreshTestCopy() {
  const userId = Number($('dev-user').value);
  if (!userId) return;
  $('dev-status').textContent = 'copying…';
  try {
    const data = await adminApi('/api/admin/test-copy', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId }),
    });
    $('dev-status').textContent = 'test copy refreshed from ' + data.copied_from;
    toast('test sandbox updated — copy of ' + data.copied_from);
    loadTestInfo();
  } catch (e) { $('dev-status').textContent = e.message; }
}

function devExit() {
  const adminToken = getAdminToken();
  setToken(adminToken || null);   // back to the admin token (or signed out)
  location.href = '/account';
}

async function devAdminExit() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  setToken(null); setAdminToken(null);
  location.href = '/login';
}

async function loadAccount() {
  try {
    const a = await apiJson('/api/account');
    $('acct-email').textContent = a.user.email;
    $('loc-input').value = a.location.label ? '' : `${a.location.lat}, ${a.location.lon}`;
    loadProfile(a.profile);
  } catch (e) { /* ignore */ }
}

// ---------- optional style profile (bio) ----------
// element id -> profile key (see app/profile.py BIO_FIELDS)
const PF_MAP = {
  bio: 'bio', sex: 'sex', height: 'height', top: 'top_size', bottom: 'bottom_size',
  shoe: 'shoe_size', warmth: 'warmth_bias', fmin: 'formality_min', fmax: 'formality_max',
  never: 'never_wear', style: 'style_keywords', occ: 'occasions',
  favcol: 'fav_colors', avoidcol: 'colors_avoid', age: 'age_range',
};
function loadProfile(p) {
  p = p || {};
  for (const [id, key] of Object.entries(PF_MAP)) {
    const el = $(id); if (!el) continue;
    el.value = p[key] ?? '';
  }
}
$('pf-save').addEventListener('click', async () => {
  const prof = {};
  for (const [id, key] of Object.entries(PF_MAP)) {
    const el = $(id); if (!el) continue;
    prof[key] = el.value.trim();
  }
  const status = $('pf-status'); status.textContent = 'saving…';
  try {
    await apiJson('/api/account/profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: prof }),
    });
    status.textContent = 'saved'; toast('style profile saved');
  } catch (e) { status.textContent = e.message; }
});
async function saveLocationFrom(inputId, statusId) {
  const status = $(statusId); status.textContent = 'resolving…';
  try {
    const r = await apiJson('/api/account/location', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location: $(inputId).value }),
    });
    status.textContent = `saved: ${r.location.name}${r.location.country ? ', ' + r.location.country : ''}`;
    loadAccount();
  } catch (e) { status.textContent = e.message; }
}
$('loc-save').addEventListener('click', () => saveLocationFrom('loc-input', 'loc-status'));
$('pw-save').addEventListener('click', async () => {
  try {
    await apiJson('/api/account/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: $('pw-current').value, new_password: $('pw-new').value }),
    });
    $('pw-current').value = ''; $('pw-new').value = '';
    $('pw-status').textContent = 'password updated'; toast('password updated');
  } catch (e) { $('pw-status').textContent = e.message; }
});
$('signout-btn').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  setToken(null);
  location.href = '/login';
});

// ---------- person photos ----------
async function loadPhotos() {
  try {
    const items = await apiJson('/api/photos');
    const grid = $('photos');
    grid.innerHTML = '';
    if (!items.length) { grid.innerHTML = '<p class="muted">no photos yet — upload one</p>'; return; }
    for (const p of items) {
      const card = document.createElement('div');
      card.className = 'photo';
      const img = document.createElement('img');
      img.alt = 'photo ' + p.id;
      setAuthImage(img, p.url + '?size=thumb');
      img.addEventListener('click', () => {
        // lightbox shows the full-res original, not the grid thumb
        openLightbox('');
        setAuthImage($('lightbox-img'), p.url + '?size=full');
      });
      const meta = document.createElement('div'); meta.className = 'meta';
      if (p.is_default) { const b = document.createElement('span'); b.className = 'badge'; b.textContent = 'default'; meta.appendChild(b); }
      const desc = document.createElement('input');
      desc.type = 'text'; desc.className = 'pdesc'; desc.value = p.description || '';
      desc.placeholder = 'description (shows in try-on)';
      const row = document.createElement('div'); row.className = 'row';
      const saveD = document.createElement('button'); saveD.className = 'ghost'; saveD.textContent = 'Save desc'; saveD.dataset.id = p.id;
      const del = document.createElement('button'); del.className = 'danger'; del.textContent = 'Delete'; del.dataset.del = p.id;
      row.appendChild(saveD);
      if (!p.is_default) {
        const def = document.createElement('button'); def.className = 'ghost'; def.textContent = 'Default'; def.dataset.setdef = p.id;
        row.appendChild(def);
      }
      row.appendChild(del);
      meta.appendChild(desc); meta.appendChild(row);
      card.appendChild(img); card.appendChild(meta);
      grid.appendChild(card);
      attachPhotoQa(card, p.id);
    }
    grid.querySelectorAll('[data-setdef]').forEach((b) => b.addEventListener('click', async () => {
      await apiJson(`/api/photos/${b.dataset.setdef}/default`, { method: 'POST' });
      loadPhotos(); toast('default photo set');
    }));
    grid.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('delete this photo?')) return;
      await apiJson(`/api/photos/${b.dataset.del}`, { method: 'DELETE' });
      loadPhotos(); toast('photo deleted');
    }));
    grid.querySelectorAll('[data-id]').forEach((b) => b.addEventListener('click', async () => {
      const card = b.closest('.photo');
      const input = card.querySelector('.pdesc');
      await apiJson(`/api/photos/${b.dataset.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: input.value }),
      });
      toast('description saved');
    }));
  } catch (e) { /* ignore */ }
}
$('photo-upload-btn').addEventListener('click', () => $('photo-upload').click());
$('photo-upload').addEventListener('change', async () => {
  const f = $('photo-upload').files[0]; if (!f) return;
  const fd = new FormData(); fd.append('person', f);
  try { await apiJson('/api/photos', { method: 'POST', body: fd }); toast('photo uploaded'); }
  catch (e) { alert(e.message); }
  $('photo-upload').value = '';
  loadPhotos();
});

function photoQaColor(score) {
  return score >= 70 ? '#2ea043' : score >= 55 ? '#d4a72c' : score >= 40 ? '#d4762c' : '#d9534f';
}
// suitability chip for a saved base image, so you can spot bad ones before
// wasting a try-on render on them
async function attachPhotoQa(card, photoId) {
  try {
    const fd = new FormData(); fd.append('kind', 'person'); fd.append('photo_id', photoId);
    const qa = await apiJson('/api/image-quality', { method: 'POST', body: fd });
    const chip = document.createElement('span');
    chip.className = 'qachip';
    chip.style.color = '#0f1216';
    chip.style.background = photoQaColor(qa.score);
    chip.textContent = `base ${qa.grade} ${qa.score}/100`;
    chip.title = qa.issues.length ? qa.issues.join(' · ') : 'looks good — great try-on base';
    const meta = card.querySelector('.meta');
    if (meta) meta.prepend(chip);
    if (qa.score < 55) {
      card.classList.add('bad');
      const note = document.createElement('div');
      note.className = 'badnote';
      note.textContent = '⚠ low-quality base — try-on will be soft';
      note.title = qa.issues.join(' · ');
      if (meta) meta.appendChild(note);
    }
  } catch (e) { /* ignore */ }
}

// ---------- boot: admin / acting-as / test sandbox / normal user ----------
async function boot() {
  let me;
  try { me = await apiJson('/api/auth/me'); } catch (e) { return; }
  const isAdmin = !!(me && me.admin);
  const acting = !!(me && me.dev && me.dev.acting_as);
  const isTest = !!(me && me.dev && me.dev.test_copy);

  // dev card state
  $('dev-card').hidden = !(isAdmin || acting || isTest);
  $('dev-admin-console').hidden = !isAdmin;
  $('dev-acting').hidden = !acting;
  $('dev-test-note').hidden = !isTest;
  if (acting && me.dev) $('dev-acting-email').textContent = me.dev.acting_as.email || 'a user';
  if (isAdmin) loadDevUsers();

  // normal account content only makes sense for a real user (or acting-as /
  // test — both resolve to a user row and can see their own account page)
  if (isAdmin) {
    for (const card of document.querySelectorAll('#view-account > .card:not(#dev-card)')) card.hidden = true;
  } else {
    loadAccount();
    loadPhotos();
  }
}

$('dev-act').addEventListener('click', devAct);
$('dev-test-copy').addEventListener('click', refreshTestCopy);
$('dev-admin-exit').addEventListener('click', devAdminExit);
$('dev-exit').addEventListener('click', devExit);

boot();
