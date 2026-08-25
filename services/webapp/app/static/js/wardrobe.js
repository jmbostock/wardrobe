// wardrobe page — add-garment form (URL fetch + AI tag-read), grid, and the
// garment detail card (outfits-style click-through with inline edit + rating).
let editingItem = null;   // garment being edited in the detail card
let wardrobeFilter = 'all';
let wardrobeSort = 'newest';
// Versioned, size-aware garment image URL. The ?v=<mtime> is the cache-buster:
// rotate/upload rewrite the file -> the backend returns a fresh image_version ->
// this URL changes -> the browser fetches the new image instead of the cached one.
// size=thumb for grids (~300px WebP), size=detail for the sheet (~768px WebP).
function gimg(g, size) {
  return '/api/wardrobe/' + g.id + '/image?size=' + (size || 'thumb') + '&v=' + (g.image_version || 0);
}

// 64-bit dHash Hamming distance (hex-string phash). Lower = more similar.
function phashHamming(a, b) {
  let n = 0;
  for (let i = 0; i < a.length && i < b.length; i++) {
    const x = parseInt(a[i], 16) ^ parseInt(b[i], 16);
    n += (x & 1) + ((x >> 1) & 1) + ((x >> 2) & 1) + ((x >> 3) & 1);
  }
  return n;
}

// "Similarity" sort: greedily walk nearest perceptual neighbours so near-
// duplicate garments (the same item re-photographed) cluster together and are
// easy to spot. Items without a phash trail at the end, newest first.
function similarOrder(items) {
  const have = items.filter((g) => g.phash);
  const rest = items.filter((g) => !g.phash);
  if (have.length < 2) return [...items].sort((a, b) => b.id - a.id);
  const pool = have.slice()
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id);
  const out = [pool.shift()];
  while (pool.length) {
    let bi = 0, bd = Infinity;
    const cur = out[out.length - 1];
    for (let i = 0; i < pool.length; i++) {
      const d = phashHamming(cur.phash, pool[i].phash);
      if (d < bd) { bd = d; bi = i; }
    }
    out.push(pool.splice(bi, 1)[0]);
  }
  rest.sort((a, b) => b.id - a.id);
  return out.concat(rest);
}

// Build the meta block for a grid card (safe textContent, no HTML injection).
function buildMeta(g) {
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
  if (g.near_dup_of) {
    const n = document.createElement('div'); n.className = 'muted'; n.style.fontSize = '12px'; n.style.color = '#d4a72c';
    n.textContent = '⚠ similar to ' + g.near_dup_of.name;
    meta.appendChild(n);
  }
  if (!g.owned) {
    const want = document.createElement('span'); want.className = 'badge'; want.textContent = 'to buy';
    want.style.background = '#d4a72c';
    meta.appendChild(want);
  }
  if (g.shared) {
    const sh = document.createElement('span'); sh.className = 'badge'; sh.textContent = 'shared';
    sh.style.background = '#2ea043';
    meta.appendChild(sh);
  }
  if (g.rating) {
    const r = document.createElement('div'); r.className = 'muted'; r.style.fontSize = '12px';
    r.textContent = '★ ' + g.rating + '/10';
    meta.appendChild(r);
  }
  return meta;
}

// Refresh ONE grid card after an edit — no full-grid reload, no re-downloading
// every other garment's image. Image only re-fetches when the version changed
// (so a name/rating save never touches the network for the photo).
function updateCard(g) {
  const card = document.querySelector('.photo[data-gid="' + g.id + '"]');
  if (!card) return;
  card._garment = g;
  const img = card.querySelector('img');
  if (img) {
    if (g.has_image) {
      const v = String(g.image_version || 0);
      if (img.dataset.v !== v) {
        img.dataset.v = v;
        setAuthImage(img, gimg(g, 'thumb'));
      }
    } else {
      img.src = ''; img.style.background = g.color_hex || '#333';
    }
  }
  const meta = card.querySelector('.meta');
  if (meta) meta.replaceWith(buildMeta(g));
}

// ---------- grid ----------
async function loadWardrobe() {
  const grid = $('wardrobe');
  grid.innerHTML = '<p class="muted">loading…</p>';
  let items;
  try { items = await apiJson('/api/wardrobe'); }
  catch (e) { grid.innerHTML = '<p class="muted">failed to load wardrobe</p>'; return; }
  if (wardrobeFilter === 'owned') items = items.filter((g) => g.owned);
  else if (wardrobeFilter === 'want') items = items.filter((g) => !g.owned);
  // sort: newest (date added) / top rated / most used / similarity (phash)
  if (wardrobeSort === 'similar') {
    items = similarOrder(items);
  } else {
    items = [...items].sort((a, b) => {
      if (wardrobeSort === 'rating') return (b.rating || 0) - (a.rating || 0) || b.id - a.id;
      if (wardrobeSort === 'used') return (b.used_count || 0) - (a.used_count || 0) || b.id - a.id;
      return (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id;
    });
  }
  if (!items.length) {
    grid.innerHTML = '<p class="muted">no garments' + (wardrobeFilter === 'all' ? '' : ' in this filter') + ' — add one above</p>';
    return;
  }
  grid.innerHTML = '';
  for (const g of items) {
    const card = document.createElement('div'); card.className = 'photo';
    card.dataset.gid = g.id; card._garment = g;
    const img = document.createElement('img'); img.alt = g.name;
    if (g.has_image) {
      img.dataset.v = g.image_version || 0;
      // parallel + cached thumbnails (~300px WebP) — no serial await, so the
      // grid renders immediately and images decode as they arrive
      setAuthImage(img, gimg(g, 'thumb'));
    } else {
      img.style.background = g.color_hex || '#333';
      img.title = 'no image yet — add one in the detail card';
    }
    // outfits-style: clicking the card opens the detail card (no Edit button)
    card.addEventListener('click', () => openGarmentDetail(card._garment));
    card.style.cursor = 'pointer';
    card.appendChild(img); card.appendChild(buildMeta(g));
    grid.appendChild(card);
  }
}
$('wardrobe-filter').addEventListener('change', (e) => { wardrobeFilter = e.target.value; loadWardrobe(); });
$('wardrobe-sort').addEventListener('change', (e) => { wardrobeSort = e.target.value; loadWardrobe(); });

// ---------- category-aware size fields ----------
// Mirrors backend SIZE_SCHEMAS (refreshed from /api/wardrobe/meta). The size
// input adapts to the garment type: pants = waist × length, bra = band × cup,
// shirts/dresses/footwear = size list with plausible suggestions.
let SIZE_SCHEMAS = {
  top:       { mode: 'list', placeholder: 'e.g. S, M, L', options: ['XS','S','M','L','XL','XXL','3XL'] },
  bottom:    { mode: 'wxl', label: 'Waist × Length', ph1: 'Waist (e.g. 30)', ph2: 'Length (e.g. 32)' },
  bra:       { mode: 'bandcup', label: 'Band × Cup', ph1: 'Band (e.g. 34)', ph2: 'Cup (e.g. C)' },
  dress:     { mode: 'list', placeholder: 'e.g. 0,2,4,6 or S,M,L', options: ['XS','S','M','L','XL','0','2','4','6','8','10','12','14'] },
  swimsuit:  { mode: 'list', placeholder: 'e.g. S, M, L or 4, 6, 8', options: ['XS','S','M','L','XL','0','2','4','6','8','10','12','14'] },
  outerwear: { mode: 'list', placeholder: 'e.g. S, M, L', options: ['XS','S','M','L','XL','XXL','3XL'] },
  footwear:  { mode: 'list', placeholder: 'e.g. 8, 8.5, 9', options: ['5','5.5','6','6.5','7','7.5','8','8.5','9','9.5','10','10.5','11'] },
  accessory: { mode: 'list', placeholder: 'e.g. One size', options: ['One size','OS'] }
};
function sizeSchema(cat) { return SIZE_SCHEMAS[cat] || SIZE_SCHEMAS.top; }

// split a stored sizes string into per-field values for a schema mode
function parseSizeFields(cat, value) {
  const s = sizeSchema(cat); const v = (value || '').trim();
  if (s.mode === 'wxl') {  // "30W x 32L" | "30x32" | "30/32" | "30,32"
    const nums = (v.match(/\d+(?:\.\d+)?/g) || []).map((x) => x);
    return { a: nums[0] || '', b: nums[1] || '' };
  }
  if (s.mode === 'bandcup') {  // "34C" | "34" | "C"
    const m = v.match(/^(\d{1,3})\s*([A-Za-z]{1,2})?$/);
    return m ? { a: m[1], b: m[2] || '' } : { a: '', b: '' };
  }
  return { a: v, b: null };
}

// render the size input(s) for a category into the wrapper element
function renderSizeInputs(wrapId, cat, value) {
  const wrap = $(wrapId); if (!wrap) return;
  const s = sizeSchema(cat);
  const f = parseSizeFields(cat, value);
  const hint = $(wrapId.replace('sizes-wrap', 'size-hint'));
  if (hint) hint.textContent = (s.mode !== 'list' && s.label) ? '· ' + s.label : '';
  wrap.innerHTML = '';
  if (s.mode === 'list') {
    const dl = document.createElement('datalist'); dl.id = wrapId + '-dl';
    (s.options || []).forEach((o) => { const x = document.createElement('option'); x.value = o; dl.appendChild(x); });
    const inp = document.createElement('input');
    inp.type = 'text'; inp.id = wrapId + '-inp'; inp.className = 'field'; inp.placeholder = s.placeholder; inp.list = dl.id; inp.value = f.a;
    wrap.appendChild(dl); wrap.appendChild(inp);
  } else {
    const mk = (id, ph, val) => {
      const i = document.createElement('input');
      i.type = 'text'; i.id = id; i.className = 'field'; i.placeholder = ph; i.value = val || '';
      return i;
    };
    wrap.appendChild(mk(wrapId + '-a', s.ph1, f.a));
    if (f.b !== null) wrap.appendChild(mk(wrapId + '-b', s.ph2, f.b));
  }
}

// read the rendered size input(s) back into a stored sizes string
function collectSizes(wrapId, cat) {
  const s = sizeSchema(cat); const wrap = $(wrapId); if (!wrap) return '';
  if (s.mode === 'list') { const i = $(wrapId + '-inp'); return i ? i.value.trim() : ''; }
  const a = ($(wrapId + '-a') || { value: '' }).value.trim();
  const b = ($(wrapId + '-b') || { value: '' }).value.trim();
  if (s.mode === 'wxl') {
    const parts = [];
    if (a) parts.push(a.toUpperCase().replace(/[WL]$/, '') + 'W');
    if (b) parts.push(b.toUpperCase().replace(/[WL]$/, '') + 'L');
    return parts.join(' x ');
  }
  if (s.mode === 'bandcup') return (a + (b ? b.toUpperCase() : '')).trim();
  return '';
}
$('g-category').addEventListener('change', (e) => renderSizeInputs('g-sizes-wrap', e.target.value, ''));
$('gd-category').addEventListener('change', (e) => renderSizeInputs('gd-sizes-wrap', e.target.value, ''));
renderSizeInputs('g-sizes-wrap', $('g-category').value, '');

// ---------- color: strict canonical dropdown (+ swatch preview) ----------
// Colors are a fixed select (not free text) so 'navy' can never be stored as
// 'navy blue' / 'dark navy' etc. and silently mismatch in the recommender.
const COLOR_HEX_FALLBACK = {
  white:'#f2f2f2', black:'#1a1a1a', gray:'#8a8f98', navy:'#1f2a44',
  blue:'#3b5ba8', 'light blue':'#9db8d9', indigo:'#3b4a6b',
  red:'#a33333', green:'#2e4a3a', beige:'#d9c9a3', brown:'#6b4a2f',
  tan:'#c8b98a', pink:'#d9b3a0', burgundy:'#6d2332', purple:'#5b3a6d', yellow:'#d9c04a',
  orange:'#c96a2e', teal:'#2c4f46', cream:'#f2efe6', khaki:'#c8b98a', olive:'#6b7a3a'
};
// Colors are stored as lowercase canonical keys (what the API/recommender compare),
// but shown Title Case in the dropdown ("Navy", "Light Blue") like every other label.
function titleCaseColor(c) {
  return (c || '').split(/[\s_]+/).map((w) => w ? w[0].toUpperCase() + w.slice(1) : w).join(' ');
}
let COLOR_HEX_MAP = COLOR_HEX_FALLBACK;
function populateColorSelects(colors, hexMap) {
  if (hexMap) COLOR_HEX_MAP = hexMap;
  ['g-color', 'gd-color'].forEach((id) => {
    const sel = $(id); if (!sel) return;
    sel.innerHTML = '';
    const ph = document.createElement('option'); ph.value = ''; ph.textContent = '— color —'; sel.appendChild(ph);
    (colors || Object.keys(COLOR_HEX_FALLBACK)).forEach((c) => {
      const o = document.createElement('option'); o.value = c; o.textContent = titleCaseColor(c); sel.appendChild(o);
    });
  });
}
function setSwatch(selId, swatchId) {
  const sw = $(swatchId); if (!sw) return;
  const sel = $(selId);
  sw.style.background = COLOR_HEX_MAP[sel ? sel.value : ''] || '#333';
}
function setColorValue(selId, value, swatchId) {
  const sel = $(selId); if (!sel) return;
  const v = (value || '').trim();
  if (v && ![...sel.options].some((o) => o.value === v)) {
    const o = document.createElement('option'); o.value = v; o.textContent = titleCaseColor(v); sel.appendChild(o);
  }
  sel.value = v;
  setSwatch(selId, swatchId);
}
['g-color', 'gd-color'].forEach((id) => {
  const sel = $(id);
  if (sel) sel.addEventListener('change', () => setSwatch(id, id.replace('-color', '-color-swatch')));
});
populateColorSelects(Object.keys(COLOR_HEX_FALLBACK), COLOR_HEX_FALLBACK);

// ---------- garment detail card (outfits-style click-through) ----------
function openGarmentDetail(g) {
  editingItem = g;
  $('gd-title').textContent = g.name;
  $('gd-name').value = g.name;
  $('gd-brand').value = g.brand || '';
  renderSizeInputs('gd-sizes-wrap', g.category, g.sizes || '');
  setColorValue('gd-color', (g.color_tags || [])[0] || '', 'gd-color-swatch');
  const cat = $('gd-category'); cat.innerHTML = '';
  Array.from($('g-category').options).forEach((o) => cat.add(new Option(o.text, o.value)));
  cat.value = g.category;
  $('gd-owned').checked = g.owned !== false && g.owned !== 0;
  // owner gets the editor + family-share toggle; a family viewer gets the fit toggle
  const isOwner = g.is_owner !== false;
  $('gd-editor').hidden = !isOwner;
  $('gd-share-block').hidden = !isOwner;
  $('gd-shared').checked = !!g.shared;
  $('gd-fit-block').hidden = isOwner;
  $('gd-fit-status').textContent = '';
  setFitActive(g.fit_ok);
  $('gd-url').value = '';
  $('gd-status').textContent = '';
  const img = $('gd-img');
  img.style.background = g.color_hex || '#333'; img.src = '';
  if (g.has_image) {
    // detail-size WebP — same cached file as the grid at a higher resolution,
    // so it appears immediately (browser cache) and never blocks the sheet
    setAuthImage(img, gimg(g, 'detail'));
  }
  $('gd-near').textContent = g.near_dup_of
    ? '⚠ looks similar to "' + g.near_dup_of.name + '" — possible duplicate'
    : '';
  bindRating('gd-rating', g.rating || 0);
  openSheet($('garment-detail'));
}
function closeGarmentDetail() { closeSheet($('garment-detail')); editingItem = null; }
async function refreshDetailImage() {
  if (editingItem && editingItem.has_image) {
    setAuthImage($('gd-img'), gimg(editingItem, 'detail'));
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
    const resp = await apiJson('/api/wardrobe/' + editingItem.id + '/image', { method: 'POST', body: fd });
    $('gd-status').textContent = 'photo saved'; $('gd-file').value = '';
    if (resp.near_dup_of) toast('⚠ near-duplicate of "' + resp.near_dup_of.name + '"');
    editingItem = Object.assign({}, editingItem, resp);
    refreshDetailImage(); updateCard(editingItem);
  } catch (e) { alert(e.message); }
});
$('gd-url-btn').addEventListener('click', async () => {
  const u = $('gd-url').value.trim(); if (!u || !editingItem) return;
  $('gd-status').textContent = 'fetching…';
  try {
    const resp = await apiJson('/api/wardrobe/' + editingItem.id + '/image-url', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: u }),
    });
    $('gd-status').textContent = 'photo saved'; $('gd-url').value = '';
    if (resp.near_dup_of) toast('⚠ near-duplicate of "' + resp.near_dup_of.name + '"');
    editingItem = Object.assign({}, editingItem, resp);
    refreshDetailImage(); updateCard(editingItem);
  } catch (e) { alert(e.message); }
});
$('gd-rotate').addEventListener('click', async () => {
  if (!editingItem) return;
  const img = $('gd-img');
  $('gd-status').textContent = 'rotating…';
  img.style.opacity = '0.35';  // instant feedback: dims the old photo
  try {
    const resp = await apiJson('/api/wardrobe/' + editingItem.id + '/rotate', { method: 'POST' });
    $('gd-status').textContent = 'rotated 90°';
    if (resp.near_dup_of) toast('⚠ near-duplicate of "' + resp.near_dup_of.name + '"');
    editingItem = Object.assign({}, editingItem, resp);
    $('gd-near').textContent = resp.near_dup_of
      ? '⚠ looks similar to "' + resp.near_dup_of.name + '" — possible duplicate'
      : '';
    await refreshDetailImage();  // fresh, versioned URL -> shows rotated photo
    img.style.opacity = '1';
    updateCard(editingItem);
  } catch (e) { img.style.opacity = '1'; $('gd-status').textContent = e.message; }
});
// family sharing + per-person fit (rec-engine v2) — quick actions in the detail card
function setFitActive(v) {
  ['gd-fit-yes', 'gd-fit-no', 'gd-fit-unsure'].forEach((id) => $(id).classList.remove('active'));
  if (v === true) $('gd-fit-yes').classList.add('active');
  else if (v === false) $('gd-fit-no').classList.add('active');
  else $('gd-fit-unsure').classList.add('active');
}
async function postFit(v) {
  if (!editingItem) return;
  try {
    await apiJson(`/api/wardrobe/${editingItem.id}/fit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fit_ok: v }),
    });
    setFitActive(v);
    $('gd-fit-status').textContent = v === null ? "won't filter suggestions" : (v ? 'fits — will be suggested' : "won't be suggested to you");
    toast('fit preference saved');
  } catch (e) { $('gd-fit-status').textContent = e.message; }
}
$('gd-fit-yes').addEventListener('click', () => postFit(true));
$('gd-fit-no').addEventListener('click', () => postFit(false));
$('gd-fit-unsure').addEventListener('click', () => postFit(null));
$('gd-shared').addEventListener('change', async () => {
  if (!editingItem) return;
  const v = $('gd-shared').checked;
  try {
    await apiJson(`/api/wardrobe/${editingItem.id}/share`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shared: v }),
    });
    $('gd-status').textContent = v ? 'shared to Family' : 'made private';
    toast(v ? 'shared to Family' : 'made private');
    loadWardrobe();
  } catch (e) { $('gd-shared').checked = !v; $('gd-status').textContent = e.message; }
});
$('gd-save').addEventListener('click', async () => {
  if (!editingItem) return;
  $('gd-status').textContent = 'saving…';
  const body = {
    name: $('gd-name').value.trim(),
    brand: $('gd-brand').value.trim(),
    sizes: collectSizes('gd-sizes-wrap', $('gd-category').value),
    category: $('gd-category').value,
    color: $('gd-color').value.trim(),
    rating: currentRating(),
    owned: $('gd-owned').checked,
  };
  try {
    const resp = await apiJson('/api/wardrobe/' + editingItem.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    $('gd-status').textContent = 'saved'; toast('garment updated');
    editingItem = Object.assign({}, editingItem, resp);
    updateCard(editingItem);
  } catch (e) { $('gd-status').textContent = e.message; }
});
$('gd-delete').addEventListener('click', async () => {
  if (!editingItem) return;
  if (!confirm('delete "' + editingItem.name + '"?')) return;
  try { await apiJson('/api/wardrobe/' + editingItem.id, { method: 'DELETE' }); toast('deleted'); }
  catch (e) { alert(e.message); }
  const card = document.querySelector('.photo[data-gid="' + editingItem.id + '"]');
  if (card) card.remove();
  closeGarmentDetail();
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
    if (info.color && !$('g-color').value.trim()) setColorValue('g-color', info.color, 'g-color-swatch');
    if (info.category) $('g-category').value = info.category;
    if (info.brand && !$('g-brand').value.trim()) $('g-brand').value = info.brand;
    if (info.sizes && !collectSizes('g-sizes-wrap', $('g-category').value)) {
      renderSizeInputs('g-sizes-wrap', $('g-category').value, info.sizes);
    }
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
  const sizes = collectSizes('g-sizes-wrap', category);
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
      const up = await apiJson('/api/wardrobe/' + g.id + '/image', { method: 'POST', body: fd });
      if (up.near_dup_of) toast('⚠ near-duplicate of "' + up.near_dup_of.name + '"');
    } else if (g.near_dup_of) {
      toast('⚠ near-duplicate of "' + g.near_dup_of.name + '"');
    }
    status.textContent = 'added "' + g.name + '"';
    $('g-name').value = ''; $('g-brand').value = '';
    renderSizeInputs('g-sizes-wrap', $('g-category').value, '');
    $('g-color').value = ''; setSwatch('g-color', 'g-color-swatch');
    $('g-url').value = ''; $('g-file').value = '';
    pickedImage = null; previewImages = [];
    $('g-preview').hidden = true; $('g-preview').innerHTML = '';
    loadWardrobe();
  } catch (e) { status.textContent = e.message; }
});
$('g-clear').addEventListener('click', () => {
  $('g-name').value = ''; $('g-brand').value = '';
  renderSizeInputs('g-sizes-wrap', $('g-category').value, '');
  $('g-color').value = ''; setSwatch('g-color', 'g-color-swatch');
  $('g-url').value = ''; $('g-file').value = '';
  $('g-category').selectedIndex = 0;
  renderSizeInputs('g-sizes-wrap', $('g-category').value, '');
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
  if (t.color && !$('g-color').value.trim()) { setColorValue('g-color', t.color, 'g-color-swatch'); bits.push(t.color); }
  if (t.sizes && !collectSizes('g-sizes-wrap', $('g-category').value)) {
    renderSizeInputs('g-sizes-wrap', $('g-category').value, t.sizes);
    bits.push('size ' + t.sizes);
  }
  if (t.category && Array.from($('g-category').options).some((o) => o.value === t.category)) {
    $('g-category').value = t.category;
  }
  status.textContent = bits.length
    ? 'AI read: ' + bits.join(', ')
    : 'AI read the photo but found no tag info — fill manually';
});

// ---------- brand/color/size suggestions from the DB (datalists) ----------
async function loadMeta() {
  let meta;
  try { meta = await apiJson('/api/wardrobe/meta'); }
  catch (e) { return; }
  if (meta.schemas && meta.schemas.top) SIZE_SCHEMAS = meta.schemas;  // only override with a real schema set
  const fill = (id, items) => {
    const dl = $(id); if (!dl) return;
    dl.innerHTML = '';
    (items || []).forEach((v) => { const o = document.createElement('option'); o.value = v; dl.appendChild(o); });
  };
  fill('brand-list', meta.brands);
  populateColorSelects(meta.colors, meta.color_hex);
  // re-render the add-form size field with the server's schemas
  renderSizeInputs('g-sizes-wrap', $('g-category').value, '');
}

loadWardrobe();
loadMeta();

// auto-open a garment when the outfit click-through lands here (?g=<id>)
const _gParam = new URLSearchParams(location.search).get('g');
if (_gParam) {
  const _card = document.querySelector('.photo[data-gid="' + Number(_gParam) + '"]');
  if (_card) openGarmentDetail(_card._garment);
}
