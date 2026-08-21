// account page — profile, location, password, person photos, sign out.
async function loadAccount() {
  try {
    const a = await apiJson('/api/account');
    $('acct-email').textContent = a.user.email;
    $('loc-input').value = a.location.label ? '' : `${a.location.lat}, ${a.location.lon}`;
  } catch (e) { /* ignore */ }
}
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
      const url = await authImageUrl(p.url);
      const card = document.createElement('div');
      card.className = 'photo';
      const img = document.createElement('img');
      img.src = url; img.alt = 'photo ' + p.id;
      img.addEventListener('click', () => openLightbox(url));
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

loadAccount(); loadPhotos();
