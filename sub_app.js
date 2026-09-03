/* A股细分行业超买超卖看板（申万二级 109 个 · 专业版 v6） - 前端逻辑
 * 依赖全局: DATA (sub_obos.json), PARENTS (主看板抽出的 31 个申万一级行业), echarts
 * 与主看板 app.js 同源: PIT 阈值时序 / 无重叠回测 + 块级检验 / 覆盖率校准区间
 * [2026-09-03] 两级可展开: 排名表默认只列 31 个申万一级行业, 点任一行在其下方展开该
 *   一级下的全部二级行业(中性淡化); 一级行指标取主看板同源数据(口径一致, 杜绝"同一
 *   行业两个分数"), 行内嵌迷你热力条显示组内二级冷热分布, 替代原独立的热力总览模块。
 *   背景: 109 个二级平铺会把极值埋掉(常态 ~78% 为中性); 改为"一级总览 + 按需下钻"后
 *   首屏 31 行, 既不藏信息(109 个一个不少, 只是收起来了)也不吵。
 *   PARENTS 为空 = 一级/二级数据不同步, 一级行退化为占位、指标列一律 "-",
 *   绝不用旧时点的分数冒充当前值(错误的数字比缺失更危险)。
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
  /* [2026-09-03] 31 个申万一级行业, 由 build_html.py --sub 从主看板 industry_obos.json 抽取注入。
   * 为空 = 一级/二级数据不同步(CI 每日重算两者必然同日), 此时一级行退化为占位。 */
  var PARS = (typeof PARENTS !== 'undefined' && PARENTS && PARENTS.length) ? PARENTS : [];
  var HAS_PAR = PARS.length > 0;
  var DETAIL_DEFAULT_DAYS = 250;
  var charts = [];
  /* [2026-09-03] echarts 改为按需注入, 不再随首屏同步阻塞加载。
   * 原写法把 echarts 作为首屏阻塞外链, 与数据文件串行: 1MB 图表库 + 数 MB 数据全部到齐
   * 才开始渲染, 期间整页白屏且无任何提示 —— 国内访问 Pages 时表现就是"加载不出来数据"。
   * (注意: 别在这里写出完整的 script 外链标签字面量, 会被 build_html.py 的反回归断言拦下)
   * 现改为: 表格/摘要/质量门禁等纯 DOM 内容先出, 图表库后台拉取, 到位后再补图;
   * 即便图表库加载失败, 数据部分也不受影响(而不是整页空白)。 */
  var echartsPending = [];
  var echartsLoading = false;
  function ensureEcharts(cb) {
    if (typeof echarts !== 'undefined') { cb(); return; }
    echartsPending.push(cb);
    if (echartsLoading) return;
    echartsLoading = true;
    var s = document.createElement('script');
    s.src = 'echarts.min.js';
    s.onload = function () {
      var q = echartsPending.slice(); echartsPending.length = 0;
      for (var i = 0; i < q.length; i++) { try { q[i](); } catch (e) { /* 单个失败不拖垮其余 */ } }
    };
    s.onerror = function () {
      echartsPending.length = 0;
      echartsLoading = false;  // 允许下次切行业时重试
      var el = document.getElementById('detail');
      if (el) el.innerHTML = '<div class="note" style="padding:24px;text-align:center">'
        + '图表库加载失败（网络问题）。上方表格数据不受影响，可刷新或稍后重试。</div>';
    };
    document.head.appendChild(s);
  }
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
  var KIDS = {};
  (function () {
    var seen = {};
    INDS.forEach(function (x) {
      var p = x.parent || '其他';
      if (!seen[p]) { seen[p] = { name: p, n: 0 }; GROUPS.push(seen[p]); }
      seen[p].n++;
      (KIDS[p] = KIDS[p] || []).push(x);
    });
  })();
  /* 一级行数据源: 有 PARENTS 用真实一级行业; 否则退化为仅含名称的占位(指标列渲染为 "-") */
  var PAR_ROWS = HAS_PAR ? PARS : GROUPS.map(function (g) { return { name: g.name }; });
  /* 展开态: 一级行业名 -> 1。默认全收起(首屏 31 行) */
  var openSet = {};

  /* ---------- [2026-09-03] 视图收敛: 状态档位多选 + 关键词搜索 ----------
   * 默认只勾选非中性四档(超买/偏热/偏冷/超卖), 中性档不勾 -> 表格默认不渲染中性行业;
   * 排序改为按 |cur_score-50| 偏离度降序, 越极端越靠前(逆向投资只关心两端)。
   * 档位全选或全不选时等价于"全部"(避免出现空表这种无意义状态)。 */
  var STATE_KEYS = ['超买', '偏热', '中性', '偏冷', '超卖'];
  var STATE_COLOR = { '超买': COLORS.ob, '偏热': COLORS.hot, '中性': COLORS.mid, '偏冷': COLORS.cold, '超卖': COLORS.os };
  /* 默认全选: 首屏只有 31 个一级行业, 信息量可接受, 无需再折叠 */
  var selStates = { '超买': 1, '偏热': 1, '中性': 1, '偏冷': 1, '超卖': 1 };
  var curQ = '';

  function deviation(x) {
    var s = (typeof x.cur_score === 'number' && isFinite(x.cur_score)) ? x.cur_score : 50;
    return Math.abs(s - 50);
  }
  function byDeviation(list) {
    return list.slice().sort(function (a, b) { return deviation(b) - deviation(a); });
  }
  /* 一级行的排序键: 有真实分数用其自身偏离度; 降级占位时用组内最极端二级的偏离度 */
  function parDeviation(p) {
    if (typeof p.cur_score === 'number' && isFinite(p.cur_score)) return Math.abs(p.cur_score - 50);
    return (KIDS[p.name] || []).reduce(function (m, x) { return Math.max(m, deviation(x)); }, 0);
  }
  function selKeys() { return STATE_KEYS.filter(function (k) { return selStates[k]; }); }
  function useAllStates() {
    var keys = selKeys();
    return keys.length === 0 || keys.length === STATE_KEYS.length;
  }
  /* 可见的一级行(受档位筛选 + 搜索影响)
   * 搜索命中组内二级时自动展开该组, 否则"搜到了却看不见" */
  function visiblePars() {
    var q = curQ ? String(curQ).toLowerCase() : '';
    var useAll = useAllStates();
    var keys = selKeys();
    var out = PAR_ROWS.filter(function (p) {
      if (!useAll && keys.indexOf(stateOf(p)) < 0) return false;
      if (!q) return true;
      if (String(p.name).toLowerCase().indexOf(q) >= 0) return true;
      return (KIDS[p.name] || []).some(function (x) {
        return String(x.name).toLowerCase().indexOf(q) >= 0;
      });
    });
    if (q) out.forEach(function (p) { openSet[p.name] = 1; });
    return out.slice().sort(function (a, b) { return parDeviation(b) - parDeviation(a); });
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

  /* ---------- 状态档位 chips (带计数, 多选; 计数对象 = 一级行业) ---------- */
  function renderStateChips() {
    var base = PAR_ROWS;
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

  /* ---------- 排名表: 两级可展开 ----------
   * 一级行(31 个申万一级) + 点击在其下方展开该一级下的全部二级行业(中性的淡化)。
   * 一级行点击 = 展开/收起(不切详情图, 避免误触把大图换掉); 二级行点击 = 切换详情图。
   * 「背离」列提到「状态」之后: 它是对状态的动能解释, 且必须在不横向滚动时可见——
   * 原位置在倒数第 6 列, 窄屏会被挤出视野(640px 断点甚至整列 display:none)。 */
  function pctOrDash(v) {
    return (typeof v === 'number' && isFinite(v)) ? (v.toFixed(1) + '%') : '-';
  }
  /* 行内迷你热力条: 一级行名称后直接显示组内二级的冷热分布(原独立热力总览模块的压缩版) */
  function miniBar(gname) {
    var kids = (KIDS[gname] || []).slice().sort(function (a, b) {
      return num(a.cur_score, 50) - num(b.cur_score, 50);
    });
    if (!kids.length) return '';
    return '<span class="miniwrap">' + kids.map(function (x) {
      return '<i class="mini" style="background:' + stateColor(x) + '" title="' + x.name
        + ' · ' + fmt(x.cur_score) + ' 分 · ' + stateOf(x) + '"></i>';
    }).join('') + '</span>';
  }
  /* 指标单元格: 一级行与二级行共用, 保证 16 列完全对齐、可直接上下对比。
   * 占位行(一级数据缺失)各字段为 undefined, 一律渲染 "-", 不用旧数冒充。 */
  function cellsHtml(x) {
    var chg = x.chg5;
    var chgTxt = (typeof chg === 'number' && isFinite(chg)) ? ((chg > 0 ? '+' : '') + fmt(chg)) : '-';
    var fcEnd = (x.forecast && x.forecast.median && x.forecast.median.length)
      ? x.forecast.median[x.forecast.median.length - 1] : x.fc_end;
    var sigTxt = x.sig === '显著超买' ? '<span class="tag-sig">超买</span>'
      : x.sig === '显著超卖' ? '<span class="tag-os">超卖</span>' : '<span class="tag-na">-</span>';
    sigTxt += ' <span style="color:#98a2b3">q=' + fmt(x.fdr_q, 2) + '</span>';
    var vst = x.vol_state, vr = x.vol_ratio;
    var vColor = vst === '放量' ? '#b1493f' : (vst === '缩量' ? '#3c8168' : '#98a2b3');
    var vTxt = (typeof vr === 'number' ? vr.toFixed(2) : '-') + (vst ? ' ' + vst : '');
    var above = (x.above_ma200 === true) ? '✓' : (x.above_ma200 === false ? '✗' : '-');
    return '<td class="c-score"><b style="color:' + stateColor(x) + '">' + fmt(x.cur_score) + '</b></td>'
      + '<td class="c-state"><span class="chip" style="background:' + stateColor(x) + '" title="PIT 分位阈值：超买 '
      + fmt(x.ob_line) + ' / 偏热 ' + fmt(x.hot_line) + ' / 偏冷 ' + fmt(x.cold_line)
      + ' / 超卖 ' + fmt(x.os_line) + '">' + stateOf(x) + '</span></td>'
      + '<td class="c-div">' + divTxt(x.divergence) + '</td>'
      + '<td class="c-sig"><span style="color:' + sigColor(x.sig_label) + ';font-weight:600">'
      + (x.sig_label || '-') + '</span></td>'
      + '<td class="c-vol"><span style="color:' + vColor + '">' + vTxt + '</span></td>'
      + '<td class="c-rs">' + fmt(x.rs_pct_now) + '</td>'
      + '<td class="c-above">' + above + '</td>'
      + '<td class="c-fdr">' + sigTxt + '</td>'
      + '<td class="c-chg5">' + chgTxt + '</td>'
      + '<td class="c-ret20">' + pctOrDash(x.ret20) + '</td>'
      + '<td class="c-ret60">' + pctOrDash(x.ret60) + '</td>'
      + '<td class="c-ret250">' + pctOrDash(x.ret250) + '</td>'
      + '<td class="c-fc">' + fmt(fcEnd) + '</td>';
  }
  function renderTable() {
    var pars = visiblePars();
    var html = '';
    var nKid = 0;
    pars.forEach(function (p, i) {
      var nm = String(p.name || '-');
      var kids = byDeviation(KIDS[nm] || []);
      nKid += kids.length;
      var open = !!openSet[nm];
      html += '<tr class="rowgrp' + (open ? ' open' : '') + '" data-g="' + nm + '">'
        + '<td class="rank c-rank">' + (i + 1) + '</td>'
        + '<td class="lft c-name"><span class="arw">' + (open ? '▾' : '▸') + '</span>'
        + '<b>' + nm + '</b>' + miniBar(nm) + '<span class="cnt">' + kids.length + '</span></td>'
        + '<td class="lft c-parent">申万一级</td>'
        + cellsHtml(p) + '</tr>';
      if (!open) return;
      if (!kids.length) {
        html += '<tr class="rowsub"><td class="rank c-rank"></td>'
          + '<td class="lft c-name" colspan="15" style="color:#98a2b3;padding-left:22px">'
          + '该一级行业下暂无细分行业数据</td></tr>';
        return;
      }
      kids.forEach(function (x, j) {
        /* 中性的二级行业淡化而非隐藏: 点开了就是要看全部, 只是降低视觉权重 */
        var dim = (stateOf(x) === '中性') ? ' dim' : '';
        html += '<tr class="rowsub' + dim + (x.code === curCode ? ' sel' : '') + '" data-code="' + x.code + '">'
          + '<td class="rank c-rank">' + (i + 1) + '.' + (j + 1) + '</td>'
          + '<td class="lft c-name">' + x.name + '</td>'
          + '<td class="lft c-parent">' + (x.parent || '-') + '</td>'
          + cellsHtml(x) + '</tr>';
      });
    });
    var noteEl = document.getElementById('tNote');
    if (noteEl) {
      var txt = '共 <b>' + pars.length + '</b> 个一级行业，下属 <b>' + nKid + '</b> 个细分行业';
      if (curQ) txt += '（搜索「' + curQ + '」，已自动展开命中的一级）';
      else if (!useAllStates()) txt += '（已按档位筛选）';
      txt += ' · 按<b>偏离 50 分的极端程度</b>排序 · <b>点击一级行业行</b>展开其下属细分行业';
      /* 一级数据缺失必须如实告知, 否则用户会误以为"所有一级行业都是中性/无信号" */
      if (!HAS_PAR) {
        txt += ' · <b style="color:#c08a3e">一级行业指标暂不可用</b>（一级与二级数据不同步，待下次刷新自动恢复）';
      }
      noteEl.innerHTML = txt;
    }
    if (!pars.length) {
      document.getElementById('rankBody').innerHTML = '<tr><td colspan="16" style="text-align:center;'
        + 'color:var(--sub);padding:26px">'
        + (curQ ? '未找到匹配「' + curQ + '」的行业' : '当前筛选条件下没有行业') + '</td></tr>';
      return;
    }
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
    /* 图表按需加载: echarts 未就绪时不阻塞, 先让表格/标题等纯 DOM 内容出齐 */
    if (!detailChart) {
      ensureEcharts(function () {
        if (curCode !== code) return;   // 加载期间用户已切到别的行业, 放弃本次绘制
        detailChart = makeChart('detail');
        if (detailChart && detailChart.getZr) detailChart.getZr().on('dblclick', resetZoom);
        if (detailChart) detailChart.setOption(buildDetailOption(x), { notMerge: true });
      });
    } else {
      detailChart.setOption(buildDetailOption(x), { notMerge: true });
    }

    /* 只给二级行加/去 sel, 且用 classList 增删以保留原有的 dim(中性淡化)类——
     * 整体覆盖 className 会把 dim 一起抹掉, 中性行就不再淡化了。 */
    var rows = document.querySelectorAll('#rankBody tr.rowsub');
    for (var r = 0; r < rows.length; r++) {
      var on = rows[r].getAttribute('data-code') === code;
      if (on) rows[r].classList.add('sel'); else rows[r].classList.remove('sel');
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
      if (!tr) return;
      /* 一级行: 整行点击 = 展开/收起。不切详情图——一级行没有详情曲线数据, 切了反而是错的 */
      if (tr.classList && tr.classList.contains('rowgrp')) {
        var g = tr.getAttribute('data-g');
        if (openSet[g]) delete openSet[g]; else openSet[g] = 1;
        renderTable();
        return;
      }
      /* 二级行: 点击切换下方详情图 */
      var code = tr.getAttribute('data-code');
      if (code) renderDetail(code);
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
    window.addEventListener('resize', function () { charts.forEach(function (c) { c.resize(); }); });
    window.addEventListener('orientationchange', function () { setTimeout(function () { if (detailChart) renderDetail(curCode); }, 250); });
  }

  renderQuality();
  renderSummary();
  renderStateChips();
  renderTable();
  renderBacktest();
  bindEvents();
  renderDetail(curCode);
  /* 能执行到这里 = 数据已就绪且表格已渲染。移除首屏加载态, 图表仍在后台补加载。 */
  var _bm = document.getElementById('bootMask');
  if (_bm && _bm.parentNode) _bm.parentNode.removeChild(_bm);
})();
