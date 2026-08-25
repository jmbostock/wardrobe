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
    if (o.motion_url) {
      const m = document.createElement('div'); m.className = 'muted'; m.style.fontSize = '12px';
      m.textContent = '🎬 clip';
      meta.appendChild(m);
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
  // motion clip: show the webp if it exists, plus a make-a-clip action.
  // If a clip is already running for this outfit (e.g. started on the Try-on
  // tab), resume tracking it instead of starting a duplicate.
  const clipBox = $('od-clip');
  clipBox.innerHTML = '';
  if (o.motion_url) {
    const c = document.createElement('img'); c.alt = 'motion clip';
    c.style.width = '100%'; c.style.borderRadius = '10px';
    setAuthImage(c, o.motion_url + '?size=detail');
    clipBox.appendChild(c);
  }
  if (o.result_url) {
    const st = document.createElement('div'); st.className = 'muted'; st.id = 'od-clip-status'; st.style.marginTop = '6px';
    clipBox.appendChild(st);
    let active = null;
    try { active = await apiJson('/api/clips/by-outfit/' + o.id); } catch (e) { /* ignore */ }
    if (active && active.clip_id && (active.status === 'queued' || active.status === 'running')) {
      st.textContent = 'clip already rendering — tracking it…';
      trackClip(o.id, active.clip_id, st);
    } else {
      const btn = document.createElement('button'); btn.className = 'ghost';
      btn.textContent = o.motion_url ? 'Regenerate clip' : '✨ Make a 3s clip';
      btn.style.marginTop = '8px';
      btn.addEventListener('click', () => makeClip(o, btn));
      clipBox.appendChild(btn);
    }
  }
  bindRating('od-rating', o.rating || 0);
  openSheet($('outfit-detail'));
}
// Poll an existing clip until it's done — used both when starting a new clip
// and when resuming one that was already running when we opened the card.
function trackClip(outfitId, clipId, st) {
  const started = Date.now();
  const timer = setInterval(async () => {
    try {
      const r = await apiJson('/api/clips/' + clipId);
      const secs = Math.round((Date.now() - started) / 1000);
      if (r.status === 'done') {
        clearInterval(timer);
        st.textContent = 'clip ready — ' + secs + 's';
        loadSavedOutfits();
        // refresh this card's motion
        const fresh = (await apiJson('/api/outfits')).find((x) => x.id === outfitId);
        if (fresh) openDetail(fresh);
      } else if (r.status === 'error') {
        clearInterval(timer);
        st.textContent = 'clip failed: ' + (r.error || 'unknown');
        loadSavedOutfits();
      } else {
        st.textContent = 'rendering clip… ' + secs + 's (runs in the background)';
      }
    } catch (e) {
      clearInterval(timer);
      st.textContent = 'error: ' + e.message;
    }
  }, 3000);
}
async function makeClip(o, btn) {
  const st = $('od-clip-status'); st.textContent = 'queuing…'; btn.disabled = true;
  let clipId = null;
  try {
    const fd = new FormData();
    fd.append('base_result', o.result_url);
    fd.append('outfit_id', String(o.id));
    clipId = (await apiJson('/api/tryon/clip', { method: 'POST', body: fd })).clip_id;
    st.textContent = 'queued — runs in the background';
  } catch (e) { st.textContent = 'failed: ' + e.message; btn.disabled = false; return; }
  btn.remove();  // the status line takes over from here
  trackClip(o.id, clipId, st);
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
