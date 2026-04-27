/**
 * Scanner AI — Frontend App
 * Connects to SSE stream, renders call cards, handles filters and alerts.
 */

// ── API base: works locally and after deploy ──────────────────────────────
const API = '__PORT_8000__'.startsWith('__')
  ? 'http://localhost:8000'
  : '__PORT_8000__';

// ── State ─────────────────────────────────────────────────────────────────
let currentFilter = 'all';
let currentTgFilter = null;
let sseSource = null;
let reconnectTimer = null;
let callCount = 0;
let alertCount = 0;
let highCount = 0;

// ── Talkgroup data (inline fallback — also fetched from API) ──────────────
const TG_DATA = {
  priority: [6324, 6332, 6414],
  all: [
    { id: 6270, name: "NET Fire 3 Colleyville",     type: "Fire-Tac" },
    { id: 6324, name: "Euless PD Patrol 1",          type: "Law Dispatch" },
    { id: 6325, name: "Euless PD Patrol 2",          type: "Law Tac" },
    { id: 6326, name: "Euless PD Patrol 3",          type: "Law Tac" },
    { id: 6327, name: "Euless PD Patrol 4",          type: "Law Tac" },
    { id: 6332, name: "Euless Fire Dispatch",         type: "Fire Dispatch" },
    { id: 6333, name: "Euless Fire 2",               type: "Fire-Tac" },
    { id: 6354, name: "Grapevine PD Dispatch",        type: "Law Dispatch" },
    { id: 6355, name: "Grapevine PD 2",              type: "Law Talk" },
    { id: 6365, name: "Grapevine Fire Alarm",         type: "Fire Dispatch" },
    { id: 6366, name: "Grapevine Fire 2",            type: "Fire-Tac" },
    { id: 6367, name: "Grapevine Fire 3",            type: "Fire-Tac" },
    { id: 6414, name: "NET PD Pat 1 (Combined)",     type: "Law Dispatch" },
    { id: 6415, name: "NET TCIC (Combined)",         type: "Law Talk" },
    { id: 6417, name: "NET Fire Alarm (Combined)",   type: "Fire Dispatch" },
    { id: 6418, name: "NET Fire 2 Response",         type: "Fire-Tac" },
  ]
};

const HIGH_KW = [
  "structure fire","working fire","shots fired","shooting","stabbing",
  "hostage","officer down","multiple callers","explosion","mayday","mass casualty"
];
const MED_KW = [
  "major accident","MVA","extrication","pursuit","chase","armed","weapon",
  "signal 5","domestic","welfare check","rollover","unconscious","cardiac arrest"
];

// ── DOM refs ──────────────────────────────────────────────────────────────
const callList     = document.getElementById('callList');
const emptyState   = document.getElementById('emptyState');
const statusDot    = document.getElementById('statusDot');
const statusLabel  = document.getElementById('statusLabel');
const statTotal    = document.getElementById('statTotal');
const statAlerts   = document.getElementById('statAlerts');
const statHigh     = document.getElementById('statHigh');
const toastContainer = document.getElementById('toastContainer');
const channelListEl  = document.getElementById('channelList');
const kwHighEl       = document.getElementById('kwHigh');
const kwMediumEl     = document.getElementById('kwMedium');

// ── Theme toggle ──────────────────────────────────────────────────────────
(function() {
  const toggle = document.querySelector('[data-theme-toggle]');
  const root = document.documentElement;
  let theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  root.setAttribute('data-theme', theme);
  if (toggle) {
    const sunSVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
    const moonSVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    const updateIcon = () => toggle.innerHTML = theme === 'dark' ? sunSVG : moonSVG;
    updateIcon();
    toggle.addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', theme);
      updateIcon();
    });
  }
})();

// ── Build channel sidebar ─────────────────────────────────────────────────
function buildChannelList() {
  const priorityIds = new Set(TG_DATA.priority);

  // "All" option
  const allItem = document.createElement('button');
  allItem.className = 'channel-item active';
  allItem.dataset.tgId = '';
  allItem.innerHTML = `
    <span class="channel-dot priority"></span>
    <span class="channel-name">All Channels</span>
  `;
  allItem.addEventListener('click', () => filterByChannel(null, allItem));
  channelListEl.appendChild(allItem);

  TG_DATA.all.forEach(tg => {
    const item = document.createElement('button');
    item.className = 'channel-item';
    item.dataset.tgId = tg.id;
    const dotClass = tg.type.toLowerCase().includes('fire') ? 'fire'
      : tg.type.toLowerCase().includes('law') ? 'law'
      : tg.type.toLowerCase().includes('ems') ? 'ems'
      : '';
    const isPri = priorityIds.has(tg.id) ? '<span class="badge badge-priority">⚡</span>' : '';
    item.innerHTML = `
      <span class="channel-dot ${dotClass}"></span>
      <span class="channel-name">${tg.name}</span>
      ${isPri}
      <span class="channel-id">${tg.id}</span>
    `;
    item.addEventListener('click', () => filterByChannel(tg.id, item));
    channelListEl.appendChild(item);
  });

  // Keyword lists
  kwHighEl.textContent = HIGH_KW.join(', ');
  kwMediumEl.textContent = MED_KW.join(', ');
}

function filterByChannel(tgId, clickedEl) {
  currentTgFilter = tgId;
  document.querySelectorAll('.channel-item').forEach(el => el.classList.remove('active'));
  clickedEl.classList.add('active');
  applyFilters();
}

// ── Filter bar ────────────────────────────────────────────────────────────
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    applyFilters();
  });
});

function applyFilters() {
  const cards = document.querySelectorAll('.call-card');
  let visible = 0;
  cards.forEach(card => {
    const isFire   = card.dataset.type?.includes('fire') || card.dataset.type?.includes('Fire');
    const isLaw    = card.dataset.type?.includes('law')  || card.dataset.type?.includes('Law');
    const isAlert  = card.dataset.priority === 'HIGH' || card.dataset.priority === 'MEDIUM';
    const isPriTg  = card.dataset.isPriority === 'true';
    const tgId     = parseInt(card.dataset.tgId);

    let show = true;

    if (currentFilter === 'fire')     show = isFire;
    if (currentFilter === 'law')      show = isLaw;
    if (currentFilter === 'alerts')   show = isAlert;
    if (currentFilter === 'priority') show = isPriTg;

    if (currentTgFilter !== null) {
      show = show && tgId === currentTgFilter;
    }

    card.classList.toggle('hidden', !show);
    if (show) visible++;
  });

  emptyState.classList.toggle('hidden', visible > 0);
}

// ── Render a call card ────────────────────────────────────────────────────
function getTgIcon(type) {
  if (!type) return '📡';
  const t = type.toLowerCase();
  if (t.includes('fire'))  return '🔥';
  if (t.includes('law'))   return '🚔';
  if (t.includes('ems'))   return '🚑';
  if (t.includes('hospital')) return '🏥';
  return '📡';
}

function highlightKeyword(text, keyword, isHigh) {
  if (!keyword || !text) return text || '';
  // Strip location prefix if present
  const kw = keyword.replace(/^📍 ?/, '');
  const cls = isHigh ? 'keyword-highlight high' : 'keyword-highlight';
  return text.replace(new RegExp(kw, 'gi'), match => `<span class="${cls}">${match}</span>`);
}

function renderCard(call) {
  const isHigh   = call.priority === 'HIGH';
  const isMedium = call.priority === 'MEDIUM';
  const isFire   = (call.tg_type || '').toLowerCase().includes('fire');
  const isLaw    = (call.tg_type || '').toLowerCase().includes('law');

  const card = document.createElement('div');
  card.className = `call-card${isHigh ? ' priority-high' : isMedium ? ' priority-medium' : ''}`;
  card.dataset.tgId = call.tg_id;
  card.dataset.type = call.tg_type || '';
  card.dataset.priority = call.priority || '';
  card.dataset.isPriority = call.is_priority ? 'true' : 'false';
  card.setAttribute('role', 'listitem');

  // Badges
  let badges = '';
  if (isHigh)           badges += `<span class="badge badge-high">🔴 HIGH</span>`;
  if (isMedium)         badges += `<span class="badge badge-medium">🟡 MED</span>`;
  if (call.is_priority) badges += `<span class="badge badge-priority">⚡ Priority</span>`;
  if (isFire)           badges += `<span class="badge badge-fire">🔥 Fire</span>`;
  if (isLaw)            badges += `<span class="badge badge-law">🚔 Police</span>`;

  // Keyword
  if (call.keyword) badges += `<span class="badge badge-medium">🔑 ${call.keyword}</span>`;

  // Transcript with keyword highlight
  const transcriptHtml = call.transcript
    ? highlightKeyword(call.transcript, call.keyword, isHigh)
    : '';

  card.innerHTML = `
    <div class="call-header">
      <div class="call-type-icon">${getTgIcon(call.tg_type)}</div>
      <div class="call-meta">
        <div class="call-channel">${call.tg_name || 'Unknown Channel'}</div>
        <div class="call-sub">
          <span class="call-time">${call.timestamp_display || ''}</span>
          <span class="call-duration">${call.duration}s</span>
        </div>
      </div>
    </div>
    ${badges ? `<div class="call-badges">${badges}</div>` : ''}
    <div class="call-transcript ${call.transcript ? '' : 'empty'}">
      ${transcriptHtml || 'No transcript available'}
    </div>
  `;

  return card;
}

function addCall(call) {
  // Skip heartbeat events
  if (call.type === 'heartbeat') return;

  // Update stats
  callCount++;
  statTotal.textContent = callCount;
  if (call.priority) {
    alertCount++;
    statAlerts.textContent = alertCount;
  }
  if (call.priority === 'HIGH') {
    highCount++;
    statHigh.textContent = highCount;
  }

  const card = renderCard(call);

  // Prepend (newest first)
  if (callList.firstChild) {
    callList.insertBefore(card, callList.firstChild);
  } else {
    callList.appendChild(card);
  }
  emptyState.classList.add('hidden');

  // Apply current filters to the new card
  applyFilters();

  // Max 100 cards in DOM
  const cards = callList.querySelectorAll('.call-card');
  if (cards.length > 100) cards[cards.length - 1].remove();

  // Show toast for alerts
  if (call.priority) showToast(call);
}

// ── Toast notifications ───────────────────────────────────────────────────
function showToast(call) {
  const toast = document.createElement('div');
  toast.className = `toast ${call.priority === 'HIGH' ? 'high' : 'medium'}`;

  const icon = call.priority === 'HIGH' ? '🔴' : '🟡';
  const short = call.transcript
    ? call.transcript.substring(0, 100) + (call.transcript.length > 100 ? '...' : '')
    : call.tg_name;

  toast.innerHTML = `
    <div class="toast-title">${icon} ${call.keyword || call.priority} — ${call.tg_name}</div>
    <div class="toast-body">${short}</div>
  `;

  toastContainer.appendChild(toast);

  // Auto-dismiss
  const dismiss = call.priority === 'HIGH' ? 8000 : 5000;
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(8px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, dismiss);
}

// ── SSE connection ─────────────────────────────────────────────────────────
function connect() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  clearTimeout(reconnectTimer);

  setStatus('connecting');

  sseSource = new EventSource(`${API}/api/stream`);

  sseSource.onopen = () => {
    setStatus('connected');
  };

  sseSource.onmessage = (e) => {
    try {
      const call = JSON.parse(e.data);
      addCall(call);
    } catch {}
  };

  sseSource.onerror = () => {
    setStatus('error');
    sseSource.close();
    sseSource = null;
    reconnectTimer = setTimeout(connect, 5000);
  };
}

function setStatus(state) {
  statusDot.className = 'status-dot';
  if (state === 'connected') {
    statusDot.classList.add('connected');
    statusLabel.textContent = 'Live';
  } else if (state === 'error') {
    statusDot.classList.add('error');
    statusLabel.textContent = 'Reconnecting...';
  } else {
    statusLabel.textContent = 'Connecting...';
  }
}

// ── Load initial stats ────────────────────────────────────────────────────
async function loadStats() {
  try {
    const res = await fetch(`${API}/api/stats`);
    const data = await res.json();
    statTotal.textContent = data.total_calls;
    statAlerts.textContent = data.total_alerts;
    statHigh.textContent = data.high_priority;
    callCount = data.total_calls;
    alertCount = data.total_alerts;
    highCount = data.high_priority;
  } catch {}
}

// ── Init ──────────────────────────────────────────────────────────────────
buildChannelList();
connect();
loadStats();
setInterval(loadStats, 60_000);

// Request browser notification permission (for mobile PWA use)
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}
