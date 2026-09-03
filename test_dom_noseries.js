/* 验证「首屏不等时序」这一契约在真实 DOM 下的表现。
 * 做法：把二级看板现场装配到临时目录，故意不带 sub_series.js
 * （等价于用户还没下完 / 加载失败 / CDN 404），看首屏到底渲染出了什么。
 * 这是唯一能覆盖"用户网络差时看到什么"的路径，与 test_dom_sub.js 互补：
 * 那个走完整加载（时序在场），这个走时序缺席。
 *
 * [2026-09-03] 原先直接拷仓库里的 sub.html 跑。sub.html 是构建产物，内联的是"上一轮构建时"
 * 的 sub_app.js —— 而 CI 里门禁跑在建产物之前，等于这道门禁一直在测陈旧代码：
 * 改了 sub_app.js 它照样绿，改坏了它也照样绿（典型的"测副本"，门禁形同虚设）。
 * 现改为从 template_sub.html + 当前 sub_app.js 现场装配（与 test_dom_sub.js 同款），
 * 测的一定是源码。
 *
 *   为什么仍然要落盘到临时目录、而不是纯 window.eval：只有 resources:'usable' 下，
 *   ensureSeries 注入的 <script src="sub_series.js"> 才会真的去取、真的 404、
 *   真的走 onerror 分支 —— 这正是本门禁要验的那条路径。纯 eval 环境里它只会永远
 *   停在"加载中"，验不到失败提示。
 */
const fs = require('fs');
const path = require('path');
const os = require('os');

const REPO = __dirname;
let JSDOM;
try {
  JSDOM = require('jsdom').JSDOM;
} catch (e) {
  console.log('SKIP: jsdom 未安装');
  process.exit(0);
}

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'obos-noseries-'));

const tpl = fs.readFileSync(REPO + '/template_sub.html', 'utf8');
const APP = fs.readFileSync(REPO + '/sub_app.js', 'utf8');
// 与 build_html.py 同款转义: 数据里若含 </script> 会提前闭合脚本标签
const DATA_RAW = fs.readFileSync(REPO + '/data/sub_obos.json', 'utf8')
  .replace(/<\/script>/g, '<\\/script>');

// PARENTS 取自构建产物 sub_data.js：验的是"构建 → 前端"这条完整链路，不另写一份抽取逻辑
let PARENTS_JS = '[]';
try {
  const sd = fs.readFileSync(REPO + '/sub_data.js', 'utf8');
  const m = sd.match(/var PARENTS = (\[[\s\S]*?\]);\s*$/m);
  if (m) PARENTS_JS = m[1];
} catch (e) { /* 取不到就退化为占位，由下面"一级行 31 个"的断言兜住 */ }

// echarts 打桩：本门禁只关心"时序缺席时首屏还剩什么"，图表库加载是另一回事
const ECHARTS_STUB = 'window.echarts={init:function(){return{setOption:function(){},'
  + 'resize:function(){},on:function(){},off:function(){},'
  + 'getZr:function(){return{on:function(){}}}}}};';

const html = tpl
  .replace('__ECHARTS__', '<script>' + ECHARTS_STUB + '</script>')
  .replace('__DATA__', '<script>var DATA=' + DATA_RAW + ';var PARENTS=' + PARENTS_JS + ';</script>')
  .replace('__APPJS__', '<script>\n' + APP + '\n</script>');

for (const k of ['__ECHARTS__', '__DATA__', '__APPJS__']) {
  if (html.indexOf(k) !== -1) {
    console.log('FAIL: template_sub.html 的占位符 ' + k + ' 未被替换（模板改动后请同步本脚本）');
    process.exit(1);
  }
}
fs.writeFileSync(path.join(TMP, 'sub.html'), html);
// 刻意不写 sub_series.js

let fail = 0;
function ck(cond, name, extra) {
  if (!cond) fail++;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' — ' + extra : ''}`);
}

// 自检：装配进去的确实是当前源码，而不是某个陈旧副本
ck(/function renderFcNote/.test(APP), '装配用的是当前 sub_app.js 源码（含 renderFcNote）');
ck(/function ensureSeries/.test(APP), '装配用的是当前 sub_app.js 源码（含 ensureSeries）');

const errs = [];
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  resources: 'usable',
  url: 'file://' + TMP + '/sub.html',
  pretendToBeVisual: true,
  beforeParse(w) {
    w.addEventListener('error', e => errs.push(String(e.message || e.error)));
    w.onerror = (m) => errs.push(String(m));
  }
});

setTimeout(() => {
  const d = dom.window.document;
  const rows = d.querySelectorAll('#rankBody tr');
  const parents = d.querySelectorAll('#rankBody tr.rowgrp');
  const fc = d.getElementById('fcNote');
  const title = d.getElementById('detailTitle');
  const detail = d.getElementById('detail');
  const boot = d.getElementById('bootMask');

  console.log('=== 时序缺席（sub_series.js 404）时的首屏 ===');
  ck(parents.length === 31, '一级行 31 个', `实得 ${parents.length}`);
  ck(rows.length === 31, '默认全收起 = 31 行', `实得 ${rows.length}`);
  ck(!!boot === false, '首屏加载遮罩已移除（渲染未中途抛错）', boot ? '遮罩仍在' : '');
  ck(!!(title && title.textContent && title.textContent.length > 10),
    '详情标题已渲染', title ? title.textContent.slice(0, 40) : '(无)');

  const fcHtml = fc ? fc.innerHTML : '';
  ck(fcHtml.length > 200, '推演口径说明(fcNote) 首屏已有内容（不等时序）', `${fcHtml.length} 字符`);
  ck(/动态阈值/.test(fcHtml), 'fcNote 含「动态阈值」段');
  ck(/跨行业类比推演/.test(fcHtml), 'fcNote 含「跨行业类比推演」段');
  ck(!/undefined|NaN/.test(fcHtml), 'fcNote 无 undefined/NaN', (fcHtml.match(/undefined|NaN/g) || []).join(','));

  const dHtml = detail ? detail.innerHTML : '';
  ck(/加载中|加载失败/.test(dHtml), '详情区给出明确提示（不是空白）',
    dHtml.replace(/<[^>]*>/g, '').trim().slice(0, 60) || '(空)');

  // 关键：表格里的指标必须是真数字，不能因为时序缺席就变 "-"
  const firstRow = parents[0] ? parents[0].textContent : '';
  ck(!/^\s*-+\s*$/.test(firstRow), '一级行指标列未塌陷为 "-"（标量数据独立于时序）');

  const fatal = errs.filter(e => !/sub_series/.test(e) && !/Could not load script/.test(e));
  ck(fatal.length === 0, '无其它 JS 运行期错误', fatal.slice(0, 2).join(' | '));

  fs.rmSync(TMP, { recursive: true, force: true });
  console.log(fail === 0 ? '\nNO-SERIES VERIFY PASSED：时序缺席时首屏内容完整（表格+标题+推演说明都在）'
                         : `\nNO-SERIES VERIFY FAILED：${fail} 项`);
  process.exit(fail === 0 ? 0 : 1);
}, 3000);
