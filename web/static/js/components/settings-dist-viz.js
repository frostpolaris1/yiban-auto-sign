/* 正态分布调参组件（设置页 · 签到调度卡）——方案 E「峰尖拖拽」生产版。

   交互（选型定稿：preview-states/dist-viz.html）：
   · 抓住峰尖圆点拖：左右 = 峰值时刻，上下 = 散布（上尖下扁）；空白处按下 = 整体推移
   · 拖时间轴上「底座」的两端：调散布（恒以峰尖对称）
   · 画布聚焦后方向键 ±1（Shift ×5），上 = 更尖
   · 可见编辑器：峰值区间 = 顶点钟点 ± 半径（半径一值定宽、顶点不动）；
     散布 = ±分钟区间——全部逐键实时预览
   · 纵轴 = 预计每分钟签到人数（随峰形自动缩放）；虚线 = 按账号数放大后的实际钟形

   状态唯一源 = 四枚隐藏 input（ss-mu-min/ss-mu-max/ss-sigma-min/ss-sigma-max，
   整数 0~100，A 档，id 与旧数字框一致）。settings-schedule 的 collect()/
   snapshotFromDom()/apply()/档位对拍字面量不因此改动：本组件把可见编辑与画布手势
   翻译成对隐藏 input 的整数 % 写入，再回调 opts.onChange()（→ markDirty + 区间警示）。

   实时依赖（谁改了要重画）：
   · 签到窗口 / 掐头去尾（含 20% 缓冲钳位，与 edgeMaxMin 同口径）→ 有效窗口即变，
     钟点换算、染色、底座位置全部实时重算——「所见即所生效」的关键
   · 分布方式 = 均匀 → 钟形置灰 + 提示（参数保留，切回正态即生效），编辑仍可用
   · 账号数（容量估算）→ σ_eff 放大曲线与高峰速率；容量卡保存后经 refreshWarn 到达
   · 主题（yiban:theme）→ 重取 CSS 变量重绘；A 档权限 → setReadonly（可见而不改）
   · 窗口倒置 / 有效宽度 ≤ 0 → 画布显示「窗口无效」，禁用编辑
   · tab 面板切换后首次可见（IntersectionObserver）与容器改宽 → 补一次绘制

   对外：mount(opts) / refresh() / setReadonly(bool)。 */

(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var DEFAULTS = { muLo: 40, muHi: 60, sgLo: 15, sgHi: 25 }; // 与 schedule._DEFAULT_* 同值
  var E_SIGMA_MAX = 33;   // 拖拽映射的 σ 上限（%）：贴着 σ_eff 的窗口/3 封顶
  var CANVAS_H = 210;

  var cfg = null;         // mount(opts)
  var root = null, cv = null, ctxRef = null;
  var ed = {};            // 可见编辑器引用
  var colors = {};
  var st = null;          // readState() 的快照
  var layout = null;      // drawE 返回的命中几何
  var rafPending = false;
  var readonly = false;

  function $(id) { return document.getElementById(id); }
  function clampPct(v) { return Math.min(100, Math.max(0, Math.round(v))); }

  /* ---------- 状态读取：隐藏 input 是唯一事实源 ---------- */
  function readPct(id, fb) {
    var el = $(id);
    var v = parseInt(el && el.value, 10);
    if (isNaN(v)) return fb;
    return Math.min(100, Math.max(0, v));
  }
  function readState() {
    var ctx = cfg.context();
    var s = {
      muLo: readPct(cfg.ids.muLo, DEFAULTS.muLo),
      muHi: readPct(cfg.ids.muHi, DEFAULTS.muHi),
      sgLo: readPct(cfg.ids.sgLo, DEFAULTS.sgLo),
      sgHi: readPct(cfg.ids.sgHi, DEFAULTS.sgHi),
      effLo: ctx.effLo, effHi: ctx.effHi, span: ctx.span,
      frontMin: ctx.frontMin || 0, backMin: ctx.backMin || 0,
      dist: ctx.dist, n: ctx.n || 0
    };
    if (s.muHi <= s.muLo) s.muHi = Math.min(100, s.muLo + 1);   // 防御：旧数据 lo>=hi 时仍可画
    if (s.sgHi <= s.sgLo) s.sgHi = Math.min(100, s.sgLo + 1);
    s.invalid = !(s.span > 0);
    st = s;
    return s;
  }
  function writeHidden(name, v) { $(cfg.ids[name]).value = String(v); }
  function changed() { if (cfg.onChange) cfg.onChange(); }

  /* ---------- 数学：钟形 / 包络 / σ_eff ---------- */
  function pdf(x, mu, sigma) {
    var s = Math.max(sigma, 0.5);
    return Math.exp(-((x - mu) * (x - mu)) / (2 * s * s)) / (s * Math.sqrt(2 * Math.PI));
  }
  // 区间盒上包络：固定 x、μ 时对 σ 求导的最优 σ=|x−μ| 夹到区间即闭式解，μ 按 25 点网格
  function envAt(xMin, s) {
    var muL = s.effLo + s.span * s.muLo / 100, muH = s.effLo + s.span * s.muHi / 100;
    var sL = s.span * s.sgLo / 100, sH = s.span * s.sgHi / 100;
    var best = 0;
    for (var i = 0; i <= 25; i++) {
      var mu = muL + (muH - muL) * i / 25;
      var sg = Math.min(Math.max(Math.abs(xMin - mu), sL), sH);
      var v = pdf(xMin, mu, sg);
      if (v > best) best = v;
    }
    return best;
  }
  function muMidPct(s) { return (s.muLo + s.muHi) / 2; }
  function sgMidPct(s) { return (s.sgLo + s.sgHi) / 2; }
  function sigmaFactor(n) {
    n = Math.max(1, Math.floor(n) || 1);
    return n > 20 ? 1 + Math.log2(n / 20) : 1;
  }
  function sigmaCap() { return Math.max(1, Math.floor(st.span / 3)); }

  function fmtT(min) {
    min = Math.round(min);
    var h = Math.floor(min / 60), m = min % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }

  /* ---------- 颜色：CSS 变量 → canvas 可用串（探针解析，主题跟随） ---------- */
  var probe = null;
  function resolve(expr) {
    probe.style.background = "";
    probe.style.background = expr;
    var v = getComputedStyle(probe).backgroundColor;
    probe.style.background = "";
    return v;
  }
  function readColors() {
    colors.curve = resolve("rgb(var(--c-blue-600))");
    colors.curveSoft = resolve("color-mix(in srgb, rgb(var(--c-blue-600)) 65%, transparent)");
    colors.envFill = resolve("color-mix(in srgb, rgb(var(--c-blue-500)) 10%, transparent)");
    colors.bandFill = resolve("color-mix(in srgb, rgb(var(--c-blue-500)) 22%, transparent)");
    colors.guide = resolve("color-mix(in srgb, rgb(var(--c-blue-600)) 55%, transparent)");
    colors.hairline = resolve("color-mix(in srgb, rgb(var(--c-blue-600)) 20%, transparent)");
    colors.grid = resolve("color-mix(in srgb, var(--control-border) 30%, transparent)");
    colors.axis = resolve("color-mix(in srgb, var(--control-border) 75%, transparent)");
    colors.hatch = resolve("color-mix(in srgb, var(--t-muted) 10%, transparent)");
    colors.hatchLine = resolve("color-mix(in srgb, var(--t-muted) 24%, transparent)");
    colors.muted = resolve("var(--t-muted)");
  }

  var FONT_SMALL = '10.5px Inter, "Noto Sans SC", "Microsoft YaHei", sans-serif';

  /* ---------- 写入：手势/编辑器 → 隐藏 input（整数 %） ---------- */
  // 峰值区间 = 顶点 ± 半径：mid/rPct 任一变化都重算两个整数端点，区间不出窗
  function applyMu(mid, rPct) {
    rPct = Math.min(rPct, mid, 100 - mid);
    var fullW = Math.max(2, Math.round(2 * rPct));
    var lo = clampPct(mid - fullW / 2);
    if (lo + fullW > 100) lo = 100 - fullW;
    st.muLo = lo;
    st.muHi = lo + fullW;
    writeHidden("muLo", lo);
    writeHidden("muHi", st.muHi);
  }
  function applySgScale(k) {
    // 底座端部/纵向拖拽：等比缩放 σ 区间（中点不变），钳 [1,100] 且保持间隙
    var nLo = clampPct(st.sgLo * k), nHi = clampPct(st.sgHi * k);
    if (nHi <= nLo) nHi = nLo + 1;
    if (nHi > 100) { nLo = Math.max(1, Math.round(nLo * 100 / nHi)); nHi = 100; }
    if (nLo < 1) nLo = 1;
    st.sgLo = Math.min(nLo, nHi - 1);
    st.sgHi = Math.max(nHi, st.sgLo + 1);
    writeHidden("sgLo", st.sgLo);
    writeHidden("sgHi", st.sgHi);
  }
  function applySgBounds(loPct, hiPct) {
    // 散布编辑器：±分钟独立设置，钳 [0,100] 并保持 hi > lo ≥ 1
    var a = clampPct(loPct), b = clampPct(hiPct);
    a = Math.max(1, a);
    b = Math.max(a + 1, b);
    st.sgLo = a;
    st.sgHi = b;
    writeHidden("sgLo", a);
    writeHidden("sgHi", b);
  }

  /* ---------- 绘制骨架：DPR 尺寸（draw 内联使用，不抽象） ---------- */
  function sizeCanvas() {
    var r = cv.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(160, Math.round(r.width)), h = Math.max(100, Math.round(r.height));
    var pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    var c = cv.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: c, w: w, h: h };
  }

  /* ---------- 主绘制 ---------- */
  function draw() {
    var s = sizeCanvas();
    var c = s.ctx, w = s.w, h = s.h;
    var padX = 46, topY = 14, axisY = h - 24;
    // x 域 = 整个签到窗口（含被裁掉的边缘，斜纹示意"不存在"）；掐头去尾是动态值
    var winStart = st.effLo - st.frontMin, winEnd = st.effHi + st.backMin;
    var winSpan = Math.max(1, winEnd - winStart);
    var ppm = (w - 2 * padX) / winSpan;                  // px / 分钟
    function xOf(min) { return padX + (min - winStart) * ppm; }
    var xEffL = xOf(st.effLo), xEffR = xOf(st.effHi);

    c.clearRect(0, 0, w, h);

    // 时间轴与刻度（按全天 20 分钟对齐的整点网格）
    c.strokeStyle = colors.axis; c.lineWidth = 1;
    c.beginPath(); c.moveTo(padX, axisY + 0.5); c.lineTo(w - padX, axisY + 0.5); c.stroke();
    c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textBaseline = "top"; c.textAlign = "center";
    var t0 = Math.ceil(winStart / 20) * 20;
    for (var t = t0; t <= winEnd; t += 20) {
      var tx = xOf(t);
      c.beginPath(); c.moveTo(tx, axisY); c.lineTo(tx, axisY + 4); c.stroke();
      c.fillText(fmtT(t), tx, axisY + 7);
    }

    if (st.invalid) {
      c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText("窗口无效：请先修正签到窗口与掐头去尾", w / 2, (topY + axisY) / 2);
      return;
    }

    // 裁剪区斜纹（掐头/去尾：区间外的时间"不存在"）
    function hatch(x0, x1) {
      if (x1 - x0 < 0.5) return;
      c.save();
      c.beginPath(); c.rect(x0, topY, x1 - x0, axisY - topY); c.clip();
      c.fillStyle = colors.hatch; c.fillRect(x0, topY, x1 - x0, axisY - topY);
      c.strokeStyle = colors.hatchLine; c.lineWidth = 1; c.beginPath();
      for (var x = x0 - (axisY - topY); x < x1; x += 6) {
        c.moveTo(x, axisY); c.lineTo(x + (axisY - topY), topY);
      }
      c.stroke(); c.restore();
    }
    hatch(xOf(winStart), xEffL);
    hatch(xEffR, xOf(winEnd));

    if (st.dist === "uniform") {
      // 均匀分布：参数保留但不生效——平铺矩形 + 提示，编辑仍可用（切回正态即生效）
      var uh = (axisY - topY) * 0.34;
      c.fillStyle = colors.envFill;
      c.fillRect(xEffL, axisY - uh, xEffR - xEffL, uh);
      c.strokeStyle = colors.guide; c.lineWidth = 1;
      c.strokeRect(xEffL + 0.5, axisY - uh + 0.5, xEffR - xEffL - 1, uh - 1);
      c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText("均匀分布：正态参数暂不生效（可先设好，切回即用）", (xEffL + xEffR) / 2, (topY + axisY - uh) / 2);
      return;
    }

    var cH = axisY - topY;
    var muMid = st.effLo + st.span * muMidPct(st) / 100;
    var sgMid = st.span * sgMidPct(st) / 100;
    // 圆点 y 与 σMid 线性（上 = 更尖）；E_SIGMA_MIN 对应散布 ±2 分钟下限的可视域
    var eTop = topY + 10, eBot = axisY - 26;
    var t = 1 - (sgMidPct(st) - 4) / (E_SIGMA_MAX - 4);
    var dotX = xOf(muMid);
    var dotY = eBot - (eBot - eTop) * Math.min(Math.max(t, 0), 1);
    var bellH = axisY - dotY;
    var peak = pdf(muMid, muMid, sgMid);
    function bellY(xm, sigma) { return axisY - pdf(xm, muMid, sigma) / peak * bellH; }

    // 纵轴刻度：预计每分钟签到人数（n 未知时退化为无数字网格）
    var peoplePerMin = st.n * peak;
    if (peoplePerMin > 0) {
      var dTop = peoplePerMin * cH / bellH;
      var step = 5000, cands = [0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
      for (var ci = 0; ci < cands.length; ci++) {
        if (dTop / cands[ci] <= 6) { step = cands[ci]; break; }
      }
      c.strokeStyle = colors.grid; c.lineWidth = 1;
      c.fillStyle = colors.muted; c.font = FONT_SMALL;
      c.textAlign = "right"; c.textBaseline = "middle";
      for (var gi = 1; ; gi++) {
        var dVal = gi * step;
        if (dVal > dTop + 1e-9) break;
        var gy = axisY - dVal / peoplePerMin * bellH;
        if (gy < topY - 0.5) break;
        c.beginPath(); c.moveTo(padX, gy); c.lineTo(w - padX, gy); c.stroke();
        c.fillText(String(dVal), padX - 6, gy);
      }
      c.save();
      c.translate(10, (topY + axisY) / 2);
      c.rotate(-Math.PI / 2);
      c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText("人 / 分钟", 0, 0);
      c.restore();
    }

    // 曲线、染色、底座全部钳在有效窗口内：引擎对越界尾部做反射折回（窗口外没有
    // 真实签到密度），画出斜纹区/画布外属于渲染失误
    c.save();
    c.beginPath();
    c.rect(xEffL, topY, xEffR - xEffL, axisY - topY);
    c.clip();

    // μ 竖虚线（区间两端）
    c.save();
    c.setLineDash([4, 4]); c.strokeStyle = colors.guide; c.lineWidth = 1;
    [st.muLo, st.muHi].forEach(function (p) {
      var gx = st.effLo + st.span * p / 100;
      c.beginPath(); c.moveTo(xOf(gx), topY); c.lineTo(xOf(gx), axisY); c.stroke();
    });
    c.restore();

    // 中位钟形（实线：形状与宽度真实、峰高锚定圆点）
    c.strokeStyle = colors.curve; c.lineWidth = 2; c.lineJoin = "round"; c.lineCap = "round";
    c.beginPath();
    for (var px = xEffL; px <= xEffR; px += 2) {
      var xm = st.effLo + (px - xEffL) / ppm;
      if (px === xEffL) c.moveTo(px, bellY(xm, sgMid));
      else c.lineTo(px, bellY(xm, sgMid));
    }
    c.stroke();

    // σ 放大后的实际钟形（虚线，同一锚点；n>20 才有意义）
    var f = sigmaFactor(st.n);
    if (f > 1 && st.n > 0) {
      c.save();
      c.setLineDash([6, 4]); c.strokeStyle = colors.curveSoft; c.lineWidth = 1.5;
      c.beginPath();
      for (var px2 = xEffL; px2 <= xEffR; px2 += 2) {
        var xm2 = st.effLo + (px2 - xEffL) / ppm;
        var yy = bellY(xm2, Math.min(sgMid * f, sigmaCap()));
        if (px2 === xEffL) c.moveTo(px2, yy);
        else c.lineTo(px2, yy);
      }
      c.stroke(); c.restore();
    }

    // ±1σ 核心染色：把"散布多宽"直接画进钟形里（约 68% 的账号落在这一段）
    var coreL = xOf(muMid - sgMid), coreR = xOf(muMid + sgMid);
    c.beginPath();
    c.moveTo(Math.max(coreL, xEffL), axisY);
    for (var pc = Math.max(coreL, xEffL); pc <= Math.min(coreR, xEffR); pc += 2) {
      var xm3 = st.effLo + (pc - xEffL) / ppm;
      c.lineTo(pc, bellY(xm3, sgMid));
    }
    c.lineTo(Math.min(coreR, xEffR), axisY);
    c.closePath();
    c.fillStyle = colors.bandFill; c.fill();

    // 散布底座：±1σ 投影到时间轴；两枚短须 = σ 区间两端
    var half = sgMid * ppm;
    var baseY = axisY - 10;
    c.fillStyle = colors.bandFill;
    c.beginPath();
    var bw = Math.max(4, half * 2);
    c.moveTo(dotX - bw / 2 + 4, baseY);
    c.arcTo(dotX + bw / 2, baseY, dotX + bw / 2, baseY + 8, 4);
    c.arcTo(dotX + bw / 2, baseY + 8, dotX - bw / 2, baseY + 8, 4);
    c.arcTo(dotX - bw / 2, baseY + 8, dotX - bw / 2, baseY, 4);
    c.arcTo(dotX - bw / 2, baseY, dotX + bw / 2, baseY, 4);
    c.closePath();
    c.fill();
    c.strokeStyle = colors.guide; c.lineWidth = 1;
    c.stroke();
    [st.span * st.sgLo / 100, st.span * st.sgHi / 100].forEach(function (sm2) {
      var d = sm2 * ppm;
      c.beginPath();
      c.moveTo(dotX - d, baseY - 3); c.lineTo(dotX - d, baseY + 11);
      c.moveTo(dotX + d, baseY - 3); c.lineTo(dotX + d, baseY + 11);
      c.stroke();
    });

    c.restore(); // 解除有效窗口裁剪

    // 竖准线（峰值位置）+ 峰尖圆点（手柄本体）
    c.strokeStyle = colors.hairline; c.lineWidth = 1;
    c.beginPath();
    c.moveTo(dotX, topY); c.lineTo(dotX, axisY);
    c.stroke();
    c.beginPath(); c.arc(dotX, dotY, 7.5, 0, Math.PI * 2);
    c.fillStyle = colors.curve; c.fill();
    c.lineWidth = 2; c.strokeStyle = "#fff"; c.stroke();

    // 右上角：API 原始单位 +（人数已知时）实际高峰速率
    c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textAlign = "right"; c.textBaseline = "top";
    c.fillText("μ " + st.muLo + "~" + st.muHi + "% · σ " + st.sgLo + "~" + st.sgHi + "%", w - padX - 4, topY + 2);
    if (st.n > 0) {
      var sigmaEffMin = Math.min(sgMid * f, sigmaCap());
      var rate = st.n / (sigmaEffMin * Math.sqrt(2 * Math.PI));
      c.fillText("高峰 ≈" + (rate >= 10 ? Math.round(rate) : rate.toFixed(1)) + " 人/分", w - padX - 4, topY + 18);
    }
    // 名义钟形尾部伸出窗外：提示反射语义（图中已裁去）
    if (muMid - sgMid < st.effLo || muMid + sgMid > st.effHi) {
      c.fillStyle = colors.muted; c.textAlign = "center";
      c.fillText("超出窗口的尾部按反射折回（图中裁去）", (xEffL + xEffR) / 2, topY + 2);
    }

    layout = {
      dotX: dotX, dotY: dotY, half: half, baseY: baseY, axisY: axisY, ppm: ppm,
      pctOfX: function (cx) { return (cx - xEffL) / (xEffR - xEffL) * 100; },
      sgOfY: function (cy) {
        var tt = 1 - Math.min(Math.max((eBot - cy) / (eBot - eTop), 0), 1);
        return 4 + tt * (E_SIGMA_MAX - 4);
      }
    };
  }

  /* ---------- 渲染调度：交互路径直绘；环境级变化走 rAF+兜底 ---------- */
  function renderNow() {
    draw();
    syncEditors();
    syncAria();
  }
  function requestRender() {
    if (rafPending) return;
    rafPending = true;
    var done = false;
    function run() {
      if (done) return;
      done = true; rafPending = false;
      renderNow();
    }
    requestAnimationFrame(run);
    // 隐藏页（tab 未激活/后台窗格）的 rAF 会被浏览器暂停，重绘会永远排队；
    // 计时器兜底让这类场景退化成同步执行
    setTimeout(run, 1200);
  }

  /* ---------- 可见编辑器（组件构建；隐藏 input 仍是唯一状态源） ---------- */
  function el(tag, cls) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }
  function buildEditors() {
    var wrap = el("div", "dist-viz-editors");

    function label(txt) {
      var sp = el("span", "field-label");
      sp.textContent = txt;
      return sp;
    }
    function group(children) {
      var g = el("span", "input-group");
      children.forEach(function (ch) { g.appendChild(ch); });
      return g;
    }
    function addon(txt) {
      var sp = el("span", "addon");
      sp.textContent = txt;
      return sp;
    }
    function numberInput() {
      var i = el("input", "input");
      i.type = "number"; i.min = "1"; i.step = "1";
      return i;
    }

    ed.muMid = el("input", "input");
    ed.muMid.type = "time"; ed.muMid.step = "60";
    ed.muR = numberInput();
    ed.sgLo = numberInput();
    ed.sgHi = numberInput();

    wrap.appendChild(label("峰值中心"));
    wrap.appendChild(group([ed.muMid, addon("±"), ed.muR, addon("分钟")]));
    wrap.appendChild(label("散布"));
    wrap.appendChild(group([addon("±"), ed.sgLo, addon("至 ±"), ed.sgHi, addon("分钟")]));

    // 峰值区间 = 顶点 ± 半径：钟点只改顶点（宽度不变），半径只改宽度（顶点不动）
    function onMuMid() {
      var v = ed.muMid.value;
      if (!/^\d{1,2}:\d{2}$/.test(v)) return;             // 分段打字的半截钟点不应用
      var p = v.split(":");
      var min = (parseInt(p[0], 10) || 0) * 60 + (parseInt(p[1], 10) || 0);
      applyMu(clampPct((min - st.effLo) / st.span * 100), (st.muHi - st.muLo) / 2);
      changed();
      renderNow();
    }
    function onMuR() {
      var v = parseInt(ed.muR.value, 10);
      if (isNaN(v)) return;
      var rMin = Math.min(Math.floor(st.span / 2), Math.max(1, v)); // 半径 ≤ 半窗
      applyMu(muMidPct(st), rMin / st.span * 100);
      changed();
      renderNow();
    }
    function onSg() {
      var a = parseInt(ed.sgLo.value, 10);
      var b = parseInt(ed.sgHi.value, 10);
      if (isNaN(a) || isNaN(b)) return;
      applySgBounds(a / st.span * 100, b / st.span * 100);
      changed();
      renderNow();
    }
    // 逐键实时预览：钟点凑齐 HH:MM 才应用（半截 "07:1" 会乱跳）
    ["change", "input"].forEach(function (evt) {
      ed.muMid.addEventListener(evt, onMuMid);
      ed.muR.addEventListener(evt, onMuR);
      ed.sgLo.addEventListener(evt, onSg);
      ed.sgHi.addEventListener(evt, onSg);
    });

    root.appendChild(wrap);
  }
  function setIfIdle(input, v) {
    if (document.activeElement !== input) input.value = v;
  }
  function syncEditors() {
    setIfIdle(ed.muMid, fmtT(st.effLo + st.span * muMidPct(st) / 100));
    setIfIdle(ed.muR, String(Math.max(1, Math.round(st.span * (st.muHi - st.muLo) / 200))));
    setIfIdle(ed.sgLo, String(Math.round(st.span * st.sgLo / 100)));
    setIfIdle(ed.sgHi, String(Math.round(st.span * st.sgHi / 100)));
  }
  function syncAria() {
    cv.setAttribute("aria-label",
      "正态分布峰尖拖拽画布：峰值中心 " + fmtT(st.effLo + st.span * muMidPct(st) / 100) +
      "，散布 ±" + Math.round(st.span * st.sgLo / 100) + " ~ " +
      Math.round(st.span * st.sgHi / 100) + " 分钟；方向键可微调" +
      (st.dist === "uniform" ? "（当前为均匀分布，参数暂不生效）" : ""));
  }

  /* ---------- 手势：峰尖相对抓取 + 底座端部 ---------- */
  function bindGestures() {
    cv.addEventListener("pointerdown", function (e) {
      if (readonly || st.invalid) return;
      if (!layout) return;
      var r = cv.getBoundingClientRect();
      var px = e.clientX - r.left, py = e.clientY - r.top;
      var L = layout;
      var baseDrag = py > L.axisY - 18 && py < L.axisY + 6 &&
        Math.abs(Math.abs(px - L.dotX) - L.half) <= 14;
      e.preventDefault();
      try { cv.setPointerCapture(e.pointerId); } catch (err) {}
      cv.style.cursor = baseDrag ? "ew-resize" : "grabbing";
      var grabDX = L.dotX - px, grabDY = L.dotY - py;
      function onMove(ev) {
        var cx = ev.clientX - r.left, cy = ev.clientY - r.top;
        if (st.invalid) return;
        if (baseDrag) {
          var halfMin = Math.abs(cx - L.dotX) / L.ppm;
          applyMu(muMidPct(st), halfMin / st.span * 100);
        } else {
          applyMu(L.pctOfX(cx + grabDX), (st.muHi - st.muLo) / 2);
          applySgScale(L.sgOfY(cy + grabDY) / sgMidPct(st));
        }
        changed();
        renderNow();
      }
      function onUp() {
        cv.style.cursor = "";
        cv.removeEventListener("pointermove", onMove);
        cv.removeEventListener("pointerup", onUp);
        cv.removeEventListener("pointercancel", onUp);
      }
      cv.addEventListener("pointermove", onMove);
      cv.addEventListener("pointerup", onUp);
      cv.addEventListener("pointercancel", onUp);
    });

    // 键盘：方向键 ±1（Shift ±5）；上 = 更尖（σ 变小），与"上尖下扁"同向
    cv.addEventListener("keydown", function (e) {
      if (readonly || st.invalid) return;
      var dMu = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
      var dSg = e.key === "ArrowUp" ? -1 : e.key === "ArrowDown" ? 1 : 0;
      if (!dMu && !dSg) return;
      e.preventDefault();
      var step = e.shiftKey ? 5 : 1;
      applyMu(muMidPct(st) + dMu * step, (st.muHi - st.muLo) / 2);
      if (dSg) applySgScale((sgMidPct(st) + dSg * step) / sgMidPct(st));
      changed();
      renderNow();
    });
  }

  /* ---------- 对外 ---------- */
  function mount(opts) {
    cfg = opts || {};
    root = document.querySelector("[data-dist-viz]");
    if (!root) return;
    probe = document.createElement("div");
    probe.style.cssText = "position:absolute;visibility:hidden;pointer-events:none";
    document.body.appendChild(probe);

    cv = el("canvas", "dist-viz-canvas");
    cv.style.height = CANVAS_H + "px";
    cv.tabIndex = 0;
    root.insertBefore(cv, root.firstChild);
    buildEditors();

    var legend = el("p", "dist-viz-legend");
    legend.textContent = "纵轴 = 预计每分钟签到人数（随峰形自动缩放）· 实线 = 名义钟形 · 深色核心 = ±1σ（约 68% 账号）· 轴上底座 = 散布宽度（短须 = σ 区间两端）· 虚线 = 按账号数放大后的实际钟形";
    root.appendChild(legend);

    bindGestures();
    readColors();

    if ("ResizeObserver" in window) {
      new ResizeObserver(function () { requestRender(); }).observe(cv);
    }
    window.addEventListener("resize", requestRender);
    document.addEventListener("yiban:theme", function () {
      readColors();
      requestRender();
    });
    if ("IntersectionObserver" in window) {
      // tab 面板切走时画布尺寸为 0，切回首次可见补一次绘制
      new IntersectionObserver(function (entries) {
        entries.forEach(function (en) { if (en.isIntersecting) requestRender(); });
      }).observe(cv);
    }

    readState();     // 先落状态再首绘（draw 依赖 st，漏读会 null 解引用）
    renderNow();
  }

  function refresh() {
    if (!cfg) return;
    readState();
    renderNow();
  }

  function setReadonly(ro) {
    readonly = !!ro;
    cv.classList.toggle("is-readonly", readonly);
    [ed.muMid, ed.muR, ed.sgLo, ed.sgHi].forEach(function (i) { i.disabled = readonly; });
    cv.setAttribute("aria-disabled", readonly ? "true" : "false");
  }

  YB.settingsDistViz = {
    mount: mount,
    refresh: refresh,
    setReadonly: setReadonly
  };
})();
