'use strict';
// Character management pages (2026-11-04): Characters list, Character (per hero), Inventory/mail (per account), Gifts.
// Uses the helpers from app.js: E, esc, fmtTime, api, V, kv, jss, route.

function note(id, cls, t) { var m = E(id); if (m) { m.className = 'msg ' + cls; m.textContent = t; } }
async function act(id, fn, okText, after) {
  note(id, '', '...');
  try { await fn(); note(id, 'ok', okText || 'Done'); if (after) after(); }
  catch (e) { note(id, 'err', e.message); }
}
async function kickLogin(login) {
  var d = await api('players'); var p = d.players.find(function (x) { return x.account === login; });
  if (!p) { route(); return; }
  if (!confirm('Kick ' + login + ' so the character can be edited?')) return;
  await api('kick', { sid: p.sid }); setTimeout(route, 400);
}
async function msgPlayer(sid) {
  var t = prompt('Message to this player (shows as a system line in their chat):'); if (!t) return;
  try { await api('player/message', { sid: sid, text: t }); alert('Sent'); } catch (e) { alert(e.message); }
}
function onlineBanner(login, online) {
  return online ? '<div class="box warn">This account is <b>online</b>. Editing needs it offline (the live session would overwrite changes). ' +
    'Gifts by mail work while online. <button class="b danger" onclick="kickLogin(\'' + jss(login) + '\')">Kick to edit</button></div>' : '';
}

// ---- Characters (every hero on the server) ----
var charSort = { key: 'level', dir: -1 }, charRows = [];
async function pageCharacters() {
  V().innerHTML = '<h1>Characters</h1><div class="box"><div class="toolbar">' +
    '<input id="cq" class="search" placeholder="Search name, account, class or map..." oninput="loadChars()">' +
    '<span id="ccount" class="muted"></span></div><div id="ctable"></div></div>';
  loadChars();
}
async function loadChars() {
  var d = await api('characters?q=' + encodeURIComponent(E('cq') ? E('cq').value : ''));
  charRows = d.characters; renderChars();
}
function sortChars(k) { charSort.dir = charSort.key === k ? -charSort.dir : (k === 'level' ? -1 : 1); charSort.key = k; renderChars(); }
function renderChars() {
  var k = charSort.key, dir = charSort.dir;
  var rows = charRows.slice().sort(function (a, b) { var x = a[k], y = b[k]; if (typeof x === 'string') { x = x.toLowerCase(); y = String(y).toLowerCase(); } return (x > y ? 1 : x < y ? -1 : 0) * dir; });
  var th = function (key, label) { return '<th style="cursor:pointer" onclick="sortChars(\'' + key + '\')">' + label + (charSort.key === key ? (charSort.dir > 0 ? ' &#9650;' : ' &#9660;') : '') + '</th>'; };
  E('ccount').textContent = rows.length + ' character(s)';
  E('ctable').innerHTML = '<table><thead><tr>' + th('name', 'Name') + th('className', 'Class') + th('level', 'Lv') + th('mapName', 'Location') + th('login', 'Account') + th('online', 'Status') + '</tr></thead><tbody>' +
    (rows.map(function (c) {
      return '<tr><td><a href="#hero/' + encodeURIComponent(c.login) + '/' + c.no + '">' + esc(c.name) + '</a></td><td>' + esc(c.className) + '</td><td>' + c.level + '</td><td>' + esc(c.mapName) + '</td>' +
        '<td><a href="#account/' + encodeURIComponent(c.login) + '">' + esc(c.login.length > 18 ? c.login.slice(0, 18) + '...' : c.login) + '</a></td>' +
        '<td>' + (c.online ? '<span class="pill on">online</span>' : '<span class="pill off">offline</span>') + (c.dead ? ' <span class="pill ban">dead</span>' : '') + '</td></tr>';
    }).join('') || '<tr><td colspan="6" class="muted">No characters.</td></tr>') + '</tbody></table>';
}

// ---- Character (one hero) ----
async function pageHero(login, no) {
  var d = await api('hero?login=' + encodeURIComponent(login) + '&no=' + no); var h = d.hero, s = h.stats;
  var dis = h.online ? ' disabled' : '';
  var statInputs = ['str', 'dex', 'int', 'con', 'agi', 'wis'].map(function (k) {
    return '<div class="stat"><label>' + k.toUpperCase() + '</label><input id="st_' + k + '" type="number" min="0" value="' + s[k] + '"' + dis + '></div>';
  }).join('');
  var opts = d.destinations.map(function (x) { return '<option value="' + x.map + '"' + (x.map === h.map ? ' selected' : '') + '>' + esc(x.name) + ' (' + x.map + ')</option>'; }).join('');
  var pct = h.maxExp ? Math.min(100, Math.round(100 * h.exp / h.maxExp)) : 0;
  V().innerHTML = '<h1>' + esc(h.name) + ' ' + (h.online ? '<span class="pill on">online</span>' : '<span class="pill off">offline</span>') + (h.dead ? ' <span class="pill ban">dead</span>' : '') + '</h1>' +
    '<p class="sub"><a href="#account/' + encodeURIComponent(login) + '">&larr; Account ' + esc(login.length > 24 ? login.slice(0, 24) + '...' : login) + '</a> &middot; <a href="#inventory/' + encodeURIComponent(login) + '">Inventory &amp; mail</a> &middot; <a href="#characters">All characters</a></p>' +
    onlineBanner(login, h.online) +
    '<div class="tiles">' + tile(h.level, 'Level') + tile(esc(h.className), 'Class') + tile(pct + '%', 'EXP ' + h.exp + ' / ' + h.maxExp) + tile(esc(h.mapName), 'Location') + '</div>' +
    '<div class="grid2">' +
    '<div class="box"><h2>Level &amp; experience</h2>' +
    '<div class="row"><label>Level (1-120)</label><input id="hlv" type="number" min="1" max="120" value="' + h.level + '" class="search" style="max-width:120px"' + dis + '></div>' +
    '<div class="row"><label>EXP</label><input id="hexp" type="number" min="0" value="' + h.exp + '" class="search" style="max-width:160px"' + dis + '><span class="muted">needs ' + h.maxExp + ' for next level</span></div>' +
    '<div class="row"><button class="b primary" onclick="heroSave(\'' + jss(login) + '\',' + h.no + ',\'lv\')"' + dis + '>Save level</button> ' +
    '<button class="b" onclick="E(\'hlv\').value=120;E(\'hexp\').value=0"' + dis + '>Max level</button></div>' +
    '<h2 style="margin-top:18px">Stats</h2><div class="stats">' + statInputs + '</div>' +
    '<div class="row"><label>Free points</label><input id="st_points" type="number" min="0" value="' + s.points + '" class="search" style="max-width:120px"' + dis + '>' +
    '<label style="min-width:auto">Level points</label><input id="st_levelpt" type="number" min="0" value="' + s.levelpt + '" class="search" style="max-width:120px"' + dis + '></div>' +
    '<div class="row"><button class="b primary" onclick="heroSave(\'' + jss(login) + '\',' + h.no + ',\'st\')"' + dis + '>Save stats</button></div>' +
    '<div class="row"><span id="hmsg" class="msg"></span></div></div>' +
    '<div><div class="box"><h2>Location</h2>' +
    kv('Now', esc(h.mapName) + (h.pos ? ' <span class="muted">(' + h.pos.join(', ') + ')</span>' : '')) +
    '<div class="row"><select id="hdest"' + dis + '>' + opts + '</select></div>' +
    '<div class="row"><button class="b primary" onclick="heroTp(\'' + jss(login) + '\',' + h.no + ',false)"' + dis + '>Teleport</button> ' +
    '<button class="b" onclick="heroTp(\'' + jss(login) + '\',' + h.no + ',true)"' + dis + '>Send to town</button></div>' +
    '<p class="muted">Takes effect on the next login. Use "Send to town" for a character stuck in a wall or a bad map.</p>' +
    '<div class="row"><span id="tmsg" class="msg"></span></div></div>' +
    '<div class="box"><h2>Character</h2>' +
    '<div class="row"><label>Rename</label><input id="hname" class="search" value="' + esc(h.name) + '"' + dis + '><button class="b" onclick="heroRename(\'' + jss(login) + '\',' + h.no + ')"' + dis + '>Rename</button></div>' +
    '<p class="muted">Friends and blocks are stored by name; other players may need to re-add a renamed character.</p>' +
    '<div class="row"><button class="b" onclick="heroFlag(\'' + jss(login) + '\',' + h.no + ',\'revive\')"' + dis + '>Revive' + (h.dead ? '' : ' (not dead)') + '</button> ' +
    '<button class="b" onclick="heroFlag(\'' + jss(login) + '\',' + h.no + ',\'clearPenalties\')"' + dis + '>Clear death penalties (' + h.penalties + ')</button></div>' +
    '<div class="row" style="margin-top:12px"><button class="b danger" onclick="heroDelete(\'' + jss(login) + '\',' + h.no + ',\'' + jss(h.name) + '\')"' + dis + '>Delete character</button></div>' +
    '<div class="row"><span id="cmsg2" class="msg"></span></div></div></div>' +
    '</div>';
}
function heroSave(login, no, part) {
  var body = { login: login, no: no };
  if (part === 'lv') { body.level = parseInt(E('hlv').value); body.exp = parseInt(E('hexp').value) || 0; }
  else { body.stats = {}; ['str', 'dex', 'int', 'con', 'agi', 'wis', 'points', 'levelpt'].forEach(function (k) { body.stats[k] = parseInt(E('st_' + k).value) || 0; }); }
  act('hmsg', function () { return api('hero/edit', body); }, 'Saved', function () { setTimeout(function () { pageHero(login, no); }, 500); });
}
function heroRename(login, no) {
  var n = E('hname').value.trim();
  act('cmsg2', function () { return api('hero/edit', { login: login, no: no, name: n }); }, 'Renamed', function () { setTimeout(function () { pageHero(login, no); }, 500); });
}
function heroFlag(login, no, flag) {
  var b = { login: login, no: no }; b[flag] = true;
  act('cmsg2', function () { return api('hero/edit', b); }, 'Done', function () { setTimeout(function () { pageHero(login, no); }, 500); });
}
function heroTp(login, no, town) {
  var b = { login: login, no: no }; if (town) b.town = true; else b.map = parseInt(E('hdest').value);
  act('tmsg', function () { return api('hero/teleport', b); }, town ? 'Sent to town' : 'Teleported', function () { setTimeout(function () { pageHero(login, no); }, 500); });
}
async function heroDelete(login, no, name) {
  if (!confirm('Delete the character ' + name + '? This cannot be undone.')) return;
  if (prompt('Type the character name to confirm:') !== name) { alert('Name did not match; nothing deleted.'); return; }
  try { await api('hero/delete', { login: login, no: no }); location.hash = '#account/' + encodeURIComponent(login); } catch (e) { note('cmsg2', 'err', e.message); }
}

// ---- Inventory, gold and mail (one account) ----
async function pageInventory(login) {
  var d = await api('inventory?login=' + encodeURIComponent(login));
  var dis = d.online ? ' disabled' : '';
  var bag = d.bag.map(function (b) {
    return '<tr><td>' + esc(b.name) + ' <span class="muted">#' + b.index + '</span></td><td>' + esc(b.type) + '</td><td>' + esc(b.rarity) + '</td><td>' + esc(b.slot) + '</td><td>' + b.count + '</td>' +
      '<td>' + (b.equipped ? '<span class="pill gm">worn: ' + esc(b.equipped) + '</span>' : '') + '</td><td style="white-space:nowrap">' +
      (b.equipped ? '<button class="b" onclick="invUnequip(\'' + jss(login) + '\',' + b.serial + ')"' + dis + '>Unequip</button> ' : '') +
      (b.count > 1 ? '<button class="b" onclick="invRemove(\'' + jss(login) + '\',' + b.serial + ',true)"' + dis + '>-N</button> ' : '') +
      '<button class="b danger" onclick="invRemove(\'' + jss(login) + '\',' + b.serial + ',false)"' + dis + '>Remove</button></td></tr>';
  }).join('');
  var mails = d.mails.map(function (m) {
    return '<tr><td class="muted">' + fmtTime(m.time) + '</td><td>' + esc(m.title) + '</td><td>' + esc(m.reward) + '</td>' +
      '<td><button class="b danger" onclick="mailDel(\'' + jss(login) + '\',' + m.no + ')">Delete</button></td></tr>';
  }).join('');
  V().innerHTML = '<h1>Inventory: ' + esc(login.length > 28 ? login.slice(0, 28) + '...' : login) + '</h1>' +
    '<p class="sub"><a href="#account/' + encodeURIComponent(login) + '">&larr; Account</a></p>' +
    onlineBanner(login, d.online) +
    '<div class="grid2"><div class="box"><h2>Gold</h2>' +
    '<div class="tiles" style="margin-bottom:10px">' + tile(Number(d.gold).toLocaleString(), 'Gold') + '</div>' +
    '<div class="row"><input id="gamt" type="number" class="search" style="max-width:160px" placeholder="amount"' + dis + '>' +
    '<button class="b primary" onclick="goldDo(\'' + jss(login) + '\',\'add\')"' + dis + '>Add</button> ' +
    '<button class="b" onclick="goldDo(\'' + jss(login) + '\',\'sub\')"' + dis + '>Take</button> ' +
    '<button class="b" onclick="goldDo(\'' + jss(login) + '\',\'set\')"' + dis + '>Set to</button></div>' +
    '<div class="row"><span id="gmsg" class="msg"></span></div></div>' +
    '<div class="box"><h2>Account progress</h2>' + kv('Quests done', d.questsDone) + kv('Attendance', d.attendance.day ? ('day ' + d.attendance.day + ' (last ' + esc(d.attendance.last) + ')') : 'not started') +
    '<div class="row"><button class="b" onclick="accReset(\'' + jss(login) + '\',\'quests\')"' + dis + '>Reset quests</button> ' +
    '<button class="b" onclick="accReset(\'' + jss(login) + '\',\'attendance\')"' + dis + '>Reset attendance</button></div>' +
    '<div class="row"><span id="rmsg" class="msg"></span></div></div></div>' +
    '<div class="box"><h2>Add item to bag</h2>' + itemPicker('ia', dis) +
    '<div class="row"><label>Count</label><input id="ia_count" type="number" min="1" value="1" class="search" style="max-width:110px"' + dis + '>' +
    '<button class="b primary" onclick="invAdd(\'' + jss(login) + '\')"' + dis + '>Add to bag</button> <span id="iamsg" class="msg"></span></div>' +
    (d.online ? '<p class="muted">Online: use "Send by mail" below instead; it reaches the player without a kick.</p>' : '') + '</div>' +
    '<div class="box"><h2>Bag (' + d.bag.length + ')</h2><table><thead><tr><th>Item</th><th>Type</th><th>Rarity</th><th>Slot</th><th>Count</th><th></th><th></th></tr></thead><tbody>' +
    (bag || '<tr><td colspan="7" class="muted">Empty.</td></tr>') + '</tbody></table><div class="row"><span id="bmsg" class="msg"></span></div></div>' +
    '<div class="box"><h2>Send by mail (works online)</h2>' + mailForm('ml', false) +
    '<div class="row"><button class="b primary" onclick="mailSend(\'ml\',\'' + jss(login) + '\')">Send mail</button> <span id="mlmsg" class="msg"></span></div></div>' +
    '<div class="box"><h2>Mailbox (' + d.mails.length + ')</h2><table><thead><tr><th>Sent</th><th>Title</th><th>Reward</th><th></th></tr></thead><tbody>' +
    (mails || '<tr><td colspan="4" class="muted">No mail waiting.</td></tr>') + '</tbody></table></div>';
}
function invAdd(login) {
  var idx = parseInt(E('ia_index').value); if (!idx) { note('iamsg', 'err', 'pick an item first'); return; }
  act('iamsg', function () { return api('inventory/add', { login: login, index: idx, count: parseInt(E('ia_count').value) || 1 }); }, 'Added', function () { setTimeout(function () { pageInventory(login); }, 500); });
}
async function invRemove(login, serial, some) {
  var n = null;
  if (some) { n = parseInt(prompt('How many to remove?', '1')); if (!n) return; }
  else if (!confirm('Remove this item from the bag?')) return;
  try { await api('inventory/remove', { login: login, serial: serial, count: n }); pageInventory(login); } catch (e) { note('bmsg', 'err', e.message); }
}
async function invUnequip(login, serial) { try { await api('inventory/unequip', { login: login, serial: serial }); pageInventory(login); } catch (e) { note('bmsg', 'err', e.message); } }
function goldDo(login, how) {
  var v = parseInt(E('gamt').value); if (isNaN(v)) { note('gmsg', 'err', 'enter an amount'); return; }
  var b = { login: login }; if (how === 'set') b.set = v; else b.add = how === 'sub' ? -v : v;
  act('gmsg', function () { return api('gold', b); }, 'Gold updated', function () { setTimeout(function () { pageInventory(login); }, 400); });
}
function accReset(login, what) {
  if (!confirm('Reset ' + what + ' for this account?')) return;
  act('rmsg', function () { return api('account/reset', { login: login, what: what }); }, 'Reset', function () { setTimeout(function () { pageInventory(login); }, 400); });
}
async function mailDel(login, no) { await api('mail/delete', { login: login, no: no }); pageInventory(login); }

// ---- shared item picker + mail form ----
var pickTimer = null;
function itemPicker(p, dis) {
  return '<div class="row"><label>Item</label><input id="' + p + '_q" class="search" placeholder="type an item name or index..." oninput="pickSearch(\'' + p + '\')"' + (dis || '') + '>' +
    '<input id="' + p + '_index" type="hidden"><span id="' + p + '_sel" class="muted"></span></div><div id="' + p + '_res" class="picks"></div>';
}
function pickSearch(p) {
  clearTimeout(pickTimer);
  pickTimer = setTimeout(async function () {
    var q = E(p + '_q').value.trim(); if (q.length < 2) { E(p + '_res').innerHTML = ''; return; }
    var d = await api('items/search?q=' + encodeURIComponent(q));
    E(p + '_res').innerHTML = d.items.map(function (i) {
      return '<div class="pick" onclick="pickItem(\'' + p + '\',' + i.index + ',\'' + jss(i.name) + '\')">' + esc(i.name) + ' <span class="muted">#' + i.index + ' &middot; ' + esc(i.type) + (i.slot ? ' &middot; ' + esc(i.slot) : '') + (i.rarity ? ' &middot; ' + esc(i.rarity) : '') + '</span></div>';
    }).join('') || '<div class="muted">No match.</div>';
  }, 200);
}
function pickItem(p, idx, name) { E(p + '_index').value = idx; E(p + '_sel').textContent = 'Selected: ' + name + ' (#' + idx + ')'; E(p + '_res').innerHTML = ''; E(p + '_q').value = name; }
function mailForm(p) {
  return '<div class="row"><label>Gift</label><select id="' + p + '_kind" onchange="E(\'' + p + '_itemrow\').style.display=this.value===\'gold\'?\'none\':\'block\'"><option value="item">Item</option><option value="gold">Gold</option></select></div>' +
    '<div id="' + p + '_itemrow">' + itemPicker(p) + '</div>' +
    '<div class="row"><label>Count / gold</label><input id="' + p + '_count" type="number" min="1" value="1" class="search" style="max-width:140px"></div>' +
    '<div class="row"><label>Title</label><input id="' + p + '_title" class="search" maxlength="40" value="A gift from the team"></div>' +
    '<div class="row"><label>Message</label><input id="' + p + '_msg" class="search" maxlength="200" style="max-width:520px" placeholder="optional"></div>';
}
function mailBody(p) {
  var kind = E(p + '_kind').value;
  var b = { kind: kind, count: parseInt(E(p + '_count').value) || 1, title: E(p + '_title').value, msg: E(p + '_msg').value };
  if (kind === 'item') { b.index = parseInt(E(p + '_index').value); if (!b.index) throw new Error('pick an item first'); }
  return b;
}
function mailSend(p, login) {
  var b; try { b = mailBody(p); } catch (e) { note(p + 'msg', 'err', e.message); return; }
  b.login = login;
  act(p + 'msg', function () { return api('mail/send', b); }, 'Mail sent', function () { setTimeout(function () { pageInventory(login); }, 600); });
}

// ---- Gifts (mail to one account or everyone) ----
async function pageGifts() {
  V().innerHTML = '<h1>Gifts &amp; mail</h1><p class="sub">System mail with an item or gold. Players claim it from their in-game mailbox; works while they are online.</p>' +
    '<div class="box"><h2>Recipient</h2><div class="row"><label>Send to</label><select id="gto" onchange="E(\'gacc\').style.display=this.value===\'one\'?\'inline-block\':\'none\'">' +
    '<option value="one">One account</option><option value="all">Every account</option></select>' +
    '<input id="gacc" class="search" placeholder="account login"></div></div>' +
    '<div class="box"><h2>Gift</h2>' + mailForm('gf') +
    '<div class="row"><button class="b primary" onclick="giftSend()">Send gift</button> <span id="gfmsg" class="msg"></span></div></div>';
}
async function giftSend() {
  var b; try { b = mailBody('gf'); } catch (e) { note('gfmsg', 'err', e.message); return; }
  if (E('gto').value === 'all') {
    b.all = true;
    if (!confirm('Send this gift to EVERY account on the server?')) return;
  } else {
    b.login = E('gacc').value.trim(); if (!b.login) { note('gfmsg', 'err', 'enter an account login'); return; }
  }
  note('gfmsg', '', '...');
  try { var r = await api('mail/send', b); note('gfmsg', 'ok', 'Sent to ' + r.sent + ' account(s)'); } catch (e) { note('gfmsg', 'err', e.message); }
}
