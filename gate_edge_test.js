/* 受控边界测试：验证二级门禁 (test_render.js --sub) 在极端市况下既不假绿、也不误报
 *
 * 背景：二级门禁的关键断言是**数据驱动 + 代码驱动**混合的——
 *   1) 首屏必须恰好 31 个一级行业行，二级全部收起（不是 109 行平铺）
 *   2) 一级行必须按偏离度 |score-50| 降序（越极端越靠前）
 *   3) 迷你热力条格子合计必须覆盖全部 109 个二级行业（总览信息不缩水）
 *   4) 一级/二级数据不同步时走降级路径：一级指标列一律 "-" 且必须如实披露
 * 这类断言最大的风险是：今天的数据恰好让它绿了，换一天市况就误报 FAIL（拖垮每日刷新）；
 * 或者更糟——在任何市况下都恒真（假绿，等于没有门禁）。
 *
 * 做法：在临时目录里造若干极端市况的 sub_obos.json / industry_obos.json（不动仓库真实数据），
 * 让**真实的门禁脚本 + 真实的 sub_app.js** 去跑，看它是否给出正确判决：
 *   - 正向：二级全中性 / 仅 1 个非中性 / 全偏热 / 全超卖 / 一级分数打乱 / 数据不同步降级
 *           —— 门禁必须判绿，且行数、降级标记都符合预期
 *   - 反向：故意改坏 sub_app.js（去排序 / 默认全展开 / 热力条漏渲染 / 降级时冒充数字 /
 *           降级却不披露）—— 门禁必须判红
 * 反向用例是"反假绿"的关键：只要门禁对坏代码仍判绿，说明断言形同虚设。
 * 补丁一旦没命中源码（重构后锚点失效），本脚本会**自己判红**，避免"以为在测其实没测"。
 *
 * 零第三方依赖（仅 fs / path / os / child_process），可安全放进 CI：
 *   node gate_edge_test.js
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const REPO = __dirname;
const NODE = process.execPath;
const TMP = path.join(os.tmpdir(), 'obos_gate_edge_' + process.pid);

const SRC = {
  gate: path.join(REPO, 'test_render.js'),
  app: path.join(REPO, 'sub_app.js'),
  sub: path.join(REPO, 'data', 'sub_obos.json'),
  par: path.join(REPO, 'data', 'industry_obos.json')
};

/* 缺文件就跳过而不是失败：本脚本是门禁的门禁，不该因为取数失败把每日刷新一起拖垮 */
for (const [k, p] of Object.entries(SRC)) {
  if (!fs.existsSync(p)) {
    console.log('SKIP gate_edge_test: missing ' + k + ' -> ' + p);
    process.exit(0);
  }
}

const gateSrc = fs.readFileSync(SRC.gate, 'utf8');
const appSrc = fs.readFileSync(SRC.app, 'utf8');
const rawSub = fs.readFileSync(SRC.sub, 'utf8');
const rawPar = fs.readFileSync(SRC.par, 'utf8');

/* 一级行业个数 = 二级数据的 parent 去重数（31）。写死 31 会在行业分类调整时误报，算出来才稳。 */
const N_PAR = new Set(JSON.parse(rawSub).industries.map(x => x.parent || '其他')).size;

/* 与 test_render.js 里完全相同的状态判定式，保证 cur_score 与 state 自洽 */
function expState(s, x) {
  return s >= x.ob_line ? '超买'
    : s <= x.os_line ? '超卖'
      : s >= x.hot_line ? '偏热'
        : s <= x.cold_line ? '偏冷' : '中性';
}
function setScore(x, s) {
  x.cur_score = Math.max(0.1, Math.min(99.9, s));
  x.state = expState(x.cur_score, x);
}
function mid(x) { return (x.cold_line + x.hot_line) / 2; }

/* 一级行业分数打乱：让"文件顺序"绝不等于"偏离度降序"。
 * 排序断言只在这样的用例下才有意义——若父数据本身就有序，去掉 sort 也看不出来，断言就成了假绿。 */
function shufflePar(p) {
  p.industries.forEach(function (x, i) { setScore(x, 50 + ((i * 13) % 7) - 3); });
}

/* 每个用例：sub 变异 + 一级数据变异 + 是否同步(asof 一致)
 * sync=false 即模拟"一级停在旧日期、二级已刷新"的真实故障，必须走降级路径 */
const cases = [
  {
    key: 'A', name: 'A 二级全中性（最冷清市况）', sync: true, par: null,
    sub(d) { d.industries.forEach(x => setScore(x, mid(x))); }
  },
  {
    key: 'B', name: 'B 二级仅 1 个非中性', sync: true, par: null,
    sub(d) { d.industries.forEach((x, i) => setScore(x, i === 0 ? x.ob_line + 1 : mid(x))); }
  },
  {
    key: 'C', name: 'C 二级全部偏热（狂热市况）', sync: true, par: null,
    sub(d) { d.industries.forEach(x => setScore(x, (x.hot_line + x.ob_line) / 2)); }
  },
  {
    key: 'D', name: 'D 二级全部超卖（恐慌市况）', sync: true, par: null,
    sub(d) { d.industries.forEach(x => setScore(x, Math.max(0.1, x.os_line - 1))); }
  },
  {
    key: 'E', name: 'E 一级分数打乱 + 二级全中性（排序契约最强用例）', sync: true, par: shufflePar,
    sub(d) { d.industries.forEach(x => setScore(x, mid(x))); }
  },
  {
    key: 'F', name: 'F 一级/二级数据不同步（降级路径：一级列渲染 "-"）', sync: false, par: null,
    sub(d) { d.industries.forEach(x => setScore(x, mid(x))); }
  },
  {
    key: 'G', name: 'G 降级路径 + 二级全超卖', sync: false, par: null,
    sub(d) { d.industries.forEach(x => setScore(x, Math.max(0.1, x.os_line - 1))); }
  }
];

/* 序列化结果缓存：sub_obos.json 有 9MB，重复 stringify 会明显拖慢 CI */
const cache = {};
function subJson(key) {
  if (!cache['sub:' + key]) {
    const c = cases.find(x => x.key === key);
    const d = JSON.parse(rawSub);
    if (c && c.sub) c.sub(d);
    cache['sub:' + key] = { str: JSON.stringify(d), asof: d.asof };
  }
  return cache['sub:' + key];
}
function parJson(subKey, parFn, sync) {
  const key = 'par:' + subKey + ':' + (parFn ? parFn.name : 'real') + ':' + sync;
  if (!cache[key]) {
    const p = JSON.parse(rawPar);
    if (parFn) parFn(p);
    const so = subJson(subKey);
    /* 不同步时把一级 asof 改坏一天，其余字段不动——这正是线上真实发生过的故障形态 */
    p.asof = sync ? so.asof : so.asof + '-STALE';
    cache[key] = JSON.stringify(p);
  }
  return cache[key];
}

function run(name, subStr, parStr, appCode) {
  const dir = path.join(TMP, name.replace(/[^A-Za-z0-9]/g, '_').slice(0, 40));
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, 'data'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'test_render.js'), gateSrc);
  fs.writeFileSync(path.join(dir, 'sub_app.js'), appCode);
  fs.writeFileSync(path.join(dir, 'data', 'sub_obos.json'), subStr);
  fs.writeFileSync(path.join(dir, 'data', 'industry_obos.json'), parStr);
  const r = cp.spawnSync(NODE, [path.join(dir, 'test_render.js'), '--sub'], { encoding: 'utf8' });
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') };
}

function probe(out) {
  const m = out.match(/rankBody OK: (\d+) rows rendered \((\d+) group-level\)/);
  return {
    rows: m ? parseInt(m[1], 10) : -1,
    groupRows: m ? parseInt(m[2], 10) : -1,
    degraded: /degraded: no PARENTS/.test(out)
  };
}

let fail = 0;
console.log('===== 正向：极端市况 / 降级路径下门禁必须判绿，且行数与降级标记符合预期 =====');
for (const c of cases) {
  const sub = subJson(c.key);
  const r = run(c.name, sub.str, parJson(c.key, c.par, c.sync), appSrc);
  const p = probe(r.out);
  const green = r.code === 0;
  const good = green && p.rows === N_PAR && p.groupRows === N_PAR && p.degraded === !c.sync;
  if (!good) fail++;
  console.log(`  ${good ? 'PASS' : 'FAIL'}  ${c.name}`);
  console.log(`        门禁=${green ? '绿' : '红(exit ' + r.code + ')'}  渲染行数=${p.rows}(一级 ${p.groupRows})`
    + ` 期望=${N_PAR}  降级=${p.degraded} 期望=${!c.sync}`);
  if (!green) {
    console.log('        ' + r.out.split('\n').filter(l => /error/i.test(l)).slice(0, 3).join('\n        '));
  }
}

console.log('\n===== 反向对照：故意改坏 app，门禁必须判红（否则断言是假绿）=====');
const broken = [
  {
    /* 用 E 的一级打乱数据：文件顺序 ≠ 偏离度降序，去掉 sort 立刻露馅 */
    name: '坏1 去掉一级行偏离度排序', key: 'E', sync: true, par: shufflePar,
    patch: s => s.replace(
      'return out.slice().sort(function (a, b) { return parDeviation(b) - parDeviation(a); });',
      'return out.slice();')
  },
  {
    name: '坏2 默认全展开（退回 109 行平铺）', key: 'E', sync: true, par: shufflePar,
    patch: s => s.replace('if (!open) return;', 'if (false) return;')
  },
  {
    /* 每组丢掉第一个行业 -> 迷你条覆盖数 109-31，"覆盖全部"断言必须判红 */
    name: '坏3 迷你热力条漏渲染（每组少一格）', key: 'E', sync: true, par: shufflePar,
    patch: s => s.replace(
      'var kids = (KIDS[gname] || []).slice().sort(function (a, b) {',
      'var kids = (KIDS[gname] || []).slice(1).sort(function (a, b) {')
  },
  {
    /* 降级时给占位行塞一个假的 50 分：用户会把它当成真实一级指标读数 */
    name: '坏4 降级时拿旧数字冒充一级指标', key: 'F', sync: false, par: null,
    patch: s => s.replace(
      'var PAR_ROWS = HAS_PAR ? PARS : GROUPS.map(function (g) { return { name: g.name }; });',
      "var PAR_ROWS = HAS_PAR ? PARS : GROUPS.map(function (g) { return { name: g.name, cur_score: 50, state: '中性', ob_line: 80, os_line: 20, hot_line: 65, cold_line: 35 }; });")
  },
  {
    name: '坏5 降级却不披露“一级指标暂不可用”', key: 'F', sync: false, par: null,
    patch: s => s.replace('if (!HAS_PAR) {', 'if (false) {')
  }
];
for (const b of broken) {
  const patched = b.patch(appSrc);
  if (patched === appSrc) {
    /* 补丁没命中 = 源码重构后锚点失效，此时本脚本已失去反假绿能力，必须红 */
    console.log(`  FAIL  ${b.name}: 补丁未命中源码（锚点失效，需更新本脚本）`);
    fail++;
    continue;
  }
  const r = run(b.name, subJson(b.key).str, parJson(b.key, b.par, b.sync), patched);
  const caught = r.code !== 0;
  if (!caught) fail++;
  console.log(`  ${caught ? 'PASS' : 'FAIL'}  ${b.name} -> 门禁${caught ? '判红（抓到）' : '仍判绿（假绿！）'}`);
  if (caught) {
    const line = r.out.split('\n').filter(l => /error|mismatch|not sorted|missing|must/i.test(l))[0];
    if (line) console.log('        ' + line.trim().slice(0, 140));
  }
}

fs.rmSync(TMP, { recursive: true, force: true });
console.log('\n' + (fail === 0
  ? 'GATE EDGE TEST PASSED：' + cases.length + ' 种极端市况/降级路径判绿且契约正确，'
    + broken.length + ' 种人为破坏全部被抓'
  : 'GATE EDGE TEST FAILED：' + fail + ' 项'));
process.exit(fail === 0 ? 0 : 1);
