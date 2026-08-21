'use strict';
// 界面只负责画，一个数都不重算 —— 所有数字来自 /api/*。
//
// 四条硬规矩在这份文件里的落点：
//   1. 只绑回环 —— 在 server.py，不在这里
//   2. 密钥永不回显 —— 后端只给掩码，这里没有明文可显示
//   3. 题数不足的维度画虚线 + 标注最小可检出量 —— renderDims()
//   4. 不同 bank_rev 不连线 —— 后端已按四个键分组，每组一张图；
//      同一模型的多个题库版本之间画版本分隔符，见 drawChart()

const $ = (s, r) => (r || document).querySelector(s);
// SVG 里不能写死颜色 —— 浅色主题下深色网格线在白底上几乎看不见。
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || '#888';
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const fmt = (x, n) => (x == null ? '—' : Number(x).toFixed(n == null ? 1 : n));
const ts = (t) => t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';

async function get(p) { const r = await fetch(p); return r.json(); }

// ---------------------------------------------------------------- 页签
document.querySelectorAll('#tabs button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('#tabs button').forEach(x => x.classList.remove('on'));
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    $('#' + b.dataset.tab).classList.add('on');
    load(b.dataset.tab);
  };
});

// ---------------------------------------------------------------- 表格
function table(cols, rows) {
  const t = el('table');
  const tr = el('tr');
  cols.forEach(c => { const th = el('th', c.num ? 'num' : '', c.label); tr.appendChild(th); });
  t.appendChild(el('thead')).appendChild(tr);
  const tb = el('tbody');
  rows.forEach(r => {
    const x = el('tr');
    cols.forEach(c => {
      const td = el('td', c.num ? 'num' : '');
      const v = c.get(r);
      if (v instanceof Node) td.appendChild(v); else td.textContent = v == null ? '—' : v;
      x.appendChild(td);
    });
    tb.appendChild(x);
  });
  t.appendChild(tb);
  return t;
}

// ---------------------------------------------------------------- 折线图
// 一条 series = 模型+档位+题库版本+端点指纹 全同的轮次。
// **不同 series 各画一张图，绝不叠在一条线上。**
function drawChart(s) {
  const W = 760, H = 180, P = { l: 44, r: 12, t: 14, b: 26 };
  const pts = s.points.filter(p => p.score != null);
  const box = el('div', 'card');
  box.appendChild(el('div', '', s.label));
  if (pts.length === 0) { box.appendChild(el('p', 'note', '这一组还没有有效分数')); return box; }

  const xs = pts.map(p => p.t), ys = pts.map(p => p.score);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.max(0, Math.min(...ys) - 8), y1 = Math.min(100, Math.max(...ys) + 8);
  const sx = t => P.l + (x1 === x0 ? (W - P.l - P.r) / 2 : (t - x0) / (x1 - x0) * (W - P.l - P.r));
  const sy = v => P.t + (1 - (v - y0) / (y1 - y0 || 1)) * (H - P.t - P.b);

  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('height', H);
  const mk = (n, a) => { const e = document.createElementNS(ns, n); for (const k in a) e.setAttribute(k, a[k]); return e; };

  [y0, (y0 + y1) / 2, y1].forEach(v => {
    svg.appendChild(mk('line', { x1: P.l, x2: W - P.r, y1: sy(v), y2: sy(v), stroke: cssv('--grid'), 'stroke-width': .5 }));
    const tx = mk('text', { x: 6, y: sy(v) + 4, fill: cssv('--dim'), 'font-size': 10 });
    tx.textContent = v.toFixed(0); svg.appendChild(tx);
  });

  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.t)},${sy(p.score)}`).join(' ');
  svg.appendChild(mk('path', { d, fill: 'none', stroke: cssv('--accent'), 'stroke-width': 1.8 }));
  pts.forEach(p => {
    const c = mk('circle', { cx: sx(p.t), cy: sy(p.score), r: 3.2, fill: cssv('--accent') });
    c.appendChild(mk('title', {})).textContent =
      `${p.run_id}\n${ts(p.t)}\n量化 ${fmt(p.score)} ／ 保守 ${fmt(p.conservative)}`;
    c.style.cursor = 'pointer';
    c.onclick = () => openRun(p.run_id);
    svg.appendChild(c);
  });
  box.appendChild(svg);
  box.appendChild(el('p', 'hint', `${pts.length} 个点 · 题库 ${s.bank_rev} · 端点 ${s.endpoint_sha}`));
  return box;
}

// ---------------------------------------------------------------- 总览
async function loadOverview() {
  const d = await get('/api/overview');
  $('#ov-note').textContent = d.note || '';
  const ch = $('#charts'); ch.innerHTML = '';
  if (!d.series || !d.series.length) { ch.appendChild(el('p', 'note', '还没有任何轮次')); }

  // 规矩 4：把同一模型+档位下的多个题库版本并排列出，并显式提示断开
  const byMT = {};
  (d.series || []).forEach(s => { (byMT[s.model + ' · ' + s.tier] = byMT[s.model + ' · ' + s.tier] || []).push(s); });
  Object.keys(byMT).sort().forEach(k => {
    const group = byMT[k];
    ch.appendChild(el('h2', '', k));
    if (group.length > 1) {
      ch.appendChild(el('p', 'note',
        `⚠ 这个模型+档位下有 ${group.length} 组不可比的配置（题库版本或端点指纹不同），` +
        `**分别画图、不连线**。把它们连起来的那条线不意味着任何东西。`));
    }
    group.forEach(s => ch.appendChild(drawChart(s)));
  });

  // 三态色块。状态**来自判定记录**，不在前端重算 ——
  // 界面自己算一遍三态，迟早和 check 给出的结论不一致。
  const mon = await get('/api/monitor');
  const stateByRun = {};
  (mon.checks || []).forEach(c => { if (c.run_id) stateByRun[c.run_id] = c; });

  const strip = $('#states'); strip.innerHTML = '';
  const recent = (mon.checks || []).slice(0, 30).reverse();
  if (!recent.length) {
    strip.appendChild(el('p', 'note', '还没有任何判定记录（check 跑过才会有）。'));
  } else {
    const row = el('div'); row.style.cssText = 'display:flex;gap:3px;flex-wrap:wrap';
    recent.forEach(c => {
      const b = el('span', 'state ' + c.state);
      b.style.cssText = 'width:16px;height:16px;border-radius:3px;display:inline-block;'
        + 'background:currentColor;cursor:default';
      b.title = `${ts(c.created_at)}\n${c.model_key} · ${c.tier}\n`
        + `${c.state} · 分数 ${fmt(c.score)} · 连续 ${c.consecutive}\n${c.reason || ''}`;
      row.appendChild(b);
    });
    strip.appendChild(row);
    strip.appendChild(el('p', 'hint',
      '每格一次判定，左旧右新。绿=正常 黄=观察 红=告警 灰=无法判定。'
      + '「观察」是单轮低于阈值 —— **单轮不告警**，要连续两轮才确认。'));
  }

  $('#runs').innerHTML = '';
  $('#runs').appendChild(table([
    { label: 'run_id', get: r => { const a = el('span', 'link', r.run_id); a.onclick = () => openRun(r.run_id); return a; } },
    { label: '时间', get: r => ts(r.started_at) },
    { label: '模型', get: r => r.model_key },
    { label: '档位', get: r => r.tier },
    { label: '题库', get: r => r.bank_rev },
    { label: '量化', num: 1, get: r => fmt(r.score) },
    { label: '保守', num: 1, get: r => fmt(r.conservative) },
    {
      label: '判定', get: r => {
        const c = stateByRun[r.run_id];
        return c ? el('span', 'state ' + c.state, c.state) : '—';
      }
    },
    { label: '请求', num: 1, get: r => r.requests },
    { label: '失败', num: 1, get: r => r.failed },
    { label: '健康', get: r => r.health },
  ], d.runs || []));
}

// ---------------------------------------------------------------- 单轮
let currentRun = null;
function openRun(rid) {
  currentRun = rid;
  CUR = 'run';
  if (location.hash !== '#run=' + encodeURIComponent(rid)) {
    history.replaceState(null, '', '#run=' + encodeURIComponent(rid));
  }
  document.querySelectorAll('#tabs button').forEach(x => x.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  document.querySelector('#tabs button[data-tab="run"]').classList.add('on');
  $('#run').classList.add('on');
  loadRun(rid);
}

function renderDims(dimsObj) {
  // 规矩 3：题数不足的维度**不画分数**，并说明为什么
  const rows = Object.values(dimsObj || {});
  return table([
    { label: '维', get: r => r.dim },
    { label: '名称', get: r => (r.display || {}).name },
    { label: '题数', num: 1, get: r => r.n_items },
    {
      label: '得分', num: 1, get: r => {
        const dp = r.display || {};
        if (!dp.show) return '不显示';
        return fmt(r.score == null ? null : r.score * 100);
      }
    },
    { label: '线型', get: r => ({ solid: '实线', dashed: '虚线（仅趋势）', none: '不画' })[(r.display || {}).style] || '—' },
    { label: '计分', get: r => (r.display || {}).in_score ? '✓' : '不计入总分' },
    { label: '说明', get: r => (r.display || {}).why },
  ], rows.sort((a, b) => (b.n_items || 0) - (a.n_items || 0)));
}

// 逐题矩阵。一道题 0 分，是「答错」还是「被截断」，记账上一模一样，
// 但原因完全不同 —— 所以截断必须单独标出来，不能混在分数里。
function renderItems(items) {
  const rows = Object.values(items || {}).sort((a, b) =>
    (a.mean == null ? -1 : a.mean) - (b.mean == null ? -1 : b.mean));
  return table([
    { label: '题号', get: r => r.id },
    { label: '维', get: r => r.dim },
    { label: '标题', get: r => (r.title || '').slice(0, 22) },
    {
      label: '平均分', num: 1, get: r => {
        if (r.observe) return '观测题';
        const v = fmt(r.mean, 2);
        if (r.truncated) { const e = el('span', 'bad', v + ' ⚠截断'); return e; }
        if (r.mean === 0) return el('span', 'bad', v);
        if (r.mean === 1) return el('span', 'good', v);
        return v;
      }
    },
    { label: '满分次数', num: 1, get: r => `${r.full_pass}/${r.trials}` },
    { label: '采样极差', num: 1, get: r => fmt(r.spread, 2) },
    { label: '失败', num: 1, get: r => r.failed_requests || 0 },
    { label: 'P50 延迟', num: 1, get: r => r.latency_p50 == null ? '—' : (r.latency_p50 / 1000).toFixed(1) + 's' },
    { label: '输出 token', num: 1, get: r => r.out_tokens_mean == null ? '—' : Math.round(r.out_tokens_mean) },
  ], rows);
}

// 跑动中的轮询。DESIGN 第五章：进度条真正的价值不是好看，
// 是**让人知道它没死** —— 所以 stalled 要显眼。
let POLL = null;
function stopPoll() { if (POLL) { clearInterval(POLL); POLL = null; } }

function renderProgress(box, p) {
  box.innerHTML = '';
  const pct = p.total ? Math.round(p.done / p.total * 100) : 0;
  box.appendChild(el('div', '', `进度 ${p.done}/${p.total}（${pct}%）· 当前题 ${p.label || '—'}`));
  const bar = el('div'); bar.style.cssText = 'height:8px;background:var(--track);border-radius:4px;overflow:hidden;margin:8px 0';
  const fill = el('div'); fill.style.cssText = `height:100%;width:${pct}%;background:${p.state === 'stalled' ? cssv('--alert') : cssv('--accent')}`;
  bar.appendChild(fill); box.appendChild(bar);
  const eta = p.eta_sec ? `预计还要 ${(p.eta_sec / 60).toFixed(1)} 分钟` : '';
  box.appendChild(el('div', 'hint', `状态 ${p.state} · 已跑 ${((p.elapsed_sec || 0) / 60).toFixed(1)} 分钟 ${eta}`));
  if (p.state === 'stalled') {
    box.appendChild(el('p', 'bad', '⚠ ' + (p.stall_note || '超过 90 秒没更新 —— 可能已经死了，不是慢')));
  }
}

async function loadRun(rid) {
  stopPoll();
  const body = $('#run-body'); body.innerHTML = '';
  if (!rid) { body.appendChild(el('p', 'note', '从「总览」里点一个 run_id 进来。')); return; }
  const d = await get('/api/run?run_id=' + encodeURIComponent(rid));
  if (d.error) { body.appendChild(el('p', 'bad', d.error)); return; }
  const s = d.summary, p = d.progress;

  if (p && p.state !== 'done') {
    const c = el('div', 'card');
    renderProgress(c, p);
    body.appendChild(c);
    // 1 秒轮询 /api/progress（**不轮询 /api/run** —— 那会把整份
    // report.md 一秒读一遍）。跑完就自动重载整页。
    POLL = setInterval(async () => {
      const np = await get('/api/progress?run_id=' + encodeURIComponent(rid));
      if (np.transient) return;                 // 原子替换瞬间读到半个文件
      if (np.error) { stopPoll(); return; }
      renderProgress(c, np);
      if (np.state === 'done' || np.state === 'failed') { stopPoll(); loadRun(rid); }
    }, 1000);
  }
  if (!s) { body.appendChild(el('p', 'note', '这一轮还没有 summary（可能仍在跑）。')); return; }

  const card = s.card || {};
  const c = el('div', 'card');
  // 首行是边界，不是分数
  if (card.headline) { const h = el('p'); h.innerHTML = '<b>' + card.headline + '</b>'; c.appendChild(h); }
  (card.fact || []).forEach(x => c.appendChild(el('div', 'hint', '· ' + x)));
  (card.read || []).forEach(x => c.appendChild(el('div', 'hint', '· ' + x)));
  body.appendChild(c);

  const perm = el('div', 'card');
  perm.appendChild(el('div', 'good', '可以说：'));
  (card.allow || []).forEach(x => perm.appendChild(el('div', 'hint', '· ' + x)));
  perm.appendChild(el('div', 'bad', '不能说：'));
  (card.deny || []).forEach(x => perm.appendChild(el('div', 'hint', '· ' + x)));
  body.appendChild(perm);

  // 成本与运行时
  const rt = s.runtime || {}, ct = s.cost || {}, hl = s.health || {};
  const meta = el('div', 'card');
  meta.appendChild(el('div', '', `成本 ${fmt(ct.total_cost, 4)} ${ct.currency || ''}`
    + ` ｜ 单请求 ${fmt(ct.cost_per_request, 4)}`
    + ` ｜ 输出 ${ct.out_tokens || 0} token`));
  meta.appendChild(el('div', 'hint',
    `请求 ${rt.requests || 0} ｜ 失败 ${rt.failed || 0} ｜ 重试 ${rt.retried || 0}`
    + ` ｜ 截断 ${rt.truncated || 0}（其中零可见输出 ${rt.truncated_empty || 0}）`));
  meta.appendChild(el('div', 'hint',
    `P50 延迟 ${((rt.latency_p50 || 0) / 1000).toFixed(1)}s`
    + ` ｜ P95 ${((rt.latency_p95 || 0) / 1000).toFixed(1)}s`
    + ` ｜ 健康 ${hl.verdict || '—'}（${hl.note || ''}）`));
  if (rt.truncated) {
    meta.appendChild(el('p', 'bad',
      `⚠ 有 ${rt.truncated} 次请求撞上 max_tokens。**截断造成的 0 分是配额问题，`
      + `不是能力问题** —— 不要据此判定这些题「太难」。`));
  }
  body.appendChild(meta);

  body.appendChild(el('h2', '', '逐维'));
  body.appendChild(renderDims(s.dims));

  body.appendChild(el('h2', '', '逐题矩阵'));
  body.appendChild(el('p', 'note', '按平均分升序 —— 最需要看的是垫底那几道。'));
  body.appendChild(renderItems(s.items));

  if (d.report_md) {
    body.appendChild(el('h2', '', '完整报告'));
    body.appendChild(el('pre', '', d.report_md));
  }
}

// ---------------------------------------------------------------- 监控
async function loadMonitor() {
  const d = await get('/api/monitor');
  $('#baselines').innerHTML = '';
  $('#baselines').appendChild(table([
    { label: '模型', get: r => r.model_key },
    { label: '档位', get: r => r.tier },
    { label: '题库', get: r => r.bank_rev },
    { label: '端点', get: r => (r.endpoint_sha || '').slice(0, 8) },
    { label: '均值', num: 1, get: r => fmt(r.mean) },
    { label: 'σ', num: 1, get: r => fmt(r.sigma, 2) },
    { label: 'σ 来源', get: r => r.sigma_source },
    { label: '阈值', num: 1, get: r => fmt(r.threshold) },
    { label: '轮数', num: 1, get: r => r.rounds },
    { label: '临时', get: r => r.provisional ? el('span', 'bad', '临时（告警需连续 3 轮）') : '正式' },
  ], d.baselines || []));
  $('#baselines').appendChild(el('p', 'note', d.note || ''));

  $('#checks').innerHTML = '';
  $('#checks').appendChild(table([
    { label: '时间', get: r => ts(r.created_at) },
    { label: '模型', get: r => r.model_key },
    { label: '档位', get: r => r.tier },
    { label: '状态', get: r => el('span', 'state ' + r.state, r.state) },
    { label: '分数', num: 1, get: r => fmt(r.score) },
    { label: '偏离', num: 1, get: r => fmt(r.delta) },
    { label: 'z', num: 1, get: r => fmt(r.z, 2) },
    { label: '连续', num: 1, get: r => r.consecutive },
  ], d.checks || []));

  const n = d.notify || {};
  $('#notify').innerHTML = '';
  const nc = el('div', 'card');
  nc.appendChild(el('div', '', `启用：${n.enabled ? '是' : '否'} ｜ 策略：${n.policy || '—'}`));
  nc.appendChild(el('div', 'hint', `webhook：${n.webhook || '（未配置）'}`));
  nc.appendChild(el('p', 'note', 'webhook 只显示域名和前两段路径 —— 飞书/Slack 的 token 就在路径里。'));
  $('#notify').appendChild(nc);
}

// ---------------------------------------------------------------- 端点
async function loadModels() {
  const d = await get('/api/models');
  const box = $('#endpoints'); box.innerHTML = '';
  box.appendChild(table([
    { label: 'key', get: r => r.key },
    { label: '标签', get: r => r.label },
    { label: '模型', get: r => r.model },
    { label: 'base_url', get: r => r.base_url },
    { label: '指纹', get: r => (r.endpoint_sha || '').slice(0, 12) },
    { label: '默认', get: r => r.default ? '✓' : '' },
    { label: '密钥', get: r => r.key_set ? el('span', 'good', `${r.key_masked}（${r.key_source}）`) : el('span', 'bad', '缺失') },
  ], d.endpoints || []));
  box.appendChild(el('p', 'note', d.note || ''));
  box.appendChild(el('p', 'note', '增删端点、发起测评请用命令行或 MCP —— 本界面是只读的。'));
}

// ---------------------------------------------------------------- 题库
async function loadBank() {
  const d = await get('/api/bank');
  const box = $('#bank-body'); box.innerHTML = '';
  if (d.error) { box.appendChild(el('p', 'bad', d.error)); return; }
  const c = el('div', 'card');
  c.appendChild(el('div', '', `bank_rev ${d.bank_rev} ｜ ${d.n_items} 道题 ｜ 冻结于 ${d.created_at}`));
  Object.entries(d.files || {}).forEach(([k, v]) =>
    c.appendChild(el('div', 'hint', `${k}：${v.items} 道 · sha256 ${String(v.sha256).slice(0, 16)}…`)));
  box.appendChild(c);
  // 题库页不复用 renderDims —— 那张表有一列「得分」，
  // 在题库视图里永远是空的，一列恒空的数据比不显示更让人困惑。
  box.appendChild(table([
    { label: '维', get: r => r.dim },
    { label: '名称', get: r => r.name },
    { label: '题数', num: 1, get: r => r.n },
    { label: '黑名单', num: 1, get: r => r.black ? el('span', 'bad', r.black) : 0 },
    { label: '可监控', num: 1, get: r => r.monitor_ok },
    { label: '计分', get: r => r.in_score ? '✓' : '不计入总分' },
    { label: '线型', get: r => ({ solid: '实线（可判定）', dashed: '虚线（仅趋势）', none: '不显示' })[r.display.style] },
    { label: '为什么', get: r => r.display.why },
  ], d.dims || []));
  box.appendChild(el('h2', '', '为什么进不了监控'));
  box.appendChild(table([
    { label: '原因', get: r => r[0] },
    { label: '题数', num: 1, get: r => r[1] },
  ], Object.entries(d.monitor_blocked || {}).sort((a, b) => b[1] - a[1])));

  const t = await get('/api/tiers');
  const tb = $('#tiers-body'); tb.innerHTML = '';
  tb.appendChild(table([
    { label: '档', get: r => r.key },
    { label: '题数', num: 1, get: r => r.n_items },
    { label: '计分请求', num: 1, get: r => r.scored_requests },
    { label: '最小可检出', num: 1, get: r => fmt(r.min_detectable) },
    { label: '节奏', get: r => r.cadence },
    { label: '用途', get: r => r.purpose },
  ], t.tiers || []));
}

// ---------------------------------------------------------------- 调度
const LOADERS = { overview: loadOverview, run: () => loadRun(currentRun), monitor: loadMonitor, models: loadModels, bank: loadBank };

// **每次切换都重新拉。** 早先做了缓存，结果一个监控界面看过一次
// 就再也不更新了 —— 对一个用来盯退化的界面，这是最不该有的行为。
let CUR = 'overview';
function load(tab) { CUR = tab; if (LOADERS[tab]) LOADERS[tab](); }

// 自动刷新：只在总览和监控页开，30 秒一次。
// 单轮页有自己的 1 秒进度轮询，别叠加。
let AUTO = null;
function setAuto(on) {
  if (AUTO) { clearInterval(AUTO); AUTO = null; }
  if (on) AUTO = setInterval(() => { if (CUR === 'overview' || CUR === 'monitor') load(CUR); }, 30000);
  localStorage.setItem('mprobe_auto', on ? '1' : '0');
}

function buildBar() {
  const bar = el('div', 'bar');
  const btn = el('button', '', '刷新');
  btn.onclick = () => load(CUR);
  const lab = el('label'); lab.style.cursor = 'pointer';
  const cb = el('input'); cb.type = 'checkbox';
  cb.checked = localStorage.getItem('mprobe_auto') === '1';
  cb.onchange = () => setAuto(cb.checked);
  lab.appendChild(cb); lab.appendChild(document.createTextNode(' 30 秒自动刷新'));
  bar.appendChild(btn); bar.appendChild(lab);
  document.querySelector('header').appendChild(bar);
  setAuto(cb.checked);
}

// 深链：#run=<run_id> 可以直接打开某一轮，刷新页面不丢。
function fromHash() {
  const m = (location.hash || '').match(/^#run=(.+)$/);
  if (m) { openRun(decodeURIComponent(m[1])); return true; }
  return false;
}
window.addEventListener('hashchange', () => { if (!fromHash() && CUR !== 'overview') load(CUR); });

buildBar();
get('/api/bank').then(d => { $('#rev').textContent = '题库 ' + (d.bank_rev || '?'); }).catch(() => { });
if (!fromHash()) loadOverview();
