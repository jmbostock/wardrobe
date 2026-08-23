// suggest.js — weather + rule-based recommendation + Cher stylist chat (DeepSeek)

// ------------------------------------------------------------------ //
// Weather                                                              //
// ------------------------------------------------------------------ //
let _lastWeather = null;

async function loadWeather() {
  try {
    const w = await apiJson('/api/weather');
    _lastWeather = w;
    $('weather').innerHTML =
      `<b>${w.temp_f}°F</b>` +
      `<span class="cond">feels ${w.feels_like_f}°F</span>` +
      `<span class="cond">${w.condition}</span>` +
      `<span class="cond">wind ${w.wind_kph} km/h</span>` +
      `<span class="cond">humidity ${w.humidity}%</span>`;
    const acct = await apiJson('/api/account');
    $('weather-loc').textContent =
      'location: ' + (acct.location.label || `custom (${acct.location.lat}, ${acct.location.lon})`);
  } catch (e) {
    $('weather').innerHTML = '<span class="muted">weather unavailable</span>';
  }
}
loadWeather();

async function saveLocationFrom(inputId, statusId) {
  const status = $(statusId);
  status.textContent = 'resolving…';
  try {
    const r = await apiJson('/api/account/location', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location: $(inputId).value }),
    });
    status.textContent = `saved: ${r.location.name}${r.location.country ? ', ' + r.location.country : ''}`;
    loadWeather();
  } catch (e) { status.textContent = e.message; }
}
$('outfit-loc-save').addEventListener('click', () => saveLocationFrom('outfit-loc', 'outfit-loc-status'));

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

// Render Cher's recommendation as a rich bubble inside the chat:
// intro text + garment cards (photos) + reasoning + a Try it on action.
function _renderRecommendBubble({ intro, outfit, reasoning, activity }) {
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
    // Try it on action on the card itself
    const btn = document.createElement('button');
    btn.className = 'recommend-tryon';
    btn.textContent = 'Try it on →';
    btn.addEventListener('click', () => _goTryOn(outfit));
    wrap.appendChild(btn);
  }

  const reasons = (reasoning || []).filter(Boolean);
  if (reasons.length) {
    const why = document.createElement('div');
    why.className = 'recommend-why';
    const h = document.createElement('h4'); h.textContent = 'Why this outfit'; why.appendChild(h);
    const ul = document.createElement('ul');
    reasons.forEach((r) => { const li = document.createElement('li'); li.textContent = r; ul.appendChild(li); });
    why.appendChild(ul);
    wrap.appendChild(why);
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

    _renderRecommendBubble({
      intro: data.intro,
      outfit: data.outfit,
      reasoning: data.reasoning || [],
      activity: data.activity,
    });

    // Update the weather bar with what was actually used
    const w = data.weather_used;
    if (w && w.temp_f != null) {
      $('weather').innerHTML =
        `<b>${w.temp_f}°F</b><span class="cond">feels ${w.feels_like_f}°F</span><span class="cond">${w.condition}</span>`;
    }
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
    _renderRecommendBubble({
      intro: msg.content,
      outfit: msg.data.outfit,
      reasoning: msg.data.reasoning || [],
      activity: msg.data.activity,
    });
    return;
  }
  _addBubble('assistant', msg.content || '');
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
            // Replace cursor with text + cursor (keep cursor at end)
            textSpan.textContent = replyText;
            textSpan.appendChild(cursor);
            _scrollChat();
          } else if (evt.type === 'error') {
            textSpan.textContent = `⚠ ${evt.message}`;
            cursor.remove();
          } else if (evt.type === 'done') {
            cursor.remove();
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
