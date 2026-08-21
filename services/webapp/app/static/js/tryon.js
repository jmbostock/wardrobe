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
let savedPhotos = [];       // cached /api/photos (saved-photo source for look-based try-on)
let savedOutfits = [];      // cached /api/outfits that have a render (Saved-image source)
let selectedSaved = null;   // the chosen saved outfit render

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

// ---------- look builder ----------
async function populateLook() {
  let items = [];
  try { items = await apiJson('/api/wardrobe'); } catch (e) { /* ignore */ }
  const withImg = items.filter((g) => g.has_image);
  for (const role of ['top', 'bottom', 'dress']) {
    const sel = document.querySelector('.look-select[data-role="' + role + '"]');
    if (!sel) continue;
    const prev = sel.value;
    sel.innerHTML = '<option value="">— none —</option>';
    withImg.filter((g) => g.category === role).forEach((g) => sel.add(new Option(g.name, g.id)));
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
  }
}
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
  if (top && top.category === 'dress') {
    setRole('dress', top); setRole('top', null); setRole('bottom', null);
  } else {
    setRole('top', top); setRole('bottom', outfit.bottom || null); setRole('dress', null);
  }
}
function currentLookIds() {
  const ids = [];
  for (const role of ['top', 'bottom', 'dress']) {
    const v = document.querySelector('.look-select[data-role="' + role + '"]').value;
    if (v) ids.push(Number(v));
  }
  return ids;
}

$('tryon-use-reco').addEventListener('click', async () => {
  const status = $('look-status');
  status.textContent = '…';
  let outfit = null;
  try { outfit = JSON.parse(localStorage.getItem(RECO_KEY) || 'null')?.outfit || null; } catch (e) { /* ignore */ }
  if (!outfit) {
    try {
      const data = await apiJson('/api/recommend', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ activity: 'office', prompt: null }),
      });
      outfit = data.outfit;
    } catch (e) { status.textContent = 'recommend failed: ' + e.message; return; }
  }
  applyOutfitToLook(outfit);
  status.textContent = 'look set from recommendation — tweak if you like';
});
$('tryon-reset').addEventListener('click', () => {
  ['top', 'bottom', 'dress'].forEach((role) => {
    document.querySelector('.look-select[data-role="' + role + '"]').value = '';
  });
  $('look-status').textContent = 'look cleared';
});
$('tryon-save').addEventListener('click', async () => {
  const ids = currentLookIds();
  if (!ids.length) { alert('pick at least one garment in the look first'); return; }
  const name = ($('outfit-name').value || '').trim() || ('Outfit ' + new Date().toLocaleDateString());
  const result_url = (lastResultLook === JSON.stringify(ids)) ? (lastResultUrl || '') : '';
  try {
    await apiJson('/api/outfits', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, garment_ids: ids, result_url }),
    });
    $('outfit-name').value = '';
    $('look-status').textContent = 'saved "' + name + '"';
    toast('outfit saved — see the Outfits page');
    loadSavedImages();
  } catch (e) { alert(e.message); }
});

// ---------- person source ----------
document.querySelectorAll('input[name=psrc]').forEach((r) => r.addEventListener('change', () => {
  const src = document.querySelector('input[name=psrc]:checked').value;
  $('saved-photo').hidden = src !== 'saved';
  $('person-file').hidden = src !== 'upload';
  $('webcam-row').hidden = src !== 'webcam';
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
}));
$('saved-photo').addEventListener('change', checkPersonImage);
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
    || document.querySelector('.look-select[data-role=dress]')?.value
    || document.querySelector('.look-select[data-role=bottom]')?.value);
  if (!sel) { el.hidden = true; return; }
  const fd = new FormData(); fd.append('kind', 'garment'); fd.append('garment_id', sel);
  try { renderQa(el, await apiJson('/api/image-quality', { method: 'POST', body: fd })); }
  catch (e) { el.hidden = true; }
}
document.querySelectorAll('.look-select').forEach((s) => s.addEventListener('change', checkGarmentImage));

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
loadSavedPhotos(); loadSavedImages(); populateLook();

// ---------- try on (shared by the Try on button and the chat bar) ----------
async function runTryon(ids, baseResult, prompt) {
  const fd = new FormData();
  fd.append('garment_ids', JSON.stringify(ids));
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
    const url = await authImageUrl(data.result_url);
    $('result').innerHTML = '';
    const img = document.createElement('img'); img.src = url; $('result').appendChild(img);
    // before/after only makes sense when altering (chat re-render / saved-image
    // refine). A plain try-on shows just the new render — never the original.
    if (baseResult) showCompare(baseUrl, url);
    else hideCompare();
    // show + re-enable the chat bar so the user can refine the image
    $('chat-bar').hidden = false;
    $('chat-input').disabled = false;
    $('chat-note').textContent = prompt ? 're-rendered with: “' + prompt + '”' : '';
    toast(baseResult ? 'updated — check the result' : 'try-on ready');
  } catch (e) { $('result').innerHTML = `<p class="muted">error: ${e}</p>`; }
  finally { clearInterval(tryonInt); tryonInt = null; }
}

$('tryon-btn').addEventListener('click', async () => {
  if (!document.querySelector('input[name=psrc]:checked')) {
    document.querySelector('input[name=psrc][value=saved]').checked = true;
  }
  const ids = currentLookIds();
  if (!ids.length) { alert('pick at least one garment in the look, or click "Use recommendation"'); return; }
  const btn = $('tryon-btn'); btn.disabled = true; btn.textContent = 'Rendering…';
  try { await runTryon(ids, null, null); }
  finally { btn.disabled = false; btn.textContent = 'Try on'; }
});

// ---------- chat feedback (Enter edits the last image via the editor) ----------
async function runEdit(baseUrl, prompt) {
  const fd = new FormData();
  fd.append('base_result', baseUrl);
  fd.append('prompt', prompt);
  const box = document.createElement('div'); box.className = 'progress';
  const spinner = document.createElement('div'); spinner.className = 'spinner';
  const txt = document.createElement('div');
  const stage = document.createElement('div'); stage.className = 'status';
  stage.textContent = 'Editing image…';
  const hint = document.createElement('div'); hint.className = 'hint';
  hint.textContent = 'applying your note with InstructPix2Pix — usually just a few seconds';
  const timer = document.createElement('div'); timer.className = 'timer'; timer.textContent = '0s';
  txt.appendChild(stage); txt.appendChild(hint);
  box.appendChild(spinner); box.appendChild(txt); box.appendChild(timer);
  $('result').innerHTML = ''; $('result').appendChild(box);
  const started = Date.now();
  clearInterval(tryonInt);
  tryonInt = setInterval(() => { timer.textContent = Math.round((Date.now() - started) / 1000) + 's'; }, 1000);
  try {
    const res = await api('/api/tryon/edit', { method: 'POST', body: fd });
    if (!res.ok) { $('result').innerHTML = `<p class="muted">edit failed: ${await errMsg(res)}</p>`; return; }
    const data = await res.json();
    lastResultUrl = data.result_url;
    const url = await authImageUrl(data.result_url);
    $('result').innerHTML = '';
    const img = document.createElement('img'); img.src = url; $('result').appendChild(img);
    // before/after: original (base) next to the edited render
    showCompare(baseUrl, url);
    $('chat-bar').hidden = false;
    $('chat-input').disabled = false;
    $('chat-note').textContent = 'edited with: “' + prompt + '”';
    toast('edited — check the result');
  } catch (e) { $('result').innerHTML = `<p class="muted">error: ${e}</p>`; }
  finally { clearInterval(tryonInt); tryonInt = null; }
}

async function sendChat() {
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) return;
  const src = document.querySelector('input[name=psrc]:checked')?.value || 'saved';
  const base = lastResultUrl || (src === 'savedimg' && selectedSaved ? selectedSaved.result_url : '');
  if (!base) { toast('generate a try-on first, or pick a saved outfit image'); return; }
  input.disabled = true;
  try { await runEdit(base, text); }
  catch (e) { /* runEdit already shows errors */ }
  finally { input.value = ''; input.disabled = false; }
}
$('chat-send').addEventListener('click', sendChat);
$('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); sendChat(); }
});

// ---------- webcam ----------
let stream = null;
$('webcam-btn').addEventListener('click', async () => {
  const video = $('webcam');
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; video.hidden = true; $('webcam-btn').textContent = 'Start webcam'; return; }
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } });
    video.srcObject = stream; video.hidden = false; await video.play();
    $('webcam-btn').textContent = 'Stop webcam';
  } catch (e) { alert('webcam unavailable: ' + e); }
});
$('webcam').addEventListener('click', () => {
  if (!stream) return;
  const canvas = document.createElement('canvas');
  const MAX = 1024;
  const scale = Math.min(MAX / $('webcam').videoWidth, 1);
  canvas.width = Math.round($('webcam').videoWidth * scale);
  canvas.height = Math.round($('webcam').videoHeight * scale);
  canvas.getContext('2d').drawImage($('webcam'), 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    const dt = new DataTransfer();
    dt.items.add(new File([blob], 'webcam.jpg', { type: 'image/jpeg' }));
    $('person-file').files = dt.files;
    document.querySelector('input[name=psrc][value=upload]').checked = true;
    $('saved-photo').hidden = true; $('person-file').hidden = false; $('webcam-row').hidden = true;
    stream.getTracks().forEach((t) => t.stop()); stream = null; $('webcam').hidden = true;
    $('webcam-btn').textContent = 'Start webcam';
    toast('frame captured — pick a garment and try it on');
    checkPersonImage();
  }, 'image/jpeg', 0.92);
});
