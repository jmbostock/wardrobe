// outfits page — saved outfits grid + the outfit edit modal (rename / rate / delete).
let editingItem = null;

async function loadSavedOutfits() {
  const box = $('saved-outfits');
  box.innerHTML = '<p class="muted">loading…</p>';
  let items;
  try { items = await apiJson('/api/outfits'); }
  catch (e) { box.innerHTML = '<p class="muted">failed to load</p>'; return; }
  if (!items.length) { box.innerHTML = '<p class="muted">no saved outfits yet — save one from the Try-on tab</p>'; return; }
  box.innerHTML = '';
  for (const o of items) {
    const card = document.createElement('div'); card.className = 'photo';
    const img = document.createElement('img'); img.alt = o.name;
    if (o.result_url) {
      const u = await authImageUrl(o.result_url);
      img.src = u;
      img.addEventListener('click', () => openLightbox(u));
    } else {
      img.style.background = 'linear-gradient(135deg,#2a3340,#1a1f27)';
      img.title = 'no render saved for this outfit yet';
    }
    const meta = document.createElement('div'); meta.className = 'meta';
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = 'outfit';
    const name = document.createElement('div'); name.textContent = o.name; name.style.fontSize = '13px';
    const desc = document.createElement('div'); desc.className = 'muted'; desc.style.fontSize = '12px';
    desc.textContent = (o.garments || []).map((g) => g.name).join(' + ') || 'n/a';
    meta.appendChild(badge); meta.appendChild(name); meta.appendChild(desc);
    if (o.rating) {
      const r = document.createElement('div'); r.className = 'muted'; r.style.fontSize = '12px';
      r.textContent = '★ ' + o.rating + '/10';
      meta.appendChild(r);
    }
    const editBtn = document.createElement('button'); editBtn.className = 'ghost'; editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', () => openEdit({ kind: 'outfit', ...o }));
    meta.appendChild(editBtn);
    card.appendChild(img); card.appendChild(meta);
    box.appendChild(card);
  }
}

// ---------- edit modal (outfit mode — shared partial, no garment fields) ----------
function openEdit(o) {
  editingItem = o;
  $('edit-title').textContent = 'Edit — ' + o.name;
  $('edit-name').value = o.name;
  $('edit-name').placeholder = 'outfit name';
  $('edit-status').textContent = '';
  bindRating('edit-rating', o.rating || 0);
  $('edit-modal').hidden = false;
}
function closeEdit() { $('edit-modal').hidden = true; editingItem = null; }
$('edit-close').addEventListener('click', closeEdit);
$('edit-modal').addEventListener('click', (e) => { if (e.target === $('edit-modal')) closeEdit(); });
$('edit-save').addEventListener('click', async () => {
  if (!editingItem) return;
  $('edit-status').textContent = 'saving…';
  const body = { name: $('edit-name').value.trim(), rating: currentRating() };
  try {
    await apiJson('/api/outfits/' + editingItem.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    $('edit-status').textContent = 'saved'; toast('outfit updated');
    loadSavedOutfits();
  } catch (e) { $('edit-status').textContent = e.message; }
});
$('edit-delete').addEventListener('click', async () => {
  if (!editingItem) return;
  if (!confirm('delete "' + editingItem.name + '"?')) return;
  try { await apiJson('/api/outfits/' + editingItem.id, { method: 'DELETE' }); toast('deleted'); }
  catch (e) { alert(e.message); }
  closeEdit(); loadSavedOutfits();
});

loadSavedOutfits();
