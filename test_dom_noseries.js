/* 验证「首屏不等时序」这一契约在真实 DOM 下的表现。
 * 做法：把看板现场装配到临时目录，故意不带 series.js / sub_series.js
 * （等价于用户还没下完 / 加载失败 / CDN 404），看首屏到底渲染出了什么。
 * 这是唯一能覆盖"用户网络差时看到什么"的路径，与 test_dom_sub.js 互补：
 * 那个走完整加载（时序在场），这个走时序缺席。
 *
 * [2026-09-03] 原先直接拷仓库里的 sub.html 跑。sub.html 是构建产物，内联的是"上一轮构建时"
 * 的 sub_app.js —— 而 CI 里门禁跑在建产物之前，等于这道门禁一直在测陈旧代码：
 * 改了 sub_app.js 它照样绿，改坏了它也照样绿（典型的"测副本"，门禁形同虚设）。
 * 现改为从模板 + 当前源码现场装配（与 test_dom_sub.js 同款），测的一定是源码。
 *
 * [2026-09-04] 主看板也做了首屏/时序拆分（data.js 1.35MB -> 78KB），同一条契约必须
 * 同样套在它头上。此前本门禁只覆盖二级看板 —— 那次改完 app.js 六道门禁全绿，
 * 但没有一道真正验过"主看板 series.js 缺席时首屏还剩什么"，属于换了个地方重犯
 * 同一个假绿错误。现改为两个看板依次装配、分别断言。
 *
 *   为什么仍然要落盘到临时目录、而不是纯 window.eval：只有 resources:'usable' 下，
 *   ensureSeries 注入的 <script src="..."> 才会真的去取、真的 404、
 *   真的走 onerror 分支 —— 这正是本门禁要验的那条路径。纯 eval 环境里它只会永远
 *   停在"加载中"，验不到失败提示。
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');

const REPO = __dirname;
let JSDOM;
try {
  JSDOM = require('jsdom').JSDOM;
} catch (e) {
  console.log('SKIP: jsdom 未安装');
  process.exit(0);
}

let fail = 0;
function ck(cond, name, extra) {
  if (!cond) fail++;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' — ' + extra : ''}`);
}

// echarts 打桩：二级看板构建时把 1MB 图表库内联进 __ECHARTS__，这里换成同接口的桩
const ECHARTS_STUB = '<script>window.echarts={init:function(){return{setOption:function(){},'
  + 'resize:function(){},on:function(){},off:function(){},'
  + 'getZr:function(){return{on:function(){}}}}}};</script>';

const BOARDS = [
  {
    key: 'main',
    label: '主看板（31 个一级行业）',
    tpl: 'template.html',
    app: 'app.js',
    dataJson: 'data/industry_obos.json',
    page: 'index.html',
    seriesFile: 'series.js',
    /* 与 build_html.py 完全一致：主看板的 __ECHARTS__ 被替换成空注释，
     * echarts 由 ensureEcharts 在点图时才注入。照抄同一行为的好处是：
     * 哪天首屏又偷偷依赖上 echarts，它会真的 404 并被下面的断言抓到。 */
    echartsSlot: '<!-- echarts 由 app 按需注入, 不阻塞首屏 -->',
    expectRows: 31,
    parentSel: null,      // 主看板是 31 行平铺，没有两级树形
    expectParents: null,
  },
  {
    key: 'sub',
    label: '二级看板（109 个二级行业 · 两级树形）',
    tpl: 'template_sub.html',
    app: 'sub_app.js',
    dataJson: 'data/sub_obos.json',
    page: 'sub.html',
    seriesFile: 'sub_series.js',
    echartsSlot: ECHARTS_STUB,
    expectRows: 31,       // 默认全收起 = 31 个一级行
    parentSel: '#rankBody tr.rowgrp',
    expectParents: 31,
  },
];

function assemble(cfg) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'obos-noseries-' + cfg.key + '-'));
  const tpl = fs.readFileSync(path.join(REPO, cfg.tpl), 'utf8');
  const APP = fs.readFileSync(path.join(REPO, cfg.app), 'utf8');
  // 与 build_html.py 同款转义: 数据里若含 </script> 会提前闭合脚本标签
  const DATA_RAW = fs.readFileSync(path.join(REPO, cfg.dataJson), 'utf8')
    .replace(/<\/script>/g, '<\\/script>');

  // 二级看板额外需要 PARENTS，取自构建产物 sub_data.js：
  // 验的是"构建 → 前端"这条完整链路，不另写一份抽取逻辑
  let extra = '';
  if (cfg.key === 'sub') {
    try {
      const sd = fs.readFileSync(path.join(REPO, 'sub_data.js'), 'utf8');
      const m = sd.match(/var PARENTS = (\[[\s\S]*?\]);\s*$/m);
      if (m) extra = ';var PARENTS=' + m[1] + ';';
    } catch (e) { /* 取不到就退化为占位，由"一级行 31 个"的断言兜住 */ }
  }

  const html = tpl
    .replace('__ECHARTS__', cfg.echartsSlot)
    .replace('__DATA__', '<script>var DATA=' + DATA_RAW + extra + '</script>')
    .replace('__APPJS__', '<script>\n' + APP + '\n</script>');

  for (const k of ['__ECHARTS__', '__DATA__', '__APPJS__']) {
    if (html.indexOf(k) !== -1) {
      console.log(`FAIL: ${cfg.tpl} 的占位符 ${k} 未被替换（模板改动后请同步本脚本）`);
      process.exit(1);
    }
  }
  fs.writeFileSync(path.join(tmp, cfg.page), html);
  // 刻意不写 cfg.seriesFile —— 本门禁要的就是它 404
  return { tmp, html, APP };
}

/* 时序的两种"缺席"要分开验，因为对应两种截然不同的线上故障：
 *   404  —— 文件真没有（CDN 未同步、部署漏文件）。浏览器会立刻 onerror，
 *           前端必须给出一个终态提示，不能让用户对着转圈图标干等。
 *   挂起 —— 请求发出去了但永不返回（跨境链路中断、连接被黑洞吞掉）。
 *           这是国内访问境外 CDN 最常见也最难受的失败模式：既不成功也不报错，
 *           前端若把首屏内容挂在时序之后，用户就是一片空白。
 *   落盘到临时目录只能造出 404（错的文件名同样 404，造不出挂起），
 *   所以挂起改为起一个真实 HTTP 服务：页面从 http 取，对时序文件的请求接受连接
 *   但永不回包 —— 这正是跨境链路被黑洞吞掉时的真实表现，比伪造事件更可信。
 *
 *   [2026-09-04 踩坑] 先试过 jsdom 30 的 requestInterceptor 返回 pending Promise，
 *   实测它对 file:// 请求压根不会被调用（命中 0 次），场景标签会变成撒谎。
 *   现在挂起场景有一条自检断言盯着：若详情区显示的是"加载失败"而不是"加载中"，
 *   就说明挂起没生效、本场景退化成了 404，门禁直接判红。 */

/* 挂起场景用的 HTTP 服务：/页面 正常返回 HTML，时序文件的请求接受后永不回包。
 * 挂起的 socket 必须记账，否则 server.close() 会被它们吊住不返回。 */
function startHangServer(cfg, html) {
  return new Promise(resolve => {
    const hanging = [];
    const server = http.createServer((req, res) => {
      const url = (req.url || '').split('?')[0];
      if (url === '/' + cfg.page) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(html);
      } else if (url.indexOf(cfg.seriesFile) !== -1) {
        hanging.push(req.socket);   // 不回包：连接就此挂住
      } else {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('not found');
      }
    });
    server.listen(0, '127.0.0.1', () => resolve({ server, hanging, port: server.address().port }));
  });
}

function stopHangServer(h) {
  h.hanging.forEach(s => { try { s.destroy(); } catch (e) { /* 已断开 */ } });
  try { server_close(h.server); } catch (e) { /* 已关闭 */ }
}
function server_close(s) {
  if (s.closeAllConnections) s.closeAllConnections();
  s.close();
}

async function runBoard(cfg, mode) {
  const hang = (mode === 'hang');
  const built = assemble(cfg);
  const { tmp, html, APP } = built;
  const errs = [];

  let baseUrl, hangSrv = null;
  if (hang) {
    hangSrv = await startHangServer(cfg, html);
    baseUrl = `http://127.0.0.1:${hangSrv.port}/${cfg.page}`;
  } else {
    baseUrl = 'file://' + tmp + '/' + cfg.page;
  }

  return new Promise(resolve => {
    const dom = new JSDOM(html, {
      runScripts: 'dangerously',
      resources: 'usable',
      url: baseUrl,
      pretendToBeVisual: true,
      beforeParse(w) {
        w.addEventListener('error', e => errs.push(String(e.message || e.error)));
        w.onerror = (m) => errs.push(String(m));
      }
    });

    setTimeout(() => {
      // 两种场景的标题格式必须一致（"时序 404" / "时序 挂起"），
      // 否则按标题切分输出的工具会只认到其中一种，让另一种悄悄逃过统计。
      const scenario = hang ? `时序 挂起（${cfg.seriesFile} 请求永不返回）` : `时序 404（${cfg.seriesFile} 不存在）`;
      console.log(`\n=== ${cfg.label}：${scenario} 时的首屏 ===`);
      const d = dom.window.document;
      const rows = d.querySelectorAll('#rankBody tr');
      const fc = d.getElementById('fcNote');
      const title = d.getElementById('detailTitle');
      const detail = d.getElementById('detail');
      const boot = d.getElementById('bootMask');

      // 自检：装配进去的确实是当前源码，而不是某个陈旧副本
      ck(/function renderFcNote/.test(APP), `装配用的是当前 ${cfg.app} 源码（含 renderFcNote）`);
      ck(/function ensureSeries/.test(APP), `装配用的是当前 ${cfg.app} 源码（含 ensureSeries）`);
      // 自检：时序文件确实不在磁盘上，否则下面验的是"加载成功"而不是"缺席"
      // （注意不能去查 HTML 里有没有 series.js 字样 —— 内联的 app 源码里
      //  本来就有 `s.src = 'series.js'`，那样判永远为真，是假断言）
      ck(!fs.existsSync(path.join(tmp, cfg.seriesFile)),
        `临时目录里确实没有 ${cfg.seriesFile}（走的是缺席路径）`);

      if (cfg.parentSel) {
        const parents = d.querySelectorAll(cfg.parentSel);
        ck(parents.length === cfg.expectParents, `一级行 ${cfg.expectParents} 个`, `实得 ${parents.length}`);
      }
      ck(rows.length === cfg.expectRows, `表格行数 = ${cfg.expectRows}`, `实得 ${rows.length}`);
      ck(!boot, '首屏加载遮罩已移除（渲染未中途抛错）', boot ? '遮罩仍在' : '');
      ck(!!(title && title.textContent && title.textContent.length > 10),
        '详情标题已渲染', title ? title.textContent.slice(0, 40) : '(无)');

      const fcHtml = fc ? fc.innerHTML : '';
      ck(fcHtml.length > 200, '推演口径说明(fcNote) 首屏已有内容（不等时序）', `${fcHtml.length} 字符`);
      ck(/动态阈值/.test(fcHtml), 'fcNote 含「动态阈值」段');
      ck(/跨行业类比推演/.test(fcHtml), 'fcNote 含「跨行业类比推演」段');
      ck(!/undefined|NaN/.test(fcHtml), 'fcNote 无 undefined/NaN', (fcHtml.match(/undefined|NaN/g) || []).join(','));

      /* 详情区的期望随场景而变，但"不能空白"是两种场景的共同底线。
       * [2026-09-04] 原先只断言 /加载中|加载失败/，那让"永远转圈"也能通过。
       * 404 场景下请求已经彻底失败，前端必须收口给出终态提示 —— 停在"加载中"
       * 说明 onerror 分支没接住（或压根没派发），用户会对着转圈图标干等。
       * 挂起场景下请求还在路上，停在"加载中"反而是诚实的未决态，不判红；
       * 这种场景真正要验的是：时序没回来，首屏其余部分照样完整（见上面各条断言）。 */
      const dText = (detail ? detail.textContent : '').trim();
      ck(dText.length > 0, '详情区不是空白', dText.slice(0, 60));
      if (!hang) {
        ck(!/加载中/.test(dText), '详情区已收口到终态提示（不是干等的"加载中"）', dText.slice(0, 60));
        ck(/加载失败|数据已更新/.test(dText), '终态提示说的是人话（失败或需刷新）', dText.slice(0, 60));
      } else {
        /* 自检：挂起必须真的挂住。若这里显示的是"加载失败"，说明请求并没有被吊住、
         * 本场景悄悄退化成了 404 —— 那样场景标签就是在撒谎，上面的断言也失去了意义。 */
        ck(/加载中/.test(dText), '【自检】挂起确实生效（停在"加载中"而非"加载失败"）', dText.slice(0, 60));
      }

      // 关键：表格里的指标必须是真数字，不能因为时序缺席就变 "-"
      const firstRow = rows[0] ? rows[0].textContent : '';
      ck(!/^\s*-+\s*$/.test(firstRow), '首行指标列未塌陷为 "-"（标量数据独立于时序）');

      // 主看板特有：首屏摘要是四格计数，同样是标量，不该受时序影响
      if (cfg.key === 'main') {
        const sumOk = ['sumOb', 'sumHot', 'sumMid', 'sumOs'].every(id => {
          const el = d.getElementById(id);
          return el && el.textContent.trim().length > 0;
        });
        ck(sumOk, '首屏四格摘要已渲染（超买/偏热/中性/偏冷）');
      }

      // 时序文件本身的 404 是预期内的，不算运行期错误
      const fatal = errs.filter(e => !(new RegExp(cfg.seriesFile).test(e)) && !/Could not load script/.test(e));
      ck(fatal.length === 0, '无其它 JS 运行期错误', fatal.slice(0, 2).join(' | '));

      dom.window.close();
      fs.rmSync(tmp, { recursive: true, force: true });
      if (hangSrv) stopHangServer(hangSrv);
      resolve();
    }, 3000);
  });
}

(async () => {
  const modes = ['404', 'hang'];
  for (const mode of modes) {
    for (const cfg of BOARDS) {
      await runBoard(cfg, mode);
    }
  }
  const scene = BOARDS.length * modes.length;
  console.log(fail === 0
    ? `\nNO-SERIES VERIFY PASSED：${scene} 个场景（${BOARDS.length} 个看板 × ${modes.length} 种缺席）首屏均完整（表格+标题+推演说明+摘要都在）`
    : `\nNO-SERIES VERIFY FAILED：${fail} 项`);
  process.exit(fail === 0 ? 0 : 1);
})();
