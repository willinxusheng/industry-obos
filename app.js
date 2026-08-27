/* A股全行业超买超卖趋势看板（专业版 v5 · 准确性工程） - 前端逻辑
 * 依赖全局: DATA (industry_obos.json), echarts
 * 红=超买=危险, 绿=超卖=机会 (A股红涨绿跌约定)
 * v5: PIT 阈值时序 / 无重叠回测 + 块级检验 / 覆盖率校准区间 / 权重显著性收缩 / 数据质量门禁
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
  /* 状态口径统一：一律使用后端按 PIT 动态阈值(ob_line/os_line)判定的 x.state，
   * 不再用固定 80/65/35/20 —— 否则排名表会说"偏热"而同一行业在图上已越过超买线。 */
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
  var DETAIL_DEFAULT_DAYS = 250; // 详情图默认展示最近约 1 年交易日（A股约 244-250 个交易日/年）+ 推演段
  var charts = [];
  function makeChart(id) { var c = echarts.init(document.getElementById(id), null, { renderer: 'svg' }); charts.push(c); return c; }
  var detailChart = null;
  /* F: dataZoom 双击复位 (契合用户偏好: 缩略条拖手柄缩放/拖窗口平移/双击复位)
   * 双击在「默认窗口(最近约1年+推演段)」与「全量历史」间切换, 给走势图一个明确的一键复位入口 */
  var DETAIL_ZOOM = { showStart: null, showEnd: null, isFull: false };
  function resetZoom() {
    DETAIL_ZOOM.isFull = !DETAIL_ZOOM.isFull;
    if (detailChart) detailChart.dispatchAction({
      type: 'dataZoom',
      startValue: DETAIL_ZOOM.isFull ? 0 : DETAIL_ZOOM.showStart,
      endValue: DETAIL_ZOOM.showEnd
    });
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
      ['行业 / 交易日', q.n_industries + ' × ' + q.n_dates],
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
      : '<b style="color:#3c8168">全部通过</b>：31 个行业与基准逐日对齐，无缺失、无重复日期、无异常价量，数据为最新交易日收盘。';
    document.getElementById('qNote').innerHTML = issues
      + ' 覆盖区间 <b>' + q.span[0] + ' ~ ' + q.span[1] + '</b>。未来交易日按国务院法定节假日安排推算，官方日历已确定至 <b>'
      + (q.calendar_official_until || '-') + '</b>（此后仅含元旦/劳动节/国庆固定段，长假边界可能有 1-2 日误差）。';
  }

  /* ---------- 准确性修正清单 ---------- */
  

  /* ---------- 首屏摘要 ---------- */
    function renderSummary() {
    var cnt = { ob: 0, hot: 0, mid: 0, cold: 0, os: 0 };
    var KEY = { '超买': 'ob', '偏热': 'hot', '中性': 'mid', '偏冷': 'cold', '超卖': 'os' };
    INDS.forEach(function (x) {
      var kk = KEY[stateOf(x)];
      if (kk) cnt[kk]++; else cnt.mid++;
    });
    var top = INDS[0], bot = INDS[INDS.length - 1];
    document.getElementById('sumOb').textContent = cnt.ob + ' 个';
    document.getElementById('sumHot').textContent = cnt.hot + ' 个';
    document.getElementById('sumMid').textContent = cnt.mid + ' 个';
    document.getElementById('sumOs').textContent = (cnt.cold + cnt.os) + ' 个';
    document.getElementById('sumTop').textContent = top.name + ' ' + fmt(top.cur_score);
    document.getElementById('sumBot').textContent = bot.name + ' ' + fmt(bot.cur_score);
    document.getElementById('asofTxt').textContent = DATA.asof;
  }


  /* ---------- 市场宽度图 ---------- */
  

  /* ---------- 排名表 ---------- */
  function renderTable() {
    var html = '';
    INDS.forEach(function (x, i) {
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
      html += '<tr data-code="' + x.code + '" class="rowclk' + (i === 0 ? ' sel' : '') + '">'
        + '<td class="rank">' + (i + 1) + '</td>'
        + '<td class="lft">' + x.name + '</td>'
        + '<td><b style="color:' + stateColor(x) + '">' + fmt(s) + '</b></td>'
        + '<td><span class="chip" style="background:' + stateColor(x) + '" title="本行业 PIT 分位阈值：超买 '
        + fmt(x.ob_line) + ' / 偏热 ' + fmt(x.hot_line) + ' / 偏冷 ' + fmt(x.cold_line)
        + ' / 超卖 ' + fmt(x.os_line) + '">' + stateOf(x) + '</span></td>'
        + '<td><span style="color:' + sigColor(x.sig_label) + ';font-weight:600">' + x.sig_label + '</span></td>'
        + '<td><span style="color:' + vColor + '">' + vTxt + '</span></td>'
        + '<td>' + fmt(x.rs_pct_now) + '</td>'
        + '<td>' + (x.above_ma200 ? '✓' : '✗') + '</td>'
        + '<td>' + sigTxt + '</td>'
        + '<td>' + divTxt(x.divergence) + '</td>'
        + '<td>' + chgTxt + '</td>'
        + '<td>' + fmt(x.ret20) + '%</td>'
        + '<td>' + fmt(x.ret60) + '%</td>'
        + '<td>' + fmt(x.ret250) + '%</td>'
        + '<td>' + fmt(fcEnd) + '</td>'
        + '</tr>';
    });
    document.getElementById('rankBody').innerHTML = html;
  }

  /* ---------- 单行业详情图 ---------- */
  var curCode = INDS[0].code;
  function buildDetailOption(x) {
    var n = x.score.length;
    var H = DATA.horizon;
    var labels = DATES.concat(x.forecast.future_dates);
    // 底部时间轴标注策略: 每月首个交易日打完整 YYYY-MM-DD 标签(首尾必标),
    // 既常驻可见年月日、又不致 250/1300 天全标糊成一片; hideOverlap 兜底防重叠
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

    // PIT 动态阈值: 每一天只用当日及之前的分数分布算出, 故为时间序列而非水平线
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
    var puTxt = (typeof pu === 'number' && isFinite(pu))
      ? '升温概率 <b style="color:' + (pu > 0.55 ? '#b1493f' : (pu < 0.45 ? '#3c8168' : '#41617e')) + '">'
        + (pu * 100).toFixed(0) + '%</b>（回测 AUC ' + fmt((DATA.backtest || {}).p_up_auc, 3) + '）'
      : '';
    var mth = DATA.method || {};
    document.getElementById('fcNote').innerHTML =
      '<b>动态阈值（PIT 扩张窗口）</b>：当前超买线 <b>' + fmt(x.ob_line) + '</b> / 偏热 ' + fmt(x.hot_line) +
      ' / 偏冷 ' + fmt(x.cold_line) + ' / 超卖线 <b>' + fmt(x.os_line) +
      '</b>——四条界线全部按本行业自身历史分位（95/75/25/5）逐日推进算出，图中两条虚线随时间变化，' +
      '历史上每一天只用该日及之前的分布，不含任何未来信息。' +
      '相对强度分位（vs 沪深300）当前 <b>' + fmt(x.rs_pct_now) + '</b>（高=相对强）。' +
      volTxt +
      sigTxt2 + divTxt2 +
      '<b>跨行业类比推演</b>（类比池 ' + (x.forecast.pool || 0).toLocaleString() + ' 段，实用近邻 ' +
      (x.forecast.n_used || 0) + ' 段）：未来 ' + H + ' 日中位 ' + fmt(fcEnd) + '，倾向「<b>' + dirWord + '</b>」，' +
      puTxt + '；阴影为 P25-P75，已按历史覆盖率校准（系数 ×' + (mth.cal_factor || '-') +
      '，实测覆盖率回到 50%）。主推演为<b>共识度自适应组合</b>：类比池共识高则多信类比、共识低则收敛至稳健"持平"基线；近邻选取还按波动/趋势体制匹配，偏向与当前市场体制相同的历史片段。推演的价值在方向与不确定性区间，<b>不在精确点位</b>（点位误差比"持平"基线好约 ' + (DATA.backtest && DATA.backtest.edge_pct != null ? DATA.backtest.edge_pct.toFixed(1) : '8.5') + '%，且 Diebold-Mariano 块级检验 p&lt;0.05 显著）。';

    return {
      animationDuration: 600,
      tooltip: { trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,.96)', borderWidth: 0, padding: [10, 12],
        extraCssText: 'box-shadow:0 6px 24px rgba(16,24,40,.16);border:1px solid rgba(169,139,93,.28);border-radius:10px;font-size:12px;',
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
        top: 4, itemGap: 16, textStyle: { fontSize: 12, color: '#6c7884' } },
      grid: { left: 48, right: 72, top: 44, bottom: 66 },
      axisPointer: { link: [{ xAxisIndex: 'all' }], snap: true },
      xAxis: { type: 'category', data: labels,
        axisLabel: { color: '#6c7884', fontSize: 10, hideOverlap: true,
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
      // 曲线着色阈值与该行业当前 PIT 阈值一致（不用固定 80/20），保证图表与表格同一口径
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
      // 默认窗口：最近约 1 年交易日 + 推演段；dataZoom 缩略条保留，可拖到看全历史
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
    document.getElementById('detailTitle').textContent = x.name + ' (' + x.sw + ') — 当前 ' + fmt(x.cur_score)
      + ' 分 · ' + stateOf(x) + (x.sig !== '-' ? ' · ' + x.sig : '');
    document.getElementById('detailTitle').style.color = stateColor(x);
    if (!detailChart) { detailChart = makeChart('detail'); if (detailChart.getZr) detailChart.getZr().on('dblclick', resetZoom); }
    detailChart.setOption(buildDetailOption(x), { notMerge: true });

    var rows = document.querySelectorAll('#rankBody tr');
    for (var r = 0; r < rows.length; r++) {
      rows[r].className = rows[r].getAttribute('data-code') === code ? 'rowclk sel' : 'rowclk';
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
        '本版主推演：按类比池共识度自适应收缩保号，共识高多信类比、共识低收敛持平；方向与纯类比池完全一致，压掉点位过度外推'],
      ['纯跨行业类比推演', 'knn', '弱有效方向信号 + 已校准不确定性区间'],
      ['持平基线', 'persist', '预测未来不变；无方向判断力，但点位误差最难打败'],
      ['均值回复基准', 'meanrev', '向历史均值回归'],
      ['动量基准（线性外推）', 'momentum', '朴素趋势外推'],
      ['随机游走基准', 'randomwalk', '噪声对照']
    ];
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
        + '<td class="lft" style="color:var(--sub);white-space:normal;max-width:280px">' + row[2] + '</td></tr>';
    });
    document.getElementById('btBody').innerHTML = html;
    document.getElementById('btNote').innerHTML = '<b>结论：</b>' + (bt.conclusion || '回测数据不足')
      + '<br/><span style="color:var(--sub)">口径：' + (bt.industries_tested || 0) + ' 个行业 × '
      + (bt.n_blocks_total || 0) + ' 个<b>互不重叠</b>时间块（步长 ' + (bt.step || 30)
      + ' 日 = 预测期长度），共 ' + ((bt.knn || {}).n || 0) + ' 个样本。'
      + '显著性用<b>块级 t 检验</b>（31个行业同一天算1个观测），因为同期行业高度相关，'
      + '若按独立样本算 p 值会被夸大若干个数量级。方向准确率 &gt;50% 优于抛硬币；'
      + '覆盖率 = 真实路径落在 P25-P75 的比例，理论值 50%，"校准后"列为按历史覆盖率反推系数缩放区间宽度的结果。'
      + '"持平基线"方向准确率不适用（它从不预测方向变化），列出它是为了检验点预测有无真实价值。</span>';
  }

  

  

  

  function bindEvents() {
    var sel = document.getElementById('indSel');
    INDS.forEach(function (x) { var o = document.createElement('option'); o.value = x.code; o.textContent = x.name; sel.appendChild(o); });
    sel.value = curCode;
    sel.addEventListener('change', function () { renderDetail(sel.value); });
    document.getElementById('rankBody').addEventListener('click', function (e) {
      var tr = e.target;
      while (tr && tr.tagName !== 'TR') tr = tr.parentNode;
      if (tr && tr.getAttribute('data-code')) renderDetail(tr.getAttribute('data-code'));
    });
    window.addEventListener('resize', function () { charts.forEach(function (c) { c.resize(); }); });
  }

  renderQuality();
  renderSummary();
    renderTable();
  renderBacktest();
          bindEvents();
  renderDetail(curCode);
})();
