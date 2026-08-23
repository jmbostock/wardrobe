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
// Outfit recommendation                                               //
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

function renderOutfit(outfit) {
  const slots = [['top', 'top'], ['bottom', 'bottom'], ['outerwear', 'outerwear'], ['footwear', 'footwear']];
  let html = '';
  for (const [key, slot] of slots) {
    const g = outfit[key];
    if (g) html += _garmentCard(g, slot);
  }
  (outfit.accessories || []).forEach((g) => { html += _garmentCard(g, 'accessory'); });
  $('outfit').innerHTML = html || '<p class="muted">No matching outfit found.</p>';
  // load the garment thumbnails (async; authImageUrl already cache-busts)
  $('outfit').querySelectorAll('.slot-img').forEach((img) => {
    authImageUrl('/api/wardrobe/' + img.dataset.gid + '/image').then((u) => { img.src = u; }).catch(() => {});
  });
  $('outfit-result').hidden = false;
  $('outfit-empty').hidden = true;
  $('try-on-btn').hidden = false;
}

$('recommend-btn').addEventListener('click', async () => {
  const btn = $('recommend-btn');
  btn.disabled = true;
  btn.textContent = '…';
  try {
    _lastActivity = $('activity').value;
    const data = await apiJson('/api/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        activity: _lastActivity,
        prompt: $('prompt').value || null,
        owned_only: $('owned-only').checked,
      }),
    });
    _lastOutfit = data.outfit;
    if (Object.values(data.outfit).every((v) => !v || (Array.isArray(v) && !v.length))) {
      $('outfit-result').hidden = true;
      $('outfit-empty').hidden = false;
    } else {
      renderOutfit(data.outfit);
    }
    // Update weather display with what was actually used
    const w = data.weather_used;
    $('weather').innerHTML =
      `<b>${w.temp_f}°F</b><span class="cond">feels ${w.feels_like_f}°F</span><span class="cond">${w.condition}</span>`;

    // Remember for Try-on page
    localStorage.setItem(RECO_KEY, JSON.stringify({
      outfit: data.outfit, activity: _lastActivity, prompt: $('prompt').value || '',
    }));

    // Seed a fresh chat session with this outfit context
    _startNewSession(data);
  } catch (e) {
    alert('recommend failed: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = '✨ Suggest outfit';
  }
});

// Try-on button: pre-load garment IDs into localStorage and navigate
$('try-on-btn').addEventListener('click', () => {
  if (_lastOutfit) {
    localStorage.setItem(RECO_KEY, JSON.stringify({
      outfit: _lastOutfit, activity: _lastActivity, prompt: $('prompt').value || '',
    }));
  }
  location.href = '/tryon';
});

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

function _startNewSession(recommendData) {
  // Clears history UI and resets session; primes Cher with context
  _sessionId = null;
  sessionStorage.removeItem('cher_session');
  // Clear old messages except the intro bubble
  const msgs = $('chat-messages');
  while (msgs.children.length > 1) msgs.removeChild(msgs.lastChild);

  // Auto-greet with the new outfit
  const names = [];
  const o = recommendData?.outfit || {};
  for (const slot of ['top', 'bottom', 'outerwear', 'footwear']) {
    if (o[slot]) names.push(o[slot].name);
  }
  const greeting = names.length
    ? `I've picked ${names.slice(0, 3).join(', ')}${names.length > 3 ? '…' : ''} for you. Ask me to adjust anything — occasion, color, warmth, vibe.`
    : `I've updated your outfit suggestion. What would you like to tweak?`;

  _addBubble('assistant', greeting);
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

// Clear conversation
$('chat-clear').addEventListener('click', async () => {
  if (_sessionId) {
    try {
      await api(`/api/recommend/chat/${_sessionId}`, { method: 'DELETE' });
    } catch (_) { /* ignore */ }
  }
  _sessionId = null;
  sessionStorage.removeItem('cher_session');
  const msgs = $('chat-messages');
  while (msgs.children.length > 1) msgs.removeChild(msgs.lastChild);
  _addBubble('assistant', 'Fresh start! Ask me anything about your wardrobe.');
});
