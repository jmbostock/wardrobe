// wardrobe page — add-garment form (URL fetch + AI tag-read), grid, and the
// garment detail card (outfits-style click-through with inline edit + rating).
let editingItem = null;   // garment being edited in the detail card
let wardrobeFilter = 'all';
let wardrobeSort = 'newest';

// ---------- grid ----------
async function loadWardrobe() {
  const grid = $('wardrobe');
  grid.innerHTML = '<p class="muted">loading…</p>';
  let items;
  try { items = await apiJson('/api/wardrobe'); }
  catch (e) { grid.innerHTML = '<p class="muted">failed to load wardrobe</p>'; return; }
  if (wardrobeFilter === 'owned') items = items.filter((g) => g.owned);
  else if (wardrobeFilter === 'want') items = items.filter((g) => !g.owned);
  // sort: newest (date added) / top rated / most used (in saved outfits)
  items = [...items].sort((a, b) => {
    if (wardrobeSort === 'rating') return (b.rating || 0) - (a.rating || 0) || b.id - a.id;
    if (wardrobeSort === 'used') return (b.used_count || 0) - (a.used_count || 0) || b.id - a.id;
    return (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id;
  });
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
    } else {
      img.style.background = g.color_hex || '#333';
      img.title = 'no image yet — add one in the detail card';
    }
    const meta = document.createElement('div'); meta.className = 'meta';
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = g.category;
    const name = document.createElement('div'); name.textContent = g.name; name.style.fontSize = '13px';
    meta.appendChild(badge); meta.appendChild(name);
    const facts = [];
    if (g.brand) facts.push(g.brand);
    if (g.sizes) facts.push('sizes ' + g.sizes);
    if (facts.length) {
      const d = document.createElement('div'); d.className = 'muted'; d.style.fontSize = '12px';
      d.textContent = facts.join(' · ');
      meta.appendChild(d);
    }
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
    if (g.used_count) {
      const u = document.createElement('div'); u.className = 'muted'; u.style.fontSize = '12px';
      u.textContent = 'used ' + g.used_count + '×';
      meta.appendChild(u);
    }
    // outfits-style: clicking the card opens the detail card (no Edit button)
    card.addEventListener('click', () => openGarmentDetail(g));
    card.style.cursor = 'pointer';
    card.appendChild(img); card.appendChild(meta);
    grid.appendChild(card);
  }
}
$('wardrobe-filter').addEventListener('change', (e) => { wardrobeFilter = e.target.value; loadWardrobe(); });
$('wardrobe-sort').addEventListener('change', (e) => { wardrobeSort = e.target.value; loadWardrobe(); });

// ---------- garment detail card (outfits-style click-through) ----------
function openGarmentDetail(g) {
  editingItem = g;
  $('gd-title').textContent = g.name;
  $('gd-name').value = g.name;
  $('gd-brand').value = g.brand || '';
  $('gd-sizes').value = g.sizes || '';
  $('gd-color').value = (g.color_tags || []).join(', ');
  const cat = $('gd-category'); cat.innerHTML = '';
  Array.from($('g-category').options).forEach((o) => cat.add(new Option(o.text, o.value)));
  cat.value = g.category;
  $('gd-owned').checked = g.owned !== false && g.owned !== 0;
  $('gd-url').value = '';
  $('gd-status').textContent = '';
  const img = $('gd-img');
  img.style.background = g.color_hex || '#333'; img.src = '';
  if (g.has_image) {
    authImageUrl('/api/wardrobe/' + g.id + '/image').then((u) => { img.src = u; }).catch(() => {});
  }
  bindRating('gd-rating', g.rating || 0);
  $('garment-detail').hidden = false;
}
function closeGarmentDetail() { $('garment-detail').hidden = true; editingItem = null; }
function refreshDetailImage() {
  if (editingItem && editingItem.has_image) {
    authImageUrl('/api/wardrobe/' + editingItem.id + '/image').then((u) => { $('gd-img').src = u; }).catch(() => {});
  }
}
$('gd-close').addEventListener('click', closeGarmentDetail);
$('garment-detail').addEventListener('click', (e) => { if (e.target === $('garment-detail')) closeGarmentDetail(); });
$('gd-upload').addEventListener('click', () => $('gd-file').click());
$('gd-file').addEventListener('change', async () => {
  const f = $('gd-file').files[0]; if (!f || !editingItem) return;
  const fd = new FormData(); fd.append('image', f);
  $('gd-status').textContent = 'uploading…';
  try {
    await apiJson('/api/wardrobe/' + editingItem.id + '/image', { method: 'POST', body: fd });
    $('gd-status').textContent = 'photo saved'; $('gd-file').value = '';
    refreshDetailImage(); loadWardrobe();
  } catch (e) { alert(e.message); }
});
$('gd-url-btn').addEventListener('click', async () => {
  const u = $('gd-url').value.trim(); if (!u || !editingItem) return;
  $('gd-status').textContent = 'fetching…';
  try {
    await apiJson('/api/wardrobe/' + editingItem.id + '/image-url', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: u }),
    });
    $('gd-status').textContent = 'photo saved'; $('gd-url').value = '';
    refreshDetailImage(); loadWardrobe();
  } catch (e) { alert(e.message); }
});
$('gd-save').addEventListener('click', async () => {
  if (!editingItem) return;
  $('gd-status').textContent = 'saving…';
  const body = {
    name: $('gd-name').value.trim(),
    brand: $('gd-brand').value.trim(),
    sizes: $('gd-sizes').value.trim(),
    category: $('gd-category').value,
    color: $('gd-color').value.trim(),
    rating: currentRating(),
    owned: $('gd-owned').checked,
  };
  try {
    await apiJson('/api/wardrobe/' + editingItem.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    $('gd-status').textContent = 'saved'; toast('garment updated');
    loadWardrobe();
  } catch (e) { $('gd-status').textContent = e.message; }
});
$('gd-delete').addEventListener('click', async () => {
  if (!editingItem) return;
  if (!confirm('delete "' + editingItem.name + '"?')) return;
  try { await apiJson('/api/wardrobe/' + editingItem.id, { method: 'DELETE' }); toast('deleted'); }
  catch (e) { alert(e.message); }
  closeGarmentDetail(); loadWardrobe();
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
    if (info.brand && !$('g-brand').value.trim()) $('g-brand').value = info.brand;
    if (info.sizes && !$('g-sizes').value.trim()) $('g-sizes').value = info.sizes;
    previewImages = info.images || [];
    renderPreview();
    const metaBits = [info.brand, info.sizes ? 'sizes ' + info.sizes : ''].filter(Boolean);
    status.textContent = (metaBits.length ? 'filled ' + metaBits.join(' · ') + ' — ' : '') +
      (previewImages.length
        ? 'found ' + previewImages.length + ' image(s) — pick one, then Add garment'
        : 'no images found');
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
  const brand = $('g-brand').value.trim();
  const sizes = $('g-sizes').value.trim();
  if (!name) { status.textContent = 'name required'; return; }
  const url = pickedImage || $('g-url').value.trim();
  const body = { name, category, color, brand, sizes, owned: $('g-owned').checked };
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
    $('g-name').value = ''; $('g-brand').value = ''; $('g-sizes').value = '';
    $('g-color').value = ''; $('g-url').value = ''; $('g-file').value = '';
    pickedImage = null; previewImages = [];
    $('g-preview').hidden = true; $('g-preview').innerHTML = '';
    loadWardrobe();
  } catch (e) { status.textContent = e.message; }
});
$('g-clear').addEventListener('click', () => {
  $('g-name').value = ''; $('g-brand').value = ''; $('g-sizes').value = '';
  $('g-color').value = ''; $('g-url').value = ''; $('g-file').value = '';
  $('g-category').selectedIndex = 0;
  pickedImage = null; previewImages = [];
  $('g-preview').hidden = true; $('g-preview').innerHTML = '';
  $('g-status').textContent = 'form cleared — paste a new link or add manually';
});

// ---------- AI tag-reader on file upload ----------
// When a photo is picked, ask the server to read visible tags (brand / color /
// category / sizes) with a vision model. Never blocks adding — it fills only
// fields the user hasn't already typed and degrades gracefully if AI is down.
$('g-file').addEventListener('change', async () => {
  const f = $('g-file').files[0];
  const status = $('g-status');
  if (!f) return;
  pickedImage = null;  // a chosen file wins over a previously-picked link image
  if (!f.type.startsWith('image/')) { status.textContent = 'pick an image file'; return; }
  status.textContent = 'reading the tag… (AI)';
  const fd = new FormData(); fd.append('image', f);
  let res;
  try {
    res = await apiJson('/api/wardrobe/ai-fill', { method: 'POST', body: fd });
  } catch (e) {
    status.textContent = 'AI unavailable — fill the fields by hand'; return;
  }
  if (!res.available) { status.textContent = res.error || 'AI unavailable — fill the fields by hand'; return; }
  const t = res.fields || {};
  const bits = [];
  if (t.name && !$('g-name').value.trim()) { $('g-name').value = t.name; bits.push('name'); }
  if (t.brand && !$('g-brand').value.trim()) { $('g-brand').value = t.brand; bits.push(t.brand); }
  if (t.color && !$('g-color').value.trim()) { $('g-color').value = t.color; bits.push(t.color); }
  if (t.sizes && !$('g-sizes').value.trim()) { $('g-sizes').value = t.sizes; bits.push('size ' + t.sizes); }
  if (t.category && Array.from($('g-category').options).some((o) => o.value === t.category)) {
    $('g-category').value = t.category;
  }
  status.textContent = bits.length
    ? 'AI read: ' + bits.join(', ')
    : 'AI read the photo but found no tag info — fill manually';
});

loadWardrobe();
