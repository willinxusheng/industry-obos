/* 真实 DOM 端到端验证（需 jsdom；未安装则跳过，不阻断 CI）
 *
 * 目的: test_render.js 用的是 DOM stub, 跑不出真实 DOM 行为(innerHTML 解析 / 事件冒泡 /
 * querySelectorAll / 类名切换 / 事件委托)。本脚本用 jsdom 加载真实模板 + 真实数据 + 真实
 * sub_app.js, 验证两级可展开表格的实际行为——补 stub 门禁覆盖不到的部分。
 *
 * 核心契约: 109 个二级行业一个都不少, 只是默认收起来了。逐个展开 31 个一级行业
 *   后必须恰好凑齐 109 个、且无重复。这是"折叠"与"藏数据"的分界线。
 *
 * 运行:  npm i jsdom (任意位置, 能 require 到即可)  ->  node test_dom_sub.js
 * 说明: 刻意不挂进 daily.yml —— npm 安装失败会拖垮每日数据刷新, 得不偿失。
 *       改动二级看板交互后本地跑一次即可。 */
const fs = require('fs');
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  console.log('jsdom 未安装，跳过真实 DOM 验证（npm i jsdom 后重跑）');
  process.exit(0);
}

const REPO = __dirname;
const errs = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errs.push('jsdomError: ' + e.message));
vc.on('error', (...a) => errs.push('console.error: ' + a.join(' ')));

const tpl = fs.readFileSync(REPO + '/template_sub.html', 'utf8')
  .replace('__ECHARTS__', '').replace('__DATA__', '').replace('__APPJS__', '');
const dom = new JSDOM(tpl, { runScripts: 'outside-only', virtualConsole: vc, pretendToBeVisual: true });
const { window } = dom;
const doc = window.document;

// echarts stub（两级表格是纯 DOM，仅详情图用 echarts）
window.echarts = {
  init() {
    return { setOption() {}, resize() {}, on() {}, off() {}, getZr() { return { on() {} }; } };
  }
};
/* [2026-09-04] DATA 与 PARENTS 都取自构建产物 sub_data.js，不再读 data/sub_obos.json。
 * 改用构建产物有两个理由：
 *   1) data/sub_obos.json 在 .gitignore 里（data/ 由 CI 独家写盘），CI checkout
 *      根本拿不到 —— 这道门禁一进 CI 就 ENOENT 挂掉，本地却一直是绿的。
 *   2) 验的本来就该是"构建 → 前端"这条链路，用源数据等于绕开了构建这一环。
 * 另注：产物里时序已被 build_html.py 剥离，所以本门禁事实上跑在"时序不在 DATA 里"
 * 的状态下 —— 这反而是好事：哪天首屏又误用了时序字段，这里会直接渲染出
 * undefined/NaN 被抓到（想验"时序在场"的加载路径请看 test_dom_noseries.js）。 */
const { readVar } = require('./test_dom_common');
const SD = fs.readFileSync(REPO + '/sub_data.js', 'utf8');
const DATA_RAW = readVar(SD, 'DATA');
if (!DATA_RAW) {
  console.log('FAIL: sub_data.js 里取不到 DATA（产物结构变了？请同步本脚本）');
  process.exit(1);
}
const DATA = JSON.parse(DATA_RAW);
window.eval('var DATA = ' + DATA_RAW + ';');

let parCount = 0;
try {
  const par = readVar(SD, 'PARENTS');
  if (par) { window.eval('var PARENTS = ' + par + ';'); parCount = JSON.parse(par).length; }
  else throw new Error('no PARENTS in sub_data.js');
} catch (e) {
  window.eval('var PARENTS = [];');
}
window.eval(fs.readFileSync(REPO + '/sub_app.js', 'utf8'));

let fail = 0;
function ok(cond, label, extra) {
  if (cond) console.log('  PASS  ' + label);
  else { console.log('  FAIL  ' + label + (extra !== undefined ? '  -> ' + extra : '')); fail++; }
}
const q = s => doc.querySelectorAll(s);
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const grpRows = () => [...q('#rankBody tr.rowgrp')];
const subRows = () => [...q('#rankBody tr.rowsub')];
const grpByName = n => grpRows().find(t => t.getAttribute('data-g') === n);
const byCode = {};
DATA.industries.forEach(x => { byCode[x.code] = x; });
const nInd = DATA.industries.length;
const parentsOf = {};
DATA.industries.forEach(x => { (parentsOf[x.parent || '其他'] = parentsOf[x.parent || '其他'] || []).push(x); });
const nParents = Object.keys(parentsOf).length;

console.log('--- 0. 数据与环境 ---');
console.log('  二级行业 ' + nInd + ' 个 / 一级行业 ' + nParents + ' 个 / PARENTS ' + parCount + ' 条'
  + (parCount ? '' : '（降级：一级指标不可用）'));
ok(nParents === 31, '一级分组数 = 31', nParents);

console.log('--- 1. 首屏：31 个一级行业，二级全部收起 ---');
ok(grpRows().length === nParents, '一级行数 = ' + nParents, grpRows().length);
ok(subRows().length === 0, '默认全收起，首屏不出现二级行', subRows().length);
const miniTotal = q('#rankBody .mini').length;
ok(miniTotal === nInd, '迷你热力条格子合计 = ' + nInd + ' 个二级行业（总览信息不缩水）', miniTotal);
ok(q('#sChips .gchip').length === 6, '档位 chips = 全部 + 5 档', q('#sChips .gchip').length);
ok(q('#gChips').length === 0, '原一级分组 chips 已移除（与行点击展开重复）');
ok(q('#heatBody').length === 0, '原热力总览模块已移除');

console.log('--- 2. 一级行排序（偏离 50 分降序）---');
function parDev(name) {
  const tr = grpByName(name);
  const td = tr && tr.querySelector('td.c-score');
  const v = td ? parseFloat(td.textContent) : NaN;
  if (isFinite(v)) return Math.abs(v - 50);
  return (parentsOf[name] || []).reduce((m, x) => Math.max(m, Math.abs((x.cur_score || 50) - 50)), 0);
}
const gnames = grpRows().map(t => t.getAttribute('data-g'));
let sorted = true, prev = Infinity;
for (const n of gnames) {
  const d = parDev(n);
  if (d > prev + 1e-9) sorted = false;
  prev = d;
}
ok(sorted, '一级行按偏离度降序（最极端的板块排最前）');
ok(gnames[0] === gnames.slice().sort((a, b) => parDev(b) - parDev(a))[0],
  '首行是最极端一级: ' + gnames[0]);

console.log('--- 3. 展开 / 收起 交互 ---');
const first = gnames[0];
click(grpByName(first));
const n1 = subRows().length;
ok(n1 === (parentsOf[first] || []).length,
  '展开「' + first + '」出现 ' + n1 + ' 个二级行（该组实有 ' + (parentsOf[first] || []).length + '）', n1);
ok(grpByName(first).classList.contains('open'), '展开后一级行带 open 类');
ok(grpByName(first).querySelector('.arw').textContent.includes('▾'), '展开后箭头为 ▾');
/* 中性的二级行业必须带 dim（淡化而非隐藏） */
const dimRows = [...q('#rankBody tr.rowsub.dim')];
const neutralRows = subRows().filter(tr => {
  const x = byCode[tr.getAttribute('data-code')];
  return x && x.state === '中性';
});
ok(dimRows.length === neutralRows.length,
  '中性二级行全部淡化（' + neutralRows.length + ' 个），未隐藏任何一个',
  dimRows.length + ' vs ' + neutralRows.length);
ok(subRows().length > 0 && subRows().every(tr => tr.getAttribute('data-code')),
  '每个二级行都有 data-code（可下钻）');
click(grpByName(first));
ok(subRows().length === 0, '再点一次收起，二级行消失', subRows().length);

console.log('--- 4. 核心契约：逐个展开 31 组，109 个一个不少 ---');
const seen = new Set();
let accumulated = 0;
for (const n of gnames) {
  const tr = grpByName(n);
  if (!tr) { ok(false, '找不到一级行 ' + n); break; }
  click(tr);
  const subs = subRows();
  accumulated += subs.length;
  subs.forEach(t => seen.add(t.getAttribute('data-code')));
  click(grpByName(n));
}
ok(accumulated === nInd, '展开 31 组累计 ' + accumulated + ' 个二级行 = ' + nInd, accumulated);
ok(seen.size === nInd, '去重后覆盖 ' + seen.size + ' 个（无重复、无遗漏）', seen.size);
ok(subRows().length === 0, '全部收起后回到 ' + grpRows().length + ' 行', subRows().length);

console.log('--- 5. 二级行下钻（切换详情图）---');
click(grpByName(first));
const before = doc.getElementById('detailTitle').textContent;
const anySub = subRows()[0];
const anyName = byCode[anySub.getAttribute('data-code')].name;
click(anySub);
ok(doc.getElementById('detailTitle').textContent.includes(anyName),
  '点二级行切换详情图: ' + anyName);
ok(q('#rankBody tr.rowsub.sel').length === 1, '选中态唯一', q('#rankBody tr.rowsub.sel').length);
ok(anySub.classList.contains('sel'), '被点的二级行带 sel 类');
/* 一级行点击只展开，不应把详情图换掉（一级没有详情曲线数据） */
const titleBefore = doc.getElementById('detailTitle').textContent;
click(grpByName(first));
click(grpByName(first));
ok(doc.getElementById('detailTitle').textContent === titleBefore,
  '点一级行不切换详情图（只展开/收起）');

console.log('--- 6. 搜索（命中组内二级时自动展开）---');
const inp = doc.getElementById('qSearch');
function search(kw) {
  inp.value = kw;
  inp.dispatchEvent(new window.Event('input', { bubbles: true }));
}
search('光伏');
const pvSubs = subRows().map(t => byCode[t.getAttribute('data-code')].name);
ok(pvSubs.some(n => n.includes('光伏')), '搜「光伏」自动展开并命中: ' + pvSubs.filter(n => n.includes('光伏')).join(','));
ok(grpRows().length >= 1 && grpRows().length <= nParents, '只显示命中的一级行', grpRows().length);
search('医药');
ok(subRows().length > 0, '搜「医药」命中 ' + subRows().length + ' 个二级行并展开');
search('不存在的行业名xyz');
ok(q('#rankBody tr').length === 1 && doc.getElementById('rankBody').innerHTML.includes('未找到'),
  '无匹配时显示空状态提示而非空白表');
search('');
ok(grpRows().length === nParents, '清空搜索后恢复 ' + nParents + ' 个一级行', grpRows().length);

console.log('--- 7. 档位 chips（作用于一级行）---');
const chip = label => [...q('#sChips .gchip')].find(c => c.textContent.startsWith(label));
click(chip('中性'));
const afterUncheck = grpRows().length;
ok(afterUncheck < nParents, '取消「中性」后一级行减少到 ' + afterUncheck + '（原 ' + nParents + '）', afterUncheck);
click(chip('全部'));
ok(grpRows().length === nParents, '点「全部」恢复 ' + nParents + ' 行', grpRows().length);
click(chip('全部'));
ok(grpRows().length === nParents, '「全部」状态下再点不塌陷（不出现空表）', grpRows().length);

console.log('--- 8. 详情图 / 回测 / 门禁区块 ---');
ok(doc.getElementById('detailTitle').textContent.length > 0, '详情标题已渲染');
ok(q('#btBody tr').length >= 6, '回测表行数 >= 6', q('#btBody tr').length);
ok(doc.getElementById('qBadge').textContent === DATA.quality.status, '质量徽章状态 = ' + DATA.quality.status);

console.log('--- 9. 运行期错误 ---');
ok(errs.length === 0, '无 JS 运行期错误', errs.join(' | '));

console.log('\n' + (fail === 0 ? 'REAL-DOM VERIFY PASSED' : 'REAL-DOM VERIFY FAILED: ' + fail + ' 项'));
process.exit(fail === 0 ? 0 : 1);
