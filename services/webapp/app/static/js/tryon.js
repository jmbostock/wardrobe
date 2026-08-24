// tryon page — look builder, try-on render, and chat-based re-render.
const TRYON_STAGES = [
  ['Uploading photo…', 'sending your photo to the renderer'],
  ['Detecting body (DensePose)…', 'locating pose & body parts on the GPU'],
  ['Building mask (SCHP)…', 'parsing clothing regions'],
  ['Rendering garment (CatVTON)…', 'diffusion step — this takes the longest'],
  ['Finalizing…', 'writing the result image'],
];
let tryonInt = null;
let lastResultUrl = null;   // last render URL (used as the chat base)
let lastResultLook = null;  // JSON of the garment ids that produced lastResultUrl
let lastIds = [];           // garment ids of the last render
let lastOutfitId = null;    // auto-saved outfit id for the last render
let savedPhotos = [];       // cached /api/photos (saved-photo source for look-based try-on)
let savedOutfits = [];      // cached /api/outfits that have a render (Saved-image source)
let selectedSaved = null;   // the chosen saved outfit render
let manualPhotoPick = false; // user hand-picked the base photo → don't auto-override it
let lookGarments = [];       // cached /api/wardrobe list — drives the look photo pickers
// Look-builder roles. 'full' = one-piece garments (dresses, swimsuits, jumpsuits)
// that don't need a separate top + bottom.
const LOOK_ROLES = ['top', 'bottom', 'full', 'outerwear', 'footwear'];
const FULL_CATEGORIES = ['dress', 'swimsuit'];
const LOOK_LABELS = { top: 'top', bottom: 'bottom', full: 'one-piece', outerwear: 'outerwear', footwear: 'shoes' };
function roleMatches(role, category) {
  return role === 'full' ? FULL_CATEGORIES.includes(category) : category === role;
}
let lookPickingRole = null;  // which category the look picker modal is open for
// cache-busting garment image URL (rotate/upload rewrite the same file/URL).
function gimg(id) { return '/api/wardrobe/' + id + '/image?v=' + Date.now(); }

async function photoBaseUrl(pid) {
  const p = savedPhotos.find((x) => String(x.id) === String(pid));
  if (!p) return null;
  return authImageUrl(p.url);
}

function setSelectedSaved() {
  const id = $('saved-img').value;
  selectedSaved = savedOutfits.find((o) => String(o.id) === id) || null;
}

// ---- dynamic before/after: original side shows the base once we have one,
// ---- latest side only appears after a re-render (no empty placeholders).
function showCompare(baseUrl, newUrl) {
  if (!baseUrl) { hideCompare(); return; }
  $('compare-base').src = baseUrl;
  $('compare-base-fig').hidden = false;
  if (newUrl) {
    $('compare-new').src = newUrl;
    $('compare-new-fig').hidden = false;
  } else {
    $('compare-new-fig').hidden = true;
  }
  $('compare').hidden = false;
}
function hideCompare() {
  $('compare').hidden = true;
  $('compare-base').src = '';
  $('compare-new').src = '';
}
async function showSavedImagePreview() {
  const img = $('savedimg-preview');
  if (!selectedSaved) { img.hidden = true; img.src = ''; return; }
  const url = await authImageUrl(selectedSaved.result_url);
  if (url) { img.src = url; img.hidden = false; }
  // Fresh saved-image selection: drop any stale render so the previous image
  // isn't mistaken for the selected saved image, and the chat bases on THIS one.
  lastResultUrl = null;
  $('result').innerHTML = '';
  hideCompare();
}

// ---------- look builder (photo pickers) ----------
async function populateLook() {
  let items = [];
  try { items = await apiJson('/api/wardrobe'); } catch (e) { /* ignore */ }
  lookGarments = items;
  const ownedOnly = $('owned-only-look') ? $('owned-only-look').checked : false;
  const withImg = items.filter((g) => g.has_image && (!ownedOnly || g.owned));
  for (const role of LOOK_ROLES) {
    const sel = document.querySelector('.look-select[data-role="' + role + '"]');
    if (!sel) continue;
    const prev = sel.value;
    sel.innerHTML = '<option value="">— none —</option>';
    withImg.filter((g) => roleMatches(role, g.category)).forEach((g) => sel.add(new Option(g.name, g.id)));
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
  }
  syncLookRows();
}
$('owned-only-look').addEventListener('change', () => { populateLook(); autoPickBestPhoto(); });

function garmentById(id) {
  return lookGarments.find((g) => String(g.id) === String(id)) || null;
}
// Refresh every look row's thumbnail + label from its hidden select value, so
// it's always clear which garment (photo) is being tried on.
function syncLookRows() {
  for (const role of LOOK_ROLES) {
    const sel = document.querySelector('.look-select[data-role="' + role + '"]');
    const thumb = document.querySelector('.look-thumb[data-thumb="' + role + '"]');
    const label = document.querySelector('.look-pick-label[data-label="' + role + '"]');
    const btn = document.querySelector('.look-pick[data-role="' + role + '"]');
    const clr = document.querySelector('.look-clear[data-role="' + role + '"]');
    if (!sel) continue;
    const g = garmentById(sel.value);
    if (thumb) {
      thumb.innerHTML = '';
      if (g && g.has_image) {
        const img = document.createElement('img'); img.alt = g.name;
        authImageUrl(gimg(g.id)).then((u) => { img.src = u; }).catch(() => {});
        thumb.appendChild(img);
      }
    }
    if (label) {
      label.textContent = g
        ? (g.name + (g.brand ? ' · ' + g.brand : '') + (g.sizes ? ' · ' + g.sizes : ''))
        : '— tap to pick a ' + (LOOK_LABELS[role] || role) + ' —';
    }
    if (btn) btn.title = g ? g.name + ' — tap to change' : 'tap to pick a ' + (LOOK_LABELS[role] || role);
    if (clr) clr.hidden = !g;   // only show the ✕ when this slot has a pick
  }
}
// Set a look role to a specific garment (or clear it) and refresh the UI.
function setLookRole(role, g) {
  const sel = document.querySelector('.look-select[data-role="' + role + '"]');
  if (!sel) return;
  sel.value = g ? String(g.id) : '';
  syncLookRows();
  checkGarmentImage();
  autoPickBestPhoto();
}
// Tap a category (Top / Bottom / Dress / Swimsuit) → show every photo in that
// category to pick from; the chosen image then shows in the look row.
function openLookPicker(role) {
  lookPickingRole = role;
  const titles = { top: 'Pick a top', bottom: 'Pick a bottom', full: 'Pick a full (one-piece)', outerwear: 'Pick an outerwear / jacket', footwear: 'Pick shoes' };
  $('lp-title').textContent = titles[role] || 'Pick';
  const grid = $('lp-grid'); grid.innerHTML = '';
  const ownedOnly = $('owned-only-look') ? $('owned-only-look').checked : false;
  const items = lookGarments.filter((g) => roleMatches(role, g.category) && g.has_image && (!ownedOnly || g.owned));
  $('lp-empty').hidden = items.length > 0;
  const cur = document.querySelector('.look-select[data-role="' + role + '"]');
  for (const g of items) {
    const card = document.createElement('div'); card.className = 'photo';
    const img = document.createElement('img'); img.alt = g.name;
    img.style.cursor = 'pointer';
    authImageUrl(gimg(g.id)).then((u) => { img.src = u; }).catch(() => {});
    const meta = document.createElement('div'); meta.className = 'meta';
    const name = document.createElement('div'); name.textContent = g.name; name.style.fontSize = '13px';
    meta.appendChild(name);
    const facts = [];
    if (g.brand) facts.push(g.brand);
    if (g.sizes) facts.push(g.sizes);
    if (facts.length) {
      const d = document.createElement('div'); d.className = 'muted'; d.style.fontSize = '12px';
      d.textContent = facts.join(' · '); meta.appendChild(d);
    }
    card.appendChild(img); card.appendChild(meta);
    if (cur && String(cur.value) === String(g.id)) card.style.outline = '3px solid var(--acc)';
    card.addEventListener('click', () => { setLookRole(role, g); closeLookPicker(); });
    grid.appendChild(card);
  }
  openSheet($('look-picker'));
}
function closeLookPicker() { closeSheet($('look-picker')); lookPickingRole = null; }
$('lp-close').addEventListener('click', closeLookPicker);
$('look-picker').addEventListener('click', (e) => { if (e.target === $('look-picker')) closeLookPicker(); });
// "None — clear this slot" clears just this category (not the whole look).
$('lp-none').addEventListener('click', () => { setLookRole(lookPickingRole, null); closeLookPicker(); });
document.querySelectorAll('.look-pick').forEach((b) => b.addEventListener('click', () => openLookPicker(b.dataset.role)));
// Per-row ✕ clears just that slot.
document.querySelectorAll('.look-clear').forEach((b) => b.addEventListener('click', () => setLookRole(b.dataset.role, null)));
function applyOutfitToLook(outfit) {
  const status = $('look-status');
  if (!outfit || !Object.keys(outfit).length) { status.textContent = 'wardrobe empty — add clothes first'; return; }
  const setRole = (role, g) => {
    const sel = document.querySelector('.look-select[data-role="' + role + '"]');
    if (!sel) return;
    if (g && [...sel.options].some((o) => o.value === String(g.id))) sel.value = g.id;
    else sel.value = '';
  };
  const top = outfit.top || outfit.dress || null;
  if (top && FULL_CATEGORIES.includes(top.category)) {
    setRole('full', top); setRole('top', null); setRole('bottom', null);
  } else {
    setRole('top', top); setRole('bottom', outfit.bottom || null); setRole('full', null);
  }
  setRole('outerwear', outfit.outerwear || null);
  setRole('footwear', outfit.footwear || null);
  syncLookRows();
}
function currentLookIds() {
  const ids = [];
  for (const role of LOOK_ROLES) {
    const v = document.querySelector('.look-select[data-role="' + role + '"]').value;
    if (v) ids.push(Number(v));
  }
  return ids;
}

// When the user taps "Try it on" from the Suggest chat, we arrive here with the
// recommendation already in RECO_KEY — fill the look automatically (no separate
// "Use recommendation" button needed anymore).
function applySavedReco() {
  const status = $('look-status');
  let outfit = null;
  try { outfit = JSON.parse(localStorage.getItem(RECO_KEY) || 'null')?.outfit || null; } catch (e) { /* ignore */ }
  if (!outfit || !Object.keys(outfit).length) return;
  applyOutfitToLook(outfit);
  const label = (() => { try { return JSON.parse(localStorage.getItem(RECO_KEY))?.activity || ''; } catch (e) { return ''; } })();
  status.textContent = 'look set from your suggestion' + (label ? ' (' + label + ')' : '') + ' — tweak if you like';
  autoPickBestPhoto();
}
$('tryon-reset').addEventListener('click', () => {
  LOOK_ROLES.forEach((role) => {
    document.querySelector('.look-select[data-role="' + role + '"]').value = '';
  });
  syncLookRows();
  $('look-status').textContent = 'look cleared';
  autoPickBestPhoto();
});

// ---------- SVD motion clip (async, queued in ComfyUI) ----------
async function submitClip() {
  const btn = $('tryon-clip');
  const status = $('clip-status');
  if (!lastResultUrl) { toast('render a look first'); return; }
  if (!lastOutfitId) { toast('no auto-saved outfit to attach the clip to'); return; }
  btn.disabled = true;
  status.textContent = 'queuing…';
  $('clip-box').innerHTML = '';
  let clipId = null;
  try {
    const fd = new FormData();
    fd.append('base_result', lastResultUrl);
    fd.append('outfit_id', String(lastOutfitId));
    const start = await apiJson('/api/tryon/clip', { method: 'POST', body: fd });
    clipId = start.clip_id;
    status.textContent = 'queued — it runs in the background, feel free to keep going';
  } catch (e) {
    status.textContent = 'clip failed: ' + e.message; btn.disabled = false; return;
  }
  const started = Date.now();
  const timer = setInterval(async () => {
    try {
      const st = await apiJson('/api/clips/' + clipId);
      const secs = Math.round((Date.now() - started) / 1000);
      if (st.status === 'done') {
        clearInterval(timer);
        btn.disabled = false;
        status.textContent = 'clip ready — ' + secs + 's';
        const url = await authImageUrl(st.result_url);
        const box = $('clip-box');
        box.innerHTML = '';
        const img = document.createElement('img'); img.src = url; img.alt = 'motion clip';
        box.appendChild(img);
        loadSavedImages();
      } else if (st.status === 'error') {
        clearInterval(timer); btn.disabled = false;
        status.textContent = 'clip failed: ' + (st.error || 'unknown');
      } else {
        status.textContent = 'rendering clip… ' + secs + 's (runs in the background)';
      }
    } catch (e) {
      clearInterval(timer); btn.disabled = false;
      status.textContent = 'clip error: ' + e.message;
    }
  }, 3000);
}
$('tryon-clip').addEventListener('click', submitClip);

// ---------- person source ----------
document.querySelectorAll('input[name=psrc]').forEach((r) => r.addEventListener('change', () => {
  const src = document.querySelector('input[name=psrc]:checked').value;
  $('saved-photo').hidden = src !== 'saved';
  $('person-file').hidden = src !== 'upload';
  $('savedimg-row').hidden = src !== 'savedimg';
  $('look-builder').hidden = src === 'savedimg';
  $('chat-bar').hidden = (src !== 'savedimg' && !lastResultUrl);
  if (src === 'savedimg') {
    setSelectedSaved();
    showSavedImagePreview();
  } else {
    $('savedimg-preview').hidden = true;
  }
  // Don't leak the previous mode's image/output into the newly-selected mode:
  // each radial only shows the controls (and images) that belong to it.
  lastResultUrl = null;
  $('result').innerHTML = '';
  hideCompare();
  checkPersonImage();
  if (src === 'saved') autoPickBestPhoto();
}));$('saved-photo').addEventListener('change', () => {
  manualPhotoPick = true;  // user chose the base photo by hand — stop auto-overriding
  checkPersonImage();
});
$('person-file').addEventListener('change', checkPersonImage);
$('saved-img').addEventListener('change', () => {
  setSelectedSaved();
  showSavedImagePreview();
  checkPersonImage();
});

// ---------- image quality feedback ----------
function renderQa(el, qa) {
  if (!qa) { el.hidden = true; return; }
  const color = qa.score >= 70 ? '#2ea043' : qa.score >= 55 ? '#d4a72c' : qa.score >= 40 ? '#d4762c' : '#d9534f';
  el.hidden = false;
  el.innerHTML =
    '<div style="display:flex;align-items:baseline;gap:8px;margin-top:8px"><b style="color:' + color + '">' +
    qa.grade + ' (' + qa.score + '/100)</b><span class="muted">image quality</span></div>' +
    (qa.issues.length
      ? '<ul class="muted" style="margin:6px 0 0;padding-left:18px">' + qa.issues.map((i) => '<li>' + i + '</li>').join('') + '</ul>'
      : '') +
    (qa.tips.length ? '<div class="muted" style="margin-top:4px">💡 ' + qa.tips.join(' ') + '</div>' : '');
}
async function checkPersonImage() {
  const el = $('person-qa'); if (!el) return;
  const src = document.querySelector('input[name=psrc]:checked')?.value || 'saved';
  let fd;
  if (src === 'saved') {
    const pid = $('saved-photo').value;
    if (!pid) { el.hidden = true; return; }
    fd = new FormData(); fd.append('kind', 'person'); fd.append('photo_id', pid);
  } else if (src === 'savedimg') {
    // the base is a saved outfit render — score it by uploading the bytes
    const base = lastResultUrl || (selectedSaved ? selectedSaved.result_url : '');
    if (!base) { el.hidden = true; return; }
    try {
      const res = await api(base);
      const blob = await res.blob();
      fd = new FormData();
      fd.append('kind', 'person');
      fd.append('image', new File([blob], 'base.png', { type: 'image/png' }));
    } catch (err) { el.hidden = true; return; }
  } else {
    const f = $('person-file').files[0];
    if (!f) { el.hidden = true; return; }
    fd = new FormData(); fd.append('kind', 'person'); fd.append('image', f);
  }
  try { renderQa(el, await apiJson('/api/image-quality', { method: 'POST', body: fd })); }
  catch (e) { el.hidden = true; }
}
async function checkGarmentImage() {
  const el = $('garment-qa'); if (!el) return;
  const sel = (document.querySelector('.look-select[data-role=top]')?.value
    || document.querySelector('.look-select[data-role=full]')?.value
    || document.querySelector('.look-select[data-role=bottom]')?.value);
  if (!sel) { el.hidden = true; return; }
  const fd = new FormData(); fd.append('kind', 'garment'); fd.append('garment_id', sel);
  try { renderQa(el, await apiJson('/api/image-quality', { method: 'POST', body: fd })); }
  catch (e) { el.hidden = true; }
}
document.querySelectorAll('.look-select').forEach((s) => s.addEventListener('change', () => {
  checkGarmentImage();
  autoPickBestPhoto();
}));

// ---------- auto-pick the best saved photo for the garment being tried on ----------
// The best photo is chosen by OUTFIT MATCH with the specific garment: a swimsuit
// garment wants a swimsuit-ish saved photo, a dress wants a dress photo, etc.
// Server-side the vision model judges it (pure-PIL heuristic if the model is
// down). The picked photo is what generates the try-on. A manual pick in the
// dropdown wins and stops auto-overriding.
function primaryGarmentId() {
  const full = document.querySelector('.look-select[data-role="full"]')?.value;
  if (full) return Number(full);                // a one-piece defines the whole outfit
  const ids = currentLookIds();
  return ids.length ? ids[0] : null;            // first garment applied in the chain
}
async function autoPickBestPhoto() {
  const sel = $('saved-photo');
  const hint = $('auto-pick-hint');
  if (!sel) return;
  const src = document.querySelector('input[name=psrc]:checked')?.value || 'saved';
  if (src !== 'saved') return;                  // only relevant when the base is a saved photo
  const gid = primaryGarmentId();
  if (!gid) { hint.hidden = true; return; }     // no garment picked yet
  if (!manualPhotoPick) {
    hint.hidden = false;
    hint.textContent = '✨ picking the best saved photo for this garment…';
  }
  let res = null;
  try { res = await apiJson('/api/photos/best-for-garment/' + gid); } catch (e) { res = null; }
  const ranked = (res && res.ranked) || [];
  const best = ranked[0] || null;
  // Re-label every option with its score; mark the best pick.
  for (const opt of sel.options) {
    if (!opt.value) continue;                   // placeholder ("no saved photos …")
    const p = ranked.find((r) => String(r.id) === opt.value);
    if (!p) continue;
    const star = p.is_default ? '★ ' : '';
    const label = p.description || ('photo ' + p.id);
    const bestTag = best && p.id === best.id ? ' ✓best' : '';
    opt.textContent = `${star}${label} (${p.score})${bestTag}`;
  }
  if (!best || manualPhotoPick) { hint.hidden = true; return; }
  const prev = sel.value;
  sel.value = String(best.id);
  if (sel.value !== prev) checkPersonImage();   // programmatic set doesn't fire change
  hint.hidden = false;
  hint.textContent = '✨ Using the best saved photo to try on ' +
    (res.garment_name || 'this garment') + ' — ' + best.grade + ' (' + best.score + '/100)';
  if (best.reason) hint.textContent += ' — ' + best.reason;
}

async function loadSavedPhotos() {
  try {
    savedPhotos = await apiJson('/api/photos');
    const sel = $('saved-photo'); sel.innerHTML = '';
    if (!savedPhotos.length) { sel.add(new Option('no saved photos — upload in Account', '')); return; }
    for (const p of savedPhotos) {
      const label = p.description ? p.description : `photo ${p.id}`;
      sel.add(new Option(`${p.is_default ? '★ ' : ''}${label}`, p.id));
    }
  } catch (e) { /* ignore */ }
}
async function loadSavedImages() {
  try {
    const items = await apiJson('/api/outfits');
    savedOutfits = items.filter((o) => o.result_url);
    const sel = $('saved-img'); sel.innerHTML = '';
    if (!savedOutfits.length) {
      sel.add(new Option('no saved outfit renders — save one from the Try-on tab', ''));
      return;
    }
    for (const o of savedOutfits) {
      const label = o.name ? o.name : ('Outfit ' + o.id);
      sel.add(new Option(label, o.id));
    }
  } catch (e) { /* ignore */ }
}
loadSavedImages(); populateLook().then(() => { applySavedReco(); });
// Show the quality score for the pre-selected (default) saved photo right away,
// without making the user interact with the dropdown first.
loadSavedPhotos().then(() => { checkPersonImage(); autoPickBestPhoto(); });

// ---------- try on (shared by the Try on button and the chat bar) ----------
async function runTryon(ids, baseResult, prompt) {
  const fd = new FormData();
  fd.append('garment_ids', JSON.stringify(ids));
  const name = ($('outfit-name') ? $('outfit-name').value : '').trim();
  if (name) fd.append('outfit_name', name);
  let baseUrl = null; // what produced the latest render — shown as "original" in the compare
  if (baseResult) {
    fd.append('base_result', baseResult);      // last render or saved outfit render as the base
    if (prompt) fd.append('prompt', prompt);
    baseUrl = await authImageUrl(baseResult);
  } else {
    const src = document.querySelector('input[name=psrc]:checked')?.value || 'saved';
    if (src === 'saved') {
      const pid = $('saved-photo').value;
      if (!pid) { alert('upload a saved photo first (Account → My photos)'); return; }
      fd.append('photo_id', pid);
      baseUrl = await photoBaseUrl(pid);
    } else {
      const f = $('person-file').files[0];
      if (!f) { alert('pick a person photo or use a saved one'); return; }
      fd.append('person', f);
      baseUrl = URL.createObjectURL(f);
    }
  }

  const multi = ids.length > 1;
  const box = document.createElement('div'); box.className = 'progress';
  const spinner = document.createElement('div'); spinner.className = 'spinner';
  const txt = document.createElement('div');
  const stage = document.createElement('div'); stage.className = 'status';
  stage.textContent = ids.length
    ? (multi ? `Rendering garment 1 of ${ids.length}…` : 'Rendering garment…')
    : 'Refining image…';
  const hint = document.createElement('div'); hint.className = 'hint';
  hint.textContent = ids.length
    ? (multi ? `applying ${ids.length} garments in sequence — this can take a while`
             : 'first run may download ~4-6GB of model weights (can take a few minutes)')
    : 'refining from the base image — no garments re-added';
  const timer = document.createElement('div'); timer.className = 'timer'; timer.textContent = '0s';
  txt.appendChild(stage); txt.appendChild(hint);
  box.appendChild(spinner); box.appendChild(txt); box.appendChild(timer);
  $('result').innerHTML = ''; $('result').appendChild(box);

  const started = Date.now();
  let stageIdx = 0, lastStageAt = started;
  const maxStage = multi ? TRYON_STAGES.length - 2 : TRYON_STAGES.length - 1;
  clearInterval(tryonInt);
  tryonInt = setInterval(() => {
    const now = Date.now();
    timer.textContent = Math.round((now - started) / 1000) + 's';
    if (now - lastStageAt > 5000 && stageIdx < maxStage) {
      stageIdx++; lastStageAt = now;
      stage.textContent = TRYON_STAGES[stageIdx][0];
      hint.textContent = TRYON_STAGES[stageIdx][1];
    }
  }, 1000);

  try {
    const res = await api('/api/tryon/outfit', { method: 'POST', body: fd });
    if (!res.ok) { $('result').innerHTML = `<p class="muted">try-on failed: ${await errMsg(res)}</p>`; return; }
    const data = await res.json();
    lastResultUrl = data.result_url;
    lastResultLook = JSON.stringify(ids);
    lastIds = ids;
    lastOutfitId = data.outfit_id || null;
    const url = await authImageUrl(data.result_url);
    $('result').innerHTML = '';
    const img = document.createElement('img'); img.src = url; $('result').appendChild(img);
    // before/after only makes sense when altering (chat re-render / saved-image
    // refine). A plain try-on shows just the new render — never the original.
    if (baseResult) showCompare(baseUrl, url);
    else hideCompare();
    // show the "coming soon" edit teaser under the result
    $('chat-bar').hidden = false;
    // offer an SVD motion clip for look-based renders (auto-saved outfit)
    if (data.outfit_id && ids.length) {
      $('clip-row').hidden = false;
      $('clip-box').innerHTML = '';
      $('clip-status').textContent = '';
      $('tryon-clip').disabled = false;
      $('look-status').textContent = 'saved to Outfits';
      loadSavedImages();
    }
    toast(baseResult ? 'updated — check the result' : 'try-on ready — saved to Outfits');
  } catch (e) { $('result').innerHTML = `<p class="muted">error: ${e}</p>`; }
  finally { clearInterval(tryonInt); tryonInt = null; }
}

$('tryon-btn').addEventListener('click', async () => {
  if (!document.querySelector('input[name=psrc]:checked')) {
    document.querySelector('input[name=psrc][value=saved]').checked = true;
  }
  const ids = currentLookIds();
  if (!ids.length) { alert('pick at least one garment in the look, or tap "Try it on" from the Suggest chat'); return; }
  const btn = $('tryon-btn'); btn.disabled = true; btn.textContent = 'Rendering…';
  try { await runTryon(ids, null, null); }
  finally { btn.disabled = false; btn.textContent = 'Try on'; }
});

// AI image editing is not ready yet — the chat edit feature is removed from the
// UI and shown as "coming soon". The backend (editor.py / /api/tryon/edit)
// stays in place for the future swap-in editor (EDITOR_ENGINE=fluxkontext).

// webcam person-source removed (2026-08-21) — picture-based sources only
// (Saved photo / Upload / Saved outfit).
