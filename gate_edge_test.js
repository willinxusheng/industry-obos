/* 受控边界测试：验证二级门禁 (test_render.js --sub) 在极端市况下既不假绿、也不误报
 *
 * 背景：a784de6 新增的两条断言是**数据驱动**的——
 *   1) 默认渲染集必须恰好 = 非中性集（折叠契约）
 *   2) 渲染顺序必须按 |score-50| 降序（偏离度排序）
 *   3) 分组热力条必须覆盖全部 109 个行业
 * 这类断言最大的风险是：今天的数据恰好让它绿了，换一天市况就误报 FAIL（拖垮每日刷新）；
 * 或者更糟——在任何市况下都恒真（假绿，等于没有门禁）。
 *
 * 做法：在临时目录里造若干极端市况的 sub_obos.json（不动仓库真实数据），
 * 让**真实的门禁脚本 + 真实的 sub_app.js** 去跑，看它是否给出正确判决：
 *   - 正向：全市场中性和 / 仅 1 个非中性 / 全部偏热 / 全部超卖 —— 门禁必须判绿且行数正确
 *   - 反向：故意改坏 sub_app.js（去排序 / 默认勾中性 / 热力条漏渲染）—— 门禁必须判红
 * 反向用例是"反假绿"的关键：只要门禁对坏代码仍判绿，说明断言形同虚设。
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
  data: path.join(REPO, 'data', 'sub_obos.json')
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
const rawData = fs.readFileSync(SRC.data, 'utf8');

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

const cases = [
  {
    name: 'A 全市场中性（非中性=0，应退化为全部）',
    mutate(d) { d.industries.forEach(x => setScore(x, mid(x))); },
    expectRows: d => d.industries.length
  },
  {
    name: 'B 仅 1 个非中性（极端冷清市况）',
    mutate(d) {
      d.industries.forEach((x, i) => setScore(x, i === 0 ? x.ob_line + 1 : mid(x)));
    },
    expectRows: () => 1
  },
  {
    name: 'C 全部偏热（极端狂热市况）',
    mutate(d) { d.industries.forEach(x => setScore(x, (x.hot_line + x.ob_line) / 2)); },
    expectRows: d => d.industries.length
  },
  {
    name: 'D 全部超卖（极端恐慌市况）',
    mutate(d) { d.industries.forEach(x => setScore(x, Math.max(0.1, x.os_line - 1))); },
    expectRows: d => d.industries.length
  }
];

function run(name, dataObj, appCode) {
  const dir = path.join(TMP, name.replace(/[^A-Za-z0-9]/g, '_').slice(0, 40));
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, 'data'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'test_render.js'), gateSrc);
  fs.writeFileSync(path.join(dir, 'sub_app.js'), appCode);
  fs.writeFileSync(path.join(dir, 'data', 'sub_obos.json'), JSON.stringify(dataObj));
  const r = cp.spawnSync(NODE, [path.join(dir, 'test_render.js'), '--sub'], { encoding: 'utf8' });
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') };
}

let fail = 0;
console.log('===== 正向：极端市况下门禁必须判绿，且渲染行数符合预期 =====');
for (const c of cases) {
  const d = JSON.parse(rawData);
  c.mutate(d);
  const r = run(c.name, d, appSrc);
  const m = r.out.match(/rankBody OK: (\d+) rows/);
  const rows = m ? parseInt(m[1], 10) : -1;
  const want = c.expectRows(d);
  const green = r.code === 0;
  const good = green && rows === want;
  if (!good) fail++;
  console.log(`  ${good ? 'PASS' : 'FAIL'}  ${c.name}`);
  console.log(`        门禁=${green ? '绿' : '红(exit ' + r.code + ')'}  渲染行数=${rows} 期望=${want}`);
  if (!green) {
    console.log('        ' + r.out.split('\n').filter(l => /error/i.test(l)).slice(0, 3).join('\n        '));
  }
}

console.log('\n===== 反向对照：故意改坏 app，门禁必须判红（否则断言是假绿）=====');
const broken = [
  {
    name: '坏1 去掉偏离度排序（打乱极端优先）',
    patch: s => s.replace(
      'return list.slice().sort(function (a, b) { return deviation(b) - deviation(a); });',
      'return list.slice();')
  },
  {
    name: '坏2 默认也把中性勾上（折叠失效）',
    patch: s => s.replace(
      "var selStates = { '超买': 1, '偏热': 1, '偏冷': 1, '超卖': 1 };",
      "var selStates = { '超买': 1, '偏热': 1, '中性': 1, '偏冷': 1, '超卖': 1 };")
  },
  {
    /* 每组丢掉第一个行业 -> 热力条覆盖数 109-31=78，"覆盖全部"断言必须判红 */
    name: '坏3 热力条漏渲染行业（每组少一格）',
    patch: s => s.replace(
      'var kids = (byGroup[g.name] || []).slice().sort(function (a, b) {',
      'var kids = (byGroup[g.name] || []).slice(1).sort(function (a, b) {')
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
  const d = JSON.parse(rawData);
  const r = run(b.name, d, patched);
  const caught = r.code !== 0;
  if (!caught) fail++;
  console.log(`  ${caught ? 'PASS' : 'FAIL'}  ${b.name} -> 门禁${caught ? '判红（抓到）' : '仍判绿（假绿！）'}`);
  if (caught) {
    const line = r.out.split('\n').filter(l => /error|mismatch|not sorted|missing/i.test(l))[0];
    if (line) console.log('        ' + line.trim().slice(0, 120));
  }
}

fs.rmSync(TMP, { recursive: true, force: true });
console.log('\n' + (fail === 0
  ? 'GATE EDGE TEST PASSED：' + cases.length + ' 种极端市况判绿且行数正确，' + broken.length + ' 种人为破坏全部被抓'
  : 'GATE EDGE TEST FAILED：' + fail + ' 项'));
process.exit(fail === 0 ? 0 : 1);
