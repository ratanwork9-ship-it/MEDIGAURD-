const $ = s => document.querySelector(s);
const state = { minutes: 60, limits: null, points: [], formFilled: false };

const num = (v, d = 1) => (v == null || isNaN(v)) ? '–' : Number(v).toFixed(d);
const clock = (ts, withDay) => {
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return withDay ? d.toLocaleDateString([], { weekday: 'short' }) + ' ' + hm : hm;
};
const ago = s => s < 5 ? 'just now' : s < 60 ? s + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago'
  : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
const dur = s => s < 60 ? s + ' s' : s < 3600 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h';

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toast.h);
  toast.h = setTimeout(() => t.className = '', 2600);
}

/* ---------- current readings ---------- */
function setGauge(id, value, lo, hi, dec, bad) {
  const g = $(id);
  const span = hi - lo, dmin = lo - span * 0.75, dmax = hi + span * 0.75;
  const pos = v => Math.max(0, Math.min(100, (v - dmin) / (dmax - dmin) * 100));
  const band = g.querySelector('.band');
  band.style.left = pos(lo) + '%';
  band.style.width = (pos(hi) - pos(lo)) + '%';
  const m = g.querySelector('.marker');
  m.style.left = (value == null ? 50 : pos(value)) + '%';
  m.style.visibility = value == null ? 'hidden' : 'visible';
  m.classList.toggle('bad', !!bad);
  const l = g.querySelector('.lo'), h = g.querySelector('.hi');
  l.textContent = num(lo, dec); l.style.left = pos(lo) + '%';
  h.textContent = num(hi, dec); h.style.left = pos(hi) + '%';
}

function renderLatest(d) {
  state.limits = d.limits;
  const L = d.limits, r = d.reading, body = document.body;
  if (!r) {
    body.dataset.state = 'empty';
    $('#headline').textContent = 'Waiting for the first reading.';
    $('#subline').textContent = 'Power on the ESP32 and check that it can reach this server.';
    $('#connText').textContent = 'No data yet';
    $('#tempVal').textContent = $('#humVal').textContent = '–';
    setGauge('#tempGauge', null, L.temp_min, L.temp_max, 1);
    setGauge('#humGauge', null, L.hum_min, L.hum_max, 0);
    return;
  }
  const s = d.status, issues = [];
  if (s.temperature === 'high') issues.push(`too warm at ${num(r.temperature)} °C`);
  if (s.temperature === 'low')  issues.push(`too cold at ${num(r.temperature)} °C`);
  if (s.humidity === 'high')    issues.push(`too humid at ${num(r.humidity, 0)}%`);
  if (s.humidity === 'low')     issues.push(`too dry at ${num(r.humidity, 0)}%`);

  const chamber = r.cooling ? 'Cooling airflow is active.' : 'Cooling airflow is inactive.';
  if (!d.online) {
    body.dataset.state = 'offline';
    $('#headline').textContent = 'Mediguard has stopped reporting.';
    $('#subline').textContent = `Last reading ${ago(d.age)}. Check the ESP32's power and Wi-Fi.`;
    $('#connText').textContent = `Offline, last seen ${ago(d.age)}`;
  } else {
    body.dataset.state = issues.length ? 'bad' : 'ok';
    $('#headline').textContent = issues.length ? `The box is ${issues.join(' and ')}.` : 'Readings are within the configured limits.';
    $('#subline').textContent = chamber;
    $('#connText').textContent = `Live, updated ${ago(d.age)}`;
  }
  $('#tempVal').textContent = num(r.temperature);
  $('#humVal').textContent = num(r.humidity, 0);
  setGauge('#tempGauge', r.temperature, L.temp_min, L.temp_max, 1, s.temperature !== 'ok');
  setGauge('#humGauge', r.humidity, L.hum_min, L.hum_max, 0, s.humidity !== 'ok');
}

/* ---------- summary ---------- */
function renderStats(s) {
  const el = $('#summary');
  if (!s.count) { el.innerHTML = ''; return; }
  const t = s.temperature, h = s.humidity;
  el.innerHTML = `
    <div><dt>Within limits</dt><dd>${num(s.in_range_pct, 0)}%<span>of ${s.count.toLocaleString()} readings</span></dd></div>
    <div><dt>Temperature</dt><dd>${num(t.min)} to ${num(t.max)} °C<span>average ${num(t.avg)} °C</span></dd></div>
    <div><dt>Humidity</dt><dd>${num(h.min, 0)} to ${num(h.max, 0)}%<span>average ${num(h.avg, 0)}%</span></dd></div>
    <div><dt>Cooling active</dt><dd>${num(s.cooling_pct, 0)}%<span>of recorded time</span></dd></div>`;
}

/* ---------- alerts ---------- */
const ALERT_NAMES = { temperature: { high: 'Too warm', low: 'Too cold' }, humidity: { high: 'Too humid', low: 'Too dry' } };
function renderAlerts(list) {
  const ul = $('#alerts');
  if (!list.length) { ul.innerHTML = '<li class="empty">No alerts so far. Readings have stayed within your limits.</li>'; return; }
  ul.innerHTML = list.map(a => {
    const val = a.metric === 'temperature' ? num(a.value, 1) + ' °C' : num(a.value, 0) + '%';
    const end = a.resolved_ts ? `Back in range after ${dur(a.resolved_ts - a.ts)}.` : 'Still out of range.';
    return `<li class="${a.resolved_ts ? 'done' : 'open'}"><strong>${ALERT_NAMES[a.metric][a.direction]}</strong><span>${val} at ${clock(a.ts, true)}. ${end}</span></li>`;
  }).join('');
}

/* ---------- charts ---------- */
function drawChart(host, key, lo, hi, unit, dec) {
  const pts = state.points.filter(p => p[key] != null);
  host.innerHTML = '';
  if (pts.length < 2) { host.innerHTML = '<p class="empty">Not enough readings in this range yet.</p>'; return; }

  const W = Math.max(280, host.clientWidth), H = 220, m = { l: 42, r: 10, t: 8, b: 24 };
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const vals = pts.map(p => p[key]);
  let ymin = Math.min(lo, ...vals), ymax = Math.max(hi, ...vals);
  const pad = (ymax - ymin) * 0.12; ymin -= pad; ymax += pad;
  const x = t => m.l + (t - t0) / ((t1 - t0) || 1) * (W - m.l - m.r);
  const y = v => m.t + (1 - (v - ymin) / (ymax - ymin)) * (H - m.t - m.b);

  // break the line where the device was silent
  const dts = pts.slice(1).map((p, i) => p.t - pts[i].t).sort((a, b) => a - b);
  const gap = Math.max(60, 5 * dts[Math.floor(dts.length / 2)]);
  let d = '', prev = null;
  pts.forEach(p => { d += (prev === null || p.t - prev > gap ? 'M' : 'L') + x(p.t).toFixed(1) + ' ' + y(p[key]).toFixed(1) + ' '; prev = p.t; });

  // ice-chamber-open spans
  let spans = '', s0 = null;
  pts.forEach(p => {
    if (p.cooling > 0) { if (s0 === null) s0 = p.t; }
    else if (s0 !== null) { spans += `<rect x="${x(s0)}" y="${m.t}" width="${Math.max(2, x(p.t) - x(s0))}" height="${H - m.t - m.b}" fill="var(--ice)" opacity=".7"/>`; s0 = null; }
  });
  if (s0 !== null) spans += `<rect x="${x(s0)}" y="${m.t}" width="${Math.max(2, x(t1) - x(s0))}" height="${H - m.t - m.b}" fill="var(--ice)" opacity=".7"/>`;

  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const v = ymin + (ymax - ymin) * i / 4;
    grid += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(dec)}</text>`;
  }
  const withDay = state.minutes === 0 || state.minutes >= 1440;
  let xl = '';
  for (let i = 0; i <= 3; i++) {
    const t = t0 + (t1 - t0) * i / 3;
    xl += `<text x="${x(t)}" y="${H - 6}" text-anchor="${i === 0 ? 'start' : i === 3 ? 'end' : 'middle'}">${clock(Math.round(t), withDay)}</text>`;
  }
  const band = `<rect x="${m.l}" y="${y(hi)}" width="${W - m.l - m.r}" height="${y(lo) - y(hi)}" fill="var(--ok-soft)" opacity=".7"/>
    <line x1="${m.l}" x2="${W - m.r}" y1="${y(hi)}" y2="${y(hi)}" stroke="var(--ok)" stroke-dasharray="4 3"/>
    <line x1="${m.l}" x2="${W - m.r}" y1="${y(lo)}" y2="${y(lo)}" stroke="var(--ok)" stroke-dasharray="4 3"/>`;

  host.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${key} over time">
    ${band}${spans}${grid}${xl}
    <path d="${d}" fill="none" stroke="var(--ink)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <line class="cross" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" visibility="hidden"/>
    <circle class="pt" r="4.5" fill="var(--ink)" stroke="#fff" stroke-width="2" visibility="hidden"/>
  </svg><div class="tip" hidden></div>`;

  const svg = host.querySelector('svg'), tip = host.querySelector('.tip');
  const cross = svg.querySelector('.cross'), dot = svg.querySelector('.pt');
  const hide = () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); };
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('pointermove', ev => {
    const mx = ev.clientX - svg.getBoundingClientRect().left;
    const t = t0 + (mx - m.l) / (W - m.l - m.r) * (t1 - t0);
    let best = pts[0], bd = Infinity;
    for (const p of pts) { const dd = Math.abs(p.t - t); if (dd < bd) { bd = dd; best = p; } }
    const bx = x(best.t), by = y(best[key]);
    cross.setAttribute('x1', bx); cross.setAttribute('x2', bx); cross.setAttribute('visibility', 'visible');
    dot.setAttribute('cx', bx); dot.setAttribute('cy', by); dot.setAttribute('visibility', 'visible');
    tip.hidden = false;
    tip.innerHTML = `<strong>${best[key].toFixed(dec)} ${unit}</strong><br>${clock(best.t, true)}${best.cooling ? '<br>Cooling active' : ''}`;
    tip.style.left = Math.max(0, Math.min(W - tip.offsetWidth, bx + 12)) + 'px';
  });
}

function drawCharts() {
  const L = state.limits;
  if (!L) return;
  drawChart($('#tempChart'), 'temperature', L.temp_min, L.temp_max, '°C', 1);
  drawChart($('#humChart'), 'humidity', L.hum_min, L.hum_max, '%', 0);
}

/* ---------- data loop ---------- */
async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(r.status); return r.json(); }

function fillForm(L) {
  for (const k of ['temp_min', 'temp_max', 'hum_min', 'hum_max']) $('#' + k).value = L[k];
  state.formFilled = true;
}

async function refresh() {
  try {
    const q = state.minutes;
    const [latest, series, alerts, stats] = await Promise.all([
      getJSON('/api/latest'), getJSON('/api/readings?minutes=' + q),
      getJSON('/api/alerts?limit=15'), getJSON('/api/stats?minutes=' + q)]);
    renderLatest(latest);
    state.points = series.readings;
    drawCharts();
    renderStats(stats);
    renderAlerts(alerts.alerts);
    if (!state.formFilled) fillForm(latest.limits);
  } catch (e) {
    document.body.dataset.state = 'offline';
    $('#headline').textContent = 'Cannot reach the Mediguard server.';
    $('#subline').textContent = 'Check that app.py is still running on this computer.';
    $('#connText').textContent = 'Server unreachable';
  }
}

document.querySelectorAll('.ranges button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.ranges button').forEach(x => x.setAttribute('aria-pressed', 'false'));
  b.setAttribute('aria-pressed', 'true');
  state.minutes = +b.dataset.min;
  $('#exportRange').href = '/api/export.csv?minutes=' + state.minutes;
  refresh();
}));
$('#exportRange').href = '/api/export.csv?minutes=' + state.minutes;

$('#limitsForm').addEventListener('submit', async e => {
  e.preventDefault();
  const v = k => parseFloat($('#' + k).value);
  const body = { temp_min: v('temp_min'), temp_max: v('temp_max'), hum_min: v('hum_min'), hum_max: v('hum_max') };
  try {
    const r = await fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Could not save the limits.');
    state.limits = j.limits;
    toast('Limits saved.');
    refresh();
  } catch (err) { toast(err.message, true); }
});

let resizeT;
addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(drawCharts, 150); });

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
