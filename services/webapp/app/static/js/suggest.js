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
    outfit: outfit || _lastOutfit, activity: _lastActivity, prompt: '',
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
    const it = items.find((x) => String(x.id) === img.dataset.gid);
    setAuthImage(img, (it && it.image_version)
      ? ('/api/wardrobe/' + it.id + '/image?size=thumb&v=' + it.image_version)
      : ('/api/wardrobe/' + img.dataset.gid + '/image?size=thumb&v=0'));
  });
  // every recommendation gets thumbs up/down + Try it on (same as the
  // Suggest-button card) so the engine learns from chat picks too
  _addFeedbackActions(bubble, _chatItemsToOutfit(items));
  _scrollChat();
}

// Shared action bar under EVERY recommendation: 👍/👎 (clean SVG icons) + Try it on.
// Feedback logs the whole outfit + activity so the engine learns.
const _THUMB_UP = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>';
const _THUMB_DOWN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>';
function _addFeedbackActions(container, outfit) {
  const actions = document.createElement('div');
  actions.className = 'recommend-actions';

  const fb = document.createElement('div');
  fb.className = 'recommend-feedback';

  const thumb = (icon, kind, title) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'fb-thumb';
    b.dataset.kind = kind;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.innerHTML = icon;
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
        b.disabled = false;
        b.classList.add('picked');
      } catch (e) {
        b.disabled = false;
        b.title = 'feedback failed';
      }
    });
    return b;
  };

  fb.appendChild(thumb(_THUMB_UP, 'liked', 'Looks good'));
  fb.appendChild(thumb(_THUMB_DOWN, 'disliked', 'Not right'));
  actions.appendChild(fb);

  const btn = document.createElement('button');
  btn.className = 'recommend-tryon';
  btn.textContent = 'Try it on →';
  btn.addEventListener('click', () => _goTryOn(outfit));
  actions.appendChild(btn);

  container.appendChild(actions);
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
    // load the garment thumbnails (parallel, cached, versioned)
    grid.querySelectorAll('.slot-img').forEach((img) => {
      const g = outfit.top && outfit.top.id === Number(img.dataset.gid) ? outfit.top :
        (outfit.bottom && outfit.bottom.id === Number(img.dataset.gid) ? outfit.bottom :
        (outfit.outerwear && outfit.outerwear.id === Number(img.dataset.gid) ? outfit.outerwear :
        (outfit.footwear && outfit.footwear.id === Number(img.dataset.gid) ? outfit.footwear :
        (outfit.accessories || []).find((x) => String(x.id) === img.dataset.gid)))));
      setAuthImage(img, g ? garmentImg(g, 'thumb')
                         : ('/api/wardrobe/' + img.dataset.gid + '/image?size=thumb&v=0'));
    });
    // one compact action row: thumbs + Try it on (like a chat action bar)
    _addFeedbackActions(wrap, outfit);
  }

  $('chat-messages').appendChild(wrap);
  _scrollChat();
  return wrap;
}

// Suggested-prompt chips live INSIDE the scrollable chat (Open-WebUI style) —
// no static compose bar eating screen space. They show on a fresh conversation
// and are REMOVED once the first message/recommendation exists (no need to
// keep offering the occasion pills mid-conversation).
const _SUGGEST_CHIPS = [
  ['office', 'Office / work'],
  ['casual', 'Casual'],
  ['date', 'Date / dinner'],
  ['hiking', 'Hiking'],
  ['beach', 'Beach'],
  ['formal', 'Formal'],
];

let _conversationStarted = false;

function _appendSuggestionChips() {
  const msgs = $('chat-messages');
  if (!msgs) return;
  // once a conversation has started the pills are not needed — remove any
  // leftover row and don't re-append
  if (_conversationStarted) {
    const old = msgs.querySelector('.suggest-chips');
    if (old) old.remove();
    return;
  }
  const old = msgs.querySelector('.suggest-chips');
  if (old) old.remove();
  const row = document.createElement('div');
  row.className = 'suggest-chips';
  for (const [act, label] of _SUGGEST_CHIPS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'suggest-chip';
    b.dataset.activity = act;
    b.textContent = label;
    b.addEventListener('click', () => suggestOutfit(act));
    row.appendChild(b);
  }
  msgs.appendChild(row);
  _scrollChat();
}

// Suggest outfit → recommendation posts into the chat as a Cher message.
async function suggestOutfit(activity) {
  activity = activity || _lastActivity || 'office';
  _lastActivity = activity;
  try {
    const data = await apiJson('/api/suggest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: _sessionId,
        activity,
        prompt: null,
        owned_only: true,
      }),
    });
    _lastOutfit = data.outfit;
    _sessionId = data.session_id;
    sessionStorage.setItem('cher_session', _sessionId);

    // Weather/location is folded into Cher's intro text ("Based on San Mateo
    // weather of 87°F …") — the old #weather bar element was removed, so don't
    // try to write to it (would throw on a null node and mask the real outfit).
    _renderRecommendBubble({ intro: data.intro, outfit: data.outfit });
    _conversationStarted = true;
  } catch (e) {
    _addBubble('assistant', `⚠ Couldn't put together a look: ${e.message}`);
    _scrollChat();
  } finally {
    _appendSuggestionChips();
  }
}

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
    _conversationStarted = true; // restored thread → occasion pills not needed
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
  _conversationStarted = true;
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
    _appendSuggestionChips();
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
  _conversationStarted = false;
  _clearChat();
  _introBubble('Hi! I\'m Cher, your personal stylist. Tap an occasion below or just ask me anything — I\'ll put together a look right here.');
  _appendSuggestionChips();
});

// On load, restore the existing conversation (if any) — then always surface
// the occasion chips so a fresh look is one tap away.
(async () => { await restoreChat(); _appendSuggestionChips(); })();
