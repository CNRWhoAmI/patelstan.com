'use strict';

/*
 * Geri Çek — Instagram "bilgilerini dışa aktar" dosyasından bekleyen takip
 * isteklerini çıkarır. Her şey tarayıcıda olur: zip'in yalnızca gereken
 * dosyası File.slice ile okunur, hiçbir veri ağa gönderilmez (CSP connect-src 'none').
 */

const STORE_KEY = 'gericek:v1';
const USERNAME_RE = /^[A-Za-z0-9._]{1,30}$/;
const USER_LABEL_RE = /user\s*name|kullan[ıi]c[ıi]\s*ad[ıi]/i;
const NAME_LABEL_RE = /^\s*(name|full name|ad|ad soyad|isim)\s*$/i;
const PENDING_JSON_RE = /(^|\/)pending_follow_requests\.json$/i;
const PENDING_HTML_RE = /(^|\/)pending_follow_requests\.html?$/i;
const CONNECTIONS_RE = /(^|\/)followers_and_following\//i;

const $ = (sel) => document.querySelector(sel);

const els = {
  drop: $('#drop'),
  file: $('#file'),
  status: $('#status'),
  steps: $('#steps'),
  results: $('#results'),
  list: $('#list'),
  doneCount: $('#doneCount'),
  totalCount: $('#totalCount'),
  progress: $('#progress'),
  nextBtn: $('#nextBtn'),
  hideDone: $('#hideDone'),
  sort: $('#sort'),
  copyBtn: $('#copyBtn'),
  clearBtn: $('#clearBtn'),
};

const state = {
  items: [],          // {key, username, name, date, order}
  done: new Set(),    // küçük harf kullanıcı adları
  visited: new Set(), // bu oturumda açılanlar
  sort: 'old',
  hideDone: false,
};

/* ------------------------------------------------------------------ */
/* ZIP okuma — sadece merkez dizini ve tek bir dosyayı okur            */
/* ------------------------------------------------------------------ */

async function readBytes(file, start, end) {
  return new DataView(await file.slice(start, end).arrayBuffer());
}

async function readZipEntries(file) {
  if (file.size < 22) throw new Error('Bu dosya geçerli bir zip değil.');

  const tailStart = Math.max(0, file.size - (22 + 0xffff + 20));
  const tail = await readBytes(file, tailStart, file.size);

  let eocd = -1;
  for (let i = tail.byteLength - 22; i >= 0; i--) {
    if (tail.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('Bu dosya geçerli bir zip değil.');

  let count = tail.getUint16(eocd + 10, true);
  let cdSize = tail.getUint32(eocd + 12, true);
  let cdOffset = tail.getUint32(eocd + 16, true);

  // 4 GB üstü export'lar ZIP64 kullanır
  const loc = eocd - 20;
  if (loc >= 0 && tail.getUint32(loc, true) === 0x07064b50) {
    const z64Offset = Number(tail.getBigUint64(loc + 8, true));
    const z64 = await readBytes(file, z64Offset, z64Offset + 56);
    if (z64.getUint32(0, true) === 0x06064b50) {
      count = Number(z64.getBigUint64(32, true));
      cdSize = Number(z64.getBigUint64(40, true));
      cdOffset = Number(z64.getBigUint64(48, true));
    }
  }

  const cd = await readBytes(file, cdOffset, cdOffset + cdSize);
  const decoder = new TextDecoder();
  const entries = [];
  let p = 0;

  for (let n = 0; n < count && p + 46 <= cd.byteLength; n++) {
    if (cd.getUint32(p, true) !== 0x02014b50) break;

    const flags = cd.getUint16(p + 8, true);
    const method = cd.getUint16(p + 10, true);
    let compSize = cd.getUint32(p + 20, true);
    let size = cd.getUint32(p + 24, true);
    const nameLen = cd.getUint16(p + 28, true);
    const extraLen = cd.getUint16(p + 30, true);
    const commentLen = cd.getUint16(p + 32, true);
    let localOffset = cd.getUint32(p + 42, true);
    const name = decoder.decode(new Uint8Array(cd.buffer, cd.byteOffset + p + 46, nameLen));

    let e = p + 46 + nameLen;
    const extraEnd = e + extraLen;
    while (e + 4 <= extraEnd) {
      const id = cd.getUint16(e, true);
      const len = cd.getUint16(e + 2, true);
      if (id === 0x0001) {
        let q = e + 4;
        if (size === 0xffffffff) { size = Number(cd.getBigUint64(q, true)); q += 8; }
        if (compSize === 0xffffffff) { compSize = Number(cd.getBigUint64(q, true)); q += 8; }
        if (localOffset === 0xffffffff) { localOffset = Number(cd.getBigUint64(q, true)); }
      }
      e += 4 + len;
    }

    entries.push({ name, flags, method, compSize, size, localOffset });
    p = extraEnd + commentLen;
  }
  return entries;
}

async function readZipEntryText(file, entry) {
  if (entry.flags & 1) throw new Error('Zip şifreli, açılamıyor.');

  const header = await readBytes(file, entry.localOffset, entry.localOffset + 30);
  if (header.getUint32(0, true) !== 0x04034b50) throw new Error('Zip bozuk görünüyor.');
  const start = entry.localOffset + 30 + header.getUint16(26, true) + header.getUint16(28, true);
  const blob = file.slice(start, start + entry.compSize);

  if (entry.method === 0) return blob.text();
  if (entry.method === 8) {
    if (typeof DecompressionStream === 'undefined') {
      throw new Error('Tarayıcın zip açmayı desteklemiyor. Zip\'i çıkar, içindeki pending_follow_requests dosyasını bırak.');
    }
    const stream = blob.stream().pipeThrough(new DecompressionStream('deflate-raw'));
    return new Response(stream).text();
  }
  throw new Error('Bu zip desteklenmeyen bir sıkıştırma kullanıyor.');
}

/* ------------------------------------------------------------------ */
/* Ayrıştırma                                                          */
/* ------------------------------------------------------------------ */

function cleanUsername(value) {
  const v = String(value || '').trim().replace(/^@/, '');
  return USERNAME_RE.test(v) ? v : '';
}

function usernameFromHref(href) {
  if (!href) return '';
  try {
    const url = new URL(href);
    // Bio'daki Spotify/VSCO vb. linkler kullanıcı adı değil
    if (!/(^|\.)instagram\.com$/i.test(url.hostname)) return '';
    const parts = url.pathname.split('/').filter(Boolean);
    return cleanUsername(parts[0] === '_u' ? parts[1] : parts[0]);
  } catch {
    return '';
  }
}

function findDate(el) {
  let node = el;
  for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
    const sib = node.nextElementSibling;
    if (!sib) continue;
    const text = sib.textContent.trim();
    return !sib.querySelector('table, a') && text.length < 60 ? text : '';
  }
  return '';
}

function parseHtml(text) {
  // Export'un kendi stil/base etiketleri sayfanın CSP'sine takılıp konsolu
  // kirletiyor; bize sadece metin ve tablolar lazım.
  const stripped = text
    .replace(/<(style|script)\b[\s\S]*?<\/\1\s*>/gi, '')
    .replace(/<(base|link|img|meta)\b[^>]*>/gi, '')
    .replace(/\sstyle\s*=\s*("[^"]*"|'[^']*')/gi, '');
  const doc = new DOMParser().parseFromString(stripped, 'text/html');
  const items = [];

  // Güncel biçim: her istek bir tablo, satırlar [etiket, değer]
  for (const table of doc.querySelectorAll('table')) {
    const rows = [...table.rows].map((tr) => {
      const cells = [...tr.cells];
      const valueCell = cells[cells.length - 1];
      const link = valueCell && valueCell.querySelector('a[href]');
      return {
        label: cells.length > 1 ? cells[0].textContent.trim() : '',
        value: valueCell ? valueCell.textContent.trim() : '',
        href: link ? link.getAttribute('href') : '',
      };
    });

    const userRow =
      rows.find((r) => USER_LABEL_RE.test(r.label)) ||
      [...rows].reverse().find((r) => cleanUsername(r.value) || usernameFromHref(r.href));
    if (!userRow) continue;

    const username = cleanUsername(userRow.value) || usernameFromHref(userRow.href);
    if (!username) continue;

    const isLink = (r) => /^https?:\/\//i.test(r.value) || /url|web|site|internet/i.test(r.label);
    const nameRow =
      rows.find((r) => r !== userRow && NAME_LABEL_RE.test(r.label)) ||
      rows.find((r) => r !== userRow && r.value && r.value !== username && !isLink(r));
    items.push({ username, name: nameRow ? nameRow.value : '', date: findDate(table), ts: null });
  }

  // Eski biçim: profil linkleri
  if (!items.length) {
    for (const a of doc.querySelectorAll('a[href*="instagram.com/"]')) {
      const username = usernameFromHref(a.getAttribute('href')) || cleanUsername(a.textContent);
      if (username) items.push({ username, name: '', date: findDate(a), ts: null });
    }
  }
  return items;
}

function parseJson(text) {
  const data = JSON.parse(text);
  const list = Array.isArray(data)
    ? data
    : data.relationships_follow_requests_sent || Object.values(data).find(Array.isArray) || [];

  return list
    .map((item) => {
      const sld = (item.string_list_data || [])[0] || {};
      const username =
        cleanUsername(sld.value) || cleanUsername(item.title) || usernameFromHref(sld.href);
      if (!username) return null;
      const ts = Number(sld.timestamp) || null;
      const date = ts
        ? new Date(ts * 1000).toLocaleString('tr-TR', { dateStyle: 'medium', timeStyle: 'short' })
        : '';
      return { username, name: '', date, ts };
    })
    .filter(Boolean);
}

function normalize(items) {
  const seen = new Set();
  const unique = [];
  for (const item of items) {
    const key = item.username.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push({ ...item, key });
  }

  // "order" küçükten büyüğe = en eskiden en yeniye
  const withTs = unique.map((it) => it.ts || Date.parse(it.date) || null);
  const first = withTs.find((t) => t);
  const last = [...withTs].reverse().find((t) => t);
  const fileIsOldestFirst = first && last && first < last;

  return unique.map((it, i) => ({
    key: it.key,
    username: it.username,
    name: it.name,
    date: it.date,
    order: fileIsOldestFirst ? i : unique.length - 1 - i,
  }));
}

/* ------------------------------------------------------------------ */
/* Dosya alma                                                          */
/* ------------------------------------------------------------------ */

async function extractFromFile(file) {
  const isZip = /\.zip$/i.test(file.name) || /zip/i.test(file.type);

  if (isZip) {
    const entries = await readZipEntries(file);
    const entry = entries.find((e) => PENDING_JSON_RE.test(e.name)) ||
                  entries.find((e) => PENDING_HTML_RE.test(e.name));
    if (!entry) {
      const hasConnections = entries.some((e) => CONNECTIONS_RE.test(e.name));
      throw new Error(
        hasConnections
          ? 'Bu export\'ta bekleyen takip isteği yok. Görünüşe göre şu an bekleyen isteğin kalmamış.'
          : 'Bu zip\'te takipçi bilgisi yok. Export birkaç parçaysa diğer zip\'leri de dene; yeni export alırken "Takipçiler ve takip edilenler"i seçtiğinden emin ol.'
      );
    }
    const text = await readZipEntryText(file, entry);
    return PENDING_JSON_RE.test(entry.name) ? parseJson(text) : parseHtml(text);
  }

  const text = await file.text();
  const looksJson = /\.json$/i.test(file.name) || /^\s*[[{]/.test(text);
  return looksJson ? parseJson(text) : parseHtml(text);
}

async function handleFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;

  setStatus('Dosya okunuyor…', 'busy');
  let lastError = null;

  for (const file of files) {
    try {
      const items = normalize(await extractFromFile(file));
      if (!items.length) throw new Error('Dosyada bekleyen takip isteği bulunamadı.');

      state.items = items;
      state.visited.clear();
      save();
      render();
      setStatus(`${items.length} bekleyen istek bulundu.`, 'ok');
      els.steps.open = false;
      els.results.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    } catch (err) {
      lastError = err;
    }
  }
  const message = lastError instanceof SyntaxError
    ? 'Dosya okunamadı. Instagram export zip\'ini ya da içindeki pending_follow_requests dosyasını seç.'
    : lastError.message;
  setStatus(message, 'error');
}

/* ------------------------------------------------------------------ */
/* Arayüz                                                              */
/* ------------------------------------------------------------------ */

function setStatus(text, kind = '') {
  els.status.textContent = text;
  els.status.dataset.kind = kind;
}

function profileUrl(username) {
  return `https://www.instagram.com/${encodeURIComponent(username)}/`;
}

function sortedItems() {
  const dir = state.sort === 'new' ? -1 : 1;
  return [...state.items].sort((a, b) => (a.order - b.order) * dir);
}

function buildRow(item) {
  const li = document.createElement('li');
  li.className = 'row';
  li.dataset.key = item.key;

  const check = document.createElement('label');
  check.className = 'row-check';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.setAttribute('aria-label', `@${item.username} geri çekildi`);
  box.addEventListener('change', () => setDone(item.key, box.checked));
  check.append(box);

  const who = document.createElement('div');
  who.className = 'who';
  const link = document.createElement('a');
  link.href = profileUrl(item.username);
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.className = 'handle';
  link.textContent = `@${item.username}`;
  link.addEventListener('click', () => markVisited(li));
  who.append(link);
  if (item.name) {
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.name;
    who.append(name);
  }

  const meta = document.createElement('div');
  meta.className = 'meta';
  if (item.date) {
    const time = document.createElement('span');
    time.className = 'date';
    time.textContent = item.date;
    meta.append(time);
  }
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.textContent = 'açıldı';
  meta.append(chip);

  const open = document.createElement('a');
  open.href = profileUrl(item.username);
  open.target = '_blank';
  open.rel = 'noopener noreferrer';
  open.className = 'btn small';
  open.textContent = 'Profili aç';
  open.addEventListener('click', () => markVisited(li));

  li.append(check, who, meta, open);
  return li;
}

function render() {
  els.list.replaceChildren(...sortedItems().map(buildRow));
  els.results.hidden = state.items.length === 0;
  els.sort.value = state.sort;
  els.hideDone.checked = state.hideDone;
  els.list.classList.toggle('hide-done', state.hideDone);
  refresh();
}

function refresh() {
  let done = 0;
  for (const row of els.list.children) {
    const isDone = state.done.has(row.dataset.key);
    if (isDone) done++;
    row.classList.toggle('done', isDone);
    row.classList.toggle('visited', state.visited.has(row.dataset.key));
    row.querySelector('input[type=checkbox]').checked = isDone;
  }
  const total = state.items.length;
  els.doneCount.textContent = done;
  els.totalCount.textContent = total;
  els.progress.max = Math.max(total, 1);
  els.progress.value = done;

  const remaining = total - done;
  els.nextBtn.disabled = remaining === 0;
  els.nextBtn.textContent = remaining === 0 ? 'Hepsi tamam 🎉' : `Sıradakini aç ↗  (${remaining} kaldı)`;
}

function setDone(key, value) {
  if (value) state.done.add(key); else state.done.delete(key);
  save();
  refresh();
}

function markVisited(row) {
  state.visited.add(row.dataset.key);
  for (const r of els.list.querySelectorAll('.current')) r.classList.remove('current');
  row.classList.add('current');
  refresh();
}

function openNext() {
  const pending = [...els.list.children].filter((r) => !state.done.has(r.dataset.key));
  if (!pending.length) return;
  const row = pending.find((r) => !state.visited.has(r.dataset.key)) || pending[0];
  const item = state.items.find((it) => it.key === row.dataset.key);
  window.open(profileUrl(item.username), '_blank', 'noopener,noreferrer');
  markVisited(row);
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* ------------------------------------------------------------------ */
/* Kalıcılık (sadece bu tarayıcı)                                      */
/* ------------------------------------------------------------------ */

function save() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      items: state.items,
      done: [...state.done],
      sort: state.sort,
      hideDone: state.hideDone,
    }));
  } catch { /* gizli sekme vb. — kaydetmeden devam */ }
}

function load() {
  try {
    const data = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (!data) return;
    state.items = Array.isArray(data.items) ? data.items : [];
    state.done = new Set(Array.isArray(data.done) ? data.done : []);
    state.sort = data.sort === 'new' ? 'new' : 'old';
    state.hideDone = Boolean(data.hideDone);
  } catch { /* bozuk kayıt — yok say */ }
}

/* ------------------------------------------------------------------ */
/* Olaylar                                                             */
/* ------------------------------------------------------------------ */

els.file.addEventListener('change', () => {
  handleFiles(els.file.files);
  els.file.value = '';
});

for (const type of ['dragenter', 'dragover']) {
  els.drop.addEventListener(type, (e) => {
    e.preventDefault();
    els.drop.classList.add('dragover');
  });
}
for (const type of ['dragleave', 'drop']) {
  els.drop.addEventListener(type, (e) => {
    e.preventDefault();
    els.drop.classList.remove('dragover');
  });
}
els.drop.addEventListener('drop', (e) => handleFiles(e.dataTransfer.files));
// Sayfanın başka yerine bırakılınca tarayıcı dosyayı açmasın
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

els.nextBtn.addEventListener('click', openNext);

els.hideDone.addEventListener('change', () => {
  state.hideDone = els.hideDone.checked;
  els.list.classList.toggle('hide-done', state.hideDone);
  save();
});

els.sort.addEventListener('change', () => {
  state.sort = els.sort.value;
  save();
  render();
});

els.copyBtn.addEventListener('click', async () => {
  const text = sortedItems().map((it) => it.username).join('\n');
  try {
    await navigator.clipboard.writeText(text);
    setStatus(`${state.items.length} kullanıcı adı panoya kopyalandı.`, 'ok');
  } catch {
    setStatus('Kopyalanamadı — tarayıcın pano erişimine izin vermedi.', 'error');
  }
});

els.clearBtn.addEventListener('click', () => {
  if (!confirm('Liste ve tüm işaretler bu tarayıcıdan silinsin mi?')) return;
  state.items = [];
  state.done.clear();
  state.visited.clear();
  try { localStorage.removeItem(STORE_KEY); } catch { /* yok say */ }
  render();
  els.steps.open = true;
  setStatus('Silindi.', 'ok');
});

// Instagram sekmesinden dönünce son açılan satırı göster
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  const current = els.list.querySelector('.current:not(.done)');
  if (current) {
    current.classList.remove('pulse');
    void current.offsetWidth;
    current.classList.add('pulse');
  }
});

load();
if (state.items.length) {
  els.steps.open = false;
  setStatus(`Kayıtlı liste yüklendi: ${state.items.length} istek.`, 'ok');
}
render();
