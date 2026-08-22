// suggest page — weather + rule-based recommendation.
async function loadWeather() {
  try {
    const w = await apiJson('/api/weather');
    $('weather').innerHTML =
      `<b>${w.temp_f}°F</b>` +
      `<span class="cond">feels ${w.feels_like_f}°F</span>` +
      `<span class="cond">${w.condition}</span>` +
      `<span class="cond">wind ${w.wind_kph} km/h</span>` +
      `<span class="cond">humidity ${w.humidity}%</span>`;
    const acct = await apiJson('/api/account');
    $('weather-loc').textContent = 'location: ' + (acct.location.label || `custom (${acct.location.lat}, ${acct.location.lon})`);
  } catch (e) { $('weather').innerHTML = '<span class="muted">weather unavailable</span>'; }
}
loadWeather();

async function saveLocationFrom(inputId, statusId) {
  const status = $(statusId); status.textContent = 'resolving…';
  try {
    const r = await apiJson('/api/account/location', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location: $(inputId).value }),
    });
    status.textContent = `saved: ${r.location.name}${r.location.country ? ', ' + r.location.country : ''}`;
    loadWeather();
  } catch (e) { status.textContent = e.message; }
}
$('outfit-loc-save').addEventListener('click', () => saveLocationFrom('outfit-loc', 'outfit-loc-status'));

// ---------- recommend ----------
function renderOutfit(outfit) {
  const order = [['top', 'Top'], ['bottom', 'Bottom'], ['outerwear', 'Outer'], ['footwear', 'Shoes']];
  let html = '';
  for (const [key, label] of order) {
    const g = outfit[key]; if (!g) continue;
    html += `<div class="garment"><span class="role">${label}</span>` +
            `<span class="swatch" style="background:${g.color_hex || '#888'}"></span>` +
            `<span class="name">${g.name}</span></div>`;
  }
  (outfit.accessories || []).forEach((g) => {
    html += `<div class="garment"><span class="role">Acc</span>` +
            `<span class="swatch" style="background:${g.color_hex || '#888'}"></span>` +
            `<span class="name">${g.name}</span></div>`;
  });
  $('outfit').innerHTML = html || '<p class="muted">no outfit — wardrobe empty?</p>';
}

$('recommend-btn').addEventListener('click', async () => {
  const btn = $('recommend-btn'); btn.disabled = true;
  try {
    const data = await apiJson('/api/recommend', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ activity: $('activity').value, prompt: $('prompt').value || null, owned_only: $('owned-only').checked }),
    });
    renderOutfit(data.outfit);
    $('reasons').innerHTML = (data.reasoning || []).map((r) => `<li>${r}</li>`).join('');
    $('weather').innerHTML =
      `<b>${data.weather_used.temp_f}°F</b>` +
      `<span class="cond">feels ${data.weather_used.feels_like_f}°F</span>` +
      `<span class="cond">${data.weather_used.condition}</span>`;
    // remember it for the Try on page ("Use recommendation")
    localStorage.setItem(RECO_KEY, JSON.stringify({
      outfit: data.outfit,
      activity: $('activity').value,
      prompt: $('prompt').value || '',
    }));
  } catch (e) { alert('recommend failed: ' + e); }
  finally { btn.disabled = false; }
});
