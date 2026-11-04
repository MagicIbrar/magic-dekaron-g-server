'use strict';
var E = function (id) { return document.getElementById(id); };
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
function fmtTime(sec) { return sec ? new Date(sec * 1000).toLocaleString() : '-'; }

async function api(name, body) {
  var opt = { method: body ? 'POST' : 'GET', headers: {} };
  if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  var r = await fetch('/api/' + name, opt);
  if (r.status === 401) { showLogin(); throw new Error('auth'); }
  var j = await r.json();
  if (j && j.error) throw new Error(j.error);
  return j;
}

// ---- auth ----
async function doLogin() {
  var m = E('loginMsg'); m.className = 'msg'; m.textContent = '...';
  try {
    var r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: E('pw').value }) });
    var j = await r.json();
    if (j.ok) { E('login').style.display = 'none'; E('app').style.display = 'flex'; if (!location.hash) location.hash = '#dashboard'; route(); }
    else { m.className = 'msg err'; m.textContent = 'Wrong password'; }
  } catch (e) { m.className = 'msg err'; m.textContent = 'Network error'; }
}
async function doLogout() { try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { } showLogin(); }
function showLogin() { E('app').style.display = 'none'; E('login').style.display = 'flex'; E('pw').value = ''; E('pw').focus(); }

// ---- router ----
var V = function () { return E('view'); };
function setActive(h) { document.querySelectorAll('.side a').forEach(function (a) { a.classList.toggle('active', a.getAttribute('href') === '#' + h); }); }
async function route() {
  var h = (location.hash || '#dashboard').slice(1);
  var parts = h.split('/'); setActive(parts[0]);
  try {
    if (parts[0] === 'dashboard') return pageDashboard();
    if (parts[0] === 'players') return pagePlayers();
    if (parts[0] === 'accounts') return pageAccounts();
    if (parts[0] === 'account') return pageAccount(decodeURIComponent(parts[1] || ''));
    if (parts[0] === 'character') return pageCharacter(decodeURIComponent(parts[1] || ''));
    if (parts[0] === 'characters') return pageCharacters();
    if (parts[0] === 'hero') return pageHero(decodeURIComponent(parts[1] || ''), parts[2]);
    if (parts[0] === 'inventory') return pageInventory(decodeURIComponent(parts[1] || ''));
    if (parts[0] === 'gifts') return pageGifts();
    if (parts[0] === 'servers') return pageServers();
    if (parts[0] === 'logs') return pageLogs();
    if (parts[0] === 'settings') return pageSettings();
    V().innerHTML = '<div class="box">Not found.</div>';
  } catch (e) { if (e.message !== 'auth') V().innerHTML = '<div class="box"><span class="msg err">' + esc(e.message) + '</span></div>'; }
}
window.addEventListener('hashchange', route);

// ---- pages ----
async function pageDashboard() {
  var d = await api('dashboard'); var c = d.config;
  V().innerHTML =
    '<h1>Dashboard</h1><p class="sub">Region <b>' + esc(d.regionName) + '</b></p>' +
    '<div class="tiles">' +
    tile(d.onlinePlayers, 'Players in world') + tile(d.onlineSessions, 'Open sessions') +
    tile(d.accounts, 'Accounts') + tile(d.heroes, 'Characters') + tile(d.communityServers, 'Community servers online') +
    '</div>' +
    '<div class="box"><h2>Server</h2>' +
    kv('Advertised host', esc(c.host)) + kv('Auth / game port', c.authPort + ' / ' + c.gamePort) +
    kv('Login HTTPS port', c.httpsPort) + kv('Guests allowed', c.guests ? 'yes' : 'no') +
    kv('Hub role', c.isHub ? 'hub (account authority)' : (c.hubAuthUrl ? 'community (accounts from hub)' : 'standalone')) +
    kv('Network accounts', c.hubAuth ? 'on (shared secret set)' : 'off') +
    kv('Content CDN', esc(c.cdnRoot)) +
    '<div class="row" style="margin-top:14px"><button class="b primary" onclick="saveAll()">Save all to database</button> <span id="saveMsg" class="muted"></span></div>' +
    '</div>';
}
function tile(n, l) { return '<div class="tile"><div class="n">' + n + '</div><div class="l">' + l + '</div></div>'; }
function kv(k, v) { return '<div class="row"><label>' + k + '</label><span>' + v + '</span></div>'; }
async function saveAll() { var m = E('saveMsg'); m.textContent = 'saving...'; try { var r = await api('save', {}); m.textContent = 'wrote ' + r.written + ' rows'; } catch (e) { m.textContent = e.message; } }

async function pagePlayers() {
  var d = await api('players');
  var rows = d.players.map(function (p) {
    return '<tr><td><a href="#account/' + encodeURIComponent(p.account) + '">' + esc(p.account) + '</a></td><td>' + esc(p.hero || '-') + '</td><td>' + (p.level || '') + '</td><td>' + (p.map || '') + '</td><td>' + esc(p.ip) + '</td>' +
      '<td><button class="b" onclick="msgPlayer(' + p.sid + ')">Message</button> <button class="b danger" onclick="kick(' + p.sid + ')">Kick</button></td></tr>';
  }).join('');
  V().innerHTML = '<h1>Players online</h1>' +
    '<div class="box"><div class="toolbar"><input id="notice" class="search" placeholder="Broadcast a notice..." style="max-width:360px">' +
    '<button class="b primary" onclick="sendNotice()">Send</button> <button class="b" onclick="route()">Refresh</button> <span id="pmsg" class="muted"></span></div>' +
    '<table><thead><tr><th>Account</th><th>Character</th><th>Lv</th><th>Map</th><th>IP</th><th></th></tr></thead><tbody>' +
    (rows || '<tr><td colspan="6" class="muted">No one online.</td></tr>') + '</tbody></table></div>';
}
async function sendNotice() { var t = E('notice').value.trim(); if (!t) return; E('pmsg').textContent = '...'; try { var r = await api('notice', { text: t }); E('pmsg').textContent = 'sent to ' + r.sent; E('notice').value = ''; } catch (e) { E('pmsg').textContent = e.message; } }
async function kick(sid) { if (!confirm('Kick session ' + sid + '?')) return; await api('kick', { sid: sid }); route(); }

async function pageAccounts() {
  var q = ''; var d = await api('accounts');
  V().innerHTML = '<h1>Accounts</h1>' +
    '<div class="box"><div class="toolbar"><input id="q" class="search" placeholder="Search login or email..." oninput="searchAcc()">' +
    '<button class="b primary" onclick="createAcc()">Create account</button></div>' +
    '<div id="accTable"></div></div>';
  renderAccounts(d.accounts);
}
async function searchAcc() { var q = E('q').value; var d = await api('accounts?q=' + encodeURIComponent(q)); renderAccounts(d.accounts); }
function renderAccounts(list) {
  var rows = list.map(function (a) {
    var role = a.role === 'operator' ? '<span class="pill op">operator</span>' : a.role === 'gm' ? '<span class="pill gm">GM</span>' : '';
    var ban = (a.banned_until && a.banned_until > Date.now() / 1000) ? ' <span class="pill ban">banned</span>' : '';
    return '<tr><td><a href="#account/' + encodeURIComponent(a.login) + '">' + esc(a.login) + '</a></td><td>' + esc(a.kind) + '</td><td>' + role + ban + '</td>' +
      '<td>' + a.heroes + '</td><td class="muted">' + fmtTime(a.last_login) + '</td></tr>';
  }).join('');
  E('accTable').innerHTML = '<table><thead><tr><th>Login</th><th>Kind</th><th>Role</th><th>Chars</th><th>Last login</th></tr></thead><tbody>' +
    (rows || '<tr><td colspan="5" class="muted">No accounts.</td></tr>') + '</tbody></table>';
}
async function createAcc() {
  var login = prompt('New account login (3-64 letters, digits, - _ .):'); if (!login) return;
  var pw = prompt('Password (blank = device/guest account, no password):') || null;
  try { await api('account/create', { login: login, password: pw }); location.hash = '#account/' + encodeURIComponent(login); } catch (e) { alert(e.message); }
}

async function pageAccount(login) {
  var a = await api('account?login=' + encodeURIComponent(login));
  var heroRows = a.heroes.map(function (h) { return '<tr><td><a href="#hero/' + encodeURIComponent(login) + '/' + h.no + '">' + esc(h.name) + '</a></td><td>' + (h.index != null ? h.index : '') + '</td><td>' + (h.level || '') + '</td><td>' + (h.map || '') + '</td></tr>'; }).join('');
  V().innerHTML = '<h1>' + esc(a.login) + ' ' + (a.online ? '<span class="pill on">online</span>' : '<span class="pill off">offline</span>') + '</h1>' +
    '<p class="sub"><a href="#accounts">&larr; Accounts</a> &middot; <a href="#inventory/' + encodeURIComponent(login) + '">Inventory, gold &amp; mail &rarr;</a>' + (a.online ? ' &middot; <a href="#" onclick="kickLogin(\'' + jss(login) + '\');return false">Kick to edit</a>' : '') + '</p>' +
    '<div class="box"><h2>Account</h2>' +
    kv('Kind', esc(a.kind)) + kv('Email', esc(a.email) || '<span class="muted">none</span>') + kv('Gold', a.gold) +
    kv('Banned', a.banned ? ('yes - ' + esc(a.banReason)) : 'no') +
    '<div class="row"><label>Role</label><select id="role"><option value="player">player</option><option value="gm">GM</option><option value="operator">operator</option></select>' +
    '<button class="b" onclick="setRole(\'' + jss(login) + '\')">Set role</button></div>' +
    '<div class="row"><label>Password</label><input id="newpw" class="search" placeholder="new password (8+)"><button class="b" onclick="setPw(\'' + jss(login) + '\')">Set</button></div>' +
    '<div class="row"><label>Ban</label><input id="bansec" class="search" style="max-width:120px" placeholder="seconds"><input id="banrs" class="search" placeholder="reason">' +
    '<button class="b danger" onclick="ban(\'' + jss(login) + '\',true)">Ban</button> <button class="b" onclick="ban(\'' + jss(login) + '\',false)">Unban</button></div>' +
    '<div class="row"><span id="amsg" class="msg"></span></div>' +
    '<div class="row" style="margin-top:10px"><button class="b danger" onclick="delAcc(\'' + jss(login) + '\')">Delete account</button></div>' +
    '</div>' +
    '<div class="box"><h2>Characters</h2>' +
    '<table><thead><tr><th>Name</th><th>Class</th><th>Lv</th><th>Map</th></tr></thead><tbody>' + (heroRows || '<tr><td colspan="4" class="muted">None.</td></tr>') + '</tbody></table>' +
    (a.online ? '<p class="muted" style="margin-top:12px">Character JSON edits apply only while the account is offline.</p>'
      : '<div class="row" style="margin-top:12px"><a class="b" href="#character/' + encodeURIComponent(login) + '">Edit character data (JSON)</a></div>') +
    '</div>';
  E('role').value = a.role;
}
function jss(s) { return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"); }
async function setRole(login) { try { await api('account/role', { login: login, role: E('role').value }); amsg('ok', 'Role updated'); } catch (e) { amsg('err', e.message); } }
async function setPw(login) { var pw = E('newpw').value; try { await api('account/password', { login: login, password: pw }); amsg('ok', 'Password set'); E('newpw').value = ''; } catch (e) { amsg('err', e.message); } }
async function ban(login, on) { try { await api('account/ban', { login: login, seconds: on ? (parseInt(E('bansec').value) || 86400) : 0, reason: E('banrs').value }); pageAccount(login); } catch (e) { amsg('err', e.message); } }
async function delAcc(login) { if (!confirm('Delete ' + login + ' and all its characters? This cannot be undone.')) return; try { await api('account/delete', { login: login }); location.hash = '#accounts'; } catch (e) { amsg('err', e.message); } }
function amsg(cls, t) { var m = E('amsg'); m.className = 'msg ' + cls; m.textContent = t; }

async function pageCharacter(login) {
  var d = await api('hero/state?login=' + encodeURIComponent(login));
  V().innerHTML = '<h1>Character data: ' + esc(login) + '</h1><p class="sub"><a href="#account/' + encodeURIComponent(login) + '">&larr; Back</a> &middot; offline only</p>' +
    '<div class="box"><textarea id="json"></textarea><div class="row" style="margin-top:12px"><button class="b primary" onclick="saveChar(\'' + jss(login) + '\')">Save</button> <span id="cmsg" class="msg"></span></div></div>';
  E('json').value = JSON.stringify(d.state, null, 2);
}
async function saveChar(login) {
  var m = E('cmsg');
  var state; try { state = JSON.parse(E('json').value); } catch (e) { m.className = 'msg err'; m.textContent = 'Not valid JSON: ' + e.message; return; }
  try { await api('hero/save', { login: login, state: state }); m.className = 'msg ok'; m.textContent = 'Saved'; } catch (e) { m.className = 'msg err'; m.textContent = e.message; }
}

async function pageServers() {
  var d = await api('servers');
  var rows = d.servers.map(function (s) {
    return '<tr><td>' + esc(s.name) + '</td><td>' + esc(s.authAddress) + '</td><td>' + s.players + '</td>' +
      '<td>' + (s.online ? '<span class="pill on">online</span>' : '<span class="pill off">offline (' + s.ageSec + 's)</span>') + '</td></tr>';
  }).join('');
  V().innerHTML = '<h1>Community servers</h1><p class="sub">Servers registered on this hub. Players pick these in the region screen.</p>' +
    '<div class="box"><div class="toolbar"><button class="b" onclick="route()">Refresh</button></div>' +
    '<table><thead><tr><th>Region name</th><th>Auth address</th><th>Players</th><th>Status</th></tr></thead><tbody>' +
    (rows || '<tr><td colspan="4" class="muted">No servers registered.</td></tr>') + '</tbody></table></div>';
}

async function pageLogs() {
  var d = await api('logs?n=150');
  var audit = d.audit.map(function (r) { return '<tr><td class="muted">' + fmtTime(r.at) + '</td><td>' + esc(r.login || '') + '</td><td>' + esc(r.what) + '</td><td class="muted">' + esc(r.detail || '') + '</td></tr>'; }).join('');
  V().innerHTML = '<h1>Logs</h1>' +
    '<div class="box"><h2>Audit</h2><table><thead><tr><th>Time</th><th>Login</th><th>Action</th><th>Detail</th></tr></thead><tbody>' + audit + '</tbody></table></div>' +
    '<div class="box"><h2>Server log</h2><div class="toolbar"><button class="b" onclick="route()">Refresh</button></div><pre class="log">' + esc(d.log) + '</pre></div>';
}

async function pageSettings() {
  var d = await api('config'); var c = d.config;
  V().innerHTML = '<h1>Settings</h1>' +
    '<div class="box"><h2>Live settings</h2>' +
    '<div class="row"><label>Region name</label><input id="region" class="search" value="' + esc(c.regionName) + '"></div>' +
    '<div class="row"><label>Guests allowed</label><input type="checkbox" id="guests" ' + (c.guests ? 'checked' : '') + ' style="width:auto"></div>' +
    '<div class="row"><button class="b primary" onclick="saveConfig()">Apply</button> <span id="cfgmsg" class="msg"></span></div>' +
    '<p class="muted">Ports, host and hub secret are set at start (environment / server.csv) and need a restart to change.</p></div>' +
    '<div class="box"><h2>Change admin password</h2>' +
    '<div class="row"><label>Current</label><input id="oldp" type="password" class="search"></div>' +
    '<div class="row"><label>New (8+)</label><input id="newp" type="password" class="search"></div>' +
    '<div class="row"><button class="b primary" onclick="changeAdminPw()">Change</button> <span id="pwmsg" class="msg"></span></div></div>';
}
async function saveConfig() { try { await api('config', { regionName: E('region').value, guests: E('guests').checked }); var m = E('cfgmsg'); m.className = 'msg ok'; m.textContent = 'Applied'; } catch (e) { var m = E('cfgmsg'); m.className = 'msg err'; m.textContent = e.message; } }
async function changeAdminPw() { var m = E('pwmsg'); try { await api('admin/password', { old: E('oldp').value, new: E('newp').value }); m.className = 'msg ok'; m.textContent = 'Password changed'; E('oldp').value = E('newp').value = ''; } catch (e) { m.className = 'msg err'; m.textContent = e.message; } }

// ---- boot: are we already signed in? (dashboard probe) ----
(async function () {
  try { await api('dashboard'); E('login').style.display = 'none'; E('app').style.display = 'flex'; if (!location.hash) location.hash = '#dashboard'; route(); }
  catch (e) { showLogin(); }
})();
