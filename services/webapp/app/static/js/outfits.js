// outfits page — saved outfits grid (auto-saved from Try-on) + detail card.
// Clicking a card opens the detail modal: full-size image, motion clip (if
// any), and inline rename / rate / delete. No separate Edit button.
let editingItem = null;
let outfitsSort = 'newest';

async function loadSavedOutfits() {
  const box = $('saved-outfits');
  box.innerHTML = '<p class="muted">loading…</p>';
  let items;
  try { items = await apiJson('/api/outfits'); }
  catch (e) { box.innerHTML = '<p class="muted">failed to load</p>'; return; }
  items = [...items].sort((a, b) => {
    if (outfitsSort === 'rating') return (b.rating || 0) - (a.rating || 0) || b.id - a.id;
    return (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id;
  });
  if (!items.length) { box.innerHTML = '<p class="muted">no saved outfits yet — render one from the Try-on tab and it appears here automatically</p>'; return; }
  box.innerHTML = '';
  for (const o of items) {
    const card = document.createElement('div'); card.className = 'photo';
    const img = document.createElement('img'); img.alt = o.name;
    if (o.result_url) {
      // thumb WebP variant of the render (renders are immutable URLs — cacheable)
      setAuthImage(img, o.result_url + '?size=thumb');
    } else {
      img.style.background = 'linear-gradient(135deg,#2a3340,#1a1f27)';
      img.title = 'no render for this outfit yet';
    }
    img.addEventListener('click', () => openDetail(o));
    const meta = document.createElement('div'); meta.className = 'meta';
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = 'outfit';
    const name = document.createElement('div'); name.textContent = o.name; name.style.fontSize = '13px';
    const desc = document.createElement('div'); desc.className = 'muted'; desc.style.fontSize = '12px';
    desc.textContent = (o.garments || []).map((g) => g.name).join(' + ') || 'n/a';
    meta.appendChild(badge); meta.appendChild(name); meta.appendChild(desc);
    if (o.ref_id) {
      const ref = document.createElement('div'); ref.className = 'monospace muted';
      ref.style.fontSize = '11px'; ref.style.cursor = 'pointer'; ref.textContent = o.ref_id;
      ref.title = 'reference id — click to copy';
      ref.addEventListener('click', (ev) => {
        ev.stopPropagation();
        navigator.clipboard && navigator.clipboard.writeText(o.ref_id);
      });
      meta.appendChild(ref);
    }
    if (o.rating) {
      const r = document.createElement('div'); r.className = 'muted'; r.style.fontSize = '12px';
      r.textContent = '★ ' + o.rating + '/10';
      meta.appendChild(r);
    }
    card.appendChild(img); card.appendChild(meta);
    box.appendChild(card);
  }
}
$('outfits-sort').addEventListener('change', (e) => { outfitsSort = e.target.value; loadSavedOutfits(); });

// ---------- detail card (image + motion + inline edit) ----------
async function openDetail(o) {
  editingItem = o;
  $('od-title').textContent = o.name;
  $('od-name').value = o.name;
  $('od-status').textContent = '';
  $('od-garments').textContent = (o.garments || []).map((g) => g.name).join(' + ') || '—';
  const refEl = $('od-ref');
  if (refEl) {
    refEl.textContent = o.ref_id || '—';
    refEl.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(o.ref_id || ''); };
  }
  const img = $('od-img');
  if (o.result_url) setAuthImage(img, o.result_url + '?size=detail');
  else { img.src = ''; img.style.background = 'linear-gradient(135deg,#2a3340,#1a1f27)'; }
  // wardrobe items used to make this look — shown as a 2-wide grid of the
  // garment photos below the render (the source person photo stays in the DB,
  // it's just not shown on the card anymore)
  const gridBox = $('od-garment-grid');
  gridBox.innerHTML = '';
  const gs = o.garments || [];
  const gmap = {}; gs.forEach((g) => { gmap[g.id] = g; });
  if (gs.length) {
    let html = '';
    for (const g of gs) {
      const label = (g.category ? g.category.replace(/^./, (c) => c.toUpperCase()) + ': ' : '') + (g.name || '');
      html += '<div class="od-g-item" data-gid="' + g.id + '" title="View ' + (g.name || 'this item') + '">' +
        '<div class="od-g-img">' +
        (g.has_image
          ? '<img data-gid="' + g.id + '" alt="' + (g.name || '') + '">'
          : '<div class="od-g-swatch" style="background:' + (g.color_hex || '#555') + '"></div>') +
        '</div><div class="od-g-name">' + label + '</div></div>';
    }
    gridBox.innerHTML = html;
    // click-through: tap a garment tile → open that item in the Wardrobe
    gridBox.querySelectorAll('.od-g-item[data-gid]').forEach((item) => {
      item.style.cursor = 'pointer';
      item.addEventListener('click', () => { location.href = '/wardrobe?g=' + item.dataset.gid; });
    });
    gridBox.querySelectorAll('.od-g-img img[data-gid]').forEach((im) => {
      const g = gmap[Number(im.dataset.gid)];
      setAuthImage(im, g ? garmentImg(g, 'thumb')
                        : ('/api/wardrobe/' + im.dataset.gid + '/image?size=thumb&v=0'));
    });
  }
  // Motion clips (SVD) are deliberately NOT surfaced here — the feature is on
  // hold. Any clip this look already has is still on disk and in the DB (nothing
  // is ever deleted); it's just not shown. See tryon.js for the same note.
  buildRefine(o);
  bindRating('od-rating', o.rating || 0);
  openSheet($('outfit-detail'));
}

// ---------- refine this outfit ----------
// One instruction in, one new render out. The ORIGINAL is never touched: the
// API saves the result as a NEW outfit carrying the same garments, so the two
// versions sit side by side on this page and either can be refined again.
function buildRefine(o) {
  const box = $('od-refine-box');
  box.innerHTML = '';
  if (!o.result_url) return;  // nothing rendered yet, so nothing to refine

  const lab = document.createElement('label');
  lab.textContent = 'Refine this outfit';
  const hint = document.createElement('p');
  hint.className = 'muted';
  hint.style.margin = '2px 0 6px';
  hint.textContent = 'Describe a change — restyle the clothes, alter the pose, ' +
    'adjust the light. Saves as a new outfit; this one stays as it is.';
  const inp = document.createElement('input');
  inp.type = 'text'; inp.className = 'field'; inp.id = 'od-refine-prompt';
  inp.maxLength = 300;
  inp.placeholder = 'e.g. make the top long-sleeved, or turn her to the side';
  const row = document.createElement('div');
  row.className = 'row'; row.style.marginTop = '8px'; row.style.alignItems = 'center';
  const btn = document.createElement('button');
  btn.id = 'od-refine-btn'; btn.textContent = '✨ Refine this outfit';
  const st = document.createElement('span');
  st.className = 'muted'; st.id = 'od-refine-status';
  row.appendChild(btn); row.appendChild(st);

  box.appendChild(lab); box.appendChild(hint); box.appendChild(inp); box.appendChild(row);
  btn.addEventListener('click', () => refineOutfit(o, inp, btn, st));
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') refineOutfit(o, inp, btn, st);
  });
}

async function refineOutfit(o, inp, btn, st) {
  const prompt = (inp.value || '').trim();
  if (!prompt) { st.textContent = 'describe the change you want'; inp.focus(); return; }
  btn.disabled = true;
  // the render is synchronous (~60-120s for one pass), so show a live timer
  const started = Date.now();
  const tick = setInterval(() => {
    st.textContent = 'rendering… ' + Math.round((Date.now() - started) / 1000) + 's';
  }, 1000);
  st.textContent = 'rendering… 0s';
  let created = null;
  try {
    const r = await apiJson('/api/outfits/' + o.id + '/refine', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: prompt }),
    });
    created = r.outfit && r.outfit.id;
  } catch (e) {
    clearInterval(tick);
    st.textContent = 'failed: ' + e.message;
    btn.disabled = false;
    return;
  }
  clearInterval(tick);
  const secs = Math.round((Date.now() - started) / 1000);
  st.textContent = 'done — ' + secs + 's, saved as a new outfit';
  toast('refined — saved as a new outfit');
  await loadSavedOutfits();
  // jump straight to the NEW outfit so the result is what you're looking at
  if (created) {
    const fresh = (await apiJson('/api/outfits')).find((x) => x.id === created);
    if (fresh) openDetail(fresh);
  }
}
function closeDetail() { closeSheet($('outfit-detail')); editingItem = null; }
$('od-close').addEventListener('click', closeDetail);
$('outfit-detail').addEventListener('click', (e) => { if (e.target === $('outfit-detail')) closeDetail(); });
$('od-save').addEventListener('click', async () => {
  if (!editingItem) return;
  $('od-status').textContent = 'saving…';
  try {
    await apiJson('/api/outfits/' + editingItem.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: $('od-name').value.trim(), rating: currentRating() }),
    });
    $('od-status').textContent = 'saved'; toast('outfit updated');
    loadSavedOutfits();
  } catch (e) { $('od-status').textContent = e.message; }
});
$('od-delete').addEventListener('click', async () => {
  if (!editingItem) return;
  if (!confirm('delete "' + editingItem.name + '"?')) return;
  try { await apiJson('/api/outfits/' + editingItem.id, { method: 'DELETE' }); toast('deleted'); }
  catch (e) { alert(e.message); }
  closeDetail(); loadSavedOutfits();
});

loadSavedOutfits();
