/**
 * scripts/consistency-check.mjs — 管理端一致性断言器（设计契约的可执行版）。
 *
 * **功能**
 * 对 9 条管理端路由 × 桌面/移动两个视口做机器判定：表头与单元格对齐同向、
 * 操作尾列右贴、数值列右对齐、动作组主按钮最右、控件高度落在 4px 格、
 * 卡头灰底计数与整页屏数；结果写 JSON，供改动前后对比。
 *
 * **归属**
 * 开发期只读探针，不参与 `npm run verify`（需要先起展示构建服务）。
 *
 * **口径**
 * 先 `npm run build:showcase` 并起 4174 预览（`npm run preview:showcase`），再：
 *   BASE=http://127.0.0.1:4174 node scripts/consistency-check.mjs
 * 判定标准与 `ui/DESIGN-SYSTEM.md` §7「一致性契约」逐条对应。
 */

import { chromium } from 'playwright'
import fs from 'node:fs';
const BASE = process.env.BASE || 'http://127.0.0.1:4174'
const OUT = process.env.OUT || '/tmp/ui-consistency.json'
const ROUTES = ['/', '/accounts', '/users', '/schools', '/queue', '/logs', '/audit', '/system', '/settings'];
const VPS = [{ n: 'desk', w: 1440, h: 900 }, { n: 'mobile', w: 390, h: 844 }];
const ACTIONISH = /^操作$|^动作$/;
const NUMERIC = /条数|占比|次数|剩余|数量|账号数|已试/;
const browser = await chromium.launch();
const out = [];
for (const vp of VPS) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, reducedMotion: 'reduce' });
  const page = await ctx.newPage();
  for (const route of ROUTES) {
    await page.goto(BASE + route, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(650);
    const d = await page.evaluate(({ ACTIONISH, NUMERIC, vh, route }) => {
      const ACTIONISH_RE = new RegExp(ACTIONISH); const NUMERIC_RE = new RegExp(NUMERIC);
      const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; };
      const tables = [...document.querySelectorAll('table')].filter(vis);
      const mismatched = []; const actionLeft = []; const numericLeft = []; const tableInfo = [];
      for (const t of tables) {
        const ths = [...t.querySelectorAll('thead th')];
        const tr = t.querySelector('tbody tr');
        const tds = tr ? [...tr.querySelectorAll('td')] : [];
        if (!ths.length || !tds.length || tds.length !== ths.length) { tableInfo.push({ cols: ths.length, note: 'skip' }); continue; }
        const norm = (v) => v === 'start' ? 'left' : v === 'end' ? 'right' : v;
        ths.forEach((th, i) => {
          const h = norm(getComputedStyle(th).textAlign); const c = norm(getComputedStyle(tds[i]).textAlign);
          const label = (th.innerText || '').trim();
          if (h !== c) mismatched.push(label + ':' + h + '/' + c);
          if (ACTIONISH_RE.test(label) && i === ths.length - 1 && c !== 'right') actionLeft.push(label + ':' + c);
          if (NUMERIC_RE.test(label) && c !== 'right') numericLeft.push(label + ':' + c);
        });
        tableInfo.push({ cols: ths.length, headers: ths.map(th => (th.innerText || '').trim()).slice(0, 9) });
      }
      // primary-last in action clusters
      const clusters = ['.app-page-actions', '.hero-actions', '.sys-card-actions', '.modal-foot', '.app-account-actions', '.app-row-actions', '.q-row-actions'];
      const primaryNotLast = []; let clusterCount = 0;
      for (const sel of clusters) for (const el of document.querySelectorAll(sel)) {
        if (!vis(el)) continue; clusterCount++;
        const kids = [...el.children].filter(k => k.nodeType === 1 && vis(k));
        const act = kids.filter(k => k.matches('button, a, [role=button]'));
        const prim = act.filter(k => /primary/i.test(k.className.toString()));
        if (prim.length && act[act.length - 1] !== prim[prim.length - 1]) primaryNotLast.push(sel + ':primNotLast:' + (prim[prim.length - 1].textContent || '').trim().slice(0, 8));
        // 只有容器自身决定水平落位时才要求 flex-end（.hero-actions 由父级 .hero 的 space-between 兜底）。
        if (/app-page-actions|sys-card-actions|modal-foot|app-account-actions|app-row-actions/.test(sel)) {
          const j = getComputedStyle(el).justifyContent;
          if (j !== 'flex-end' && j !== 'center' && j !== 'space-between') primaryNotLast.push(sel + ':justify=' + j);
        }
      }
      // heights
      const ctl = [...document.querySelectorAll('button, input, select, textarea, [role=button]')].filter(vis);
      const badH = ctl.filter(el => { const h = el.getBoundingClientRect().height; return Math.abs(h - Math.round(h)) < 0.6 && Math.round(h) % 4 !== 0; }).length;
      const heads = { std: document.querySelectorAll('.card-head').length, gray: document.querySelectorAll('.q-card-head, .a-card-head').length };
      return { route, mismatched, actionLeft, numericLeft, primaryNotLast: [...new Set(primaryNotLast)].slice(0, 8), badH, ctl: ctl.length, heads, docH: Math.round(document.documentElement.scrollHeight), screens: +(document.documentElement.scrollHeight / vh).toFixed(2), tableInfo };
    }, { ACTIONISH: ACTIONISH.source, NUMERIC: NUMERIC.source, vh: vp.h, route });
    out.push({ vp: vp.n, ...d });
    console.log(vp.n, route, 'mismatch', d.mismatched.length, 'actionLeft', d.actionLeft.length, 'numLeft', d.numericLeft.length, 'primNotLast', d.primaryNotLast.length, 'badH', d.badH + '/' + d.ctl, 'gray', d.heads.gray, 'screens', d.screens);
  }
  await ctx.close();
}
await browser.close();
fs.writeFileSync(OUT, JSON.stringify(out, null, 1));
console.log('report:', OUT)
