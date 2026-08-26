/* 无头冒烟测试: 在 vm + DOM stub 下执行 app.js，验证逻辑无运行时错误 + 字段完整 */
const fs = require('fs');
const vm = require('vm');
const path = __dirname;

const data = JSON.parse(fs.readFileSync(path + '/data/industry_obos.json', 'utf8'));
const appsrc = fs.readFileSync(path + '/app.js', 'utf8');

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
const echarts = { init() { return { setOption() {}, resize() {} }; } };
const window = { addEventListener() {} };
const sandbox = { DATA: data, echarts, document, window, console, Math, JSON, Array, Object,
  String, Number, isFinite, parseFloat, parseInt, setTimeout, RegExp };
vm.createContext(sandbox);
try {
  vm.runInContext(appsrc, sandbox, { filename: 'app.js' });
  const touched = Object.keys(elements).filter(k => elements[k].innerHTML || elements[k].textContent);
  console.log('app.js executed OK. cells updated:', touched.join(', '));
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
  // 排名表含综合信号 + 背离
  const rb = elements.rankBody;
  if (!rb || !rb.innerHTML) { console.error('rankBody NOT rendered'); process.exit(1); }
  if (!String(rb.innerHTML).includes('机会') && !String(rb.innerHTML).includes('风险')) {
    console.error('rankBody missing composite signal'); process.exit(1);
  }
  if (!String(rb.innerHTML).includes('看涨') && !String(rb.innerHTML).includes('看跌') && !String(rb.innerHTML).includes('>-<')) {
    console.error('rankBody missing divergence col'); process.exit(1);
  }
  console.log('rankBody OK: composite+divergence columns present');
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
  // 排名表 chip 文字必须逐行等于后端 state
  for (const x of data.industries) {
    if (x.state && x.state !== '-' && !String(rb.innerHTML).includes('>' + x.state + '</span>')) {
      console.error('rankBody chip missing state', x.name, x.state); process.exit(1);
    }
  }
  console.log('state consistency OK: summary/table/chart all use PIT thresholds (%s)',
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
