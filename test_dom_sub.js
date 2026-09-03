/* 真实 DOM 端到端验证（需 jsdom；未安装则跳过，不阻断 CI）
 *
 * 目的: test_render.js 用的是 DOM stub, 跑不出真实 DOM 行为(innerHTML 解析 / 事件冒泡 /
 * querySelectorAll / 类名切换 / 事件委托)。本脚本用 jsdom 加载真实模板 + 真实数据 + 真实
 * sub_app.js, 验证分组热力条与表格折叠/排序/搜索/联动的实际行为——补 stub 门禁覆盖不到的部分。
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

// echarts stub（热力条已改为纯 DOM，仅详情图用 echarts）
window.echarts = {
  init() {
    return { setOption() {}, resize() {}, on() {}, off() {}, getZr() { return { on() {} }; } };
  }
};
const DATA_RAW = fs.readFileSync(REPO + '/data/sub_obos.json', 'utf8');
const DATA = JSON.parse(DATA_RAW);
window.eval('var DATA = ' + DATA_RAW + ';');
window.eval(fs.readFileSync(REPO + '/sub_app.js', 'utf8'));

let fail = 0;
function ok(cond, label, extra) {
  if (cond) console.log('  PASS  ' + label);
  else { console.log('  FAIL  ' + label + (extra !== undefined ? '  -> ' + extra : '')); fail++; }
}
const q = s => doc.querySelectorAll(s);
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

console.log('--- 1. 首屏渲染 ---');
const nInd = DATA.industries.length;
ok(q('#heatBody .hrow').length === 31, '热力条行数 = 31 个一级行业', q('#heatBody .hrow').length);
ok(q('#heatBody .hcell').length === nInd, '热力条格子数 = ' + nInd + ' 个二级行业', q('#heatBody .hcell').length);
ok(q('#sChips .gchip').length === 6, '档位 chips = 全部 + 5 档', q('#sChips .gchip').length);
ok(q('#gChips .gchip').length === 32, '一级 chips = 全部 + 31 组', q('#gChips .gchip').length);

const nonNeutral = DATA.industries.filter(x => x.state && x.state !== '中性' && x.state !== '-').length;
const rows0 = q('#rankBody tr').length;
ok(rows0 === nonNeutral, '表格默认行数 = 非中性数 ' + nonNeutral, rows0);
ok(doc.getElementById('tNote').innerHTML.includes(String(nonNeutral)), 'tNote 显示数量与档位说明');
/* 只校验"状态"列(.c-state)不含中性——注意"综合信号"列渲染的是 sig_label,
   其值也可能是"中性", 两者是不同字段, 不能混为一谈 */
ok([...q('#rankBody td.c-state')].every(td => td.textContent.trim() !== '中性'),
  '默认视图状态列不含中性档位',
  [...q('#rankBody td.c-state')].map(td => td.textContent.trim()).filter(t => t === '中性').length);

console.log('--- 2. 排序契约（偏离 50 分降序）---');
const codes = [...q('#rankBody tr')].map(tr => tr.getAttribute('data-code'));
const byCode = {};
DATA.industries.forEach(x => { byCode[x.code] = x; });
let sorted = true, prev = Infinity;
for (const c of codes) {
  const d = Math.abs(byCode[c].cur_score - 50);
  if (d > prev + 1e-9) sorted = false;
  prev = d;
}
ok(sorted, '行序按 |score-50| 降序（越极端越靠前）');
ok(Math.abs(byCode[codes[0]].cur_score - 50) >= Math.abs(byCode[codes[codes.length - 1]].cur_score - 50),
  '首行是最极端行业: ' + byCode[codes[0]].name + ' ' + byCode[codes[0]].cur_score);

console.log('--- 3. 档位 chips 交互 ---');
const chip = label => [...q('#sChips .gchip')].find(c => c.textContent.startsWith(label));
click(chip('中性'));
ok(q('#rankBody tr').length === nInd, '点「中性」后显示全部 ' + nInd + ' 行', q('#rankBody tr').length);
click(chip('中性'));
ok(q('#rankBody tr').length === nonNeutral, '再点「中性」收回 ' + nonNeutral + ' 行', q('#rankBody tr').length);
click(chip('全部'));
ok(q('#rankBody tr').length === nInd, '点「全部」= ' + nInd + ' 行', q('#rankBody tr').length);
click(chip('全部'));
ok(q('#rankBody tr').length === nInd, '「全部」状态下再点不塌陷（不出现空表）', q('#rankBody tr').length);

console.log('--- 4. 搜索框 ---');
const inp = doc.getElementById('qSearch');
inp.value = '光伏';
inp.dispatchEvent(new window.Event('input', { bubbles: true }));
const hit = [...q('#rankBody tr')].map(tr => byCode[tr.getAttribute('data-code')].name);
ok(hit.length === 1 && hit[0].includes('光伏'), '搜「光伏」命中 1 行: ' + hit.join(','), hit.join(','));
inp.value = '医药';
inp.dispatchEvent(new window.Event('input', { bubbles: true }));
const hit2 = [...q('#rankBody tr')].map(tr => byCode[tr.getAttribute('data-code')].parent);
ok(hit2.length > 0 && hit2.every(p => p && p.includes('医药')), '按所属一级搜「医药」命中 ' + hit2.length + ' 行');
inp.value = '不存在的行业名';
inp.dispatchEvent(new window.Event('input', { bubbles: true }));
ok(q('#rankBody tr').length === 1 && doc.getElementById('rankBody').innerHTML.includes('未找到'),
  '无匹配时显示空状态提示而非空白表');
inp.value = '';
inp.dispatchEvent(new window.Event('input', { bubbles: true }));
ok(q('#rankBody tr').length === nInd, '清空搜索后恢复（当前为全部档位）', q('#rankBody tr').length);

console.log('--- 5. 热力条下钻 + 联动高亮 ---');
const cell = q('#heatBody .hcell')[0];
const cellName = cell.textContent;
click(cell);
ok(doc.getElementById('detailTitle').textContent.includes(cellName),
  '点格子切换详情图: ' + cellName);
ok(q('#heatBody .hcell.sel').length === 1, '热力条选中态唯一', q('#heatBody .hcell.sel').length);
ok(cell.classList.contains('sel'), '被点的格子带 sel 类');

console.log('--- 6. 一级分组 chips 联动 ---');
const gchip = [...q('#gChips .gchip')].find(c => c.textContent.startsWith('电力设备'));
click(gchip);
ok(q('#rankBody tr').length > 0, '选「电力设备」后表格有数据', q('#rankBody tr').length);
ok([...q('#rankBody tr')].every(tr => byCode[tr.getAttribute('data-code')].parent === '电力设备'),
  '表格只含该一级下的二级行业');
ok(q('#heatBody .hrow.dim').length === 30, '热力条其余 30 行被淡化（dim）', q('#heatBody .hrow.dim').length);
ok(q('#heatBody .hcell').length === nInd, '淡化不影响格子总数（仍覆盖全部）');
click([...q('#gChips .gchip')].find(c => c.textContent.startsWith('全部')));
ok(q('#heatBody .hrow.dim').length === 0, '回到全部后淡化清除');

console.log('--- 7. 详情图 / 回测 / 门禁区块 ---');
ok(doc.getElementById('detailTitle').textContent.length > 0, '详情标题已渲染');
ok(q('#btBody tr').length >= 6, '回测表行数 >= 6', q('#btBody tr').length);
ok(doc.getElementById('qBadge').textContent === DATA.quality.status, '质量徽章状态 = ' + DATA.quality.status);

console.log('--- 8. 运行期错误 ---');
ok(errs.length === 0, '无 JS 运行期错误', errs.join(' | '));

console.log('\n' + (fail === 0 ? 'REAL-DOM VERIFY PASSED' : 'REAL-DOM VERIFY FAILED: ' + fail + ' 项'));
process.exit(fail === 0 ? 0 : 1);
