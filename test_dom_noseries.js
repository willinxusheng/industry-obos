/* 验证「首屏不等时序」这一契约在真实 DOM 下的表现。
 * 做法：把 sub.html 拷进临时目录，故意不带 sub_series.js（等价于用户还没下完 / 加载失败 / CDN 404），
 * 看首屏到底渲染出了什么。这是唯一能覆盖"用户网络差时看到什么"的路径。
 * 与 test_dom_sub.js 互补：那个走完整加载（时序在场），这个走时序缺席。
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
for (const f of ['sub.html', 'sub_data.js', 'sub_app.js', 'echarts.min.js']) {
  fs.copyFileSync(path.join(REPO, f), path.join(TMP, f));
}
// 刻意不拷 sub_series.js

let fail = 0;
function ck(cond, name, extra) {
  if (!cond) fail++;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' — ' + extra : ''}`);
}

const html = fs.readFileSync(path.join(TMP, 'sub.html'), 'utf8');
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
