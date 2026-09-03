/* A股细分行业超买超卖看板（申万二级 109 个 · 专业版 v5） - 前端逻辑
 * 依赖全局: DATA (sub_obos.json), echarts
 * 与主看板 app.js 同源: PIT 阈值时序 / 无重叠回测 + 块级检验 / 覆盖率校准区间
 * [2026-09-03] 视图收敛: 分组热力条(31行) + 状态档位多选 + 关键词搜索
 *   + 表格默认折叠中性 + 按 |cur_score-50| 偏离度排序
 *   背景: 109 个二级行业常态 ~78% 为中性(实测 2026-09-03 为 85 中性 / 24 非中性),
 *   平铺 109 行会把极值埋掉, 故默认只呈现非中性四档, 并按极端程度排序。
 * 红=超买=危险, 绿=超卖=机会 (A股红涨绿跌约定)
 */
(function () {
  'use strict';

  var COLORS = {
    ob: '#b1493f', hot: '#c08a3e', mid: '#41617e', cold: '#5f9b86', os: '#3c8168',
    band: 'rgba(65,97,126,0.13)', idx: '#8a96a3', rs: '#7c6f99'
  };
  var DATES = DATA.benchmark.dates;

  function num(v, fb) { return (typeof v === 'number' && isFinite(v)) ? v : (fb === undefined ? '-' : fb); }
  function fmt(v, d) {
    if (typeof v !== 'number' || !isFinite(v)) return '-';
    return v.toFixed(d === undefined ? 1 : d);
  }
  /* 状态口径统一：一律使用后端按 PIT 动态阈值(ob_line/os_line)判定的 x.state */
  var ST_COLOR = { '超买': COLORS.ob, '偏热': COLORS.hot, '中性': COLORS.mid, '偏冷': COLORS.cold, '超卖': COLORS.os };
  function stateOf(x) {
    if (x && typeof x === 'object') {
      if (x.state && x.state !== '-') return x.state;
      return fallbackState(x.cur_score, x.ob_line, x.os_line, x.hot_line, x.cold_line);
    }
    return fallbackState(x);
  }
  function fallbackState(s, ob, os, hot, cold) {
    if (typeof s !== 'number' || !isFinite(s)) return '-';
    var o = (typeof ob === 'number') ? ob : 80, u = (typeof os === 'number') ? os : 20;
    var h = (typeof hot === 'number') ? hot : o - (o - u) * 0.25;
    var c = (typeof cold === 'number') ? cold : u + (o - u) * 0.25;
    if (s >= o) return '超买';
    if (s <= u) return '超卖';
    if (s >= h) return '偏热';
    if (s <= c) return '偏冷';
    return '中性';
  }
  function stateColor(x) { return ST_COLOR[stateOf(x)] || '#98a2b3'; }
  function sigColor(label) {
    if (label && label.indexOf('机会') >= 0) return COLORS.os;
    if (label && label.indexOf('风险') >= 0) return COLORS.ob;
    return '#98a2b3';
  }
  function divTxt(d) {
    if (d === 'bullish') return '<span style="color:#3c8168;font-weight:600">看涨</span>';
    if (d === 'bearish') return '<span style="color:#b1493f;font-weight:600">看跌</span>';
    return '<span style="color:#98a2b3">-</span>';
  }
  function pct(v, d) {
    if (typeof v !== 'number' || !isFinite(v)) return '-';
    return (v * 100).toFixed(d === undefined ? 1 : d) + '%';
  }

  var INDS = DATA.industries;
  var DETAIL_DEFAULT_DAYS = 250;
  var charts = [];
  function makeChart(id) { var c = echarts.init(document.getElementById(id), null, { renderer: 'svg' }); charts.push(c); return c; }
  var detailChart = null;
  var DETAIL_ZOOM = { showStart: null, showEnd: null, isFull: false };
  function resetZoom() {
    DETAIL_ZOOM.isFull = !DETAIL_ZOOM.isFull;
    if (detailChart) detailChart.dispatchAction({
      type: 'dataZoom',
      startValue: DETAIL_ZOOM.isFull ? 0 : DETAIL_ZOOM.showStart,
      endValue: DETAIL_ZOOM.showEnd
    });
  }

  /* ---------- 一级分组（保持申万一级官方顺序, 按出现次序） ---------- */
  var GROUPS = [];
  (function () {
    var seen = {};
    INDS.forEach(function (x) {
      var p = x.parent || '其他';
      if (!seen[p]) { seen[p] = { name: p, n: 0 }; GROUPS.push(seen[p]); }
      seen[p].n++;
    });
  })();
  var curGroup = '__ALL__';

  /* ---------- [2026-09-03] 视图收敛: 状态档位多选 + 关键词搜索 ----------
   * 默认只勾选非中性四档(超买/偏热/偏冷/超卖), 中性档不勾 -> 表格默认不渲染中性行业;
   * 排序改为按 |cur_score-50| 偏离度降序, 越极端越靠前(逆向投资只关心两端)。
   * 档位全选或全不选时等价于"全部"(避免出现空表这种无意义状态)。 */
  var STATE_KEYS = ['超买', '偏热', '中性', '偏冷', '超卖'];
  var STATE_COLOR = { '超买': COLORS.ob, '偏热': COLORS.hot, '中性': COLORS.mid, '偏冷': COLORS.cold, '超卖': COLORS.os };
  var selStates = { '超买': 1, '偏热': 1, '偏冷': 1, '超卖': 1 };
  var curQ = '';

  function deviation(x) {
    var s = (typeof x.cur_score === 'number' && isFinite(x.cur_score)) ? x.cur_score : 50;
    return Math.abs(s - 50);
  }
  function groupBase() {
    if (curGroup === '__ALL__') return INDS;
    return INDS.filter(function (x) { return (x.parent || '其他') === curGroup; });
  }
  function byDeviation(list) {
    return list.slice().sort(function (a, b) { return deviation(b) - deviation(a); });
  }
  function visibleInds() {
    var keys = STATE_KEYS.filter(function (k) { return selStates[k]; });
    var useAll = (keys.length === 0 || keys.length === STATE_KEYS.length);
    var q = curQ ? String(curQ).toLowerCase() : '';
    var out = groupBase().filter(function (x) {
      if (!useAll && keys.indexOf(stateOf(x)) < 0) return false;
      if (q && (String(x.name) + ' ' + String(x.parent || '')).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
    return byDeviation(out);
  }

  /* ---------- 数据质量门禁 ---------- */
  function renderQuality() {
    var q = DATA.quality;
    var badge = document.getElementById('qBadge');
    var grid = document.getElementById('qGrid');
    if (!q) { document.getElementById('qCard').style.display = 'none'; return; }
    var color = q.status === 'PASS' ? '#3c8168' : (q.status === 'WARN' ? '#c08a3e' : '#b1493f');
    badge.textContent = q.status;
    badge.style.background = color;
    var items = [
      ['细分行业 / 交易日', q.n_industries + ' × ' + q.n_dates],
      ['日历对齐覆盖率', pct(q.align_coverage)],
      ['缺失单元格', q.missing_cells],
      ['重复日期', q.dup_dates],
      ['异常价 / 负量', (q.nonpositive_close || 0) + ' / ' + (q.negative_volume || 0)],
      ['数据滞后', (q.lag_days === 0 ? '当日最新' : q.lag_days + ' 日')]
    ];
    grid.innerHTML = items.map(function (it) {
      var bad = (it[0].indexOf('缺失') >= 0 || it[0].indexOf('重复') >= 0 || it[0].indexOf('异常') >= 0)
        && String(it[1]).replace(/[^0-9]/g, '') !== '' && parseInt(String(it[1]), 10) > 0;
      return '<div class="metric"><div class="k">' + it[0] + '</div><div class="v" style="font-size:15px;color:'
        + (bad ? '#b1493f' : 'var(--ink)') + '">' + it[1] + '</div></div>';
    }).join('');
    var issues = (q.issues && q.issues.length)
      ? '<b style="color:' + color + '">发现 ' + q.issues.length + ' 项问题：</b>' + q.issues.join('；')
      : '<b style="color:#3c8168">全部通过</b>：' + q.n_industries + ' 个细分行业与基准逐日对齐，无缺失、无重复日期、无异常价量，数据为最新交易日收盘。';
    // [2026-09-03] 指数口径真空期如实披露: 个别二级指数在申万2021版生效(2021-12-13)前无逐日数据
    var vac = q.prefix_vacuum || {};
    var vacKeys = Object.keys(vac);
    if (vacKeys.length) {
      issues += '<br/><b style="color:#c08a3e">口径真空期披露</b>：' + vacKeys.map(function (k) {
        return vac[k].name + ' 在 ' + vac[k].valid_from + ' 前无逐日数据（申万2021版分类生效前，共 ' + vac[k].cells + ' 个交易日），该段指标为空、不进入回测';
      }).join('；') + '。';
    }
    document.getElementById('qNote').innerHTML = issues
      + ' 覆盖区间 <b>' + q.span[0] + ' ~ ' + q.span[1] + '</b>。';
  }

  /* ---------- 首屏摘要 ---------- */
  function renderSummary() {
    var cnt = { ob: 0, hot: 0, mid: 0, cold: 0, os: 0 };
    var KEY = { '超买': 'ob', '偏热': 'hot', '中性': 'mid', '偏冷': 'cold', '超卖': 'os' };
    INDS.forEach(function (x) {
      var kk = KEY[stateOf(x)];
      if (kk) cnt[kk]++; else cnt.mid++;
    });
    /* 最超买/最超卖一律显式取极值, 不依赖后端数组顺序(顺序一变就会静默报错) */
    var top = null, bot = null;
    INDS.forEach(function (x) {
      if (typeof x.cur_score !== 'number' || !isFinite(x.cur_score)) return;
      if (!top || x.cur_score > top.cur_score) top = x;
      if (!bot || x.cur_score < bot.cur_score) bot = x;
    });
    if (!top) { top = INDS[0]; bot = INDS[INDS.length - 1]; }
    document.getElementById('sumOb').textContent = cnt.ob + ' 个';
    document.getElementById('sumHot').textContent = cnt.hot + ' 个';
    document.getElementById('sumMid').textContent = cnt.mid + ' 个';
    document.getElementById('sumOs').textContent = (cnt.cold + cnt.os) + ' 个';
    document.getElementById('sumTop').textContent = top.name + ' ' + fmt(top.cur_score);
    document.getElementById('sumBot').textContent = bot.name + ' ' + fmt(bot.cur_score);
    document.getElementById('asofTxt').textContent = DATA.asof;
    var nEl = document.getElementById('nIndTxt');
    if (nEl) nEl.textContent = INDS.length;
  }

  /* ---------- 分组热力条 (31 行: 每行一个申万一级, 行内嵌该组二级格子) ----------
   * 替代原 treemap(109 散块): 小行业(煤炭/综合仅 1 个二级)在 treemap 里几乎没有视觉存在感,
   * 名字也挤不下。改为分段条后视觉单元 109 块 -> 31 行, 且板块内部冷热分布一眼可见。
   * 行序: 按组内最极端偏离度降序(有极值的板块排最前); 格序: 组内按分数升序(冷->热);
   * 格宽: 成分股数占比(min-width 兜底保证可点)。热力条恒显示全部行业, 不受表格筛选影响。 */
  function renderHeatBars() {
    var byGroup = {};
    INDS.forEach(function (x) {
      var p = x.parent || '其他';
      if (!byGroup[p]) byGroup[p] = [];
      byGroup[p].push(x);
    });
    var rows = GROUPS.map(function (g) {
      var kids = (byGroup[g.name] || []).slice().sort(function (a, b) {
        return num(a.cur_score, 50) - num(b.cur_score, 50);
      });
      var dev = kids.reduce(function (m, x) { return Math.max(m, deviation(x)); }, 0);
      return { name: g.name, kids: kids, dev: dev };
    }).sort(function (a, b) { return b.dev - a.dev; });

    var html = rows.map(function (r) {
      var dim = (curGroup !== '__ALL__' && curGroup !== r.name) ? ' dim' : '';
      var cells = r.kids.map(function (x) {
        var w = Number(x.n_constituents) || 10;
        var st = stateOf(x);
        return '<div class="hcell' + (x.code === curCode ? ' sel' : '') + '" data-code="' + x.code + '"'
          + ' style="flex:' + w + ';background:' + stateColor(x) + '"'
          + ' title="' + x.name + '（' + (x.parent || '-') + '）· ' + st + ' · 当前 ' + fmt(x.cur_score)
          + ' 分 · 成分股 ' + w + ' 只 · 点击查看详情">' + x.name + '</div>';
      }).join('');
      return '<div class="hrow' + dim + '"><div class="hlab">' + r.name
        + '<span class="hn">' + r.kids.length + '</span></div><div class="hbar">' + cells + '</div></div>';
    }).join('');
    document.getElementById('heatBody').innerHTML = html;
  }

  /* ---------- 分组筛选 chips ---------- */
  function renderGroupChips() {
    var box = document.getElementById('gChips');
    var html = '<span class="gchip' + (curGroup === '__ALL__' ? ' on' : '') + '" data-g="__ALL__">全部<span class="n">' + INDS.length + '</span></span>';
    GROUPS.forEach(function (g) {
      html += '<span class="gchip' + (curGroup === g.name ? ' on' : '') + '" data-g="' + g.name + '">'
        + g.name + '<span class="n">' + g.n + '</span></span>';
    });
    box.innerHTML = html;
  }

  /* ---------- 状态档位 chips (带计数, 多选; 计数随当前一级分组联动) ---------- */
  function renderStateChips() {
    var base = groupBase();
    var cnt = {};
    STATE_KEYS.forEach(function (k) { cnt[k] = 0; });
    base.forEach(function (x) { var s = stateOf(x); if (s in cnt) cnt[s]++; });
    var keys = STATE_KEYS.filter(function (k) { return selStates[k]; });
    var allOn = (keys.length === 0 || keys.length === STATE_KEYS.length);
    var html = '<span class="gchip' + (allOn ? ' on' : '') + '" data-s="__ALL__">全部<span class="n">'
      + base.length + '</span></span>';
    STATE_KEYS.forEach(function (k) {
      html += '<span class="gchip' + (selStates[k] ? ' on' : '') + '" data-s="' + k + '" style="--sc:'
        + STATE_COLOR[k] + '">' + k + '<span class="n">' + cnt[k] + '</span></span>';
    });
    document.getElementById('sChips').innerHTML = html;
  }

  /* ---------- 排名表 ---------- */
  function renderTable() {
    var html = '';
    var list = visibleInds();
    var base = groupBase();
    var keys = STATE_KEYS.filter(function (k) { return selStates[k]; });
    var allOn = (keys.length === 0 || keys.length === STATE_KEYS.length);
    /* 档位筛选可能筛出空集(如某日全市场无一个非中性行业), 此时退化为全部并如实说明, 不留空表 */
    var fellBack = false;
    if (!list.length && !curQ) { list = byDeviation(base); fellBack = true; }
    var noteEl = document.getElementById('tNote');
    if (noteEl) {
      var txt = '显示 <b>' + list.length + '</b> / ' + base.length + ' 个';
      if (curQ) txt += '（搜索「' + curQ + '」）';
      else if (fellBack) txt += ' · 当前无符合档位的行业，已显示全部';
      else if (!allOn) txt += ' · 已折叠 ' + (base.length - list.length) + ' 个中性行业';
      txt += ' · 按<b>偏离 50 分的极端程度</b>排序，越极端越靠前';
      if (curGroup !== '__ALL__') txt += ' · 已限定「' + curGroup + '」';
      noteEl.innerHTML = txt;
    }
    if (!list.length) {
      document.getElementById('rankBody').innerHTML = '<tr><td colspan="16" style="text-align:center;'
        + 'color:var(--sub);padding:26px">'
        + (curQ ? '未找到匹配「' + curQ + '」的细分行业' : '当前筛选条件下没有细分行业') + '</td></tr>';
      return;
    }
    list.forEach(function (x, i) {
      var s = x.cur_score;
      var chg = x.chg5;
      var chgTxt = (typeof chg === 'number' && isFinite(chg)) ? ((chg > 0 ? '+' : '') + fmt(chg)) : '-';
      var fcEnd = (x.forecast.median && x.forecast.median.length) ? x.forecast.median[x.forecast.median.length - 1] : null;
      var sigTxt = x.sig === '显著超买' ? '<span class="tag-sig">超买</span>'
        : x.sig === '显著超卖' ? '<span class="tag-os">超卖</span>' : '<span class="tag-na">-</span>';
      sigTxt += ' <span style="color:#98a2b3">q=' + fmt(x.fdr_q, 2) + '</span>';
      var vst = x.vol_state, vr = x.vol_ratio;
      var vColor = vst === '放量' ? '#b1493f' : (vst === '缩量' ? '#3c8168' : '#98a2b3');
      var vTxt = (typeof vr === 'number' ? vr.toFixed(2) : '-') + ' ' + vst;
      html += '<tr data-code="' + x.code + '" class="rowclk' + (x.code === curCode ? ' sel' : '') + '">'
        + '<td class="rank c-rank">' + (i + 1) + '</td>'
        + '<td class="lft c-name">' + x.name + '</td>'
        + '<td class="lft c-parent">' + (x.parent || '-') + '</td>'
        + '<td class="c-score"><b style="color:' + stateColor(x) + '">' + fmt(s) + '</b></td>'
        + '<td class="c-state"><span class="chip" style="background:' + stateColor(x) + '" title="本行业 PIT 分位阈值：超买 '
        + fmt(x.ob_line) + ' / 偏热 ' + fmt(x.hot_line) + ' / 偏冷 ' + fmt(x.cold_line)
        + ' / 超卖 ' + fmt(x.os_line) + '">' + stateOf(x) + '</span></td>'
        + '<td class="c-sig"><span style="color:' + sigColor(x.sig_label) + ';font-weight:600">' + x.sig_label + '</span></td>'
        + '<td class="c-vol"><span style="color:' + vColor + '">' + vTxt + '</span></td>'
        + '<td class="c-rs">' + fmt(x.rs_pct_now) + '</td>'
        + '<td class="c-above">' + (x.above_ma200 ? '✓' : '✗') + '</td>'
        + '<td class="c-fdr">' + sigTxt + '</td>'
        + '<td class="c-div">' + divTxt(x.divergence) + '</td>'
        + '<td class="c-chg5">' + chgTxt + '</td>'
        + '<td class="c-ret20">' + fmt(x.ret20) + '%</td>'
        + '<td class="c-ret60">' + fmt(x.ret60) + '%</td>'
        + '<td class="c-ret250">' + fmt(x.ret250) + '%</td>'
        + '<td class="c-fc">' + fmt(fcEnd) + '</td>'
        + '</tr>';
    });
    document.getElementById('rankBody').innerHTML = html;
  }

  /* ---------- 单行业详情图（与主看板完全同构） ---------- */
  /* 默认详情 = 当前最极端的行业(偏离 50 分最大), 打开即见当日最值得看的一个 */
  var curCode = INDS.slice().sort(function (a, b) { return deviation(b) - deviation(a); })[0].code;
  function buildDetailOption(x) {
    var _vw = (typeof window !== 'undefined' && window.innerWidth) ? window.innerWidth : 1280;
    var IS_MOBILE = _vw <= 640;
    var n = x.score.length;
    var H = DATA.horizon;
    var labels = DATES.concat(x.forecast.future_dates);
    var showIdx = [];
    for (var si = 0; si < labels.length; si++) {
      if (si === 0) { showIdx.push(true); continue; }
      var curM = labels[si].slice(0, 7), prevM = labels[si - 1].slice(0, 7);
      showIdx.push(curM !== prevM);
    }
    if (labels.length) showIdx[labels.length - 1] = true;
    var lastScore = x.score[n - 1];

    var hist = x.score.slice();
    for (var i = 0; i < H; i++) hist.push(null);

    var med = [], base = [], band = [];
    for (var j = 0; j < n - 1; j++) { med.push(null); base.push(null); band.push(null); }
    if (x.forecast.median) {
      med.push(lastScore); base.push(lastScore); band.push(0);
      for (var k = 0; k < H; k++) {
        med.push(x.forecast.median[k]); base.push(x.forecast.p25[k]);
        var _lo = x.forecast.p25[k], _hi = x.forecast.p75[k];
        band.push((typeof _lo === 'number' && typeof _hi === 'number' && isFinite(_lo) && isFinite(_hi))
                   ? +(+_hi - +_lo).toFixed(1) : null);
      }
    } else {
      for (var k2 = 0; k2 <= H; k2++) { med.push(null); base.push(null); band.push(null); }
    }

    var rsArr = x.rs_pct.slice();
    for (var r = 0; r < H; r++) rsArr.push(null);

    var idxLine = x.close.slice();
    for (var i2 = 0; i2 < H; i2++) idxLine.push(null);

    var obS = (x.ob_series || []).slice(), osS = (x.os_series || []).slice();
    for (var q1 = 0; q1 < H; q1++) { obS.push(null); osS.push(null); }

    var fcEnd = x.forecast.median ? x.forecast.median[H - 1] : null;
    var dirWord = '-';
    if (typeof fcEnd === 'number' && typeof lastScore === 'number') {
      dirWord = fcEnd > lastScore + 3 ? '升温' : (fcEnd < lastScore - 3 ? '降温' : '震荡');
    }

    var vr = x.vol_ratio, vst = x.vol_state;
    var volTxt = '量能：当前量比 ' + (typeof vr === 'number' ? vr.toFixed(2) : '-') + '（' + vst + '）'
      + (vst === '放量' ? '，超买放量需警惕' : (vst === '缩量' ? '，超卖缩量或近底部' : '')) + '。';
    var divTxt2 = x.divergence === 'bullish' ? '⚠️ <b style="color:#3c8168">看涨背离</b>：近期价格新低但分位未新低，下跌动能或衰竭，逆向关注。'
      : x.divergence === 'bearish' ? '⚠️ <b style="color:#b1493f">看跌背离</b>：近期价格新高但分位未新高，上涨动能或衰竭，注意风险。'
      : '近期无明显量价/分位背离。';
    var sigTxt2 = '综合信号：<b style="color:' + sigColor(x.sig_label) + '">' + x.sig_label + '</b>（机会度 ' + (x.opp_score == null ? '-' : x.opp_score) + ' / 风险度 ' + (x.risk_score == null ? '-' : x.risk_score) + '）。';

    var pu = x.forecast.p_up;
    // [2026-09-03] p_up 已停用 isotonic 后验映射(样本外验证无一配置改善, 见 obos_core [E2] 注), 交付原始概率
    var puTxt = (typeof pu === 'number' && isFinite(pu))
      ? '升温概率 <b style="color:' + (pu > 0.55 ? '#b1493f' : (pu < 0.45 ? '#3c8168' : '#41617e')) + '">'
        + (pu * 100).toFixed(0) + '%</b>（回测 AUC ' + fmt((DATA.backtest || {}).p_up_auc, 3) + '）'
      : '';
    var mth = DATA.method || {};
    var mainDesc = mth.forecast_main || '跨行业类比推演';
    var bt2 = DATA.backtest || {};
    var dmv = (bt2.dm_combo_mkt_vs_persist && (bt2.main_method || 'knn') === 'combo_mkt') ? bt2.dm_combo_mkt_vs_persist
      : (bt2.dm_combo_vs_persist || {});
    var dmTxt = '';
    if (dmv && typeof dmv.dm_p === 'number') {
      dmTxt = dmv.dm_p < 0.05 ? '，Diebold-Mariano 块级检验 p=' + fmt(dmv.dm_p, 4) + ' 显著'
        : '，Diebold-Mariano 块级检验 p=' + fmt(dmv.dm_p, 4) + ' 不显著';
    }
    var edgeTxt = (bt2.edge_pct != null) ? bt2.edge_pct.toFixed(1) : '-';
    document.getElementById('fcNote').innerHTML =
      '<b>动态阈值（PIT 扩张窗口）</b>：当前超买线 <b>' + fmt(x.ob_line) + '</b> / 偏热 ' + fmt(x.hot_line) +
      ' / 偏冷 ' + fmt(x.cold_line) + ' / 超卖线 <b>' + fmt(x.os_line) +
      '</b>——四条界线全部按本细分行业自身历史分位（95/75/25/5）逐日推进算出，图中两条虚线随时间变化，' +
      '历史上每一天只用该日及之前的分布，不含任何未来信息。' +
      '相对强度分位（vs 沪深300）当前 <b>' + fmt(x.rs_pct_now) + '</b>（高=相对强）。' +
      volTxt +
      sigTxt2 + divTxt2 +
      '<b>跨行业类比推演</b>（类比池 ' + (x.forecast.pool || 0).toLocaleString() + ' 段，实用近邻 ' +
      (x.forecast.n_used || 0) + ' 段）：未来 ' + H + ' 日中位 ' + fmt(fcEnd) + '，倾向「<b>' + dirWord + '</b>」，' +
      puTxt + '；阴影为 P25-P75，已按历史覆盖率校准（系数 ×' + (mth.cal_factor || '-') +
      '，样本外滚动实测覆盖约 50%）。主推演为<b>' + mainDesc + '</b>：类比池共识高则多信类比、共识低则收敛至稳健"持平"基线；近邻选取还按波动/趋势体制匹配，偏向与当前市场体制相同的历史片段。推演的价值在方向与不确定性区间，<b>不在精确点位</b>（点位误差比"持平"基线好约 ' + edgeTxt + '%' + dmTxt + '）。';

    return {
      animationDuration: 600,
      tooltip: { trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,.96)', borderWidth: 0, padding: IS_MOBILE ? [8, 10] : [10, 12],
        extraCssText: 'box-shadow:0 6px 24px rgba(16,24,40,.16);border:1px solid rgba(169,139,93,.28);border-radius:10px;font-size:' + (IS_MOBILE ? 11 : 12) + 'px;',
        axisPointer: { type: 'line', snap: true,
          lineStyle: { color: '#9aa6b2', width: 1, type: 'dashed' },
          label: { show: true, backgroundColor: '#a98b5d', color: '#fff', fontSize: 11 } },
        formatter: function (ps) {
          var txt = ps[0].axisValueLabel;
          ps.forEach(function (p) {
            if (p.value === null || p.value === undefined) return;
            if (p.seriesName === 'band' || p.seriesName === 'base') return;
            txt += '<br/>' + p.marker + p.seriesName + ': ' + fmt(p.value, p.seriesName === '行业指数' ? 2 : 1);
          });
          return txt;
        } },
      legend: { data: ['超买超卖分', '相对强度分位', '推演中位', '行业指数', '动态超买线', '动态超卖线'],
        top: IS_MOBILE ? 2 : 4, itemGap: IS_MOBILE ? 8 : 16, type: IS_MOBILE ? 'scroll' : 'plain',
        textStyle: { fontSize: IS_MOBILE ? 10 : 12, color: '#6c7884' } },
      grid: { left: IS_MOBILE ? 40 : 48, right: IS_MOBILE ? 50 : 72, top: IS_MOBILE ? 42 : 44, bottom: IS_MOBILE ? 58 : 66 },
      axisPointer: { link: [{ xAxisIndex: 'all' }], snap: true },
      xAxis: { type: 'category', data: labels,
        axisLabel: { color: '#6c7884', fontSize: IS_MOBILE ? 9 : 10, hideOverlap: true,
          formatter: function (v, idx) { return showIdx[idx] ? v : ''; } },
        axisLine: { lineStyle: { color: '#d8dce2' } },
        axisPointer: { show: true, snap: true, label: { show: true, backgroundColor: '#41617e', color: '#fff', fontSize: 11 } } },
      yAxis: [
        { type: 'value', min: 0, max: 100, name: '分(0-100)', nameTextStyle: { fontSize: 11, color: '#6c7884' },
          splitLine: { lineStyle: { color: '#f0f2f6' } }, axisLabel: { color: '#6c7884' },
          axisPointer: { show: true, label: { show: true, formatter: '{value}', backgroundColor: '#a98b5d', color: '#fff', fontSize: 11 } } },
        { type: 'value', scale: true, name: '行业指数', position: 'right',
          nameTextStyle: { fontSize: 11, color: COLORS.idx }, axisLabel: { color: COLORS.idx }, splitLine: { show: false },
          axisPointer: { show: true, label: { show: true, formatter: '{value}', backgroundColor: COLORS.idx, color: '#fff', fontSize: 11 } } }
      ],
      visualMap: { show: false, seriesIndex: 0, dimension: 1, pieces: (function () {
        var o = (typeof x.ob_line === 'number') ? x.ob_line : 80;
        var u = (typeof x.os_line === 'number') ? x.os_line : 20;
        var h = (typeof x.hot_line === 'number') ? x.hot_line : o - (o - u) * 0.25;
        var c = (typeof x.cold_line === 'number') ? x.cold_line : u + (o - u) * 0.25;
        return [
          { gte: o, color: COLORS.ob },
          { gte: h, lt: o, color: COLORS.hot },
          { gt: c, lt: h, color: COLORS.mid },
          { gt: u, lte: c, color: COLORS.cold },
          { lte: u, color: COLORS.os }
        ];
      })() },
      dataZoom: (function () {
        var showStart = Math.max(0, DATES.length - DETAIL_DEFAULT_DAYS);
        var showEnd = labels.length - 1;
        DETAIL_ZOOM.showStart = showStart; DETAIL_ZOOM.showEnd = showEnd;
        return [
          { type: 'inside', xAxisIndex: 0, startValue: showStart, endValue: showEnd },
          { type: 'slider', xAxisIndex: 0, height: 20, bottom: 8, startValue: showStart, endValue: showEnd,
            borderColor: '#d8dce2', fillerColor: 'rgba(169,139,93,.14)' }
        ];
      })(),
      series: [
        { name: '超买超卖分', type: 'line', data: hist, showSymbol: false, smooth: 0.25, z: 5, lineStyle: { width: 1.6 } },
        { name: '动态超买线', type: 'line', data: obS, showSymbol: false, smooth: 0.2, z: 2,
          lineStyle: { width: 1.1, type: 'dashed', color: COLORS.ob }, itemStyle: { color: COLORS.ob } },
        { name: '动态超卖线', type: 'line', data: osS, showSymbol: false, smooth: 0.2, z: 2,
          lineStyle: { width: 1.1, type: 'dashed', color: COLORS.os }, itemStyle: { color: COLORS.os } },
        { name: '相对强度分位', type: 'line', data: rsArr, showSymbol: false, smooth: 0.25, z: 4,
          lineStyle: { width: 1.3, type: 'dotted', color: COLORS.rs }, itemStyle: { color: COLORS.rs } },
        { name: '推演中位', type: 'line', data: med, showSymbol: false, smooth: 0.25, z: 6,
          lineStyle: { width: 2, type: 'dashed', color: '#41617e' }, itemStyle: { color: '#41617e' } },
        { name: 'base', type: 'line', data: base, stack: 'fc', showSymbol: false, lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 }, silent: true, legendHoverLink: false },
        { name: 'band', type: 'line', data: band, stack: 'fc', showSymbol: false, lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 }, silent: true, legendHoverLink: false, areaStyle: { color: COLORS.band } },
        { name: '行业指数', type: 'line', yAxisIndex: 1, data: idxLine, showSymbol: false, smooth: 0.25, z: 3,
          lineStyle: { width: 1, color: COLORS.idx }, itemStyle: { color: COLORS.idx } }
      ]
    };
  }

  function renderDetail(code) {
    curCode = code;
    var x = null;
    for (var i = 0; i < INDS.length; i++) if (INDS[i].code === code) { x = INDS[i]; break; }
    if (!x) return;
    document.getElementById('detailTitle').textContent = x.name + ' (' + x.sw + ' · ' + (x.parent || '-') + ') — 当前 ' + fmt(x.cur_score)
      + ' 分 · ' + stateOf(x) + (x.sig !== '-' ? ' · ' + x.sig : '');
    document.getElementById('detailTitle').style.color = stateColor(x);
    if (!detailChart) { detailChart = makeChart('detail'); if (detailChart.getZr) detailChart.getZr().on('dblclick', resetZoom); }
    detailChart.setOption(buildDetailOption(x), { notMerge: true });

    var rows = document.querySelectorAll('#rankBody tr');
    for (var r = 0; r < rows.length; r++) {
      rows[r].className = rows[r].getAttribute('data-code') === code ? 'rowclk sel' : 'rowclk';
    }
    /* 热力条同步高亮(热力条恒显示全部行业, 不受表格筛选影响) */
    var cells = document.querySelectorAll('#heatBody .hcell');
    for (var c = 0; c < cells.length; c++) {
      var on = cells[c].getAttribute('data-code') === code;
      cells[c].className = on ? 'hcell sel' : 'hcell';
    }
    var sel = document.getElementById('indSel');
    if (sel && sel.value !== code) sel.value = code;
  }

  /* ---------- 回测表 ---------- */
  function renderBacktest() {
    var bt = DATA.backtest || {};
    var main = bt.main_method || 'knn';
    var rows = [
      ['组合推演（共识度自适应 0.3–0.7）', 'combo',
        '类比池与持平基线按共识度自适应收缩保号，共识高多信类比、共识低收敛持平；方向与纯类比池完全一致，压掉点位过度外推'],
      ['纯跨行业类比推演', 'knn', '弱有效方向信号 + 已校准不确定性区间'],
      ['持平基线', 'persist', '预测未来不变；无方向判断力，但点位误差最难打败'],
      ['均值回复基准', 'meanrev', '向历史均值回归'],
      ['动量基准（线性外推）', 'momentum', '朴素趋势外推'],
      ['随机游走基准', 'randomwalk', '噪声对照']
    ];
    if (bt.combo_mkt) {
      rows.splice(1, 0, ['市场因子分解推演（β·大盘+特质）', 'combo_mkt',
        '行业分=β·沪深300分+截距+特质残差，系统性(β·大盘)与特质(残差)分别用各自类比池推演后重构']);
    }
    var dmb = bt.dm_vs_baseline || {};
    var html = '';
    rows.forEach(function (row) {
      var m = bt[row[1]];
      if (!m) return;
      var hl = row[1] === main ? ' class="main"' : '';
      var dmTxt = '<span style="color:#98a2b3">—</span>';
      if (row[1] === 'knn') {
        dmTxt = '<span style="color:#98a2b3">基准</span>';
      } else if (row[1] === 'combo' && bt.dm_combo_vs_knn) {
        var dc = bt.dm_combo_vs_knn;
        dmTxt = (dc.dm_t != null ? dc.dm_t.toFixed(2) : '-') + ' / '
          + (dc.dm_p != null ? dc.dm_p.toFixed(3) : '-')
          + (dc.dm_p != null && dc.dm_p < 0.05 ? ' <span style="color:#41617e">✓</span>' : '');
      } else if (dmb[row[1]]) {
        var d0 = dmb[row[1]];
        dmTxt = (d0.dm_t != null ? d0.dm_t.toFixed(2) : '-') + ' / '
          + (d0.dm_p != null ? d0.dm_p.toFixed(3) : '-')
          + (d0.dm_p != null && d0.dm_p < 0.05 ? ' <span style="color:#41617e">✓</span>' : '');
      }
      var dirTxt, tpTxt;
      if (m.no_direction) {
        dirTxt = '<span style="color:#98a2b3">不适用</span>';
        tpTxt = '<span style="color:#98a2b3">-</span>';
      } else {
        var good = (m.dir_acc || 0) > 0.5;
        dirTxt = '<b style="color:' + (good ? '#b1493f' : '#3c8168') + '">' + pct(m.dir_acc) + '</b>';
        var sig = (m.block_p != null && m.block_p < 0.05);
        tpTxt = (m.block_t != null ? m.block_t.toFixed(2) : '-') + ' / '
          + (m.block_p != null ? (m.block_p < 0.0001 ? '&lt;0.0001' : m.block_p.toFixed(4)) : '-')
          + (sig ? ' <span style="color:#41617e">✓</span>' : '');
      }
      html += '<tr' + hl + '><td class="lft">' + row[0] + '</td>'
        + '<td>' + dirTxt + '</td>'
        + '<td>' + tpTxt + '</td>'
        + '<td>' + pct(m.coverage_raw) + ' → <b>' + pct(m.coverage_cal) + '</b>'
        + '<span style="color:#98a2b3"> ×' + (m.cal != null ? m.cal.toFixed(2) : '-') + '</span></td>'
        + '<td>' + (m.mae_end != null ? m.mae_end : '-') + '</td>'
        + '<td>' + (m.rmse_path != null ? m.rmse_path : '-') + '</td>'
        + '<td>' + dmTxt + '</td>'
        + '<td>' + (m.n || 0) + '</td>'
        + '<td class="lft c-bt-note" style="color:var(--sub);white-space:normal;max-width:280px">' + row[2] + '</td></tr>';
    });
    document.getElementById('btBody').innerHTML = html;
    document.getElementById('btNote').innerHTML = '<b>结论：</b>' + (bt.conclusion || '回测数据不足')
      + '<br/><span style="color:var(--sub)">口径：' + (bt.industries_tested || 0) + ' 个细分行业 × '
      + (bt.n_blocks_total || 0) + ' 个<b>互不重叠</b>时间块（步长 ' + (bt.step || 30)
      + ' 日 = 预测期长度），共 ' + ((bt.knn || {}).n || 0) + ' 个样本。'
      + '显著性用<b>块级 t 检验</b>（' + (bt.industries_tested || 0) + ' 个细分行业同一天算1个观测），因为同期行业高度相关，'
      + '若按独立样本算 p 值会被夸大若干个数量级。方向准确率 &gt;50% 优于抛硬币；'
      + '覆盖率 = 真实路径落在 P25-P75 的比例，理论值 50%，"校准后"列为按历史覆盖率反推系数缩放区间宽度的结果。'
      + '"持平基线"方向准确率不适用（它从不预测方向变化），列出它是为了检验点预测有无真实价值。'
      + '细分行业成分股较少、波动大于一级行业，读数噪声相应更高，请以方向与区间为主、不纠结单点分数。</span>';
  }

  function bindEvents() {
    var sel = document.getElementById('indSel');
    // 下拉按一级分组(optgroup)
    var byGroup = {};
    GROUPS.forEach(function (g) { byGroup[g.name] = []; });
    INDS.forEach(function (x) { (byGroup[x.parent || '其他'] || []).push(x); });
    GROUPS.forEach(function (g) {
      var og = document.createElement('optgroup');
      og.label = g.name;
      (byGroup[g.name] || []).forEach(function (x) {
        var o = document.createElement('option');
        o.value = x.code; o.textContent = x.name;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    sel.value = curCode;
    sel.addEventListener('change', function () { renderDetail(sel.value); });
    document.getElementById('rankBody').addEventListener('click', function (e) {
      var tr = e.target;
      while (tr && tr.tagName !== 'TR') tr = tr.parentNode;
      if (tr && tr.getAttribute('data-code')) renderDetail(tr.getAttribute('data-code'));
    });
    document.getElementById('gChips').addEventListener('click', function (e) {
      var t = e.target;
      while (t && !(t.classList && t.classList.contains('gchip'))) t = t.parentNode;
      if (!t) return;
      curGroup = t.getAttribute('data-g');
      renderGroupChips();
      renderStateChips();
      renderHeatBars();
      renderTable();
    });
    document.getElementById('sChips').addEventListener('click', function (e) {
      var t = e.target;
      while (t && !(t.classList && t.classList.contains('gchip'))) t = t.parentNode;
      if (!t) return;
      var k = t.getAttribute('data-s');
      if (k === '__ALL__') {
        STATE_KEYS.forEach(function (kk) { selStates[kk] = 1; });
      } else {
        selStates[k] = selStates[k] ? 0 : 1;
      }
      renderStateChips();
      renderTable();
    });
    var qEl = document.getElementById('qSearch');
    if (qEl) qEl.addEventListener('input', function () { curQ = qEl.value || ''; renderTable(); });
    var hb = document.getElementById('heatBody');
    if (hb) hb.addEventListener('click', function (e) {
      var t = e.target;
      while (t && !(t.classList && t.classList.contains('hcell'))) t = t.parentNode;
      if (!t) return;
      var code = t.getAttribute('data-code');
      if (!code) return;
      renderDetail(code);
      var el = document.getElementById('detailTitle');
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    window.addEventListener('resize', function () { charts.forEach(function (c) { c.resize(); }); });
    window.addEventListener('orientationchange', function () { setTimeout(function () { if (detailChart) renderDetail(curCode); }, 250); });
  }

  renderQuality();
  renderSummary();
  renderGroupChips();
  renderStateChips();
  renderHeatBars();
  renderTable();
  renderBacktest();
  bindEvents();
  renderDetail(curCode);
})();
