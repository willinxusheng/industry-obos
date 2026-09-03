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
  // 状态判定必须与该行业自身 PIT 五档阈值自洽（不能出现"分数已越过超买线却标偏热"）
  const hasL = ['ob_line', 'os_line', 'hot_line', 'cold_line'].every(k => typeof x[k] === 'number');
  if (!hasL) bad.push(x.name + '.pit_lines_missing');
  if (x.state && x.state !== '-' && hasL) {
    const exp = x.cur_score >= x.ob_line ? '超买'
      : x.cur_score <= x.os_line ? '超卖'
        : x.cur_score >= x.hot_line ? '偏热'
          : x.cur_score <= x.cold_line ? '偏冷' : '中性';
    if (exp !== x.state) bad.push(x.name + '.state(' + x.state + '!=' + exp + ')');
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
  return { textContent: '', innerHTML: '', style: {}, className: '', value: '',
    appendChild() {}, addEventListener() {}, getAttribute() { return null; },
    querySelectorAll() { return []; } };
}
const elements = {};
const document = {
  getElementById(id) { return elements[id] || (elements[id] = elStub()); },
  createElement() { return elStub(); },
  querySelectorAll() { return []; },
  addEventListener() {}
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
     *  2) 一级行必须按偏离度降序(同步时用自身分数, 降级时用组内最极端二级的偏离度)
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
    function parDev(name) {
      const p = byParent[name];
      if (p && typeof p.cur_score === 'number' && isFinite(p.cur_score)) return Math.abs(p.cur_score - 50);
      return (kidsOf[name] || []).reduce(function (m, x) {
        var s = (typeof x.cur_score === 'number' && isFinite(x.cur_score)) ? x.cur_score : 50;
        return Math.max(m, Math.abs(s - 50));
      }, 0);
    }
    let prevDev = Infinity;
    for (const name of shownParents) {
      const d = parDev(name);
      if (d > prevDev + 1e-9) {
        console.error('parent rows not sorted by deviation desc at %s (%s > %s)', name, d, prevDev);
        process.exit(1);
      }
      prevDev = d;
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
    console.log('two-level tree OK: %d parent rows collapsed, sorted by deviation, mini bars cover all %d subs%s',
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
