// suggest.js — weather + rule-based recommendation + Cher stylist chat (DeepSeek)

// ------------------------------------------------------------------ //
// Weather                                                              //
// ------------------------------------------------------------------ //
let _lastWeather = null;

async function loadWeather() {
  try {
    _lastWeather = await apiJson('/api/weather');
  } catch (e) { _lastWeather = null; }
}
loadWeather();

// Location is handled in the chat itself — ask Cher about another city/zip and
// the recommendation is built for that destination's live weather.

// ------------------------------------------------------------------ //
// Outfit recommendation → posted INTO the chat thread                  //
// ------------------------------------------------------------------ //
let _lastOutfit = null;
let _lastActivity = 'office';

function _garmentCard(g, slot) {
  const label = (slot === 'top' && (g.category === 'dress' || g.category === 'swimsuit'))
    ? 'One-piece' : { top: 'Top', bottom: 'Bottom', outerwear: 'Outer', footwear: 'Shoes', accessory: 'Acc' }[slot] || slot;
  const stars = g.rating ? `<span class="garment-rating">★ ${g.rating}/10</span>` : '';
  // show the garment photo when it has one; otherwise fall back to the color swatch
  const img = g.has_image
    ? `<img class="slot-swatch slot-img" data-gid="${g.id}" alt="${g.name}">`
    : `<div class="slot-swatch" style="background:${g.color_hex || '#555'}"></div>`;
  return `
    <div class="outfit-slot-card" data-slot="${slot}" data-id="${g.id}">
      <span class="slot-label">${label}</span>
      ${img}
      <div class="slot-info">
        <span class="slot-name">${g.name}</span>
        ${stars}
      </div>
    </div>`;
}

function _hasGarments(outfit) {
  return !!(outfit && Object.values(outfit).some(
    (v) => (Array.isArray(v) && v.length) || (v && typeof v === 'object')
  ));
}

function _goTryOn(outfit) {
  localStorage.setItem(RECO_KEY, JSON.stringify({
    outfit: outfit || _lastOutfit, activity: _lastActivity, prompt: $('prompt').value || '',
  }));
  location.href = '/tryon';
}

// Cher flags her MAIN picks with a machine-only [OUTFIT: "name", ...] line at
// the end of a reply — strip it from what the user sees.
function _stripOutfitMarker(text) {
  const i = text.indexOf('[OUTFIT:');
  return i === -1 ? text : text.slice(0, i).replace(/[ \t]+$/, '');
}

// Map Cher's marked main picks (by category) to an outfit for the Try-on page.
function _chatItemsToOutfit(items) {
  const FULL = ['dress', 'swimsuit', 'jumpsuit', 'overall'];
  const outfit = { top: null, bottom: null, outerwear: null, footwear: null, accessories: [] };
  for (const it of items || []) {
    const cat = it.category;
    const g = { id: it.id, name: it.name, category: cat };
    if (FULL.includes(cat)) outfit.top = g;                 // one-piece fills the top slot
    else if (cat === 'top') outfit.top = g;
    else if (cat === 'bottom') outfit.bottom = g;
    else if (cat === 'outerwear') outfit.outerwear = g;
    else if (cat === 'footwear') outfit.footwear = g;
    else outfit.accessories.push(g);
  }
  return outfit;
}

// Render Cher's marked main picks as garment photos inside a chat bubble,
// with a Try it on → button that sends the look to the Try-on page.
function _renderChatGarments(bubble, items) {
  if (!items || !items.length) return;
  let grid = bubble.querySelector('.recommend-outfit');
  if (!grid) {
    grid = document.createElement('div');
    grid.className = 'recommend-outfit chat-garments';
    bubble.appendChild(grid);
  }
  let html = '';
  for (const it of items) {
    html += `
      <div class="outfit-slot-card" data-id="${it.id}">
        <span class="slot-label">Wear</span>
        <img class="slot-swatch slot-img" data-gid="${it.id}" alt="${it.name}">
        <div class="slot-info"><span class="slot-name">${it.name}</span></div>
      </div>`;
  }
  grid.insertAdjacentHTML('beforeend', html);
  grid.querySelectorAll('.slot-img').forEach((img) => {
    if (img.src) return;
    authImageUrl('/api/wardrobe/' + img.dataset.gid + '/image')
      .then((u) => { img.src = u; }).catch(() => {});
  });
  // Try on the main picks right from the chat
  const btn = document.createElement('button');
  btn.className = 'recommend-tryon';
  btn.textContent = 'Try it on →';
  btn.addEventListener('click', () => _goTryOn(_chatItemsToOutfit(items)));
  bubble.appendChild(btn);
  _scrollChat();
}

// Render Cher's recommendation as ONE chat message: the intro prose carries the
// "why" (no separate reasons card — that duplicated the prose), the garment
// cards (photos) show the items inside the chat, and a Try it on action.
function _renderRecommendBubble({ intro, outfit }) {
  const wrap = document.createElement('div');
  wrap.className = 'chat-bubble assistant recommend-bubble';

  const text = document.createElement('div');
  text.className = 'bubble-text';
  text.textContent = intro || '';
  wrap.appendChild(text);

  if (_hasGarments(outfit)) {
    const grid = document.createElement('div');
    grid.className = 'recommend-outfit';
    const slots = [['top', 'top'], ['bottom', 'bottom'], ['outerwear', 'outerwear'], ['footwear', 'footwear']];
    let html = '';
    for (const [key, slot] of slots) {
      const g = outfit[key];
      if (g) html += _garmentCard(g, slot);
    }
    (outfit.accessories || []).forEach((g) => { html += _garmentCard(g, 'accessory'); });
    grid.innerHTML = html;
    wrap.appendChild(grid);
    // load the garment thumbnails (async; authImageUrl already cache-busts)
    grid.querySelectorAll('.slot-img').forEach((img) => {
      authImageUrl('/api/wardrobe/' + img.dataset.gid + '/image').then((u) => { img.src = u; }).catch(() => {});
    });
    // thumbs up/down under the photos — logs liked/disliked for the whole
    // outfit with the activity context (feeds L2 style + L3 ALS learning)
    const fb = document.createElement('div');
    fb.className = 'recommend-feedback';
    const fbBtn = (label, kind) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ghost'; b.textContent = label;
      b.addEventListener('click', async () => {
        fb.querySelectorAll('button').forEach((x) => (x.disabled = true));
        try {
          await apiJson('/api/recommend/feedback', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              outfit, kind, activity: _lastActivity || 'casual',
              prompt: ($('prompt') && $('prompt').value) || null,
            }),
          });
          fb.textContent = kind === 'liked'
            ? '👍 Got it — more like this' : '👎 Got it — will avoid this for ' + (_lastActivity || 'this');
        } catch (e) { fb.textContent = 'feedback failed: ' + e.message; }
      });
      return b;
    };
    fb.appendChild(fbBtn('👍 Looks good', 'liked'));
    fb.appendChild(fbBtn('👎 Not right', 'disliked'));
    wrap.appendChild(fb);
    // Try it on action below the thumbs
    const btn = document.createElement('button');
    btn.className = 'recommend-tryon';
    btn.textContent = 'Try it on →';
    btn.addEventListener('click', () => _goTryOn(outfit));
    wrap.appendChild(btn);
  }

  $('chat-messages').appendChild(wrap);
  _scrollChat();
  return wrap;
}

// Suggest outfit → recommendation posts into the chat as a Cher message.
async function suggestOutfit() {
  const btn = $('recommend-btn');
  btn.disabled = true;
  btn.textContent = '…';
  try {
    _lastActivity = $('activity').value;
    const data = await apiJson('/api/suggest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: _sessionId,
        activity: _lastActivity,
        prompt: $('prompt').value || null,
        owned_only: $('owned-only').checked,
      }),
    });
    _lastOutfit = data.outfit;
    _sessionId = data.session_id;
    sessionStorage.setItem('cher_session', _sessionId);

    // Weather/location is folded into Cher's intro text ("Based on San Mateo
    // weather of 87°F …") — the old #weather bar element was removed, so don't
    // try to write to it (would throw on a null node and mask the real outfit).
    _renderRecommendBubble({ intro: data.intro, outfit: data.outfit });
  } catch (e) {
    _addBubble('assistant', `⚠ Couldn't put together a look: ${e.message}`);
    _scrollChat();
  } finally {
    btn.disabled = false;
    btn.textContent = '✨ Suggest outfit';
  }
}
$('recommend-btn').addEventListener('click', suggestOutfit);

// ------------------------------------------------------------------ //
// Cher stylist chat                                                    //
// ------------------------------------------------------------------ //
let _sessionId = sessionStorage.getItem('cher_session') || null;
let _chatStreaming = false;

function _scrollChat() {
  const el = $('chat-messages');
  el.scrollTop = el.scrollHeight;
}

function _addBubble(role, text, id) {
  const wrap = document.createElement('div');
  wrap.className = `chat-bubble ${role}`;
  if (id) wrap.id = id;
  const span = document.createElement('span');
  span.className = 'bubble-text';
  span.textContent = text;
  wrap.appendChild(span);
  $('chat-messages').appendChild(wrap);
  _scrollChat();
  return wrap;
}

function _clearChat() { $('chat-messages').innerHTML = ''; }

function _introBubble(text) {
  const wrap = document.createElement('div');
  wrap.className = 'chat-bubble assistant intro-bubble';
  const span = document.createElement('span');
  span.className = 'bubble-text';
  span.innerHTML = text; // intro text is static/trusted
  wrap.appendChild(span);
  $('chat-messages').appendChild(wrap);
  _scrollChat();
}

// Render one persisted chat message (user / assistant / recommend card).
function renderMessage(msg) {
  if (!msg) return;
  if (msg.role === 'user') { _addBubble('user', msg.content || ''); return; }
  if (msg.role === 'assistant' && msg.kind === 'recommend' && msg.data) {
    _renderRecommendBubble({ intro: msg.content, outfit: msg.data.outfit });
    return;
  }
  const bubble = _addBubble('assistant', msg.content || '');
  if (msg.garments && msg.garments.length) _renderChatGarments(bubble, msg.garments);
}

// Reload the current conversation so the thread survives a page refresh.
async function restoreChat() {
  if (!_sessionId) return;
  try {
    const data = await apiJson(`/api/recommend/chat/${_sessionId}`);
    const msgs = data.messages || [];
    if (!msgs.length) return; // empty session → keep the intro bubble
    _clearChat();
    msgs.forEach(renderMessage);
  } catch (e) {
    // Stale / deleted session — start fresh
    _sessionId = null;
    sessionStorage.removeItem('cher_session');
  }
}

function _setStatus(txt) { $('chat-status').textContent = txt; }

async function sendChat(message) {
  if (_chatStreaming || !message.trim()) return;
  _chatStreaming = true;
  $('chat-send').disabled = true;
  _setStatus('Cher is typing…');

  _addBubble('user', message);

  // Create streaming bubble for assistant reply
  const assistantBubble = _addBubble('assistant', '');
  const textSpan = assistantBubble.querySelector('.bubble-text');
  const cursor = document.createElement('span');
  cursor.className = 'chat-cursor';
  cursor.textContent = '▋';
  textSpan.appendChild(cursor);

  // Build request body — include weather/outfit context only for new sessions
  const body = {
    message,
    session_id: _sessionId,
    activity: _lastActivity,
  };
  if (!_sessionId) {
    body.weather_ctx = _lastWeather ? {
      temp_c: _lastWeather.temp_c,
      feels_like_c: _lastWeather.feels_like_c,
      condition: _lastWeather.condition,
      wind_kph: _lastWeather.wind_kph,
      humidity: _lastWeather.humidity,
      uv_index: _lastWeather.uv_index,
    } : null;
    body.outfit_ctx = _lastOutfit || null;
  }

  try {
    const res = await api('/api/recommend/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await errMsg(res);
      textSpan.textContent = `⚠ Error: ${err}`;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let replyText = '';
    let hadError = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop(); // keep incomplete line
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        try {
          const evt = JSON.parse(raw);
          if (evt.type === 'token') {
            replyText += evt.content;
            // Replace cursor with text + cursor (keep cursor at end); hide the
            // machine-only [OUTFIT: ...] marker Cher appends at the end.
            textSpan.textContent = _stripOutfitMarker(replyText);
            textSpan.appendChild(cursor);
            _scrollChat();
          } else if (evt.type === 'recommend') {
            // user asked about a different city/zip → show a look built for it
            if (evt.outfit) {
              _lastOutfit = evt.outfit;
              if (evt.activity) _lastActivity = evt.activity;
              const card = _renderRecommendBubble({ intro: evt.intro, outfit: evt.outfit });
              assistantBubble.parentNode.insertBefore(card, assistantBubble);
            }
          } else if (evt.type === 'garments') {
            // Render Cher's main picks as garment photos in the bubble
            if (evt.items) _renderChatGarments(assistantBubble, evt.items);
          } else if (evt.type === 'error') {
            hadError = true;
            textSpan.textContent = `⚠ ${evt.message}`;
            cursor.remove();
          } else if (evt.type === 'done') {
            cursor.remove();
            // insurance: never leave an empty bubble if the model returned nothing
            if (!hadError && !_stripOutfitMarker(replyText).trim()) {
              textSpan.textContent = 'Hmm, I could not finish that — want to rephrase?';
            }
            if (evt.session_id) {
              _sessionId = evt.session_id;
              sessionStorage.setItem('cher_session', _sessionId);
            }
          }
        } catch (_) { /* ignore malformed SSE lines */ }
      }
    }
  } catch (e) {
    textSpan.textContent = `⚠ ${e.message}`;
    cursor.remove();
  } finally {
    _chatStreaming = false;
    $('chat-send').disabled = false;
    _setStatus('');
    _scrollChat();
  }
}

// Send on button click
$('chat-send').addEventListener('click', () => {
  const input = $('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';
  sendChat(msg);
});

// Send on Enter (Shift+Enter = newline)
$('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const msg = $('chat-input').value.trim();
    if (!msg) return;
    $('chat-input').value = '';
    $('chat-input').style.height = 'auto';
    sendChat(msg);
  }
});

// Auto-resize textarea
$('chat-input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// Clear conversation (start fresh)
$('chat-clear').addEventListener('click', async () => {
  if (_sessionId) {
    try {
      await api(`/api/recommend/chat/${_sessionId}`, { method: 'DELETE' });
    } catch (_) { /* ignore */ }
  }
  _sessionId = null;
  sessionStorage.removeItem('cher_session');
  _lastOutfit = null;
  _clearChat();
  _introBubble('Fresh start! Pick an occasion above and hit <strong>Suggest outfit</strong>, or just ask me anything about your wardrobe.');
});

// On load, restore the existing conversation (if any)
restoreChat();
