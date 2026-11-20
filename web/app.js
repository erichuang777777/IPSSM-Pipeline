/* IPSS-M 本地網頁介面 — 純原生 JavaScript，無外部資源。
 * 所有動態文字都經由 el() 以 textContent 加入 DOM (不使用 innerHTML)，
 * 因為病患 ID、欄位值等可能含有特殊字元。 */
'use strict';

// ---------------------------------------------------------------------------
//  工具
// ---------------------------------------------------------------------------
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'html') throw new Error('html 屬性已停用 (避免 XSS)');
    else if (k === 'value') n.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') n[k] = !!v;
    else n.setAttribute(k, v === true ? '' : v);
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else n.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return n;
}
const $ = (s, r = document) => r.querySelector(s);
// 表頭: 結尾加 # 代表數字欄 (靠右對齊)
const TH = (h) => el('th', { class: h.endsWith('#') ? 'num' : '' }, h.replace(/#$/, ''));

async function api(method, path, body) {
  const opts = { method, headers: { 'X-IPSSM-Request': '1' }, credentials: 'same-origin' };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); }
  catch (e) { throw Object.assign(new Error('無法連線到程式，可能已經結束。請重新執行啟動程式。'), { status: 0 }); }
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { /* 非 JSON */ }
  if (!res.ok) throw Object.assign(new Error((data && data.message) || `發生錯誤 (${res.status})`), { status: res.status, data });
  return data;
}

function toast(msg, kind) {
  const t = el('div', { class: 'toast ' + (kind || '') }, msg);
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmtSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB');
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* 隱私模式 */ } },
};

const MODES = {
  auto: ['自動判斷（建議）', '先試算：缺失只造成小範圍不確定的病人，用平均值補上並計算；範圍太大的病人不計算，改列在「UNCERTAIN 清單」，並保留它的風險區間供你參考。'],
  impute: ['全部保留，缺失用平均值補', '所有病人都計算。缺失的欄位以人群平均值補上；若缺失讓結果範圍很大，只會標示「UNCERTAIN」，仍會給出分數。'],
  skip: ['只算資料完整的病人', '任何一個模型欄位缺失的病人都不計算。結果完全建立在實際檢測的資料上，但可能排除很多人。'],
};
const RULE_TEXT = {
  nd_not_detected_to_0: 'ND（檢測過、未偵測到）視為 0', binary_keyword: '文字轉成 0 / 1', numeric_binary: '1.0 轉成 1',
  unit_g_per_l_to_g_per_dl: '單位 g/L 換算成 g/dL', decimal_comma: '小數逗號改成小數點', percent_to_fraction: '百分比換算成 0–1 的小數',
  vaf_percent_assumed: '大於 1，推測是百分比，換算成 0–1', llm_interpretation: 'LLM 判讀', na_alias: '空白 / NA 標準化為缺失',
  strip_unit: '去除單位', cyto_case_normalized: '分類名稱寫法統一', tp53mut_multi_alias: 'TP53 多重突變寫法統一',
  nd_to_na: 'ND 無法對應成數值，視為缺失', id_from_row: '沒有 ID，改用列位置代替', unparsed: '無法解析',
};
const EXCL_TEXT = { validation: '資料有誤', missing: '有缺失（跳過模式）', uncertain: '缺失造成範圍過大 (UNCERTAIN)' };
const STEPS = ['選擇檔案', '設定與檢查', '核對資料', '結果'];
const SCREEN_STEP = { upload: 0, settings: 1, running: 1, review: 2, results: 3 };

// ---------------------------------------------------------------------------
//  狀態
// ---------------------------------------------------------------------------
const S = {
  screen: 'upload', status: null, busy: null, job: null, inspect: null, sel: [], run: null, runLabel: null,
  review: null, downloads: [], tab: 'todo', rows: null, rowsQ: { q: '', flagged: false, offset: 0 },
  highlight: null, results: null, resFilter: 'all', auditFilter: 'needs_review', auditShown: 200,
  options: {
    missing: 'auto', max_range: 1, sheet_join: '',
    llm: { enabled: false, provider: 'openai', endpoint: 'http://localhost:11434/v1', model: '', api_key: '',
           min_confidence: 0.8, allow_remote: false },
  },
  llmTest: null, reviewer: store.get('ipssm_reviewer') || '', confirmed: false, scroll: {},
};
let pollTimer = null;

function go(screen) { S.screen = screen; render(); window.scrollTo(0, 0); }

// ---------------------------------------------------------------------------
//  共用元件
// ---------------------------------------------------------------------------
function dialog(title, body, actions) {
  const d = $('#dialog');
  d.replaceChildren(el('div', { class: 'dlg' }, el('h2', {}, title), body,
    el('div', { class: 'row', style: 'justify-content:flex-end;margin-top:18px' },
      (actions || [{ label: '關閉' }]).map((a) => el('button', {
        class: 'btn ' + (a.primary ? 'btn-primary' : a.danger ? 'btn-danger' : ''), type: 'button',
        onclick: async () => { if (!a.keepOpen) d.close(); if (a.onclick) await a.onclick(); },
      }, a.label)))));
  d.showModal();
}

function glossary() {
  const dl = el('dl', {},
    el('dt', {}, 'IPSS-M'), el('dd', {}, '用臨床資料、染色體與基因突變估計骨髓增生異常症候群 (MDS) 風險的分數。分數越高風險越高，分成六級：Very Low 到 Very High。'),
    el('dt', {}, 'Best / Mean / Worst'), el('dd', {}, '有欄位缺失時，程式會算三種情況：Best＝缺的欄位對病人最有利、Worst＝最不利、Mean＝用人群平均值補上。最終分數採用 Mean，Best 到 Worst 的差距就是缺失造成的不確定程度。'),
    el('dt', {}, 'CONFIDENT / UNCERTAIN'), el('dd', {}, '範圍（Worst − Best）不超過 1 標示 CONFIDENT，結果可信；超過 1 標示 UNCERTAIN，缺失可能讓病人跨好幾個風險等級，請謹慎解讀。'),
    el('dt', {}, '缺失（空白、NA）≠ 0'), el('dd', {}, '空白與 NA 都代表「未知」。0 代表「檢測過、沒有突變」。把未檢測誤填成 0 會低估風險。'),
    el('dt', {}, 'ND'), el('dd', {}, 'Not Detected：有檢測、沒偵測到突變，程式會當作 0（只限基因、染色體異常與 TP53mut 欄位）。'),
    el('dt', {}, '請不要自己先補平均值'), el('dd', {}, '自己補值會讓程式以為資料完整、範圍變小而標成 CONFIDENT。請保留空白，讓程式處理並記錄。'),
    el('dt', {}, '跨欄位矛盾'), el('dd', {}, '各欄位單獨看都合理，但彼此衝突（例如 TP53 沒突變卻有 VAF）。只提醒，不會自動修改，請你判斷哪個值才對。'));
  dialog('名詞說明', dl);
}

function copyButton(text, label) {
  return el('button', { class: 'btn btn-sm', type: 'button', onclick: async () => {
    try { await navigator.clipboard.writeText(text); toast('已複製', 'ok'); } catch (e) { toast('無法自動複製，請手動選取文字', 'bad'); }
  } }, label || '複製');
}

function rStatusBanner() {
  const r = S.status && S.status.r;
  if (!r || r.ready) return null;
  const cmd = 'install.packages("remotes"); remotes::install_github("papaemmelab/ipssm")';
  return el('div', { class: 'callout warn' },
    el('strong', { class: 't' }, r.rscript ? '已找到 R，但還沒有安裝 ipssm 套件' : '找不到 R'),
    el('p', {}, r.rscript
      ? '計算需要官方的 ipssm 套件，只需安裝一次。開啟 R（或 RStudio），貼上下面這行並執行（需要網路）：'
      : '計算需要 R（4.3 版以上）與官方 ipssm 套件，兩者都只需安裝一次。請先到 R 官方網站 cran.r-project.org 下載安裝 R，再開啟 R（或 RStudio），貼上下面這行並執行（需要網路）：'),
    el('div', { class: 'row' }, el('code', { class: 'mono' }, cmd), copyButton(cmd)),
    el('p', { class: 'small muted', style: 'margin-top:8px' }, '安裝完成後按「重新檢查」。在此之前仍可上傳與整理資料，但無法試算與計算。'),
    el('button', { class: 'btn btn-sm', type: 'button', onclick: refreshStatus }, '重新檢查'));
}

// ---------------------------------------------------------------------------
//  標頭
// ---------------------------------------------------------------------------
function renderChrome() {
  const cur = SCREEN_STEP[S.screen];
  $('#steps').replaceChildren(...STEPS.map((t, i) => el('li', { class: i < cur ? 'done' : i === cur ? 'now' : '' },
    el('span', { class: 'n' }, i < cur ? '✓' : String(i + 1)), el('span', { class: 't' }, t))));
  const pill = $('#r-pill'), r = S.status && S.status.r;
  pill.className = 'pill ' + (!r ? 'pill-muted' : r.ready ? 'pill-ok' : 'pill-warn');
  pill.textContent = !r ? '檢查 R 環境…' : r.ready ? 'R 與 ipssm 已就緒' : (r.rscript ? '缺少 ipssm 套件' : '找不到 R');
}

async function refreshStatus() {
  try { S.status = await api('GET', '/api/status?force=1'); } catch (e) { toast(e.message, 'bad'); }
  render();
}

// ---------------------------------------------------------------------------
//  1. 上傳
// ---------------------------------------------------------------------------
function screenUpload() {
  const input = el('input', { type: 'file', accept: '.xlsx,.xlsm,.csv,.tsv', hidden: true, onchange: (e) => e.target.files[0] && uploadFile(e.target.files[0]) });
  const dz = el('div', { class: 'dropzone', tabindex: '0', role: 'button', 'aria-label': '選擇或拖放檔案',
    onclick: () => input.click(), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } },
    ondragover: (e) => { e.preventDefault(); dz.classList.add('over'); }, ondragleave: () => dz.classList.remove('over'),
    ondrop: (e) => { e.preventDefault(); dz.classList.remove('over'); if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); } },
    el('div', { class: 'big' }, S.busy === 'upload' ? '上傳並讀取中…' : '把檔案拖到這裡，或點一下選擇'),
    el('div', { class: 'muted' }, '支援 Excel（.xlsx）與 CSV。可以直接用醫院匯出的原始檔，有多個分頁也沒關係。'), input);

  const resume = store.get('ipssm_job');
  return el('div', {},
    el('div', { class: 'hero' }, el('h1', {}, '把病患資料變成 IPSS-M 風險分數'),
      el('p', {}, '上傳檔案後，程式會幫你整理格式、找出缺漏與矛盾，讓你核對確認後才計算。全程在這台電腦上進行。')),
    rStatusBanner(), dz,
    resume && !S.busy ? el('div', { class: 'row', style: 'justify-content:center;margin-top:14px' },
      el('button', { class: 'btn', type: 'button', onclick: () => resumeJob(resume) }, '繼續上次的工作')) : null,
    el('ol', { class: 'how' },
      el('li', {}, el('b', {}, '上傳檔案'), '選擇原始 Excel 或 CSV，有多個分頁時由你決定要用哪一頁。'),
      el('li', {}, el('b', {}, '整理與檢查'), '自動統一格式、換算單位，並找出缺失與互相矛盾的資料。'),
      el('li', {}, el('b', {}, '核對與修改'), '逐項檢視程式動過什麼，可直接修改，確認無誤再繼續。'),
      el('li', {}, el('b', {}, '取得結果'), '下載含風險分數、信心等級與完整紀錄的 Excel。')),
    el('div', { class: 'callout info' }, el('strong', { class: 't' }, '關於你的資料'),
      '檔案只會存放在這台電腦的工作資料夾（', el('span', { class: 'mono' }, S.status ? S.status.workspace : '…'),
      '），不會傳到網路上。用完可在結果頁按「清除這次的資料」刪除。請使用已去識別化的資料。'));
}

async function uploadFile(file) {
  S.busy = 'upload'; render();
  try {
    const res = await fetch('/api/upload?name=' + encodeURIComponent(file.name), {
      method: 'POST', credentials: 'same-origin', body: file,
      headers: { 'X-IPSSM-Request': '1', 'Content-Type': 'application/octet-stream' } });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '上傳失敗');
    S.job = data.job; S.inspect = data.inspect; S.review = null; S.results = null; S.confirmed = false;
    S.options.sheet_join = ''; initSelection(); store.set('ipssm_job', S.job.id);
    S.busy = null; go('settings');
  } catch (e) { S.busy = null; render(); toast(e.message, 'bad'); }
}

async function resumeJob(id) {
  try {
    const data = await api('GET', '/api/jobs/' + id);
    S.job = data.job; S.review = data.review || null; S.downloads = data.downloads;
    if (data.job.stage === 'calculated') { await loadResults(); go('results'); }
    else if (data.job.stage === 'reviewed' && S.review) { S.tab = S.review.items.length ? 'todo' : 'data'; await loadRows(); go('review'); }
    else { S.inspect = await api('POST', `/api/jobs/${id}/inspect`, {}); initSelection(); go('settings'); }
  } catch (e) { store.set('ipssm_job', null); toast('找不到上次的工作（資料可能已清除）', 'bad'); render(); }
}

// ---------------------------------------------------------------------------
//  2. 設定
// ---------------------------------------------------------------------------
function initSelection() {
  const info = S.inspect && (S.inspect.source || {});
  const sheets = (S.inspect && S.inspect.sheets) || [];
  const chosen = (info && info.selected) || [];
  S.sel = sheets.map((s, i) => (chosen.includes(s.name) ? i + 1 : 0)).filter(Boolean);
  S.selTouched = false;
}

async function reinspect() {
  try {
    S.inspect = await api('POST', `/api/jobs/${S.job.id}/inspect`, { sheet: S.selTouched ? S.sel.join(',') : '', sheet_join: S.options.sheet_join });
    if (S.inspect.ok && !S.selTouched) initSelection();
  } catch (e) { toast(e.message, 'bad'); }
  render();
}

function sheetCard() {
  const sheets = (S.inspect && S.inspect.sheets) || [];
  if (sheets.length <= 1 && S.job.ext !== '.xlsx' && S.job.ext !== '.xlsm') return null;
  const amb = S.inspect && S.inspect.ambiguous && !S.inspect.ok;
  const rows = sheets.map((s, i) => {
    const idx = i + 1, on = S.sel.includes(idx);
    return el('tr', {}, el('td', {}, el('input', { type: 'checkbox', checked: on, 'aria-label': `使用分頁 ${s.name}`, onchange: () => {
      S.selTouched = true; S.sel = on ? S.sel.filter((x) => x !== idx) : [...S.sel, idx].sort((a, b) => a - b);
      if (S.sel.length < 2) S.options.sheet_join = ''; reinspect(); } })),
      el('td', {}, el('strong', {}, s.name), s.state !== 'visible' ? el('span', { class: 'pill pill-muted', style: 'margin-left:6px' }, '隱藏') : null),
      el('td', { class: 'num' }, s.rows), el('td', { class: 'num' }, `第 ${s.header_row} 列`), el('td', { class: 'num' }, s.matched),
      el('td', {}, s.looks_like_data ? el('span', { class: 'pill pill-ok' }, '像病患資料') : el('span', { class: 'pill pill-muted' }, '不像')));
  });
  return el('div', { class: 'card' }, el('h2', {}, el('span', { class: 'num' }, '2'), '選擇要用的分頁'),
    amb ? el('div', { class: 'callout warn' }, el('strong', { class: 't' }, '無法自動決定，需要你選'), S.inspect.message) :
      el('p', { class: 'muted' }, '程式已依欄位內容選出最像病患資料的分頁。如果資料分散在多個分頁，可以勾選多個，一起計算。'),
    el('div', { class: 'table-wrap', style: 'max-height:none' }, el('table', {},
      el('thead', {}, el('tr', {}, ['用', '分頁', '資料列#', '表頭位置#', '對得上的標準欄位#', ''].map(TH))), el('tbody', {}, rows))),
    S.sel.length >= 2 ? el('div', { style: 'margin-top:12px' }, el('label', { class: 'field' }, '多個分頁怎麼合併？',
      el('select', { onchange: (e) => { S.options.sheet_join = e.target.value; reinspect(); } },
        [['', '自動判斷'], ['stack', '上下串接（各分頁欄位相同，例如不同年度或批次）'], ['merge', '依病患 ID 合併（各分頁欄位不同，例如臨床頁＋基因頁）']]
          .map(([v, t]) => el('option', { value: v, selected: S.options.sheet_join === v }, t))),
      el('span', { class: 'hint' }, '合併時若某位病患只出現在其中一頁，另一頁的欄位會視為缺失，並在核對頁提醒你。'))) : null);
}

function mappingCard() {
  const ins = S.inspect;
  if (!ins || !ins.ok) return null;
  const miss = ins.missing_required || [];
  const matched = Object.entries(ins.matched);
  return el('div', { class: 'card' }, el('h2', {}, el('span', { class: 'num' }, '3'), '欄位對應'),
    miss.length ? el('div', { class: 'callout bad' }, el('strong', { class: 't' }, `找不到必要欄位：${miss.join('、')}`),
      '這些欄位在檔案裡找不到對應的欄名，所有病人的這些欄位都會被當成缺失。請確認欄名（例如 Hb、血紅素），必要時改欄名後重新上傳。')
      : el('p', {}, el('span', { class: 'pill pill-ok' }, `已對應 ${matched.length} 個欄位`), ' 臨床欄位（HB、PLT、BM_BLAST）都找到了。'),
    (ins.warnings || []).map((w) => el('div', { class: 'callout warn' }, w)),
    ins.unmapped.length ? el('p', { class: 'small muted' }, `沒有用到的欄位（${ins.unmapped.length}）：${ins.unmapped.slice(0, 12).join('、')}${ins.unmapped.length > 12 ? '…' : ''}`) : null,
    el('details', {}, el('summary', {}, '查看完整對應'),
      el('div', { class: 'table-wrap', style: 'max-height:260px' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, '你的欄名'), el('th', {}, '對應到'))),
        el('tbody', {}, matched.map(([a, b]) => el('tr', {}, el('td', {}, a), el('td', { class: 'mono' }, b))))))));
}

function llmCard() {
  const L = S.options.llm;
  const setL = (k, v) => { L[k] = v; };
  const remote = L.enabled && !/^https?:\/\/(localhost|127\.|\[::1\]|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/i.test(L.endpoint || '');
  const field = (label, key, type, hint, ph) => el('label', { class: 'field' }, label,
    el('input', { type: type || 'text', value: L[key], placeholder: ph || '', oninput: (e) => setL(key, e.target.value), autocomplete: 'off' }),
    hint ? el('span', { class: 'hint' }, hint) : null);
  return el('details', { class: 'card', open: L.enabled }, el('summary', {}, '進階：用 LLM 協助判讀無法解析的儲存格（選用）'),
    el('p', { class: 'muted small', style: 'margin-top:8px' }, '只處理程式規則看不懂的文字（例如「weakly positive」），並附上信心程度。LLM 只能解讀已有的文字，不會替你補缺失值。每個被採用的結果都會要求你核對。送出的內容只有「欄位名稱＋那一格的文字」，不含病患 ID 或其他欄位。'),
    el('label', { class: 'row' }, el('input', { type: 'checkbox', checked: L.enabled, onchange: (e) => { L.enabled = e.target.checked; render(); } }), '啟用 LLM 輔助'),
    L.enabled ? el('div', { class: 'grid cols-3', style: 'margin-top:12px' },
      el('label', { class: 'field' }, '種類', el('select', { onchange: (e) => { L.provider = e.target.value; if (L.provider === 'anthropic') { L.endpoint = 'https://api.anthropic.com'; L.model = L.model || 'claude-haiku-5-5'; } else { L.endpoint = 'http://localhost:11434/v1'; } render(); } },
        [['openai', 'OpenAI 相容（本機 Ollama、LM Studio 等，或雲端）'], ['anthropic', 'Anthropic']].map(([v, t]) => el('option', { value: v, selected: L.provider === v }, t)))),
      field('端點網址', 'endpoint', 'text', '本機服務通常是 http://localhost:11434/v1'), field('模型名稱', 'model', 'text', '', '例如 llama3.1'),
      field('API key（本機服務可留空）', 'api_key', 'password', '只存在記憶體，這次執行結束就消失，不會寫入檔案'),
      field('最低信心（0–1）', 'min_confidence', 'number', '低於這個值的判讀不採用')) : null,
    remote ? el('div', { class: 'callout warn', style: 'margin-top:12px' }, el('strong', { class: 't' }, '這是遠端服務'),
      '會把「欄位名稱和那一格的文字」傳到你的電腦以外。請確認資料已去識別化，並符合使用條款與院內規範。',
      el('label', { class: 'row', style: 'margin-top:8px' }, el('input', { type: 'checkbox', checked: L.allow_remote, onchange: (e) => { L.allow_remote = e.target.checked; } }), '我了解並同意使用遠端服務')) : null,
    L.enabled ? el('div', { class: 'row', style: 'margin-top:12px' }, el('button', { class: 'btn btn-sm', type: 'button', onclick: testLlm }, '測試連線'),
      S.llmTest ? el('span', { class: 'pill ' + (S.llmTest.ok ? 'pill-ok' : 'pill-bad') }, S.llmTest.text) : null) : null);
}

async function testLlm() {
  S.llmTest = { ok: false, text: '測試中…' }; render();
  try {
    const r = await api('POST', '/api/llm/test', S.options.llm);
    S.llmTest = r.ok ? { ok: true, text: `連線成功（信心 ${r.confidence.toFixed(2)}）` } : { ok: false, text: r.error || '回應不符預期' };
  } catch (e) { S.llmTest = { ok: false, text: e.message }; }
  render();
}

function screenSettings() {
  const ins = S.inspect, ready = S.status && S.status.r.ready;
  const unresolved = !ins || !ins.ok;
  const O = S.options;
  const canStart = !unresolved;
  const modeCards = Object.entries(MODES).map(([k, [t, d]]) => el('label', { class: 'choice' + (O.missing === k ? ' sel' : '') },
    el('input', { type: 'radio', name: 'mode', checked: O.missing === k, onchange: () => { O.missing = k; render(); } }),
    el('span', { class: 't' }, t, k === 'auto' ? el('span', { class: 'pill pill-info tag' }, '建議') : null), el('span', { class: 'd' }, d)));
  return el('div', {}, el('h1', {}, '設定與檢查'),
    rStatusBanner(),
    el('div', { class: 'card' }, el('h2', {}, el('span', { class: 'num' }, '1'), '你的檔案'),
      el('div', { class: 'row' }, el('strong', {}, S.job.original_name), el('span', { class: 'muted' }, fmtSize(S.job.size)), el('span', { class: 'spacer' }),
        el('button', { class: 'btn btn-sm', type: 'button', onclick: () => { S.job = null; go('upload'); } }, '換一個檔案'))),
    sheetCard(), mappingCard(),
    el('div', { class: 'card' }, el('h2', {}, el('span', { class: 'num' }, '4'), '資料有缺漏時怎麼辦？'),
      el('p', { class: 'muted' }, '病患資料常有沒填的欄位。空白和 NA 都代表「未知」，不會被當成 0。請選擇處理方式：'),
      el('div', { class: 'choices' }, modeCards),
      O.missing === 'auto' ? el('div', { style: 'margin-top:14px' }, el('label', { class: 'field', style: 'max-width:420px' }, '可接受的不確定範圍（Worst − Best）',
        el('input', { type: 'number', min: '0', max: '20', step: '0.1', value: O.max_range, oninput: (e) => { O.max_range = parseFloat(e.target.value); } }),
        el('span', { class: 'hint' }, '預設 1，是模型本身用來判斷「可信」的門檻。調小會排除更多人，調大則接受更多不確定。'))) : null,
      el('p', { style: 'margin-top:12px' }, el('button', { class: 'btn btn-sm', type: 'button', onclick: glossary }, '什麼是 Best / Mean / Worst？'))),
    llmCard(),
    el('div', { class: 'row', style: 'margin-top:8px' },
      el('button', { class: 'btn btn-primary btn-lg', type: 'button', disabled: !canStart || !ready, onclick: startReview }, '開始整理與檢查'),
      !ready ? el('span', { class: 'muted small' }, '需要先安裝好 R 與 ipssm 套件') : unresolved ? el('span', { class: 'muted small' }, '請先在上方選好要用的分頁') : null));
}

async function startReview() {
  const O = S.options;
  if (!(O.max_range >= 0 && O.max_range <= 20)) return toast('範圍門檻要在 0 到 20 之間', 'bad');
  if (O.llm.enabled && !O.llm.model.trim()) return toast('請填寫 LLM 模型名稱', 'bad');
  try {
    await api('POST', `/api/jobs/${S.job.id}/review`, { missing: O.missing, max_range: O.max_range,
      sheet: S.selTouched ? S.sel.join(',') : '', sheet_join: O.sheet_join, llm: O.llm });
    S.runLabel = 'review'; S.run = null; go('running'); poll();
  } catch (e) { toast(e.message, 'bad'); }
}

// ---------------------------------------------------------------------------
//  執行中
// ---------------------------------------------------------------------------
function screenRunning() {
  const r = S.run, label = S.runLabel;
  const err = r && r.status === 'error' ? r.error : null;
  const stages = label === 'review' ? ['整理並檢查資料', '試算缺失值造成的風險範圍', '產生核對用資料'] : label === 'calculate' ? ['正式計算風險分數'] : ['重新試算'];
  const now = r && r.status === 'running' ? Math.min(Math.max((r.stage_no || 1), 1), stages.length) : 0;
  if (err) {
    return el('div', {}, el('h1', {}, '沒有成功'),
      el('div', { class: 'callout bad' }, el('strong', { class: 't' }, label === 'calculate' ? '計算失敗' : '無法完成整理與檢查'), err.message),
      err.code === 'source_selection' ? el('p', {}, '請回到設定頁，在「選擇要用的分頁」勾選要用的分頁。') : null,
      el('div', { class: 'row' }, el('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        if (err.code === 'source_selection') { await reinspect(); }
        S.review ? go('review') : go('settings'); } }, S.review && label !== 'review' ? '回到核對頁' : '回到設定'),
        r.log ? el('button', { class: 'btn', type: 'button', onclick: () => { S.showLog = !S.showLog; render(); } }, S.showLog ? '隱藏詳細日誌' : '顯示詳細日誌') : null),
      S.showLog ? el('pre', { class: 'log' }, r.log) : null);
  }
  return el('div', {}, el('h1', {}, label === 'calculate' ? '正在計算…' : '正在處理…'),
    el('div', { class: 'card' },
      el('ul', { class: 'stagelist' }, stages.map((t, i) => el('li', { class: i + 1 < now ? 'done' : i + 1 === now ? 'now' : '' },
        i + 1 < now ? el('span', {}, '✓') : i + 1 === now ? el('div', { class: 'spinner' }) : el('span', {}, '○'), t))),
      el('p', { class: 'muted' }, `已經過 ${r ? r.seconds : 0} 秒。資料很多時可能需要幾分鐘，請不要關閉這個分頁。`),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: () => { S.showLog = !S.showLog; render(); } }, S.showLog ? '隱藏詳細日誌' : '顯示詳細日誌'),
      S.showLog && r ? el('pre', { class: 'log' }, r.log) : null));
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    S.run = await api('GET', `/api/jobs/${S.job.id}/progress`);
    if (S.screen === 'running') render();
    if (S.run.status === 'running') { pollTimer = setTimeout(poll, 700); return; }
    if (S.run.status === 'done') {
      await loadJob();
      if (S.runLabel === 'calculate') { await loadResults(); go('results'); }
      else { S.tab = S.runLabel === 'retrial' ? S.tab : (S.review.items.length ? 'todo' : 'data'); await loadRows(); go('review'); }
    }
  } catch (e) { toast(e.message, 'bad'); }
}

async function loadJob() {
  const d = await api('GET', '/api/jobs/' + S.job.id);
  S.job = d.job; S.review = d.review || null; S.downloads = d.downloads;
}

// ---------------------------------------------------------------------------
//  3. 核對
// ---------------------------------------------------------------------------
async function loadRows() {
  const q = S.rowsQ;
  S.rows = await api('GET', `/api/jobs/${S.job.id}/rows?limit=50&offset=${q.offset}&q=${encodeURIComponent(q.q)}&flagged=${q.flagged ? 1 : 0}`);
}

function stat(v, l, kind) { return el('div', { class: 'stat ' + (kind || '') }, el('div', { class: 'v' }, v), el('div', { class: 'l' }, l)); }

function screenReview() {
  const R = S.review, c = R.counts;
  const todo = R.items.filter((i) => !i.acked).length;
  const tabs = [['todo', '待確認', R.items.length ? (todo ? String(todo) : '✓') : null], ['unc', '缺失與不確定', c.uncertain_trial || c.with_missing || null],
    ['excl', '被排除的病人', c.excluded_validation + c.excluded_missing + c.excluded_uncertain || null], ['data', '標準欄位資料', null],
    ['audit', '格式整理紀錄', null], ['src', '來源檔案', null]];
  return el('div', {}, el('h1', {}, '請核對整理後的資料'),
    el('p', { class: 'muted' }, '程式已把資料整理成標準格式。以下是它動過的地方、缺漏與疑似矛盾的資料。確認沒問題（或修正後）再按最下方的「確認並計算」。'),
    el('div', { class: 'grid cols-cards', style: 'margin-bottom:16px' },
      stat(c.input_rows, '檔案中的病人'), stat(c.included, '將進入計算', 'ok'),
      stat(c.excluded_validation + c.excluded_missing + c.excluded_uncertain, '被排除', (c.excluded_validation + c.excluded_missing + c.excluded_uncertain) ? 'warn' : ''),
      stat(c.conflicts, '資料矛盾', c.conflicts ? 'bad' : ''), stat(c.needs_review_cells, '程式替你改了、需確認', c.needs_review_cells ? 'warn' : ''),
      stat(R.trial_available ? c.uncertain_trial : '—', '試算結果 UNCERTAIN', c.uncertain_trial ? 'warn' : '')),
    R.trial_stale ? el('div', { class: 'callout warn' }, el('strong', { class: 't' }, '你修改過資料，風險範圍尚未更新'),
      '「缺失與不確定」頁的數字是修改前試算的。最後計算時一定會用最新資料重算，如想先看看請按重新試算。',
      el('div', { style: 'margin-top:8px' }, el('button', { class: 'btn btn-sm', type: 'button', onclick: retrial }, '重新試算'))) : null,
    !R.trial_available ? el('div', { class: 'callout warn' }, '找不到 R，沒有做缺失值試算，因此沒有 UNCERTAIN 判斷。') : null,
    el('div', { class: 'tabs', role: 'tablist' }, tabs.map(([k, t, n]) => el('button', { class: 'tab', role: 'tab', type: 'button', 'aria-selected': S.tab === k,
      onclick: async () => { S.tab = k; if (k === 'data') await loadRows(); render(); } }, t, n !== null ? el('span', { class: 'pill pill-info count' }, n) : null))),
    el('div', { id: 'tabbody' }, tabBody()), confirmBar());
}

function itemCard(it) {
  const jump = () => { S.tab = 'data'; S.rowsQ = { q: it.patient, flagged: false, offset: 0 }; S.highlight = { patient: it.patient, column: it.column || (it.fields || '').split(',')[0].trim() };
    loadRows().then(render); };
  const body = it.kind === 'cell' ? [
    el('div', {}, el('strong', {}, it.patient), ' 的 ', el('strong', {}, it.column)),
    el('div', {}, el('span', { class: 'val' }, it.original || '（空白）'), el('span', { class: 'arrow' }, '→'), el('span', { class: 'val' }, it.normalized)),
    el('div', { class: 'small muted' }, (RULE_TEXT[it.rule] || it.rule), it.source === 'llm' ? ` · LLM 判讀，信心 ${it.confidence}` : '', it.note ? ` · ${it.note}` : '')] : [
    el('div', {}, el('strong', {}, it.patient), '：', it.message),
    el('div', { class: 'small muted' }, `${it.kind === 'conflict' ? '資料矛盾' : '提示'} ${it.rule} · 相關欄位：${it.fields}`)];
  return el('div', { class: `item ${it.kind}${it.acked ? ' acked' : ''}` }, el('div', { class: 'body' }, body),
    el('div', { class: 'acts' }, el('button', { class: 'btn btn-sm', type: 'button', onclick: jump }, '到資料表修改'),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: () => ack([it.id], !it.acked) }, it.acked ? '取消確認' : '我看過了')));
}

async function ack(ids, value) {
  try { await api('POST', `/api/jobs/${S.job.id}/ack`, { ids, value }); await loadJob(); render(); } catch (e) { toast(e.message, 'bad'); }
}

function tabBody() {
  const R = S.review;
  if (S.tab === 'todo') {
    if (!R.items.length) return el('div', { class: 'empty' }, '沒有需要特別確認的項目。仍建議到「標準欄位資料」快速瀏覽一遍。');
    const groups = [['conflict', '資料矛盾（請判斷哪個值才對）'], ['cell', '程式替你轉換、請確認轉換正確'], ['note', '提示（不一定有問題，但會影響結果）']];
    return el('div', {}, el('div', { class: 'row', style: 'margin-bottom:10px' }, el('span', { class: 'muted' }, '逐項檢視後可按「我看過了」。這只是你的核對記錄，不會改資料。'), el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: () => ack(R.items.filter((i) => !i.acked).map((i) => i.id), true) }, '全部標為看過了')),
      groups.map(([k, t]) => { const list = R.items.filter((i) => i.kind === k); return list.length ? el('div', {}, el('h3', {}, `${t}（${list.length}）`), list.map(itemCard)) : null; }));
  }
  if (S.tab === 'unc') return uncertainTab(R);
  if (S.tab === 'excl') return excludedTab(R);
  if (S.tab === 'data') return dataTab();
  if (S.tab === 'audit') return auditTab(R);
  return sourceTab(R);
}

function interval(best, mean, worst) {
  const lo = -3, hi = 4, pos = (v) => Math.min(100, Math.max(0, ((v - lo) / (hi - lo)) * 100));
  const b = parseFloat(best), m = parseFloat(mean), w = parseFloat(worst);
  if ([b, m, w].some(isNaN)) return '';
  return el('span', { class: 'interval', title: `最有利 ${b}　平均 ${m}　最不利 ${w}` },
    el('span', { class: 'span', style: `left:${pos(b)}%;width:${Math.max(1, pos(w) - pos(b))}%` }), el('span', { class: 'mean', style: `left:calc(${pos(m)}% - 2px)` }));
}
const riskClass = (cat) => ({ 'Very Low': 'r0', Low: 'r1', 'Moderate Low': 'r2', 'Moderate High': 'r3', High: 'r4', 'Very High': 'r5' }[cat] || '');

function uncertainTab(R) {
  const rows = R.missing_check.filter((m) => m.Missing_Fields !== 'NONE' || m.Confidence_Level === 'UNCERTAIN');
  if (!rows.length) return el('div', { class: 'empty' }, '沒有缺失欄位，每位病人的資料都是完整的。');
  return el('div', {}, el('div', { class: 'callout info' },
    R.mode === 'auto' ? `「自動判斷」模式：Worst − Best 超過 ${R.max_range} 的病人（標示 UNCERTAIN）不會進入計算，但下表仍保留它的風險區間供你參考。` :
      R.mode === 'skip' ? '「只算完整」模式：有缺失的病人已在前面被排除，這裡列出的是通過後仍有缺失的紀錄。' : '「全部保留」模式：UNCERTAIN 的病人也會計算，結果旁會標示不確定。',
    ' 區間圖：淺色是 Best 到 Worst，深色小條是 Mean（採用的分數）。'),
    el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['病人', '缺失欄位', '單獨就會 UNCERTAIN 的缺失', 'Best#', 'Mean#', 'Worst#', '範圍#', '', '判定'].map(TH))),
      el('tbody', {}, rows.map((m) => el('tr', { class: m.Confidence_Level === 'UNCERTAIN' ? 'bad' : '' },
        el('td', {}, m.ID), el('td', { class: 'wrap' }, m.Missing_Fields), el('td', { class: 'wrap' }, m.Key_Missing_Fields === 'NONE' ? '—' : m.Key_Missing_Fields),
        el('td', { class: 'num' }, m.IPSSMscore_best || '—'), el('td', { class: 'num' }, m.IPSSMscore_mean || '—'), el('td', { class: 'num' }, m.IPSSMscore_worst || '—'),
        el('td', { class: 'num' }, m.Range_Score || '—'), el('td', {}, interval(m.IPSSMscore_best, m.IPSSMscore_mean, m.IPSSMscore_worst)),
        el('td', {}, m.Decision === 'INCLUDE' ? (m.Confidence_Level === 'UNCERTAIN' ? el('span', { class: 'pill pill-warn' }, '計算（標示 UNCERTAIN）') : el('span', { class: 'pill pill-ok' }, '計算（可信）'))
          : el('span', { class: 'pill pill-bad' }, '排除'))))))));
}

function excludedTab(R) {
  if (!R.excluded.length) return el('div', { class: 'empty' }, '沒有病人被排除。');
  return el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['病人', '原因類型', '說明'].map(TH))),
    el('tbody', {}, R.excluded.map((x) => el('tr', {}, el('td', {}, x.ID), el('td', {}, EXCL_TEXT[x.Source] || x.Source), el('td', { class: 'wrap' }, x.Reason))))));
}

function auditTab(R) {
  const un = R.unresolved || [];
  const types = [['needs_review', '需確認'], ['all', '全部'], ['unresolved', '無法解析']];
  return el('div', {}, el('p', { class: 'muted' }, '程式對每個儲存格做過的格式整理（空白的統一標記為缺失這類例行動作不在「需確認」裡）。完整紀錄也可在最後下載。'),
    R.edits.length ? el('div', { class: 'callout ok' }, el('strong', { class: 't' }, `你手動修改了 ${R.edits.length} 處`),
      R.edits.slice(-8).map((e) => el('div', { class: 'small' }, `${e.time.replace('T', ' ')}　${e.patient} · ${e.column}：`, el('span', { class: 'val' }, e.old), '→', el('span', { class: 'val' }, e.new)))) : null,
    un.length ? el('div', { class: 'callout bad' }, `有 ${un.length} 個儲存格規則（及 LLM）都無法解析，對應的病人已被排除。見「被排除的病人」。`) : null,
    el('div', { class: 'row', style: 'margin-bottom:8px' }, types.map(([k, t]) => el('button', { class: 'btn btn-sm' + (S.auditFilter === k ? ' btn-primary' : ''), type: 'button', onclick: async () => { S.auditFilter = k; S.auditShown = 200; await loadAudit(); render(); } }, t))),
    S.audit ? auditTable() : el('div', { class: 'empty' }, '載入中…'));
}
async function loadAudit() {
  const d = await api('GET', `/api/jobs/${S.job.id}/audit?status=${S.auditFilter}`);
  S.audit = d;
}
function auditTable() {
  const d = S.audit;
  if (!d.rows.length) return el('div', { class: 'empty' }, '沒有符合的紀錄。');
  return el('div', {}, el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['位置', '病人', '欄位', '原值', '改成', '原因', '來源', '狀態'].map(TH))),
    el('tbody', {}, d.rows.slice(0, S.auditShown).map((e) => el('tr', {}, el('td', {}, e.Row), el('td', {}, e.ID), el('td', {}, e.Column), el('td', { class: 'mono' }, e.Original || '（空白）'),
      el('td', { class: 'mono' }, e.Normalized), el('td', { class: 'wrap' }, RULE_TEXT[e.Rule] || e.Rule), el('td', {}, e.Source), el('td', {}, e.Status)))))),
    d.rows.length > S.auditShown ? el('div', { class: 'row', style: 'margin-top:8px' }, el('span', { class: 'muted' }, `顯示 ${S.auditShown} / ${d.rows.length} 筆`),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: () => { S.auditShown += 500; render(); } }, '顯示更多')) : null);
}

function sourceTab(R) {
  const info = R.source.info || {};
  return el('div', {}, (info.sheets || []).length ? el('div', { class: 'table-wrap', style: 'max-height:none' }, el('table', {}, el('thead', {}, el('tr', {}, ['分頁', '使用', '表頭位置', '資料列#', '對得上的欄位#'].map(TH))),
    el('tbody', {}, info.sheets.map((s) => el('tr', {}, el('td', {}, s.name), el('td', {}, (info.selected || []).includes(s.name) ? '✓' : ''), el('td', {}, `第 ${s.header_row} 列`), el('td', { class: 'num' }, s.rows), el('td', { class: 'num' }, s.matched)))))) : null,
    info.join ? el('p', {}, '多分頁合併方式：', el('strong', {}, info.join === 'merge' ? '依病患 ID 合併' : '上下串接')) : null,
    R.source.warnings.length ? el('div', { style: 'margin-top:12px' }, el('h3', {}, '來源檔案提醒'), R.source.warnings.map((w) => el('div', { class: 'callout warn' }, w))) : el('p', { class: 'muted' }, '沒有來源檔案相關的提醒。'),
    R.llm_info ? el('p', { class: 'muted' }, 'LLM 輔助：' + R.llm_info) : null);
}

// ---- 標準欄位資料 (可編輯) ----
function dataTab() {
  const d = S.rows, q = S.rowsQ;
  const search = el('input', { type: 'text', placeholder: '搜尋病人 ID', value: q.q, 'aria-label': '搜尋病人 ID',
    onkeydown: async (e) => { if (e.key === 'Enter') { S.rowsQ = { ...q, q: e.target.value, offset: 0 }; await loadRows(); render(); } } });
  if (!d) return el('div', { class: 'empty' }, '載入中…');
  const pages = Math.max(1, Math.ceil(d.total / 50)), page = Math.floor(q.offset / 50) + 1;
  const flagCls = (r) => (r.flags.length ? 'bad' : (r.missing.length || Object.values(r.cells).some((c) => c.status === 'needs_review')) ? 'flag' : '');
  const body = d.rows.map((r) => el('tr', { class: flagCls(r) }, d.columns.map((col) => {
    const v = r.values[col], info = r.cells[col], na = v === 'NA';
    if (col === 'ID') return el('td', { title: r.flags.length ? '有矛盾：' + r.flags.join(', ') : '' }, v);
    const cls = ['cell', na ? 'na' : '', na && r.missing.includes(col) ? 'missing' : '', info && info.status === 'needs_review' ? 'review' : '', info && info.status === 'user_edited' ? 'edited' : ''].join(' ');
    const td = el('td', { class: cls, tabindex: '0', title: info ? `原值「${info.original || '空白'}」· ${RULE_TEXT[info.rule] || info.rule}` : '點一下可修改', dataset: { patient: r.values.ID, col },
      onclick: () => editCell(td, r, col), onkeydown: (e) => { if (e.key === 'Enter') editCell(td, r, col); } }, v);
    return td;
  })));
  const nav = el('div', { class: 'row', style: 'margin:10px 0' }, search,
    el('label', { class: 'row' }, el('input', { type: 'checkbox', checked: q.flagged, onchange: async (e) => { S.rowsQ = { ...q, flagged: e.target.checked, offset: 0 }; await loadRows(); render(); } }), '只看有問題的'),
    q.q ? el('button', { class: 'btn btn-sm', type: 'button', onclick: async () => { S.rowsQ = { q: '', flagged: q.flagged, offset: 0 }; await loadRows(); render(); } }, '清除搜尋') : null,
    el('span', { class: 'spacer' }), el('span', { class: 'muted small' }, `${d.total} 位 · 第 ${page}/${pages} 頁`),
    el('button', { class: 'btn btn-sm', type: 'button', disabled: q.offset === 0, onclick: async () => { S.rowsQ = { ...q, offset: Math.max(0, q.offset - 50) }; await loadRows(); render(); } }, '上一頁'),
    el('button', { class: 'btn btn-sm', type: 'button', disabled: page >= pages, onclick: async () => { S.rowsQ = { ...q, offset: q.offset + 50 }; await loadRows(); render(); } }, '下一頁'));
  return el('div', {}, nav,
    el('div', { class: 'legend' }, el('span', {}, el('i', { style: 'background:var(--warn-bg)' }), '程式替你改的，請確認'), el('span', {}, el('i', { style: 'background:var(--bad-bg)' }), '缺失（空白 / NA）'),
      el('span', {}, el('i', { style: 'background:var(--ok-bg)' }), '你修改過'), el('span', {}, '點任一格可直接修改，Enter 儲存、Esc 取消。可輸入 0 / 1 / NA / ND、或「Positive」等文字，程式會自動轉換。')),
    d.rows.length ? el('div', { class: 'table-wrap' }, el('table', { class: 'data-table' }, el('thead', {}, el('tr', {}, d.columns.map((c) => el('th', {}, c)))), el('tbody', {}, body)))
      : el('div', { class: 'empty' }, '沒有符合的病人。'));
}

function editCell(td, row, col) {
  if (td.querySelector('input')) return;
  const old = row.values[col];
  const input = el('input', { type: 'text', value: old, 'aria-label': `${row.values.ID} 的 ${col}` });
  let done = false;
  const finish = async (commit) => {
    if (done) return; done = true;
    const val = input.value.trim();
    if (!commit || val === old) { render(); return; }
    try {
      const r = await api('POST', `/api/jobs/${S.job.id}/edit`, { row: row.idx, column: col, value: val });
      if (r.changed) toast(`已修改 ${row.values.ID} 的 ${col}：${r.old} → ${r.value}`, 'ok');
      S.scroll = saveScroll(); await loadJob(); await loadRows(); render(); restoreScroll();
    } catch (e) { toast(e.message, 'bad'); done = false; input.focus(); input.select(); }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => setTimeout(() => { if (!done && document.activeElement !== input) finish(true); }, 150));
  td.replaceChildren(input); input.focus(); input.select();
}
function saveScroll() { const w = $('.table-wrap'); return w ? { l: w.scrollLeft, t: w.scrollTop } : {}; }
function restoreScroll() { const w = $('.table-wrap'); if (w && S.scroll.l !== undefined) { w.scrollLeft = S.scroll.l; w.scrollTop = S.scroll.t; } }

async function retrial() {
  try { await api('POST', `/api/jobs/${S.job.id}/retrial`, {}); S.runLabel = 'retrial'; S.run = null; go('running'); poll(); } catch (e) { toast(e.message, 'bad'); }
}

function confirmBar() {
  const R = S.review, c = R.counts, left = R.items.filter((i) => !i.acked).length;
  const ready = S.status && S.status.r.ready;
  const ok = () => ready && S.confirmed && S.reviewer.trim() && c.included > 0;
  return el('div', { class: 'bar' }, el('div', { class: 'bar-inner' },
    el('label', { class: 'row stmt' }, el('input', { type: 'checkbox', checked: S.confirmed, onchange: (e) => { S.confirmed = e.target.checked; render(); } }),
      el('span', {}, `我已核對上述資料，同意以這 ${c.included} 位病人的標準欄位資料進行計算`)),
    el('div', { class: 'grp' },
      el('input', { type: 'text', placeholder: '核對者（姓名或代號）', 'aria-label': '核對者姓名或代號', value: S.reviewer, style: 'min-width:210px',
        oninput: (e) => { S.reviewer = e.target.value; store.set('ipssm_reviewer', S.reviewer); const b = $('#btn-confirm'); if (b) b.disabled = !ok(); } }),
      left ? el('span', { class: 'small', style: 'color:var(--warn)' }, `還有 ${left} 項待確認未標記`) : null),
    el('div', { class: 'grp' },
      el('button', { class: 'btn', type: 'button', onclick: () => go('settings') }, '回到設定'),
      downloadLink('review', '核對檔'), downloadLink('standardized', '標準欄位 CSV'),
      el('button', { id: 'btn-confirm', class: 'btn btn-primary', type: 'button', disabled: !ok(), onclick: confirmCalc }, '確認並計算'))));
}

function downloadLink(kind, label) {
  return el('a', { class: 'btn btn-sm', href: `/api/jobs/${S.job.id}/download/${kind}`, download: '' }, label);
}

async function confirmCalc() {
  try {
    await api('POST', `/api/jobs/${S.job.id}/confirm`, { confirmed: true, reviewer: S.reviewer.trim() });
    S.runLabel = 'calculate'; S.run = null; go('running'); poll();
  } catch (e) { toast(e.message, 'bad'); }
}

// ---------------------------------------------------------------------------
//  4. 結果
// ---------------------------------------------------------------------------
async function loadResults() { S.results = await api('GET', `/api/jobs/${S.job.id}/results`); await loadJob(); }

function screenResults() {
  const D = S.results, rows = D.rows;
  const unc = rows.filter((r) => r.Confidence_Level === 'UNCERTAIN').length;
  const flagged = rows.filter((r) => r.Consistency_Flags && r.Consistency_Flags !== 'NONE').length;
  const withMissing = rows.filter((r) => r.Missing_Fields && r.Missing_Fields !== 'NONE').length;
  const shown = rows.filter((r) => S.resFilter === 'all' || (S.resFilter === 'unc' && r.Confidence_Level === 'UNCERTAIN') || (S.resFilter === 'flag' && r.Consistency_Flags !== 'NONE') || (S.resFilter === 'miss' && r.Missing_Fields !== 'NONE'));
  const filt = [['all', `全部 ${rows.length}`], ['unc', `UNCERTAIN ${unc}`], ['miss', `有缺失 ${withMissing}`], ['flag', `有矛盾旗標 ${flagged}`]];
  return el('div', {}, el('h1', {}, '計算完成'),
    el('div', { class: 'callout ok' }, `已計算 ${rows.length} 位病人。`, D.signoff ? ` 核對者：${D.signoff.reviewer}　${D.signoff.time}（已記錄在結果檔的 Review_SignOff 工作表）` : ''),
    el('div', { class: 'grid cols-cards', style: 'margin-bottom:16px' }, stat(rows.length, '已計算', 'ok'), stat(rows.length - unc, 'CONFIDENT（可信）', 'ok'), stat(unc, 'UNCERTAIN', unc ? 'warn' : ''),
      stat(withMissing, '含缺失（以 Mean 補值）'), stat(flagged, '有矛盾旗標', flagged ? 'warn' : ''), stat(D.uncertain_excluded.length, '因 UNCERTAIN 未計算', D.uncertain_excluded.length ? 'warn' : '')),
    el('div', { class: 'card' }, el('h2', {}, '下載'), el('div', { class: 'row' },
      el('a', { class: 'btn btn-primary btn-lg', href: `/api/jobs/${S.job.id}/download/results`, download: '' }, '下載結果 Excel'),
      ...['review:核對檔', 'standardized:標準欄位 CSV', 'audit:逐欄位紀錄 CSV', 'log:驗證日誌'].map((x) => { const [k, l] = x.split(':'); return S.downloads.includes(k) ? downloadLink(k, l) : null; })),
      el('p', { class: 'small muted', style: 'margin-top:10px' }, '結果檔含：Summary（分數與信心）、R_Full_Output（完整數據）、Review_SignOff（核對記錄）、Manual_Edits（你的手動修改）' + (D.uncertain_excluded.length ? '、Uncertain_Excluded（未計算者的風險區間）' : '') + '。')),
    el('div', { class: 'row', style: 'margin-bottom:8px' }, filt.map(([k, t]) => el('button', { class: 'btn btn-sm' + (S.resFilter === k ? ' btn-primary' : ''), type: 'button', onclick: () => { S.resFilter = k; render(); } }, t))),
    el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['病人', '分數 (Mean)#', '風險分類', '信心', 'Best–Worst#', '', '缺失欄位', '矛盾旗標'].map(TH))),
      el('tbody', {}, shown.map((r) => el('tr', { class: r.Confidence_Level === 'UNCERTAIN' ? 'bad' : '' }, el('td', {}, r.ID), el('td', { class: 'num' }, r.Reported_Score),
        el('td', {}, el('span', { class: 'risk ' + riskClass(r.Reported_Cat) }, r.Reported_Cat)),
        el('td', {}, r.Confidence_Level === 'UNCERTAIN' ? el('span', { class: 'pill pill-warn' }, 'UNCERTAIN') : el('span', { class: 'pill pill-ok' }, 'CONFIDENT')),
        el('td', { class: 'num' }, `${r.IPSSMscore_best} ~ ${r.IPSSMscore_worst}`), el('td', {}, interval(r.IPSSMscore_best, r.Reported_Score, r.IPSSMscore_worst)),
        el('td', { class: 'wrap' }, r.Missing_Fields === 'NONE' ? '' : r.Missing_Fields), el('td', {}, r.Consistency_Flags === 'NONE' ? '' : r.Consistency_Flags)))))),
    D.uncertain_excluded.length ? el('details', { style: 'margin-top:16px' }, el('summary', {}, `因缺失造成範圍過大而未計算的病人（${D.uncertain_excluded.length}）`),
      el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['病人', '缺失欄位', 'Best#', 'Mean#', 'Worst#', '範圍#'].map(TH))),
        el('tbody', {}, D.uncertain_excluded.map((u) => el('tr', {}, el('td', {}, u.ID), el('td', { class: 'wrap' }, u.Missing_Fields), el('td', { class: 'num' }, u.IPSSMscore_best), el('td', { class: 'num' }, u.IPSSMscore_mean), el('td', { class: 'num' }, u.IPSSMscore_worst), el('td', { class: 'num' }, u.Range_Score))))))) : null,
    el('div', { class: 'row', style: 'margin-top:22px' },
      el('button', { class: 'btn', type: 'button', onclick: () => { S.confirmed = false; go('review'); } }, '回到核對頁'),
      el('button', { class: 'btn', type: 'button', onclick: () => { S.job = null; store.set('ipssm_job', null); go('upload'); } }, '處理另一份檔案'),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-danger', type: 'button', onclick: wipeJob }, '清除這次的資料')));
}

function wipeJob() {
  dialog('清除這次的資料？', el('p', {}, '會刪除工作資料夾中這次上傳的原始檔、整理後的資料與所有結果。刪除後無法復原，請先確認已下載需要的檔案。'),
    [{ label: '取消' }, { label: '永久刪除', danger: true, onclick: async () => {
      try { await api('POST', `/api/jobs/${S.job.id}/delete`, {}); S.job = null; store.set('ipssm_job', null); toast('已清除', 'ok'); go('upload'); }
      catch (e) { toast(e.message, 'bad'); } } }]);
}

// ---------------------------------------------------------------------------
//  畫面切換 / 啟動
// ---------------------------------------------------------------------------
function render() {
  renderChrome();
  const screens = { upload: screenUpload, settings: screenSettings, running: screenRunning, review: screenReview, results: screenResults };
  const main = $('#app');
  const y = window.scrollY;
  main.replaceChildren(screens[S.screen]());
  window.scrollTo(0, y);
  if (S.highlight && S.screen === 'review' && S.tab === 'data') {
    const { patient, column } = S.highlight;
    const cell = [...document.querySelectorAll('td.cell')].find((n) => n.dataset.patient === patient && n.dataset.col === column);
    if (cell) { cell.scrollIntoView({ block: 'center', inline: 'center' }); cell.focus(); }
    S.highlight = null;
  }
  if (S.screen === 'review' && S.tab === 'audit' && !S.audit) loadAudit().then(render);
}

$('#btn-help').addEventListener('click', glossary);
$('#btn-quit').addEventListener('click', () => dialog('結束程式？', el('p', {}, '會關閉這個本機服務。你的檔案與結果仍保留在工作資料夾，下次開啟可以「繼續上次的工作」。'),
  [{ label: '取消' }, { label: '結束程式', danger: true, onclick: async () => {
    try { await api('POST', '/api/shutdown', {}); } catch (e) { /* 伺服器已關閉 */ }
    $('#app').replaceChildren(el('div', { class: 'hero' }, el('h1', {}, '程式已結束'), el('p', {}, '可以關閉這個分頁了。')));
  } }]));

(async function boot() {
  render();
  try { S.status = await api('GET', '/api/status'); } catch (e) { toast(e.message, 'bad'); }
  render();
})();
