'use strict';
/* OfferRadar web app. No external libraries. All text coming from Telegram is escaped before it touches the DOM. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const inr = n => (n == null ? '—' : '₹' + Math.round(n).toLocaleString('en-IN'));
const href = u => (/^https?:\/\//i.test(u || '') ? esc(u) : '#');
const PLAT = {amazon: 'Amazon', flipkart: 'Flipkart', meesho: 'Meesho'};
const STATUS = {live: 'চালু', free: 'ফ্রি', stale: 'পুরোনো হতে পারে', dead: 'শেষ'};
const VERDICT = {buy: ['✅ কিনতে পারেন', 'good'], wait: ['⏳ অপেক্ষা করুন', 'warn'], avoid: ['🚫 এড়িয়ে চলুন', 'bad']};
const FLAG = {otp_request: 'OTP চাইছে', payment_request: 'টাকা পাঠাতে বলছে', apk: 'APK ফাইল', forward_chain: 'forward চেইন',
  lottery: 'লটারি/জ্যাকপট', dm_bait: 'DM-এ যেতে বলছে', extreme_discount: 'অস্বাভাবিক বেশি ছাড়', lookalike: 'নকল domain'};
const SEVERE = new Set(['otp_request', 'payment_request', 'apk', 'forward_chain', 'lottery', 'lookalike']);
const STEPS = ['অপেক্ষায়', 'যাচাই চলছে', 'সম্পন্ন'];
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

const S = {tab: 'deals', platform: '', status: 'live', sort: 'score', q: '', deals: [], tasks: [], state: null,
  feed: [], es: null, chatReady: false, timers: {}};

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return 'এইমাত্র';
  if (s < 3600) return Math.round(s / 60) + ' মিনিট আগে';
  if (s < 86400) return Math.round(s / 3600) + ' ঘণ্টা আগে';
  return Math.round(s / 86400) + ' দিন আগে';
}
function debounce(key, fn, ms) { clearTimeout(S.timers[key]); S.timers[key] = setTimeout(fn, ms); }
function toast(msg, bad) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  $('#toasts').append(t);
  setTimeout(() => t.remove(), 4500);
}
async function api(path, {method = 'GET', body} = {}) {
  const res = await fetch(path, {method, credentials: 'same-origin',
    headers: body ? {'Content-Type': 'application/json'} : undefined, body: body ? JSON.stringify(body) : undefined});
  if (res.status === 401) { showLogin(); throw new Error('login'); }
  if (!res.ok) {
    let m = res.statusText;
    try { m = (await res.json()).detail || m; } catch (e) { /* keep status text */ }
    throw new Error(typeof m === 'string' ? m : 'অনুরোধ ব্যর্থ');
  }
  return res.json();
}

/* ---------- login / boot ---------- */
function showLogin() {
  $('#app').hidden = true; $('#login').hidden = false;
  if (S.es) { S.es.close(); S.es = null; }
}
async function boot() {
  try { S.state = await api('/api/state'); } catch (e) { return; }
  $('#login').hidden = true; $('#app').hidden = false;
  $('#appName').textContent = S.state.app_name; document.title = S.state.app_name;
  (S.state.recent || []).forEach(ev => S.feed.unshift(ev));
  renderHero(); fillProfile();
  loadDeals(); connect(); switchTab(S.tab);
}
$('#loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  try { await api('/api/login', {method: 'POST', body: {token: $('#pw').value}}); $('#pw').value = ''; $('#loginErr').textContent = ''; boot(); }
  catch (err) { $('#loginErr').textContent = err.message === 'login' ? '' : 'পাসওয়ার্ড ঠিক নয়, আবার চেষ্টা করুন।'; }
});
$('#logout').addEventListener('click', async () => { await api('/api/logout', {method: 'POST'}).catch(() => {}); showLogin(); });

/* ---------- tabs ---------- */
function switchTab(tab) {
  S.tab = tab;
  $$('.nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  $$('.view').forEach(v => { v.hidden = v.id !== 'v-' + tab; });
  if (tab === 'watch') loadTasks();
  if (tab === 'chat') initChat();
  if (tab === 'live') { refreshState().then(renderLive); renderFeed(); }
  if (tab === 'more') loadChannels();
  if (tab === 'deals') loadDeals();
  window.scrollTo({top: 0});
}
$('#nav').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) switchTab(b.dataset.tab); });
async function refreshState() { try { S.state = await api('/api/state'); renderHero(); } catch (e) { /* login handled */ } return S.state; }

/* ---------- deals ---------- */
function ph(d) { return `<div class="ph ${esc(d.platform)}"><span>${esc((d.title || '?').trim().charAt(0).toUpperCase())}</span></div>`; }
function cardHTML(d) {
  const price = d.status === 'free' ? '<b class="price free">ফ্রি</b>' : d.price == null ? '<b class="price">দাম অজানা</b>' : `<b class="price">${inr(d.price)}</b>`;
  const mrp = d.mrp && d.mrp > (d.price || 0) ? `<s>${inr(d.mrp)}</s>` : '';
  const off = d.discount ? `<span class="badge off">${Math.round(d.discount)}% ছাড়</span>` : '';
  const severe = (d.flags || []).some(f => SEVERE.has(f));
  const mark = severe ? '<span class="warn-mark">⚠ সন্দেহজনক</span>' : `<span>${d.sources} channel</span>`;
  const img = d.image ? `<img src="${esc(d.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ph(d);
  const score = Math.round(d.score || 0);
  return `<article class="card ${esc(d.status)}" data-id="${d.id}" tabindex="0" role="button" aria-label="${esc(d.title)}">
    <div class="media">${img}<span class="plat">${esc(PLAT[d.platform] || '')}</span>${off}<span class="st ${esc(d.status)}">${esc(STATUS[d.status] || '')}</span></div>
    <div class="body"><h3>${esc(d.title)}</h3><div class="prices">${price}${mrp}</div>
    <div class="meta">${mark}<span class="ring" style="--p:${score}" title="deal স্কোর"><i>${score}</i></span></div></div></article>`;
}
async function loadDeals() {
  const grid = $('#grid');
  if (!S.deals.length) grid.innerHTML = '<div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div>';
  const p = new URLSearchParams({status: S.status, sort: S.sort, platform: S.platform, q: S.q, limit: '60'});
  try { S.deals = await api('/api/deals?' + p); } catch (e) { return; }
  renderDeals();
}
function renderDeals() {
  const grid = $('#grid');
  if (!S.deals.length) {
    grid.innerHTML = `<div class="empty"><h3>এখানে এখনো কিছু নেই</h3>
      <p class="mut">Telegram যুক্ত হলে offer এখানে আসতে শুরু করবে। এখনই দেখে নিতে চাইলে demo ডেটা লোড করুন।</p>
      <button class="btn primary" id="emptyDemo" type="button">demo ডেটা লোড করুন</button></div>`;
    return;
  }
  grid.innerHTML = S.deals.map(cardHTML).join('');
}
$('#grid').addEventListener('click', e => {
  if (e.target.id === 'emptyDemo') return loadDemo();
  const c = e.target.closest('.card'); if (c) openDeal(+c.dataset.id);
});
$('#grid').addEventListener('keydown', e => { if (e.key === 'Enter') { const c = e.target.closest('.card'); if (c) openDeal(+c.dataset.id); } });
$('#filters').addEventListener('click', e => {
  const b = e.target.closest('.seg button'); if (!b) return;
  const seg = b.parentElement; S[seg.dataset.key] = b.dataset.v;
  $$('button', seg).forEach(x => x.classList.toggle('on', x === b));
  loadDeals();
});
$('#sort').addEventListener('change', e => { S.sort = e.target.value; loadDeals(); });
$('#searchForm').addEventListener('submit', e => { e.preventDefault(); S.q = $('#q').value.trim(); if (S.tab !== 'deals') switchTab('deals'); else loadDeals(); });
document.addEventListener('error', e => {
  const t = e.target;
  if (t && t.tagName === 'IMG') { const d = document.createElement('div'); d.className = 'ph'; d.innerHTML = '<span>🛒</span>'; t.replaceWith(d); }
}, true);

/* 3D tilt: only for mouse-like pointers, never with reduced motion */
if (!reduced && matchMedia('(hover: hover) and (pointer: fine)').matches) {
  const grid = $('#grid');
  grid.addEventListener('pointermove', e => {
    const c = e.target.closest('.card'); if (!c) return;
    const r = c.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
    c.classList.add('tilting'); c.style.setProperty('--ry', (x * 9).toFixed(2) + 'deg'); c.style.setProperty('--rx', (-y * 9).toFixed(2) + 'deg');
  });
  grid.addEventListener('pointerout', e => {
    const c = e.target.closest('.card'); if (!c || c.contains(e.relatedTarget)) return;
    c.classList.remove('tilting'); c.style.removeProperty('--rx'); c.style.removeProperty('--ry');
  });
}

/* ---------- hero ---------- */
function renderHero() {
  const st = S.state && S.state.stats; if (!st) return;
  $('#heroTitle').textContent = `এখন ${st.live + st.free}টি deal চালু`;
  $('#heroSub').textContent = `${st.free}টি ফ্রি • আজ ${st.posts_today}টি post পড়া হয়েছে • ${st.blocked_today}টি সন্দেহজনক link আটকানো`;
}
function ticker(text) { $('#ticker').textContent = text; }

/* ---------- deal dialog ---------- */
function spark(points) {
  if (!points || points.length < 2) return '<p class="mut">দামের ইতিহাস জমছে। আরও post এলে chart দেখা যাবে।</p>';
  const w = 320, h = 80, pad = 6, ps = points.map(p => p.price), min = Math.min(...ps), max = Math.max(...ps), span = max - min || 1;
  const xy = points.map((p, i) => [pad + i * (w - 2 * pad) / (points.length - 1), h - pad - (p.price - min) / span * (h - 2 * pad)]);
  const line = xy.map(p => p.join(',')).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-label="দামের chart: সর্বনিম্ন ${inr(min)}, সর্বোচ্চ ${inr(max)}">
    <polygon points="${pad},${h - pad} ${line} ${w - pad},${h - pad}" fill="rgba(91,61,245,.15)"/>
    <polyline points="${line}" fill="none" stroke="#5b3df5" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    ${xy.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="#ff5c3a"/>`).join('')}</svg>
    <p class="mut">সর্বনিম্ন ${inr(min)} • সর্বোচ্চ ${inr(max)} • ${points.length}টি তথ্য</p>`;
}
function verdictHTML(v) {
  if (!v) return '<p class="mut">এখনো AI যাচাই হয়নি। নিচের বোতামে চাপুন।</p>';
  const [label, cls] = VERDICT[v.verdict] || ['', ''];
  const li = a => (a || []).map(x => `<li>${esc(x)}</li>`).join('');
  return `<div class="verdict ${cls}"><b>${label}</b> <span class="chip">${v.confidence}% নিশ্চিত</span>
    ${v.source === 'rules' ? '<span class="chip warn">শুধু নিয়ম দিয়ে, AI যাচাই হয়নি</span>' : ''}
    <p>${esc(v.summary)}</p>${v.fit ? `<p><b>আপনার জন্য:</b> ${esc(v.fit)}</p>` : ''}
    ${(v.reasons || []).length ? `<h4>কারণ</h4><ul>${li(v.reasons)}</ul>` : ''}${(v.risks || []).length ? `<h4>ঝুঁকি</h4><ul>${li(v.risks)}</ul>` : ''}</div>`;
}
async function openDeal(id) {
  let d; try { d = await api('/api/deals/' + id); } catch (e) { return toast('deal খোলা গেল না', true); }
  const dlg = $('#dlg');
  const flags = (d.flags || []).map(f => FLAG[f] || f);
  const price = d.status === 'free' ? '<b class="price free">ফ্রি</b>' : `<b class="price">${inr(d.price)}</b>`;
  dlg.innerHTML = `<div class="sheet">
    <button class="x" data-close aria-label="বন্ধ করুন">✕</button>
    <div class="dhead"><div class="thumb">${d.image ? `<img class="dimg" src="${esc(d.image)}" alt="" referrerpolicy="no-referrer">` : ph(d)}</div>
      <div><h2>${esc(d.title)}</h2><div class="chips"><span class="chip">${esc(PLAT[d.platform] || '')}</span><span class="chip ${d.status === 'live' || d.status === 'free' ? 'good' : ''}">${esc(STATUS[d.status])}</span>
      <span class="chip ${d.link_state === 'verified' ? 'good' : 'warn'}">${d.link_state === 'verified' ? '✓ দোকানের তথ্যে যাচাই' : 'channel-এর দাবি'}</span></div></div></div>
    <div class="dprice">${price}${d.mrp ? `<s class="mut">${inr(d.mrp)}</s>` : ''}${d.discount ? `<span class="chip bad">${Math.round(d.discount)}% ছাড়</span>` : ''}${d.coupon ? `<span class="chip">coupon: ${esc(d.coupon)}</span>` : ''}</div>
    ${flags.length ? `<div class="alertbox">⚠ সতর্কতা: ${esc(flags.join(', '))}</div>` : ''}
    ${d.end_reason ? `<p class="mut">শেষের কারণ: ${esc({ended_notice: 'channel-এ শেষ বলা হয়েছে', link_dead: 'link আর কাজ করছে না', not_reposted: 'অনেকক্ষণ কেউ আবার post করেনি', api_unavailable: 'দোকান বলছে পাওয়া যাচ্ছে না'}[d.end_reason] || d.end_reason)}</p>` : ''}
    <section><h4>AI রায়</h4><div id="vbox">${verdictHTML(d.verdict_data)}</div></section>
    <section><h4>দামের ইতিহাস</h4>${spark(d.history)}</section>
    <section><h4>কোন channel-এ এসেছে (${d.channels.length})</h4><ul class="clist">${d.channels.map(c => `<li><span>${esc(c.name)}</span><span class="mut">${ago(c.ts)} • ভরসা ${Math.round((c.trust ?? .5) * 100)}%</span></li>`).join('')}</ul></section>
    <div class="acts"><a class="btn primary" href="${href(d.url)}" target="_blank" rel="noopener noreferrer">দোকানে খুলুন</a>
      <button class="btn" data-act="check" data-id="${d.id}">🔎 AI দিয়ে যাচাই</button>
      <input id="wPrice" type="number" min="0" inputmode="numeric" placeholder="লক্ষ্য দাম ₹" value="${d.price ? Math.round(d.price) : ''}" aria-label="লক্ষ্য দাম">
      <button class="btn" data-act="watch" data-id="${d.id}">🔔 নজরে রাখুন</button></div>
    <p class="note">দাম যদি "channel-এর দাবি" হয়, কেনার আগে দোকানের পাতায় নিজে মিলিয়ে নিন।</p></div>`;
  if (!dlg.open) dlg.showModal();
}
$('#dlg').addEventListener('click', async e => {
  if (e.target.id === 'dlg' || e.target.closest('[data-close]')) return $('#dlg').close();
  const b = e.target.closest('[data-act]'); if (!b) return;
  const id = b.dataset.id;
  if (b.dataset.act === 'check') {
    b.disabled = true; b.textContent = 'যাচাই চলছে…';
    try { const r = await api(`/api/deals/${id}/check`, {method: 'POST'}); $('#vbox').innerHTML = verdictHTML(r.verdict); loadDeals(); }
    catch (err) { toast('যাচাই করা গেল না: ' + err.message, true); }
    b.disabled = false; b.textContent = '🔎 আবার যাচাই';
  } else if (b.dataset.act === 'watch') {
    const v = $('#wPrice').value;
    try { await api(`/api/deals/${id}/watch`, {method: 'POST', body: {target_price: v ? +v : null}}); toast('✅ নজরে রাখা হচ্ছে'); }
    catch (err) { toast(err.message, true); }
  }
});

/* ---------- tasks ---------- */
async function loadTasks() { try { S.tasks = await api('/api/tasks'); } catch (e) { return; } renderTasks(); }
function renderTasks() {
  const ul = $('#taskList');
  if (!S.tasks.length) { ul.innerHTML = '<li class="empty">কোনো task নেই। উপরে লিখুন, কোন পণ্য কত দামে চান।</li>'; return; }
  ul.innerHTML = S.tasks.map(t => {
    const step = {waiting: 0, checking: 1, done: 2}[t.status] ?? 0;
    const r = t.result || {};
    let res = '';
    if (t.status === 'done') res = `<p class="res">🎉 ${esc(r.title || '')} — ${inr(r.price)} • ${esc((VERDICT[r.verdict] || [''])[0])} (${r.confidence}%) • ${r.telegram_sent ? 'Telegram-এ পাঠানো হয়েছে' : 'Telegram bot যুক্ত নেই, তাই এখানে দেখানো হলো'}</p>`;
    else if (r.rejected) res = `<p class="res mut">একটা deal পেয়েছিলাম কিন্তু যাচাইয়ে বাদ গেছে: ${esc(r.rejected)}</p>`;
    else if (t.last_checked) res = `<p class="res mut">শেষ দেখা: ${ago(t.last_checked)}</p>`;
    const kws = (t.keywords || []).map(k => `<span class="chip">${esc(k)}</span>`).join(' ');
    return `<li class="task ${esc(t.status)}"><header><div><b>${esc(t.text)}</b><br><small>${kws} ${t.target_price != null ? 'সর্বোচ্চ ' + inr(t.target_price) : ''} ${t.platform && t.platform !== 'any' ? '• ' + esc(PLAT[t.platform] || '') : ''}</small></div>
      ${['waiting', 'checking'].includes(t.status) ? `<button class="btn" data-cancel="${t.id}">বন্ধ</button>` : `<span class="chip">${t.status === 'done' ? 'সম্পন্ন' : 'বাতিল'}</span>`}</header>
      ${t.status === 'cancelled' ? '' : `<ol class="steps">${STEPS.map((s, i) => `<li class="${i <= step ? 'on' : ''} ${i === step ? 'cur' : ''}">${s}</li>`).join('')}</ol>`}${res}</li>`;
  }).join('');
}
$('#taskForm').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {text: $('#tText').value, target_price: $('#tPrice').value ? +$('#tPrice').value : null, platform: $('#tPlat').value || null};
  try { await api('/api/tasks', {method: 'POST', body}); $('#tText').value = ''; toast('✅ task চালু হয়েছে'); loadTasks(); } catch (err) { toast(err.message, true); }
});
$('#taskList').addEventListener('click', async e => {
  const b = e.target.closest('[data-cancel]'); if (!b) return;
  await api('/api/tasks/' + b.dataset.cancel, {method: 'DELETE'}); loadTasks();
});

/* ---------- chat ---------- */
function msgHTML(role, text, extra = {}) {
  const cards = (extra.cards || []).length ? `<div class="grid">${extra.cards.map(cardHTML).join('')}</div>` : '';
  const chart = extra.chart ? `<div>${spark(extra.chart.points)}</div>` : '';
  return `<div class="msg ${role}">${esc(text)}${cards}${chart}</div>`;
}
async function initChat() {
  if (S.chatReady) return; S.chatReady = true;
  const box = $('#msgs');
  box.innerHTML = msgHTML('ai', 'হ্যালো! যেকোনো পণ্য নিয়ে জিজ্ঞেস করুন, বা বলুন "এই পণ্যটা কম দামে চাই", আমি ২৪ ঘণ্টা নজর রাখব আর Telegram-এ জানাব।');
  try { (await api('/api/chat/history')).forEach(m => box.insertAdjacentHTML('beforeend', msgHTML(m.role === 'user' ? 'user' : 'ai', m.text))); } catch (e) { /* ignore */ }
  box.lastElementChild.scrollIntoView({block: 'end'});
}
async function sendChat(text) {
  text = text.trim(); if (!text) return;
  const box = $('#msgs'); $('#sugg').hidden = true;
  box.insertAdjacentHTML('beforeend', msgHTML('user', text));
  box.insertAdjacentHTML('beforeend', '<div class="msg ai typing" id="typing">ভাবছি…</div>');
  $('#typing').scrollIntoView({block: 'end'});
  try {
    const out = await api('/api/chat', {method: 'POST', body: {text}});
    $('#typing').remove(); box.insertAdjacentHTML('beforeend', msgHTML('ai', out.reply, out));
    if (out.task) { S.tasks = []; }
  } catch (err) { $('#typing')?.remove(); box.insertAdjacentHTML('beforeend', msgHTML('ai', 'এখন উত্তর দিতে পারছি না: ' + err.message)); }
  box.lastElementChild.scrollIntoView({block: 'end'});
}
$('#chatForm').addEventListener('submit', e => { e.preventDefault(); const v = $('#chatIn').value; $('#chatIn').value = ''; sendChat(v); });
$('#sugg').addEventListener('click', e => { if (e.target.tagName === 'BUTTON') sendChat(e.target.textContent); });
$('#msgs').addEventListener('click', e => { const c = e.target.closest('.card'); if (c) openDeal(+c.dataset.id); });

/* ---------- live view ---------- */
const EV = {
  post: d => `📨 ${d.channel}: ${d.text}`,
  deal: d => `🏷 ${d.title} — ${d.status === 'free' ? 'ফ্রি' : inr(d.price)} (${PLAT[d.platform] || ''}, ${d.channel})`,
  deal_status: d => `⌛ ${d.title} → ${STATUS[d.status] || d.status}`,
  blocked: d => `⛔ ${d.channel}: সন্দেহজনক link আটকানো হয়েছে (${d.reason})`,
  task: d => `📌 task #${d.id} → ${d.status}`,
  alert: d => `🔔 ${d.sent ? 'Telegram-এ পাঠানো হয়েছে' : 'পাওয়া গেছে'}: ${d.title} — ${inr(d.price)}`,
  verdict: d => `🤖 যাচাই: ${d.title} → ${(VERDICT[d.verdict] || [d.verdict])[0]} (${d.confidence}%)`,
  telegram: d => (d.connected ? '🟢 Telegram পাঠক যুক্ত' : '🔴 Telegram পাঠক বিচ্ছিন্ন'),
  check: () => '🔎 যাচাই শুরু হয়েছে',
};
function renderFeed() {
  $('#feed').innerHTML = S.feed.slice(0, 60).map(ev => `<li><time>${ago(ev.ts)}</time><span>${esc((EV[ev.type] || (() => ev.type))(ev.data))}</span></li>`).join('') || '<li><span class="mut">এখনো কিছু ঘটেনি।</span></li>';
}
function renderLive() {
  const s = S.state; if (!s) return;
  const u = s.ai, pct = Math.min(100, Math.round(u.total / Math.max(1, u.limit) * 100));
  $('#usage').innerHTML = `<h3>আজকের AI ব্যবহার</h3><p class="mut">${u.total} / ${u.limit} request (মিনিটে সর্বোচ্চ ${u.rpm})</p>
    <div class="bar ${pct > 85 ? 'hot' : ''}"><i style="width:${pct}%"></i></div>
    ${Object.entries(u.shares).filter(([k]) => k !== 'reserve').map(([k, cap]) => {
      const used = u.by_kind[k] || 0;
      return `<div class="mut">${({parse: 'post পড়া', deep: 'গভীর যাচাই', chat: 'চ্যাট', alert: 'alert-এর আগে যাচাই'})[k] || k}: ${used}/${cap}</div><div class="bar ${used >= cap ? 'hot' : ''}"><i style="width:${Math.min(100, used / Math.max(1, cap) * 100)}%"></i></div>`;
    }).join('')}`;
  const v = s.services, row = (ok, name, note) => `<div class="svc"><span><span class="dot ${ok ? 'ok' : 'no'}"></span>${name}</span><span class="mut">${note}</span></div>`;
  $('#services').innerHTML = `<h3>সংযোগ</h3>
    ${row(v.telegram_reader_connected, 'Telegram পাঠক', v.telegram_reader_connected ? 'চালু' : v.telegram_reader_configured ? 'সংযোগ হচ্ছে…' : 'যুক্ত নয়')}
    ${row(v.bot_paired, 'Telegram bot (alert)', v.bot_paired ? 'জোড়া লেগেছে' : v.bot_configured ? 'bot-এ /start কোড দিন' : 'যুক্ত নয়')}
    ${row(v.ai_keys > 0, 'AI key', v.ai_keys + 'টি')}
    ${row(v.tavily, 'Tavily search', v.tavily ? 'চালু' : 'যুক্ত নয়')}
    ${row(v.price_apis.length > 0, 'দোকানের price API', v.price_apis.join(', ') || 'যুক্ত নয়')}
    ${row(s.stats.pending_ai === 0, 'AI-র অপেক্ষায় post', s.stats.pending_ai + 'টি')}`;
}
function onEvent(ev) {
  S.feed.unshift(ev); S.feed = S.feed.slice(0, 80);
  const d = ev.data || {};
  if (ev.type === 'deal') ticker(`${d.title} — ${d.status === 'free' ? 'ফ্রি' : inr(d.price)}`);
  if (ev.type === 'alert') toast('🔔 ' + EV.alert(d));
  if (ev.type === 'blocked') ticker('⛔ সন্দেহজনক link আটকানো হয়েছে');
  if (['deal', 'deal_status', 'verdict'].includes(ev.type) && S.tab === 'deals') debounce('deals', loadDeals, 1500);
  if (ev.type === 'task' || ev.type === 'alert') { if (S.tab === 'watch') debounce('tasks', loadTasks, 600); }
  if (S.tab === 'live') debounce('feed', () => { renderFeed(); refreshState().then(renderLive); }, 800);
  else debounce('state', refreshState, 4000);
}
function connect() {
  if (S.es) S.es.close();
  const es = S.es = new EventSource('/api/stream');
  es.onopen = () => $('#liveDot').classList.add('on');
  es.onerror = () => $('#liveDot').classList.remove('on');
  Object.keys(EV).forEach(t => es.addEventListener(t, m => { try { onEvent(JSON.parse(m.data)); } catch (e) { /* ignore */ } }));
}

/* ---------- settings ---------- */
function fillProfile() {
  const p = (S.state && S.state.profile) || {};
  $('#pBudget').value = p.budget_max ?? ''; $('#pInterests').value = (p.interests || []).join(', ');
}
$('#profileForm').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {budget_max: $('#pBudget').value ? +$('#pBudget').value : null,
    interests: $('#pInterests').value.split(',').map(x => x.trim()).filter(Boolean)};
  try { await api('/api/profile', {method: 'POST', body}); toast('✅ সংরক্ষণ হয়েছে'); } catch (err) { toast(err.message, true); }
});
async function loadChannels() {
  let rows = []; try { rows = await api('/api/channels'); } catch (e) { return; }
  $('#chanTable').innerHTML = rows.length ? rows.map(c => `<div><span>${esc(c.name)} <small class="mut">${c.posts} post</small></span>
    <span><div class="bar ${c.trust < .35 ? 'hot' : ''}"><i style="width:${Math.round(c.trust * 100)}%"></i></div></span></div>`).join('') : '<p class="mut">এখনো কোনো channel থেকে post আসেনি।</p>';
}
$('#ingestForm').addEventListener('submit', async e => {
  e.preventDefault();
  try { await api('/api/dev/ingest', {method: 'POST', body: {text: $('#iText').value}}); toast('✅ post পাঠানো হয়েছে, "ডিল" ট্যাবে দেখুন'); $('#iText').value = ''; } catch (err) { toast(err.message, true); }
});
async function loadDemo() { try { const r = await api('/api/dev/demo', {method: 'POST'}); toast(`✅ ${r.loaded}টি demo post লোড হয়েছে`); setTimeout(loadDeals, 800); } catch (err) { toast(err.message, true); } }
$('#demoBtn').addEventListener('click', loadDemo);
setInterval(() => { if (S.tab === 'live') renderFeed(); }, 30000);

boot();
