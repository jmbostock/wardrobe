// wardrobe page — add-garment form, grid, and the garment edit modal (with rating).
let editingItem = null;
let wardrobeFilter = 'all';

// ---------- grid ----------
async function loadWardrobe() {
  const grid = $('wardrobe');
  grid.innerHTML = '<p class="muted">loading…</p>';
  let items;
  try { items = await apiJson('/api/wardrobe'); }
  catch (e) { grid.innerHTML = '<p class="muted">failed to load wardrobe</p>'; return; }
  if (wardrobeFilter === 'owned') items = items.filter((g) => g.owned);
  else if (wardrobeFilter === 'want') items = items.filter((g) => !g.owned);
  if (!items.length) {
    grid.innerHTML = '<p class="muted">no garments' + (wardrobeFilter === 'all' ? '' : ' in this filter') + ' — add one above</p>';
    return;
  }
  grid.innerHTML = '';
  for (const g of items) {
    const card = document.createElement('div'); card.className = 'photo';
    const img = document.createElement('img'); img.alt = g.name;
    if (g.has_image) {
      const url = await authImageUrl('/api/wardrobe/' + g.id + '/image');
      img.src = url;
      img.addEventListener('click', () => openLightbox(url));
    } else {
      img.style.background = g.color_hex || '#333';
      img.title = 'no image yet — add one in Edit';
    }
    const meta = document.createElement('div'); meta.className = 'meta';
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = g.category;
    const name = document.createElement('div'); name.textContent = g.name; name.style.fontSize = '13px';
    meta.appendChild(badge); meta.appendChild(name);
    if (!g.owned) {
      const want = document.createElement('span'); want.className = 'badge'; want.textContent = 'to buy';
      want.style.background = '#d4a72c';
      meta.appendChild(want);
    }
    if (g.rating) {
      const r = document.createElement('div'); r.className = 'muted'; r.style.fontSize = '12px';
      r.textContent = '★ ' + g.rating + '/10';
      meta.appendChild(r);
    }
    const editBtn = document.createElement('button'); editBtn.className = 'ghost'; editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', () => openEdit({ kind: 'garment', ...g }));
    meta.appendChild(editBtn);
    card.appendChild(img); card.appendChild(meta);
    grid.appendChild(card);
  }
}
$('wardrobe-filter').addEventListener('change', (e) => { wardrobeFilter = e.target.value; loadWardrobe(); });

// ---------- edit modal (garment mode — shared partial) ----------
function openEdit(g) {
  editingItem = g;
  $('edit-title').textContent = 'Edit — ' + g.name;
  $('edit-name').value = g.name;
  $('edit-name').placeholder = 'garment name';
  $('edit-color').value = (g.color_tags || []).join(', ');
  const cat = $('edit-category'); cat.innerHTML = '';
  Array.from($('g-category').options).forEach((o) => cat.add(new Option(o.text, o.value)));
  cat.value = g.category;
  $('edit-owned').checked = g.owned !== false && g.owned !== 0;
  $('edit-url').value = '';
  $('edit-status').textContent = '';
  const img = $('edit-img');
  img.style.background = g.color_hex || '#333'; img.src = '';
  if (g.has_image) {
    authImageUrl('/api/wardrobe/' + g.id + '/image').then((u) => { img.src = u; }).catch(() => {});
  }
  bindRating('edit-rating', g.rating || 0);
  $('edit-modal').hidden = false;
}
function closeEdit() { $('edit-modal').hidden = true; editingItem = null; }
function refreshEditImage() {
  if (editingItem && editingItem.kind === 'garment' && editingItem.has_image) {
    authImageUrl('/api/wardrobe/' + editingItem.id + '/image').then((u) => { $('edit-img').src = u; }).catch(() => {});
  }
}
$('edit-close').addEventListener('click', closeEdit);
$('edit-modal').addEventListener('click', (e) => { if (e.target === $('edit-modal')) closeEdit(); });
$('edit-upload').addEventListener('click', () => $('edit-file').click());
$('edit-file').addEventListener('change', async () => {
  const f = $('edit-file').files[0]; if (!f || !editingItem) return;
  const fd = new FormData(); fd.append('image', f);
  $('edit-status').textContent = 'uploading…';
  try {
    await apiJson('/api/wardrobe/' + editingItem.id + '/image', { method: 'POST', body: fd });
    $('edit-status').textContent = 'photo saved'; $('edit-file').value = '';
    refreshEditImage(); loadWardrobe();
  } catch (e) { alert(e.message); }
});
$('edit-url-btn').addEventListener('click', async () => {
  const u = $('edit-url').value.trim(); if (!u || !editingItem) return;
  $('edit-status').textContent = 'fetching…';
  try {
    await apiJson('/api/wardrobe/' + editingItem.id + '/image-url', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: u }),
    });
    $('edit-status').textContent = 'photo saved'; $('edit-url').value = '';
    refreshEditImage(); loadWardrobe();
  } catch (e) { alert(e.message); }
});
$('edit-save').addEventListener('click', async () => {
  if (!editingItem) return;
  $('edit-status').textContent = 'saving…';
  const body = {
    name: $('edit-name').value.trim(),
    category: $('edit-category').value,
    color: $('edit-color').value.trim(),
    rating: currentRating(),
    owned: $('edit-owned').checked,
  };
  try {
    await apiJson('/api/wardrobe/' + editingItem.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    $('edit-status').textContent = 'saved'; toast('garment updated');
    loadWardrobe();
  } catch (e) { $('edit-status').textContent = e.message; }
});
$('edit-delete').addEventListener('click', async () => {
  if (!editingItem) return;
  if (!confirm('delete "' + editingItem.name + '"?')) return;
  try { await apiJson('/api/wardrobe/' + editingItem.id, { method: 'DELETE' }); toast('deleted'); }
  catch (e) { alert(e.message); }
  closeEdit(); loadWardrobe();
});

// ---------- add garment form ----------
let pickedImage = null;
let previewImages = [];

$('g-fetch').addEventListener('click', async () => {
  const status = $('g-status');
  const url = $('g-url').value.trim();
  if (!url) { status.textContent = 'paste a product page or image URL first'; return; }
  status.textContent = 'fetching product details…';
  pickedImage = null; previewImages = [];
  try {
    const info = await apiJson('/api/wardrobe/parse-link', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
    });
    if (info.name && !$('g-name').value.trim()) $('g-name').value = info.name;
    if (info.color && !$('g-color').value.trim()) $('g-color').value = info.color;
    if (info.category) $('g-category').value = info.category;
    previewImages = info.images || [];
    renderPreview();
    status.textContent = previewImages.length
      ? 'found ' + previewImages.length + ' image(s) — pick one, then Add garment'
      : 'no images found';
  } catch (e) {
    status.textContent = e.message;
    $('g-preview').hidden = true; $('g-preview').innerHTML = '';
  }
});
function renderPreview() {
  const box = $('g-preview');
  box.innerHTML = ''; box.hidden = false;
  const label = document.createElement('div'); label.className = 'muted'; label.style.marginBottom = '8px';
  label.textContent = 'Choose the image to use (one only):';
  box.appendChild(label);
  const grid = document.createElement('div'); grid.className = 'photos';
  previewImages.forEach((u) => {
    const card = document.createElement('div'); card.className = 'photo';
    const img = document.createElement('img');
    img.style.objectFit = 'contain';
    img.src = u; img.alt = 'image option'; img.loading = 'lazy';
    card.appendChild(img);
    card.dataset.url = u;
    card.addEventListener('click', () => selectPreview(card));
    grid.appendChild(card);
  });
  box.appendChild(grid);
  const first = grid.querySelector('.photo');
  if (first) selectPreview(first);
}
function selectPreview(card) {
  pickedImage = card.dataset.url;
  document.querySelectorAll('#g-preview .photo').forEach((c) => (c.style.outline = ''));
  card.style.outline = '3px solid var(--acc)';
}
$('g-add').addEventListener('click', async () => {
  const status = $('g-status');
  const name = $('g-name').value.trim().slice(0, 200);
  const category = $('g-category').value;
  const color = $('g-color').value.trim();
  if (!name) { status.textContent = 'name required'; return; }
  const url = pickedImage || $('g-url').value.trim();
  const body = { name, category, color, owned: $('g-owned').checked };
  if (url) body.image_url = url;
  status.textContent = 'adding…';
  try {
    const g = await apiJson('/api/wardrobe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const f = $('g-file').files[0];
    if (f && !url) {
      const fd = new FormData(); fd.append('image', f);
      await apiJson('/api/wardrobe/' + g.id + '/image', { method: 'POST', body: fd });
    }
    status.textContent = 'added "' + g.name + '"';
    $('g-name').value = ''; $('g-color').value = ''; $('g-url').value = ''; $('g-file').value = '';
    pickedImage = null; previewImages = [];
    $('g-preview').hidden = true; $('g-preview').innerHTML = '';
    loadWardrobe();
  } catch (e) { status.textContent = e.message; }
});
$('g-clear').addEventListener('click', () => {
  $('g-name').value = ''; $('g-color').value = ''; $('g-url').value = ''; $('g-file').value = '';
  $('g-category').selectedIndex = 0;
  pickedImage = null; previewImages = [];
  $('g-preview').hidden = true; $('g-preview').innerHTML = '';
  $('g-status').textContent = 'form cleared — paste a new link or add manually';
});

loadWardrobe();
