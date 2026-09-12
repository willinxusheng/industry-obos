/* 无头冒烟测试: 在 vm + DOM stub 下执行 app.js，验证逻辑无运行时错误 + 字段完整
 * [2026-09-03] --sub: 改测细分看板 (data/sub_obos.json + sub_app.js) */
const fs = require('fs');
const vm = require('vm');
const path = __dirname;

const SUB = process.argv.includes('--sub');
const data = JSON.parse(fs.readFileSync(path + (SUB ? '/data/sub_obos.json' : '/data/industry_obos.json'), 'utf8'));
const appsrc = fs.readFileSync(path + (SUB ? '/sub_app.js' : '/app.js'), 'utf8');
const appName = SUB ? 'sub_app.js' : 'app.js';

/* [2026-09-03] 一级行业数据: 与 build_html.py --sub 的 _parents_js 同款抽取 + 同款同步判据。
 * 门禁必须测真实构建路径(31 个一级行带真实指标); 数据不同步时也必须测降级路径(指标列一律 "-")。
 * 两条路径都要覆盖——只测一条, 另一条就会悄悄烂掉。 */
const PARENT_KEYS = ['code', 'name', 'cur_score', 'state', 'sig_label', 'divergence',
  'chg5', 'ret20', 'ret60', 'ret250', 'vol_ratio', 'vol_state',
  'rs_pct_now', 'above_ma200', 'fdr_q', 'sig', 'ob_line', 'hot_line', 'cold_line', 'os_line'];
let parents = [];
let parentsSynced = false;
if (SUB) {
  const pdata = JSON.parse(fs.readFileSync(path + '/data/industry_obos.json', 'utf8'));
  parentsSynced = (pdata.asof === data.asof);
  if (parentsSynced) {
    parents = pdata.industries.map(function (x) {
      const rec = {};
      for (const k of PARENT_KEYS) rec[k] = x[k];
      const med = (x.forecast || {}).median;
      rec.fc_end = Array.isArray(med) && med.length ? med[med.length - 1] : null;
      return rec;
    });
  }
  console.log('parents: synced=%s count=%d (一级 asof=%s vs 二级 asof=%s)',
    parentsSynced, parents.length, pdata.asof, data.asof);
}

// 1) 数据字段完整性校验
const need = ['rs_pct', 'ob_line', 'os_line', 'ob_series', 'os_series', 'above_ma200', 'sig', 'fdr_q',
  'ma200', 'rs_pct_now', 'rel_now', 'opp_score', 'risk_score', 'sig_label', 'sig_kind', 'divergence'];
let bad = [];
const nDates = (data.benchmark && data.benchmark.dates) ? data.benchmark.dates.length : 0;
for (const x of data.industries) {
  for (const k of need) if (!(k in x)) bad.push(x.name + '.' + k);
  // PIT 阈值序列必须与日期等长（否则图上会错位）
  if (Array.isArray(x.ob_series) && x.ob_series.length !== nDates) bad.push(x.name + '.ob_series.len');
  if (Array.isArray(x.os_series) && x.os_series.length !== nDates) bad.push(x.name + '.os_series.len');
  const f = x.forecast || {};
  for (const k of ['median', 'p25', 'p75', 'pool', 'n_used', 'p_up', 'future_dates']) {
    if (!(k in f)) bad.push(x.name + '.forecast.' + k);
  }
  // 推演区间必须 p25 <= median <= p75
  if (Array.isArray(f.median) && Array.isArray(f.p25) && Array.isArray(f.p75)) {
    for (let i = 0; i < f.median.length; i++) {
      if (!(f.p25[i] <= f.median[i] + 1e-9 && f.median[i] <= f.p75[i] + 1e-9)) {
        bad.push(x.name + '.forecast.band_order@' + i); break;
      }
    }
    if (f.future_dates.length !== f.median.length) bad.push(x.name + '.forecast.dates_len');
  }
  if (typeof f.p_up === 'number' && (f.p_up < 0 || f.p_up > 1)) bad.push(x.name + '.forecast.p_up_range');
  /* 状态判定必须与该行业自身 PIT 五档阈值自洽（不能出现"分数已越过超买线却标偏热"）
   * [2026-09-09] 容差说明: state 由后端按**全精度**判定, 而 cur_score 与四条阈值线
   *   都是 round(x,1) 后才写进 JSON(compute.py 的 r1)。两者真值可能相差至多 0.1
   *   (各自舍入误差 0.05), 于是会出现"展示上 cur==hot 但真值 cur<hot"——
   *   实测 2026-09-09 二级「教育」cur=60.8 / hot_line=60.8 / state=中性 即此情形。
   *   拿舍入后的展示值做精确比较会把这种正常歧义判成不自洽(09-09 CI 连续 5 次失败即此)。
   *   故按 ±TOL 各判一次, 三个期望取并集: 真值偏离超过 TOL 时三者一致, 依然判红。 */
  const hasL = ['ob_line', 'os_line', 'hot_line', 'cold_line'].every(k => typeof x[k] === 'number');
  if (!hasL) bad.push(x.name + '.pit_lines_missing');
  if (x.state && x.state !== '-' && hasL) {
    const stOf = function (c) {
      return c >= x.ob_line ? '超买'
        : c <= x.os_line ? '超卖'
          : c >= x.hot_line ? '偏热'
            : c <= x.cold_line ? '偏冷' : '中性';
    };
    const TOL = 0.1;
    const acc = [stOf(x.cur_score), stOf(x.cur_score - TOL), stOf(x.cur_score + TOL)];
    if (acc.indexOf(x.state) < 0) {
      bad.push(x.name + '.state(' + x.state + ' not in [' + acc.join('/') + '])');
    }
  }
  // 阈值必须严格有序: os <= cold <= hot <= ob
  if (hasL && !(x.os_line <= x.cold_line && x.cold_line <= x.hot_line && x.hot_line <= x.ob_line)) {
    bad.push(x.name + '.threshold_order');
  }
}
if (!data.benchmark || !Array.isArray(data.benchmark.dates) || nDates < 1000) bad.push('benchmark.dates');
if (!data.breadth || !Array.isArray(data.breadth.pct)) bad.push('breadth.pct');
if (!data.backtest || !data.backtest.knn) bad.push('backtest.knn');
// v5 新增: 质量门禁 / 权重收缩 / 方法口径
const q = data.quality || {};
for (const k of ['status', 'align_coverage', 'missing_cells', 'dup_dates', 'span', 'calendar_official_until']) {
  if (!(k in q)) bad.push('quality.' + k);
}
if (q.status && !['PASS', 'WARN', 'FAIL'].includes(q.status)) bad.push('quality.status=' + q.status);
const w = data.weights || {};
for (const k of ['lam', 'lam_full', 't_abs', 't_full_gate', 'prior', 'shrink_note',
  'pit_jump_max', 'pit_jump_max_raw', 'w_smooth', 'smooth_note', 'wf_windows']) {
  if (!(k in w)) bad.push('weights.' + k);
}
// 权重必须归一
const wsum = (w.rsi || 0) + (w.pos || 0) + (w.bias || 0);
if (Math.abs(wsum - 1) > 0.02) bad.push('weights.sum=' + wsum.toFixed(4));
// EMA 平滑必须真的把伪影压下去
if (typeof w.pit_jump_max === 'number' && typeof w.pit_jump_max_raw === 'number'
  && w.pit_jump_max > w.pit_jump_max_raw) bad.push('weights.smoothing_ineffective');
for (const k of ['pit_threshold', 'forecaster', 'cal_factor', 'analog_pool']) {
  if (!(k in (data.method || {}))) bad.push('method.' + k);
}
/* [2026-09-12] 市场环境条的数据契约：字段齐 + 阈值线不倒挂 + 迷你趋势与日期等长。
 * 阈值线顺序是"前端画什么颜色"的依据，倒挂会让同一分数同时落在两个档；
 * recent 与 recent_dates 错位则会把趋势图标到错误的日期段上（看着正常，实则错）。 */
const mk = data.market;
if (!mk) bad.push('market');
else {
  for (const k of ['name', 'code', 'cur_score', 'ob_line', 'os_line', 'hot_line', 'cold_line',
    'state', 'regime', 'recent', 'recent_dates', 'method', 'scope', 'scope_note']) {
    if (!(k in mk)) bad.push('market.' + k);
  }
  /* 市场分由本管线的 PIT 权重拟合，两条管线（一级 31 / 二级 109）同一交易日并不相等
   * （实测 22.9 vs 20.1）。前端靠 scope 决定要不要披露这件事 —— 标错就会
   * 让二级页要么冒充主看板口径、要么在主看板上多一段莫名其妙的免责。 */
  if (mk.scope !== (SUB ? 'sub' : 'main')) bad.push('market.scope=' + mk.scope);
  if (SUB && !String(mk.scope_note || '').includes('互相独立')) bad.push('market.scope_note 缺失(二级必填)');
  if (!SUB && mk.scope_note) bad.push('market.scope_note 应为主看板留空');
  if (!['超买', '偏热', '中性', '偏冷', '超卖'].includes(mk.state)) bad.push('market.state=' + mk.state);
  if (!['cold', 'mid', 'hot'].includes(mk.regime)) bad.push('market.regime=' + mk.regime);
  if (typeof mk.cur_score === 'number'
    && !(mk.os_line <= mk.cold_line && mk.cold_line <= mk.hot_line && mk.hot_line <= mk.ob_line)) {
    bad.push('market.lines_order=' + [mk.os_line, mk.cold_line, mk.hot_line, mk.ob_line].join('/'));
  }
  if (Array.isArray(mk.recent) && Array.isArray(mk.recent_dates) && mk.recent.length !== mk.recent_dates.length) {
    bad.push('market.recent_len=' + mk.recent.length + '/' + mk.recent_dates.length);
  }
}
// 回测: 每个方法字段齐 + 覆盖率校准到位
for (const m of ['knn', 'persist', 'meanrev', 'momentum', 'randomwalk']) {
  const o = data.backtest[m];
  if (!o) { bad.push('backtest.' + m); continue; }
  for (const k of ['dir_acc', 'block_t', 'block_p', 'no_direction', 'coverage_raw', 'coverage_cal', 'cal', 'mae_end', 'rmse_path', 'n']) {
    if (!(k in o)) bad.push('backtest.' + m + '.' + k);
  }
  if (o.no_direction !== true && (o.dir_acc === null || o.block_p === null)) bad.push('backtest.' + m + '.dir_null_without_flag');
  if (typeof o.coverage_cal === 'number' && Math.abs(o.coverage_cal - 0.5) > 0.06) {
    bad.push('backtest.' + m + '.coverage_cal=' + o.coverage_cal);
  }
}
if (!('p_up_auc' in data.backtest)) bad.push('backtest.p_up_auc');
if (!data.backtest.conclusion || !String(data.backtest.conclusion).includes('持平')) bad.push('backtest.conclusion_not_honest');
// JSON 无 NaN/Infinity
const ds = JSON.stringify(data);
if (ds.includes('NaN') || ds.includes('Infinity')) bad.push('NaN/Infinity-in-data');
if (bad.length) { console.error('DATA FIELD ERROR:', bad.join(', ')); process.exit(1); }
console.log('data fields OK: industries=%d, dates=%d, quality=%s, knn.dir_acc=%s, cov_cal=%s, artifact %s->%s',
  data.industries.length, nDates, q.status, data.backtest.knn.dir_acc,
  data.backtest.knn.coverage_cal, w.pit_jump_max_raw, w.pit_jump_max);

// 2) 执行 app.js (vm + DOM/echarts stub)
function elStub() {
  /* parentNode.removeChild 打 __removed 标记: 用于断言首屏加载态遮罩确实被移除。
   * 若 app 渲染中途抛错, 遮罩不会被移除, 用户会永远卡在"数据加载中…"——
   * 这种失败不会体现在任何内容断言里(内容早就渲染好了), 只能靠这条拦。 */
  return { textContent: '', innerHTML: '', style: {}, className: '', value: '',
    parentNode: { removeChild(node) { if (node) node.__removed = true; } },
    appendChild() {}, addEventListener() {}, getAttribute() { return null; },
    querySelectorAll() { return []; } };
}
const elements = {};
const document = {
  getElementById(id) { return elements[id] || (elements[id] = elStub()); },
  createElement() { return elStub(); },
  querySelectorAll() { return []; },
  addEventListener() {},
  /* [2026-09-03] echarts 改为 app 内动态注入(ensureEcharts), 会走 head.appendChild */
  head: { appendChild() {} }
};
const echarts = { init() { return { setOption() {}, resize() {}, on() {}, off() {} }; } };
const window = { addEventListener() {} };
const sandbox = { DATA: data, PARENTS: parents, echarts, document, window, console, Math, JSON, Array, Object,
  String, Number, isFinite, parseFloat, parseInt, setTimeout, RegExp };
vm.createContext(sandbox);
try {
  vm.runInContext(appsrc, sandbox, { filename: appName });
  const touched = Object.keys(elements).filter(k => elements[k].innerHTML || elements[k].textContent);
  console.log(appName + ' executed OK. cells updated:', touched.join(', '));
  // 关键内容检查
  const must = ['btBody', 'btNote', 'qBadge', 'qGrid', 'qNote', 'fcNote'];
  const miss = must.filter(k => !(k in elements));
  if (miss.length) { console.error('MISSING ELEMENT OUTPUTS:', miss.join(', ')); process.exit(1); }

  /* [2026-09-03] 首屏加载态遮罩必须被移除。
   * app 若在中途抛错(如某列字段改名), 遮罩会一直盖在页面上, 用户永远看到"数据加载中…"。
   * 这类失败内容断言抓不到——内容早就渲染完了, 只有收尾的移除动作没跑到。 */
  const bootMask = elements.bootMask;
  if (!bootMask || !bootMask.__removed) {
    console.error('bootMask not removed: app 渲染未走完, 用户会永远卡在"数据加载中…"');
    process.exit(1);
  }
  console.log('bootMask removed: 首屏加载态已收尾（渲染未中途抛错）');

  if (!String(elements.btNote.innerHTML).includes('结论')) { console.error('btNote empty'); process.exit(1); }

  // v5: 质量门禁已渲染且状态一致
  if (String(elements.qBadge.textContent) !== q.status) {
    console.error('qBadge status mismatch:', elements.qBadge.textContent, 'vs', q.status); process.exit(1);
  }
  if (!String(elements.qGrid.innerHTML).includes('对齐覆盖率')) { console.error('qGrid not rendered'); process.exit(1); }
  console.log('quality gate rendered: %s', q.status);

  // v5: 回测表必须含持平基线 + 块级 p + 校准前后覆盖率
  const bb = String(elements.btBody.innerHTML);
  for (const kw of ['持平基线', '不适用', '→']) {
    if (!bb.includes(kw)) { console.error('btBody missing:', kw); process.exit(1); }
  }
  if (bb.includes('undefined') || bb.includes('NaN')) { console.error('btBody has undefined/NaN'); process.exit(1); }
  if (!String(elements.btNote.innerHTML).includes('块级')) { console.error('btNote missing block-test note'); process.exit(1); }
  console.log('backtest table OK: persist baseline + block test + calibrated coverage');

  // v5: 详情说明含 PIT 阈值 + 升温概率 + 校准
  const fn = String(elements.fcNote.innerHTML);
  for (const kw of ['PIT', '升温概率', '校准']) {
    if (!fn.includes(kw)) { console.error('fcNote missing:', kw); process.exit(1); }
  }
  if (fn.includes('undefined')) { console.error('fcNote has undefined'); process.exit(1); }
  console.log('fcNote OK: PIT threshold + p_up + calibration disclosed');

  /* [2026-09-12] 市场环境条必须真的渲染出来，且带口径注记。
   * 它承载"行业状态要放在市场环境下读"这一判据（深回测：同一行业状态在不同市场环境下
   * 含义相反）。渲染失败会静默退化成"只剩行业状态"，用户不会察觉少了什么 —— 故必须真断言，
   * 不能只看 market 字段在不在 data 里（那只能证明后端给了，证明不了前端用了）。 */
  const mkd = data.market;
  if (!mkd || typeof mkd.cur_score !== 'number') { console.error('market.cur_score 缺失，无法校验渲染'); process.exit(1); }
  const mn = String(elements.mkNote.innerHTML);
  if (!mn.includes('口径')) { console.error('mkNote 缺口径说明'); process.exit(1); }
  if (!mn.includes('n=1814')) { console.error('mkNote 缺分层样本量（口径不完整）'); process.exit(1); }
  if (/undefined|NaN/.test(mn)) { console.error('mkNote has undefined/NaN'); process.exit(1); }
  if (String(elements.mkScore.textContent) !== mkd.cur_score.toFixed(1)) {
    console.error('mkScore mismatch:', elements.mkScore.textContent, 'vs', mkd.cur_score.toFixed(1)); process.exit(1);
  }
  if (String(elements.mkChip.textContent) !== String(mkd.state)) {
    console.error('mkChip mismatch:', elements.mkChip.textContent, 'vs', mkd.state); process.exit(1);
  }
  if (elements.mkBar.hidden !== false) { console.error('mkBar 未显示（应 hidden=false）'); process.exit(1); }
  if (!/<svg/.test(String(elements.mkSpark.innerHTML))) { console.error('mkSpark 未渲染内联 SVG 趋势'); process.exit(1); }
  /* 口径差异必须在二级页上披露出来（两个管线的市场分不相等，用户来回切会同时看到两个数）。
   * 主看板反过来不该出现这段——那是"替别人解释"，只会稀释它自己那行的信息量。 */
  const mlines = String(elements.mkLines.innerHTML);
  if (SUB && !mlines.includes('互相独立')) { console.error('二级 mkLines 缺口径差异披露'); process.exit(1); }
  if (!SUB && mlines.includes('互相独立')) { console.error('主看板不该出现二级口径披露'); process.exit(1); }
  if (/undefined|NaN/.test(mlines)) { console.error('mkLines has undefined/NaN'); process.exit(1); }
  console.log('market bar OK: score=%s state=%s scope=%s spark=inline-svg', mkd.cur_score, mkd.state, mkd.scope);
  // 聚类模块已移除：确认 DOM 中不再存在 cluster 模块
  if (elements.cluster !== undefined && elements.cluster !== null) {
    console.error('cluster module should have been removed but still present');
    process.exit(1);
  }
  console.log('cluster module removed: OK');
  const rb = elements.rankBody;
  if (!rb || !rb.innerHTML) { console.error('rankBody NOT rendered'); process.exit(1); }
  const rbh = String(rb.innerHTML);
  const rowHtmls = rbh.match(/<tr[\s\S]*?<\/tr>/g) || [];
  if (!rowHtmls.length) { console.error('rankBody has no <tr> rows'); process.exit(1); }
  // 逐行校验(只校验实际渲染的行), 一级行(data-g)与二级行(data-code)分开:
  //  一级行 -> 状态 chip 必须与 PARENTS 逐字一致; 降级时必须渲染 "-" 而不是假数字
  //  二级行 -> 综合信号 / 状态 chip 必须与后端字段逐字一致
  // 比原先"全文搜索标签"更严格——错行、错层级、用旧数冒充都能抓出来。
  const byCode = {};
  for (const x of data.industries) byCode[x.code] = x;
  const byParent = {};
  for (const p of parents) byParent[p.name] = p;
  const shownCodes = [];
  const shownParents = [];
  for (const rh of rowHtmls) {
    const mg = rh.match(/data-g="([^"]+)"/);
    const mc = rh.match(/data-code="([^"]+)"/);
    if (mg && !mc) {
      shownParents.push(mg[1]);
      if (parentsSynced) {
        const p = byParent[mg[1]];
        if (!p) { console.error('rankBody parent row not in PARENTS:', mg[1]); process.exit(1); }
        if (p.state && p.state !== '-' && !rh.includes('>' + p.state + '</span>')) {
          console.error('rankBody parent chip missing state', p.name, p.state); process.exit(1);
        }
      } else {
        /* 降级路径: 一级指标不可用, 综合分单元格必须渲染 "-"——出现数字说明拿旧数据冒充了。
         * 只查"整行含 >-< "不够严格(涨跌/收益列的 "-" 会把它糊过去), 必须锁定 c-score 这一格。 */
        var scCell = rh.match(/<td class="c-score">[\s\S]*?<\/td>/);
        if (!scCell) {
          console.error('degraded parent row missing c-score cell:', mg[1]); process.exit(1);
        }
        if (!/>-<\/b>/.test(scCell[0])) {
          console.error('degraded parent row must render "-" instead of stale numbers:', mg[1], scCell[0]);
          process.exit(1);
        }
      }
      continue;
    }
    if (mc) {
      const x = byCode[mc[1]];
      if (!x) { console.error('rankBody row code not in data:', mc[1]); process.exit(1); }
      if (x.sig_label && !rh.includes('>' + x.sig_label + '<')) {
        console.error('rankBody sig_label mismatch:', x.name, x.sig_label); process.exit(1);
      }
      if (x.state && x.state !== '-' && !rh.includes('>' + x.state + '</span>')) {
        console.error('rankBody chip missing state', x.name, x.state); process.exit(1);
      }
      shownCodes.push(mc[1]);
      continue;
    }
    console.error('rankBody row with neither data-g nor data-code:', rh.slice(0, 120)); process.exit(1);
  }
  if (shownCodes.length > data.industries.length) {
    console.error('rankBody sub rows exceed industries:', shownCodes.length); process.exit(1);
  }
  // 背离列必须存在(且不能因为列序调整或窄屏隐藏而消失)
  if (!rbh.includes('看涨') && !rbh.includes('看跌') && !rbh.includes('>-<')) {
    console.error('rankBody missing divergence col'); process.exit(1);
  }
  console.log('rankBody OK: %d rows rendered (%d group-level), divergence column present',
    shownParents.length + shownCodes.length, shownParents.length);

  if (SUB) {
    /* [2026-09-03] 两级可展开契约(默认全收起):
     *  1) 一级行数必须恰好 = 一级分组数(31), 一个不多一个不少
     *  2) [2026-09-04] 一级行按当前分 cur_score 从高到低(同步用自身分数,
     *     降级占位时用组内最高的二级当前分 —— 与 sub_app.js 的 parScore 同款回退)
     *  3) 默认全收起 -> 首屏不得出现任何二级行(否则又退回 109 行平铺)
     *  4) 每行的迷你热力条格子数必须 = 该组二级行业数, 合计覆盖全部 109 个(总览信息不缩水) */
    const nParents = new Set(data.industries.map(x => x.parent || '其他')).size;
    if (shownParents.length !== nParents) {
      console.error('default view: %d parent rows, expected %d', shownParents.length, nParents); process.exit(1);
    }
    if (shownCodes.length !== 0) {
      console.error('default view must be fully collapsed, got %d sub rows', shownCodes.length); process.exit(1);
    }
    const kidsOf = {};
    for (const x of data.industries) (kidsOf[x.parent || '其他'] = kidsOf[x.parent || '其他'] || []).push(x);
    function parSc(name) {
      const p = byParent[name];
      if (p && typeof p.cur_score === 'number' && isFinite(p.cur_score)) return p.cur_score;
      return (kidsOf[name] || []).reduce(function (m, x) {
        var s = (typeof x.cur_score === 'number' && isFinite(x.cur_score)) ? x.cur_score : -1;
        return Math.max(m, s);
      }, -1);
    }
    let prevSc = Infinity;
    for (const name of shownParents) {
      const d = parSc(name);
      if (d > prevSc + 1e-9) {
        console.error('parent rows not sorted by cur_score desc at %s (%s > %s)', name, d, prevSc);
        process.exit(1);
      }
      prevSc = d;
    }
    let miniSum = 0;
    for (const s of rbh.split('<tr').slice(1)) {
      const mg = s.match(/data-g="([^"]+)"/);
      if (!mg) continue;
      const nMini = (s.match(/class="mini"/g) || []).length;
      const nKid = (kidsOf[mg[1]] || []).length;
      if (nMini !== nKid) {
        console.error('mini bar mismatch at %s: %d cells vs %d subs', mg[1], nMini, nKid); process.exit(1);
      }
      miniSum += nMini;
    }
    if (miniSum !== data.industries.length) {
      console.error('mini bars cover %d subs, expected all %d', miniSum, data.industries.length); process.exit(1);
    }
    console.log('two-level tree OK: %d parent rows collapsed, sorted by cur_score desc, mini bars cover all %d subs%s',
      shownParents.length, data.industries.length, parentsSynced ? '' : ' (degraded: no PARENTS)');
    // 档位 chips + 表格说明必须渲染(否则筛选/搜索入口缺失), 且说明里必须有展开提示
    for (const id of ['sChips', 'tNote']) {
      if (!(elements[id] && elements[id].innerHTML)) { console.error(id + ' NOT rendered'); process.exit(1); }
    }
    if (!String(elements.tNote.innerHTML).includes('点击')) {
      console.error('tNote missing expand hint'); process.exit(1);
    }
    /* 一级数据缺失必须如实披露, 否则用户会把"没数据"误读成"所有一级行业都没信号" */
    if (!parentsSynced && !String(elements.tNote.innerHTML).includes('暂不可用')) {
      console.error('tNote must disclose degraded parent metrics'); process.exit(1);
    }
    console.log('state chips + table note rendered: OK');
  }
  // v5: 摘要计数必须与 PIT state 口径一致（不能一套固定80/20、一套动态阈值）
  const stCnt = { '超买': 0, '偏热': 0, '中性': 0, '偏冷': 0, '超卖': 0 };
  for (const x of data.industries) if (x.state in stCnt) stCnt[x.state]++;
  const shown = {
    ob: parseInt(String(elements.sumOb.textContent), 10),
    hot: parseInt(String(elements.sumHot.textContent), 10),
    os: parseInt(String(elements.sumOs.textContent), 10)
  };
  if (shown.ob !== stCnt['超买'] || shown.hot !== stCnt['偏热']
    || shown.os !== stCnt['偏冷'] + stCnt['超卖']) {
    console.error('summary counts inconsistent with PIT state:', JSON.stringify(shown), JSON.stringify(stCnt));
    process.exit(1);
  }
  console.log('state consistency OK: summary counts match PIT state (%s)',
    JSON.stringify(stCnt));
  // 全景已移除：确认 DOM 中不再存在 panorama 模块
  if (elements.panorama !== undefined && elements.panorama !== null) {
    console.error('panorama module should have been removed but still present');
    process.exit(1);
  }
  console.log('panorama module removed: OK (detail chart covers per-industry curve)');
  console.log('SMOKE TEST PASSED');
} catch (e) {
  console.error('RUNTIME ERROR:', e.message, '\n', e.stack);
  process.exit(1);
}
