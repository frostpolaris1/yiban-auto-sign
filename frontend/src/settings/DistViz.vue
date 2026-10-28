<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  E_SIGMA_MAX,
  PILL_H,
  PILL_PAD_X,
  applyMu,
  applySgBounds,
  applySgScale,
  distHitKind,
  fmtT,
  muMidPct,
  pdf,
  pillRect,
  sgMidPct,
  sigmaCap,
  sigmaFactor,
} from "./model.js";

/* 正态分布调参（设置页 · 签到调度卡）——方案 E「峰尖拖拽」的忠实搬运。
   legacy 是 `components/settings-dist-viz.js`（613 行、零测试）；本组件把它的 canvas 绘制与
   手势原样搬进 Vue，数学纯函数抽进 model.js（Vitest 直测）。**状态唯一源**仍是四枚整数
   %（muLo/muHi/sgLo/sgHi），由父组件持有；本组件把可见编辑与画布手势翻译成整数 % 再
   emit 上去。

   交互：抓峰尖拖（左右=峰值时刻、上下=散布）/ 空白处按下=整体推移 / 拖时间轴底座两端=调
   散布（恒以峰尖对称）/ 画布聚焦后方向键 ±1（Shift ×5，上=更尖）/ 可见编辑器预览
   （数字字段逐键实时；峰值中心为 EP el-time-picker，提交式——Enter/失焦/面板选择才生效）。
   实时依赖：窗口与掐头去尾（含 20% 缓冲钳位，父组件算好经 ctxData 传入）、分布方式=均匀
   （钟形置灰 + 提示，编辑仍可用）、账号数（σ_eff 放大曲线与高峰速率）、主题（重取 CSS
   变量重绘）、只读（A 档权限）、直接操作门（`editGate`，卡级「编辑」按钮）。

   直接操作门（工单 4gvh，用户 2026-10-04 口径）：画布这类"直接操作控件"默认只读，父组件
   的「编辑」按钮按下后才可操作，以降低窄屏误触。门是**真状态**——只读时画布吃不到指针
   事件（CSS `pointer-events: none`）、键盘分支早退、`tabindex` 退到 -1；可见的 μ/σ 数字
   编辑器不在此门内（逐键键入不是误触形状，且"先设好、切回即用"的便利不倒退）。

   ⚠ P3 收官重设计（2026-10-04）在本组件上落地三件事：
   ① 画布按分布态降级——只有 `dist === "normal"` 渲染 210px 画布与图例；`front`（默认，
      提前铺完）与 `uniform` 都没有钟形可画（旧观感是"空矩形 + 解释不存在之物的图例"，
      是卡里最重的噪音），改由一行紧凑说明承担；峰值中心/散布编辑器保留（"先设好、切回即用"
      的便利不倒退）。`data-dist-state` 是三态锚点（front / uniform / normal）。
   ② 参数行换行不再"散布"孤行——标签与控件组包成不可拆分的 `.dist-viz-editor` 单元。
   ③ 峰尖拖拽手感精修——更大的命中区、hover/拖拽光标、悬停外扩光环 + 拖动数值药丸
      （μ 时刻与 σ 散布一眼可见）、方向键可达性在画布与可视提示行双写。
   动效纪律：新增反馈只在 canvas 内按帧绘制（无 CSS 位移/尺寸动画），不引入新的动效时长。 */

const CANVAS_H = 210;
const FONT_SMALL = '10.5px Inter, "Noto Sans SC", "Microsoft YaHei", sans-serif';

const props = defineProps<{
  modelValue: { muLo: number; muHi: number; sgLo: number; sgHi: number };
  ctxData: { effLo: number; effHi: number; span: number; frontMin: number; backMin: number; dist: string; n: number };
  readonly: boolean;
  /** 直接操作门（用户 2026-10-04 口径）：发起指针拖拽与方向键微调的**唯一**开关。
      父组件持卡级「编辑」按钮的真实状态；`readonly` 只是权限口径（A 档），两者都不满足
      时画布是只读的。 */
  editGate: boolean;
}>();
const emit = defineEmits<{
  (e: "update:modelValue", v: { muLo: number; muHi: number; sgLo: number; sgHi: number }): void;
  (e: "change"): void;
}>();

const rootEl = ref<HTMLDivElement | null>(null);
const canvasEl = ref<HTMLCanvasElement | null>(null);
const editors = ref({ muMid: "", muR: "", sgLo: "", sgHi: "" });

let colors: Record<string, string> = {};
let probe: HTMLDivElement | null = null;
let layout: {
  dotX: number; dotY: number; half: number; baseY: number; axisY: number; ppm: number;
  pctOfX: (cx: number) => number;
  sgOfY: (cy: number) => number;
} | null = null;
let rafPending = false;
let rafId = 0;
let fallbackTimer: ReturnType<typeof setTimeout> | null = null;
let cleanups: Array<() => void> = [];
// 进行中的指针手势收尾（卸载时回收 pointermove/up/cancel 监听与光标）
let endDrag: (() => void) | null = null;
// 交互反馈态：hover 命中的是峰尖还是底座端；activeDrag 为真时同时画光环与数值药丸。
let hoverKind: "none" | "peak" | "base" = "none";
let activeDrag = false;

const distState = computed(() => {
  const d = props.ctxData.dist;
  return d === "normal" ? "normal" : (d === "front" ? "front" : "uniform");
});
/* 只有正态态有钟形可画（画布 v-if=isNormal）；front / uniform 都是一行紧凑说明。 */
const isNormal = computed(() => distState.value === "normal");
/* 画布能否被直接操作：权限（非只读）**且**编辑门开着。这是真门——只读时画布连指针事件都
   收不到（CSS `pointer-events: none`），键盘分支也在此早退，不存在"看得见拖得动"的假门。 */
const direct = computed(() => !props.readonly && props.editGate);

/* 状态读取：四枚整数 % 是唯一事实源；lo>=hi 时防御性拉开（旧数据仍可画）。 */
function readState() {
  const m = props.modelValue;
  const c = props.ctxData;
  const s = {
    muLo: clampPct(m.muLo), muHi: clampPct(m.muHi), sgLo: clampPct(m.sgLo), sgHi: clampPct(m.sgHi),
    effLo: c.effLo, effHi: c.effHi, span: c.span,
    frontMin: c.frontMin || 0, backMin: c.backMin || 0,
    dist: c.dist, n: c.n || 0,
  };
  if (s.muHi <= s.muLo) s.muHi = Math.min(100, s.muLo + 1);
  if (s.sgHi <= s.sgLo) s.sgHi = Math.min(100, s.sgLo + 1);
  s.invalid = !(s.span > 0);
  return s;
}
function clampPct(v: number): number {
  return Math.min(100, Math.max(0, Math.round(v)));
}
/* 写回四枚整数 %：**一次手势步只 emit 一次合并对象**。
   Vue 的 props 要等父组件下次渲染才更新，若在一次手势里先 writeMu 再 writeSg，第二次
   展开的 `props.modelValue` 仍是拖拽前的旧值，父组件会把过期字段写回——峰尖拖拽的 μ
   （峰时）被静默还原、只有 σ 生效。故调用方先算出全部新值，这里合并成一次提交。 */
function write(next: Partial<{ muLo: number; muHi: number; sgLo: number; sgHi: number }>): void {
  emit("update:modelValue", { ...props.modelValue, ...next });
  emit("change");
}

/* ---------- 颜色：CSS 变量 → canvas 可用串（探针解析，主题跟随） ---------- */
function resolve(expr: string): string {
  if (!probe) return "#000";
  probe.style.background = "";
  probe.style.background = expr;
  const v = getComputedStyle(probe).backgroundColor;
  probe.style.background = "";
  return v;
}
function readColors(): void {
  colors.curve = resolve("rgb(var(--c-blue-600))");
  colors.curveSoft = resolve("color-mix(in srgb, rgb(var(--c-blue-600)) 65%, transparent)");
  colors.bandFill = resolve("color-mix(in srgb, rgb(var(--c-blue-500)) 22%, transparent)");
  colors.guide = resolve("color-mix(in srgb, rgb(var(--c-blue-600)) 55%, transparent)");
  colors.hairline = resolve("color-mix(in srgb, rgb(var(--c-blue-600)) 20%, transparent)");
  colors.grid = resolve("color-mix(in srgb, var(--control-border) 30%, transparent)");
  colors.axis = resolve("color-mix(in srgb, var(--control-border) 75%, transparent)");
  colors.hatch = resolve("color-mix(in srgb, var(--t-muted) 10%, transparent)");
  colors.hatchLine = resolve("color-mix(in srgb, var(--t-muted) 24%, transparent)");
  colors.muted = resolve("var(--t-muted)");
  // 拖动数值药丸：卡片底 + 主文字色 + 软边，跟随主题翻转。
  colors.pillBg = resolve("var(--bg-card)");
  colors.pillFg = resolve("var(--t-base)");
  colors.pillBorder = resolve("var(--border)");
}

function sizeCanvas(): { c: CanvasRenderingContext2D; w: number; h: number } | null {
  const cv = canvasEl.value;
  if (!cv) return null;
  const r = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(160, Math.round(r.width));
  const h = Math.max(100, Math.round(r.height));
  const pw = Math.round(w * dpr);
  const ph = Math.round(h * dpr);
  if (cv.width !== pw || cv.height !== ph) {
    cv.width = pw;
    cv.height = ph;
  }
  const c = cv.getContext("2d");
  if (!c) return null;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { c, w, h };
}

/* 峰尖旁的数值药丸：圆角底 + 主文字色。定位/夹取交给 model.js 的 pillRect（纯函数可单测），
   这里只负责画。横向夹在绘图区内、纵向避让峰尖。 */
function drawPill(
  c: CanvasRenderingContext2D, dotX: number, dotY: number, label: string,
  minX: number, maxX: number, topY: number,
): void {
  c.save();
  c.font = FONT_SMALL; c.textBaseline = "middle"; c.textAlign = "left";
  const tw = c.measureText(label).width;
  const box = pillRect(dotX, dotY, tw, minX, maxX, topY);
  c.globalAlpha = 0.96;
  c.fillStyle = colors.pillBg;
  c.strokeStyle = colors.pillBorder; c.lineWidth = 1;
  if (typeof c.roundRect === "function") {
    c.beginPath(); c.roundRect(box.x, box.y, box.w, PILL_H, 6); c.fill(); c.stroke();
  } else {
    c.fillRect(box.x, box.y, box.w, PILL_H);
  }
  c.fillStyle = colors.pillFg;
  c.fillText(label, box.x + PILL_PAD_X, box.y + PILL_H / 2);
  c.restore();
}

/* ---------- 主绘制（忠实搬运 legacy draw()） ---------- */
function draw(): void {
  const s = readState();
  const sz = sizeCanvas();
  if (!sz) return;
  const c = sz.c, w = sz.w, h = sz.h;
  const padX = 46, topY = 14, axisY = h - 24;
  const winStart = s.effLo - s.frontMin, winEnd = s.effHi + s.backMin;
  const winSpan = Math.max(1, winEnd - winStart);
  const ppm = (w - 2 * padX) / winSpan;
  const xOf = (min: number): number => padX + (min - winStart) * ppm;
  const xEffL = xOf(s.effLo), xEffR = xOf(s.effHi);

  c.clearRect(0, 0, w, h);

  c.strokeStyle = colors.axis; c.lineWidth = 1;
  c.beginPath(); c.moveTo(padX, axisY + 0.5); c.lineTo(w - padX, axisY + 0.5); c.stroke();
  c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textBaseline = "top"; c.textAlign = "center";
  const t0 = Math.ceil(winStart / 20) * 20;
  for (let t = t0; t <= winEnd; t += 20) {
    const tx = xOf(t);
    c.beginPath(); c.moveTo(tx, axisY); c.lineTo(tx, axisY + 4); c.stroke();
    c.fillText(fmtT(t), tx, axisY + 7);
  }

  if (s.invalid) {
    c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textAlign = "center"; c.textBaseline = "middle";
    c.fillText("窗口无效：请先修正签到窗口与掐头去尾", w / 2, (topY + axisY) / 2);
    return;
  }

  const hatch = (x0: number, x1: number): void => {
    if (x1 - x0 < 0.5) return;
    c.save();
    c.beginPath(); c.rect(x0, topY, x1 - x0, axisY - topY); c.clip();
    c.fillStyle = colors.hatch; c.fillRect(x0, topY, x1 - x0, axisY - topY);
    c.strokeStyle = colors.hatchLine; c.lineWidth = 1; c.beginPath();
    for (let x = x0 - (axisY - topY); x < x1; x += 6) {
      c.moveTo(x, axisY); c.lineTo(x + (axisY - topY), topY);
    }
    c.stroke(); c.restore();
  };
  hatch(xOf(winStart), xEffL);
  hatch(xEffR, xOf(winEnd));

  if (s.dist !== "normal") {
    // 非正态态（front / uniform）：画布在模板层已不渲染（v-if），此处只是防御性兜底；
    // 清掉交互态避免切回正态时残留光环/药丸。
    hoverKind = "none";
    activeDrag = false;
    return;
  }

  const cH = axisY - topY;
  const muMid = s.effLo + s.span * muMidPct(s) / 100;
  const sgMid = s.span * sgMidPct(s) / 100;
  const eTop = topY + 10, eBot = axisY - 26;
  const t = 1 - (sgMidPct(s) - 4) / (E_SIGMA_MAX - 4);
  const dotX = xOf(muMid);
  const dotY = eBot - (eBot - eTop) * Math.min(Math.max(t, 0), 1);
  const bellH = axisY - dotY;
  const peak = pdf(muMid, muMid, sgMid);
  const bellY = (xm: number, sigma: number): number => axisY - pdf(xm, muMid, sigma) / peak * bellH;

  const peoplePerMin = s.n * peak;
  if (peoplePerMin > 0) {
    const dTop = peoplePerMin * cH / bellH;
    let step = 5000;
    const cands = [0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
    for (const cand of cands) {
      if (dTop / cand <= 6) { step = cand; break; }
    }
    c.strokeStyle = colors.grid; c.lineWidth = 1;
    c.fillStyle = colors.muted; c.font = FONT_SMALL;
    c.textAlign = "right"; c.textBaseline = "middle";
    for (let gi = 1; ; gi++) {
      const dVal = gi * step;
      if (dVal > dTop + 1e-9) break;
      const gy = axisY - dVal / peoplePerMin * bellH;
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

  c.save();
  c.beginPath();
  c.rect(xEffL, topY, xEffR - xEffL, axisY - topY);
  c.clip();

  c.save();
  c.setLineDash([4, 4]); c.strokeStyle = colors.guide; c.lineWidth = 1;
  [s.muLo, s.muHi].forEach((p) => {
    const gx = s.effLo + s.span * p / 100;
    c.beginPath(); c.moveTo(xOf(gx), topY); c.lineTo(xOf(gx), axisY); c.stroke();
  });
  c.restore();

  c.strokeStyle = colors.curve; c.lineWidth = 2; c.lineJoin = "round"; c.lineCap = "round";
  c.beginPath();
  for (let px = xEffL; px <= xEffR; px += 2) {
    const xm = s.effLo + (px - xEffL) / ppm;
    if (px === xEffL) c.moveTo(px, bellY(xm, sgMid));
    else c.lineTo(px, bellY(xm, sgMid));
  }
  c.stroke();

  const f = sigmaFactor(s.n);
  if (f > 1 && s.n > 0) {
    c.save();
    c.setLineDash([6, 4]); c.strokeStyle = colors.curveSoft; c.lineWidth = 1.5;
    c.beginPath();
    for (let px2 = xEffL; px2 <= xEffR; px2 += 2) {
      const xm2 = s.effLo + (px2 - xEffL) / ppm;
      const yy = bellY(xm2, Math.min(sgMid * f, sigmaCap(s.span)));
      if (px2 === xEffL) c.moveTo(px2, yy);
      else c.lineTo(px2, yy);
    }
    c.stroke(); c.restore();
  }

  const coreL = xOf(muMid - sgMid), coreR = xOf(muMid + sgMid);
  c.beginPath();
  c.moveTo(Math.max(coreL, xEffL), axisY);
  for (let pc = Math.max(coreL, xEffL); pc <= Math.min(coreR, xEffR); pc += 2) {
    const xm3 = s.effLo + (pc - xEffL) / ppm;
    c.lineTo(pc, bellY(xm3, sgMid));
  }
  c.lineTo(Math.min(coreR, xEffR), axisY);
  c.closePath();
  c.fillStyle = colors.bandFill; c.fill();

  const half = sgMid * ppm;
  const baseY = axisY - 10;
  c.fillStyle = colors.bandFill;
  c.beginPath();
  const bw = Math.max(4, half * 2);
  c.moveTo(dotX - bw / 2 + 4, baseY);
  c.arcTo(dotX + bw / 2, baseY, dotX + bw / 2, baseY + 8, 4);
  c.arcTo(dotX + bw / 2, baseY + 8, dotX - bw / 2, baseY + 8, 4);
  c.arcTo(dotX - bw / 2, baseY + 8, dotX - bw / 2, baseY, 4);
  c.arcTo(dotX - bw / 2, baseY, dotX + bw / 2, baseY, 4);
  c.closePath();
  c.fill();
  c.strokeStyle = colors.guide; c.lineWidth = 1;
  c.stroke();
  [s.span * s.sgLo / 100, s.span * s.sgHi / 100].forEach((sm2) => {
    const d = sm2 * ppm;
    c.beginPath();
    c.moveTo(dotX - d, baseY - 3); c.lineTo(dotX - d, baseY + 11);
    c.moveTo(dotX + d, baseY - 3); c.lineTo(dotX + d, baseY + 11);
    c.stroke();
  });

  c.restore();

  c.strokeStyle = colors.hairline; c.lineWidth = 1;
  c.beginPath();
  c.moveTo(dotX, topY); c.lineTo(dotX, axisY);
  c.stroke();
  c.beginPath(); c.arc(dotX, dotY, 7.5, 0, Math.PI * 2);
  c.fillStyle = colors.curve; c.fill();
  c.lineWidth = 2; c.strokeStyle = "#fff"; c.stroke();

  // 峰尖交互反馈：悬停时只画外扩光环（只改 globalAlpha，不引入位移动画）；**拖拽时**才加
  // 数值药丸——药丸与画布右上角的 μ/σ 读数信息重叠，常显会打架，拖动时不显示又读不到联动，
  // 故只在 activeDrag 显示（悬停给"能抓"的信号，拖拽给"正在怎么变"的读数）。
  if (activeDrag || hoverKind === "peak") {
    c.save();
    c.globalAlpha = activeDrag ? 0.9 : 0.42;
    c.beginPath(); c.arc(dotX, dotY, 13, 0, Math.PI * 2);
    c.strokeStyle = colors.curve; c.lineWidth = 2; c.stroke();
    c.restore();
    if (activeDrag) {
      drawPill(c, dotX, dotY, fmtT(muMid) + "  ±" + Math.round(sgMid) + " 分钟", padX, w - padX, topY);
    }
  }

  c.fillStyle = colors.muted; c.font = FONT_SMALL; c.textAlign = "right"; c.textBaseline = "top";
  c.fillText("μ " + s.muLo + "~" + s.muHi + "% · σ " + s.sgLo + "~" + s.sgHi + "%", w - padX - 4, topY + 2);
  if (s.n > 0) {
    const sigmaEffMin = Math.min(sgMid * f, sigmaCap(s.span));
    const rate = s.n / (sigmaEffMin * Math.sqrt(2 * Math.PI));
    c.fillText("高峰 ≈" + (rate >= 10 ? Math.round(rate) : rate.toFixed(1)) + " 人/分", w - padX - 4, topY + 18);
  }
  if (muMid - sgMid < s.effLo || muMid + sgMid > s.effHi) {
    c.fillStyle = colors.muted; c.textAlign = "center";
    c.fillText("超出窗口的尾部按反射折回（图中裁去）", (xEffL + xEffR) / 2, topY + 2);
  }

  layout = {
    dotX, dotY, half, baseY, axisY, ppm,
    pctOfX: (cx: number) => (cx - xEffL) / (xEffR - xEffL) * 100,
    sgOfY: (cy: number) => {
      const tt = 1 - Math.min(Math.max((eBot - cy) / (eBot - eTop), 0), 1);
      return 4 + tt * (E_SIGMA_MAX - 4);
    },
  };
}

function syncEditors(): void {
  const s = readState();
  const cv = canvasEl.value;
  const setIfIdle = (el: HTMLInputElement | null, v: string): void => {
    if (el && document.activeElement !== el) el.value = v;
  };
  const root = rootEl.value;
  if (!root) return;
  // muMid 不再在此写入：它是 EP el-time-picker，显示值由 `:model-value="muMidText"` 受控派生。
  // 其余三枚仍是原生 number input，保留"焦点时不覆盖"的即时回写。
  setIfIdle(root.querySelector<HTMLInputElement>('[data-ed="muR"]'), String(Math.max(1, Math.round(s.span * (s.muHi - s.muLo) / 200))));
  setIfIdle(root.querySelector<HTMLInputElement>('[data-ed="sgLo"]'), String(Math.round(s.span * s.sgLo / 100)));
  setIfIdle(root.querySelector<HTMLInputElement>('[data-ed="sgHi"]'), String(Math.round(s.span * s.sgHi / 100)));
  if (cv) {
    // 可及性文案跟着门走：只读时不得继续宣称"拖动峰尖可调"——读屏用户据此操作会毫无反馈。
    // 三种只读要分开说：权限不足（A 档，按「编辑」也没用）与"门还没开"不是同一件事，
    // 混成一句会让非主管理员被指去按一枚 `disabled` 的按钮。
    let how = "；当前只读，先按上方「编辑」进入可操作态";
    if (direct.value) how = "；拖动峰尖可调峰值时刻与散布，方向键可微调";
    else if (props.readonly) how = "；当前只读（无操作权限）";
    cv.setAttribute(
      "aria-label",
      "正态分布峰尖拖拽画布：峰值中心 " + fmtT(s.effLo + s.span * muMidPct(s) / 100) +
      "，散布 ±" + Math.round(s.span * s.sgLo / 100) + " ~ " +
      Math.round(s.span * s.sgHi / 100) + " 分钟" + how +
      (s.dist === "normal" ? "" : "（当前非正态分布，参数暂不生效）"),
    );
  }
}

function renderNow(): void {
  draw();
  syncEditors();
}
function requestRender(): void {
  if (rafPending) return;
  rafPending = true;
  let done = false;
  const run = (): void => {
    if (done) return;
    done = true;
    rafPending = false;
    rafId = 0;
    if (fallbackTimer != null) { clearTimeout(fallbackTimer); fallbackTimer = null; }
    renderNow();
  };
  rafId = requestAnimationFrame(run);
  fallbackTimer = setTimeout(run, 1200);
}

/* ---------- 可见编辑器（数字字段逐键实时；峰值中心 el-time-picker 提交式） ---------- */
// 峰值中心由 EP el-time-picker 受控（`:model-value="muMidText"`），回调收到的是
// value-format="HH:mm" 的字符串（清空时为 null）。语义与旧原生 type=time 一致：
// 形状非法或空 → 不改状态。
function onMuMidInput(v: string | null | undefined): void {
  const s = readState();
  if (!(s.span > 0)) return;
  if (typeof v !== "string" || !/^\d{1,2}:\d{2}$/.test(v)) return;
  const p = v.split(":");
  const min = (parseInt(p[0], 10) || 0) * 60 + (parseInt(p[1], 10) || 0);
  write(applyMu(clampPct((min - s.effLo) / s.span * 100), (s.muHi - s.muLo) / 2));
  renderNow();
}
function onMuRInput(e: Event): void {
  const s = readState();
  if (!(s.span > 0)) return;
  const v = parseInt((e.target as HTMLInputElement).value, 10);
  if (isNaN(v)) return;
  const rMin = Math.min(Math.floor(s.span / 2), Math.max(1, v));
  write(applyMu(muMidPct(s), rMin / s.span * 100));
  renderNow();
}
function onSgInput(): void {
  const s = readState();
  if (!(s.span > 0)) return;
  const root = rootEl.value;
  if (!root) return;
  const a = parseInt(root.querySelector<HTMLInputElement>('[data-ed="sgLo"]')?.value ?? "", 10);
  const b = parseInt(root.querySelector<HTMLInputElement>('[data-ed="sgHi"]')?.value ?? "", 10);
  if (isNaN(a) || isNaN(b)) return;
  write(applySgBounds(a / s.span * 100, b / s.span * 100));
  renderNow();
}

/* ---------- 手势：峰尖相对抓取 + 底座端部 ---------- */
/* 命中判定（重设计后放大，几何在 model.js 的 distHitKind 里，Vitest 直测）：
   底座端部 ±18px / 上 20px 下 10px；峰尖 ±22px 圆。非上述区域仍按下即走峰尖拖拽
   （legacy 的"整面可拖"保留）。这里只把指针坐标翻译成 layout 参数。 */
function hitKind(px: number, py: number): "base" | "peak" | "none" {
  const L = layout;
  if (!L) return "none";
  return distHitKind(px, py, L.dotX, L.dotY, L.half, L.axisY) as "base" | "peak" | "none";
}
function onHoverMove(e: PointerEvent): void {
  if (endDrag || !direct.value) return;
  const cv = canvasEl.value;
  if (!cv) return;
  const r = cv.getBoundingClientRect();
  const k = hitKind(e.clientX - r.left, e.clientY - r.top);
  // 光标是"这里能干什么"的就地提示：底座端 = 横向缩放，其余 = 可抓（拖峰尖）。
  cv.style.cursor = k === "base" ? "ew-resize" : "";
  if (k !== hoverKind) {
    hoverKind = k;
    renderNow();
  }
}
function onHoverLeave(): void {
  const cv = canvasEl.value;
  if (cv && !endDrag) cv.style.cursor = "";
  if (hoverKind !== "none") {
    hoverKind = "none";
    if (!endDrag) renderNow();
  }
}
function onPointerDown(e: PointerEvent): void {
  const cv = canvasEl.value;
  if (!cv || !direct.value) return;
  const s = readState();
  if (s.invalid || !layout) return;
  const r = cv.getBoundingClientRect();
  const px = e.clientX - r.left, py = e.clientY - r.top;
  const L = layout;
  const baseDrag = hitKind(px, py) === "base";
  e.preventDefault();
  try { cv.setPointerCapture(e.pointerId); } catch { /* 忽略 */ }
  activeDrag = true;
  cv.style.cursor = baseDrag ? "ew-resize" : "grabbing";
  const grabDX = L.dotX - px, grabDY = L.dotY - py;
  const onMove = (ev: PointerEvent): void => {
    const cur = readState();
    if (cur.invalid) return;
    const cx = ev.clientX - r.left, cy = ev.clientY - r.top;
    if (baseDrag) {
      const halfMin = Math.abs(cx - L.dotX) / L.ppm;
      write(applyMu(muMidPct(cur), halfMin / cur.span * 100));
    } else {
      // 峰尖拖拽：横向 = 峰时 μ、纵向 = 散布 σ。两者**必须同一次 emit**（否则第二次
      // 展开的是父组件尚未更新的 props，μ 被旧值覆盖回退）。
      const nextMu = applyMu(L.pctOfX(cx + grabDX), (cur.muHi - cur.muLo) / 2);
      const nextSg = applySgScale(cur.sgLo, cur.sgHi, L.sgOfY(cy + grabDY) / sgMidPct(cur));
      write({ ...nextMu, ...nextSg });
    }
    renderNow();
  };
  const onUp = (): void => {
    activeDrag = false;
    hoverKind = "none";
    cv.style.cursor = "";
    cv.removeEventListener("pointermove", onMove);
    cv.removeEventListener("pointerup", onUp);
    cv.removeEventListener("pointercancel", onUp);
    endDrag = null;
    renderNow();
  };
  endDrag = onUp;
  cv.addEventListener("pointermove", onMove);
  cv.addEventListener("pointerup", onUp);
  cv.addEventListener("pointercancel", onUp);
  // 按下即反馈：立刻画出光环与数值药丸（"抓住了"不靠等待）。
  renderNow();
}
function onKeydown(e: KeyboardEvent): void {
  if (!direct.value) return;
  const s = readState();
  if (s.invalid) return;
  const dMu = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
  const dSg = e.key === "ArrowUp" ? -1 : e.key === "ArrowDown" ? 1 : 0;
  if (!dMu && !dSg) return;
  e.preventDefault();
  const step = e.shiftKey ? 5 : 1;
  const nextMu = applyMu(muMidPct(s) + dMu * step, (s.muHi - s.muLo) / 2);
  if (dSg) {
    const nextSg = applySgScale(s.sgLo, s.sgHi, (sgMidPct(s) + dSg * step) / sgMidPct(s));
    write({ ...nextMu, ...nextSg });
  } else {
    write(nextMu);
  }
  renderNow();
}

onMounted(() => {
  probe = document.createElement("div");
  probe.style.cssText = "position:absolute;visibility:hidden;pointer-events:none";
  document.body.appendChild(probe);
  readColors();
  // 观察**常驻的 rootEl**（不是 canvas）：canvas 是 v-if="isNormal"，默认（front / uniform）
  // 非正态态下
  // onMounted 时 canvasEl 为 null——旧写法 `ro.observe(cv)`/`io.observe(cv)` 会因 cv 为空
  // 整段跳过，且 uniform→normal 切换后也不再补挂。后果：?tab= 深链落在其它页签（画布在
  // display:none 容器里按 160×100 兜底绘制）时既无 RO 也无 IO 触发重绘，切回调度页后
  // 画布发虚、layout 过期导致拖拽命中错位，直到一次窗口 resize 才自愈。
  // rootEl 始终存在：均匀态→正态时它的尺寸变化会触发 RO 重绘，canvasEl 由 sizeCanvas 现取。
  const root = rootEl.value;
  const onResize = (): void => requestRender();
  window.addEventListener("resize", onResize);
  const onTheme = (): void => { readColors(); requestRender(); };
  document.addEventListener("yiban:theme", onTheme);
  cleanups.push(() => window.removeEventListener("resize", onResize));
  cleanups.push(() => document.removeEventListener("yiban:theme", onTheme));
  // 兜底定时器与进行中的指针手势一并回收（与 resize/theme/RO/IO 同口径）
  cleanups.push(() => {
    if (fallbackTimer != null) { clearTimeout(fallbackTimer); fallbackTimer = null; }
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  });
  cleanups.push(() => { if (endDrag) endDrag(); });
  if (root && "ResizeObserver" in window) {
    const ro = new ResizeObserver(() => requestRender());
    ro.observe(root);
    cleanups.push(() => ro.disconnect());
  }
  if (root && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) requestRender(); });
    });
    io.observe(root);
    cleanups.push(() => io.disconnect());
  }
  renderNow();
});
onUnmounted(() => {
  cleanups.forEach((fn) => fn());
  cleanups = [];
  if (probe && probe.parentNode) probe.parentNode.removeChild(probe);
  probe = null;
});

watch(
  () => [props.modelValue, props.ctxData, props.readonly, props.editGate],
  () => {
    if (props.ctxData.dist !== "normal") {
      // 画布在非正态态不渲染：清掉交互态，切回正态时不残留光环/药丸。
      hoverKind = "none";
      activeDrag = false;
    }
    requestRender();
  },
  { deep: true },
);

// 门在拖动途中关上（父组件收口）时立刻收尾：不收尾则残留 pointermove/up 监听、
// grabbing 光标与光环，画布此后"看着能拖"。与卸载路径同一收尾函数（endDrag 里一并清）。
watch(direct, (on) => {
  if (!on && endDrag) endDrag();
  requestRender();
});

const muMidText = computed(() => {
  const s = readState();
  return s.span > 0 ? fmtT(s.effLo + s.span * muMidPct(s) / 100) : "00:00";
});
</script>

<template>
  <div class="dist-viz" data-dist-viz :data-dist-state="distState" ref="rootEl">
    <!-- 画布只在正态分布下渲染：front / uniform 没有钟形可画，210px 空矩形 + 图例是纯噪音。 -->
    <canvas
      v-if="isNormal"
      ref="canvasEl"
      class="dist-viz-canvas"
      :class="{ 'is-readonly': !direct }"
      :style="{ height: CANVAS_H + 'px' }"
      :tabindex="direct ? 0 : -1"
      :aria-disabled="direct ? 'false' : 'true'"
      @pointerdown="onPointerDown"
      @pointermove="onHoverMove"
      @pointerleave="onHoverLeave"
      @keydown="onKeydown"
    />
    <!-- 非正态态降级：一行紧凑说明替代画布；峰值中心/散布编辑器保留（先设好、切回即用）。 -->
    <p v-if="distState === 'front'" class="dist-viz-flat" data-dist-flat>
      当前为「提前铺完」：账号按安全速率铺进签到窗口前段，尾部留作重试与兜底。以下峰值中心与散布在切换回「正态分布」后生效。
    </p>
    <p v-else-if="distState === 'uniform'" class="dist-viz-flat" data-dist-flat>
      当前为均匀分布：账号在签到窗口内均匀铺开，无需调参。以下峰值中心与散布在切换回「正态分布」后生效。
    </p>
    <div class="dist-viz-editors">
      <!-- 每个「标签 + 控件组」是一个不可拆分的编辑器单元：换行只在单元之间发生，
           标签永远不会与它的控件分家（旧布局把它们平铺，窄屏折行后「散布」标签孤行）。 -->
      <span class="dist-viz-editor">
        <span class="field-label">峰值中心</span>
        <span class="input-group">
          <!-- 时间输入一律走 EP el-time-picker：原生 <input type="time"> 的滚轮选择器由 UA
               渲染、样式不可控（用户既定禁令）；与同页 ScheduleCard 的签到窗口同一控件形态。
               data-ed="muMid" 保留为两栈共通的 e2e 锚点（落在包裹层，e2e 取内层 input 的值）。 -->
          <span class="dist-mu-time" data-ed="muMid">
            <el-time-picker
              :model-value="muMidText"
              format="HH:mm"
              value-format="HH:mm"
              :disabled="readonly"
              aria-label="峰值中心"
              style="width: 96px"
              @update:model-value="onMuMidInput"
            />
          </span>
          <span class="addon">±</span>
          <input class="input" type="number" min="1" step="1" data-ed="muR" :disabled="readonly" @change="onMuRInput" @input="onMuRInput" />
          <span class="addon">分钟</span>
        </span>
      </span>
      <span class="dist-viz-editor">
        <span class="field-label">散布</span>
        <span class="input-group">
          <span class="addon">±</span>
          <input class="input" type="number" min="1" step="1" data-ed="sgLo" :disabled="readonly" @change="onSgInput" @input="onSgInput" />
          <span class="addon">至 ±</span>
          <input class="input" type="number" min="1" step="1" data-ed="sgHi" :disabled="readonly" @change="onSgInput" @input="onSgInput" />
          <span class="addon">分钟</span>
        </span>
      </span>
    </div>
    <p v-if="isNormal" class="dist-viz-legend">纵轴 = 预计每分钟签到人数 · 实线 = 名义钟形 · 深色核心 = ±1σ（约 68% 账号）· 虚线 = 按账号数放大后的实际钟形 · 轴上底座 = 散布宽度</p>
    <!-- 操作提示只在门开着时出现：只读态宣称"拖峰尖"是空头承诺（按钮自证，不加解释文案）。 -->
    <p v-if="isNormal && direct" class="dist-viz-hint" data-dist-hint>拖峰尖：左右改峰值时刻、上下改散布；画布聚焦后方向键微调（Shift ×5）。</p>
  </div>
</template>
