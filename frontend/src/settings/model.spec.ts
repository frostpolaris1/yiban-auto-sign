import { describe, expect, it } from "vitest";
import {
  PILL_H,
  PILL_PAD_X,
  adminToPlaceholder,
  applyMu,
  applySgBounds,
  applySgScale,
  annPublishState,
  canDo,
  clean,
  clampGap,
  collectSmtps,
  distHitKind,
  distWarnText,
  driftPlaceholder,
  edgeMaxMin,
  edgeVal,
  envAt,
  healthBody,
  healthSnapshot,
  mailBody,
  mailStatusText,
  newSmtpId,
  norm,
  notifyBody,
  notifySnapshot,
  notifyStatusText,
  pauseHintText,
  pdf,
  pillRect,
  quotaPart,
  resolvePaintValue,
  sanitizeAnnouncement,
  scheduleBody,
  scheduleFormSnapshot,
  scheduleSnapshot,
  scheduleWarnText,
  sentinelText,
  sigmaFactor,
  smtpRowsFrom,
  switchesBody,
  windowSec,
} from "./model.js";

/* 设置页口径层（纯函数）单测。Math-heavy 的正态部分与"未知枚举不静默换值""夹取而非拒绝"
   等口径在此直测；页面行为层由 e2e（logs.spec 的设置段）覆盖。 */

describe("签到调度口径", () => {
  it("edgeVal 0.5 分钟对齐后转秒，越界夹到 0~5 分钟", () => {
    expect(edgeVal("1")).toBe(60);
    expect(edgeVal("1.3")).toBe(90); // 1.3 → 1.5 分钟
    expect(edgeVal("9")).toBe(300);
    expect(edgeVal("-2")).toBe(0);
    expect(edgeVal("abc")).toBe(0);
  });

  it("edgeMaxMin 与后端 edge_cap_sec/60 同式（含 0 fail-closed）", () => {
    // 窗口 80 分钟 = 4800s → 20% = 960s 但封顶 300s → 5 分钟
    expect(edgeMaxMin(4800)).toBe(5);
    // 窗口 10 分钟 = 600s → 20% = 120s，30s 粒度向下 → 120 → 2 分钟
    expect(edgeMaxMin(600)).toBe(2);
    // 窗口 0（倒置/未知）→ 0，不退回最大量程
    expect(edgeMaxMin(0)).toBe(0);
    expect(edgeMaxMin(-1)).toBe(0);
  });

  it("clampGap 夹到 0~3600（与后端静默钳位同显示值）", () => {
    expect(clampGap("11")).toBe(11);
    expect(clampGap("9999")).toBe(3600);
    expect(clampGap("-3")).toBe(0);
    expect(clampGap("x")).toBe(0);
  });

  it("windowSec 只在结束晚于开始时为正", () => {
    expect(windowSec("06:30", "07:50")).toBe(4800);
    expect(windowSec("07:50", "06:30")).toBe(0);
    expect(windowSec("06:30", "06:30")).toBe(0);
  });

  it("scheduleBody 只提交相对快照变化的字段（部分更新）", () => {
    const snap = scheduleSnapshot({ sign_order: "sequence", sign_dist: "uniform", gap_max: 10 });
    const form = {
      order: "random", dist: "uniform", edgeFront: 1, edgeBack: 1, gap: 10,
      pref: false, sat: false, sun: false, windowStart: "06:30", windowEnd: "07:50",
      muMin: 40, muMax: 60, sigmaMin: 15, sigmaMax: 25,
    };
    const body = scheduleBody(snap, form) as Record<string, unknown>;
    expect(body.sign_order).toBe("random");
    expect(Object.keys(body)).toEqual(["sign_order"]);
  });

  it("scheduleFormSnapshot 与回填口径一致（缺字段用默认）", () => {
    const s = scheduleFormSnapshot({
      order: "", dist: "", edgeFront: "", edgeBack: "", gap: "", pref: 0, sat: 0, sun: 0,
      windowStart: "06:30", windowEnd: "07:50", muMin: 40, muMax: 60, sigmaMin: 15, sigmaMax: 25,
    });
    expect(s.order).toBe("sequence");
    expect(s.dist).toBe("uniform");
    expect(s.edgeFront).toBe(0);
    expect(s.window).toBe("06:30 ~ 07:50");
  });

  it("scheduleWarnText：窗口异常提示 + 缓冲超 20% + 容量不足三条各自可现", () => {
    const fallback = scheduleWarnText({
      fallbackText: "窗口配置异常，已按 06:30~07:50 运行", winSec: 4800, maxMin: 5,
      frontSec: 60, backSec: 60, gap: 10, n: 2,
    });
    expect(fallback).toContain("窗口配置异常");
    const over = scheduleWarnText({ winSec: 600, maxMin: 2, frontSec: 180, backSec: 0, gap: 0, n: 1 });
    expect(over).toContain("超过窗口的 20%");
    const tight = scheduleWarnText({ winSec: 300, maxMin: 1, frontSec: 60, backSec: 60, gap: 60, n: 5 });
    expect(tight).toContain("部分账号可能签不上");
    expect(scheduleWarnText({ winSec: 4800, maxMin: 5, frontSec: 60, backSec: 60, gap: 0, n: 1 })).toBe("");
  });

  it("distWarnText：下限不小于上限才提示（服务端回退默认）", () => {
    expect(distWarnText(60, 40, "mu")).toContain("默认 40~60");
    expect(distWarnText(25, 15, "sigma")).toContain("默认 15~25");
    expect(distWarnText(40, 60, "mu")).toBe("");
  });
});

describe("下拉未知枚举不静默换值", () => {
  it("resolvePaintValue 未知值原样返回（不换成首项）", () => {
    const opts = [{ v: "sequence", t: "顺序" }, { v: "random", t: "随机" }];
    const unknown = resolvePaintValue(opts, "weird_mode");
    expect(unknown.value).toBe("weird_mode");
    expect(unknown.hit).toBeNull();
    expect(resolvePaintValue(opts, "random").hit).toBe(opts[1]);
  });
});

describe("时间字段 norm", () => {
  it("越界与形状不符回退，合法值原样", () => {
    expect(norm("25:00", "06:30")).toBe("06:30");
    expect(norm("24:00", "06:30")).toBe("06:30");
    expect(norm("23:60", "06:30")).toBe("06:30");
    expect(norm("23:59", "x")).toBe("23:59");
    expect(norm("6:30", "x")).toBe("x");
    expect(norm("", "x")).toBe("x");
  });
});

describe("正态分布数学", () => {
  it("pdf 峰值在 mu、随 sigma 变宽变矮", () => {
    expect(pdf(5, 5, 2)).toBeGreaterThan(pdf(7, 5, 2));
    expect(pdf(5, 5, 1)).toBeGreaterThan(pdf(5, 5, 3));
  });

  it("sigmaFactor：≤20 不放大、>20 递增", () => {
    expect(sigmaFactor(1)).toBe(1);
    expect(sigmaFactor(20)).toBe(1);
    expect(sigmaFactor(40)).toBeCloseTo(2, 5);
  });

  it("envAt 包络非负且随区间变化", () => {
    const s = { effLo: 390, span: 480, muLo: 40, muHi: 60, sgLo: 15, sgHi: 25 };
    expect(envAt(500, s)).toBeGreaterThan(0);
    expect(envAt(1000, s)).toBeGreaterThanOrEqual(0);
  });

  it("applyMu 保持区间不出窗、宽度为偶数", () => {
    const a = applyMu(50, 30);
    expect(a.muLo).toBeGreaterThanOrEqual(0);
    expect(a.muHi).toBeLessThanOrEqual(100);
    expect((a.muHi - a.muLo) % 2).toBe(0);
    const edge = applyMu(2, 50);
    expect(edge.muLo).toBe(0);
    expect(edge.muHi).toBeLessThanOrEqual(100);
  });

  it("applySgScale 保持 hi > lo ≥ 1 且钳在 [1,100]（P3 收官修掉单边落 0）", () => {
    const grown = applySgScale(15, 25, 4);
    expect(grown.sgHi).toBeLessThanOrEqual(100);
    expect(grown.sgLo).toBeGreaterThanOrEqual(1);
    expect(grown.sgHi).toBeGreaterThan(grown.sgLo);
    // 极小缩放：legacy 的 `Math.min(nLo, nHi-1)` 会落到 0（σ 下限 0 = 散布为 0，与
    // 文档承诺的 [1,100] 相悖，且 pdf() 会把 sigma 兜底成 0.5，画形与读数不一致）。
    // P3 收官改为下限恒 ≥1 —— 这是**有意偏差**，已写进 docs/refactor/29 §15j 的偏差清单。
    const shrunk = applySgScale(2, 3, 0.1);
    expect(shrunk.sgLo).toBeGreaterThanOrEqual(1);
    expect(shrunk.sgHi).toBeGreaterThan(shrunk.sgLo);
    // 上界收缩：hi 撞 100 时整体下压，lo 仍 ≥1
    const capped = applySgScale(40, 60, 10);
    expect(capped.sgHi).toBe(100);
    expect(capped.sgLo).toBeGreaterThanOrEqual(1);
    expect(capped.sgHi).toBeGreaterThan(capped.sgLo);
  });

  it("applySgBounds 保证 hi > lo ≥ 1", () => {
    expect(applySgBounds(0, 0)).toEqual({ sgLo: 1, sgHi: 2 });
    expect(applySgBounds(30, 10)).toEqual({ sgLo: 30, sgHi: 31 });
  });
});

describe("峰尖手势几何（distHitKind / pillRect，P3 收官抽出）", () => {
  const DOT_X = 400, DOT_Y = 100, HALF = 60, AXIS_Y = 186;

  it("distHitKind：峰尖 ±22px 圆内为 peak，圆外为 none", () => {
    expect(distHitKind(DOT_X, DOT_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("peak");
    expect(distHitKind(DOT_X + 21, DOT_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("peak");
    expect(distHitKind(DOT_X + 23, DOT_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("none");
    expect(distHitKind(DOT_X, DOT_Y + 23, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("none");
  });

  it("distHitKind：底座端部命中带 ±18px / 上 20px 下 10px，越界即离带", () => {
    expect(distHitKind(DOT_X + HALF, AXIS_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("base");
    expect(distHitKind(DOT_X + HALF + 17, AXIS_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("base");
    expect(distHitKind(DOT_X + HALF + 19, AXIS_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("none");
    expect(distHitKind(DOT_X + HALF, AXIS_Y - 19, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("base");
    expect(distHitKind(DOT_X + HALF, AXIS_Y - 21, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("none");
    expect(distHitKind(DOT_X + HALF, AXIS_Y + 9, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("base");
    expect(distHitKind(DOT_X + HALF, AXIS_Y + 11, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("none");
    // 左侧端点对称成立
    expect(distHitKind(DOT_X - HALF, AXIS_Y, DOT_X, DOT_Y, HALF, AXIS_Y)).toBe("base");
  });

  it("distHitKind：底座优先于峰尖（真正 base∧peak 重叠点仍判 base）", () => {
    // 构造一个**同时**落在底座命中带与峰尖 ±22 圆内的坐标：峰尖距轴 10px、半宽 10px，
    // 取端点右偏 10px、与峰尖同高。该点到峰尖圆心仅 10px（<22，在圆内），横向偏离端点
    // 0px（≤18）、纵向在轴上方 10px（<20）——两判定同时为真，正是优先级分支的输入。
    // 旧用例的几何是「峰尖距轴 86px」，那个点根本不在峰尖圆内，并未真正覆盖优先级。
    const dotY = AXIS_Y - 10;
    const px = DOT_X + 10;
    const py = AXIS_Y - 10;
    const half = 10;
    const inPeak = (px - DOT_X) * (px - DOT_X) + (py - dotY) * (py - dotY) <= 22 * 22;
    const inBase = py > AXIS_Y - 20 && py < AXIS_Y + 10 &&
      Math.abs(Math.abs(px - DOT_X) - half) <= 18;
    expect(inPeak && inBase).toBe(true); // 前提：确为重叠点（否则本用例形同虚设）
    expect(distHitKind(px, py, DOT_X, dotY, half, AXIS_Y)).toBe("base");
  });

  it("pillRect：水平夹进绘图区、宽度超出时贴左缘", () => {
    const a = pillRect(400, 100, 80, 46, 900, 14);
    expect(a.w).toBe(80 + PILL_PAD_X * 2);
    expect(a.h).toBe(PILL_H);
    expect(a.x).toBe(400 - a.w / 2);
    expect(a.y).toBe(100 - 14 - PILL_H);
    expect(pillRect(50, 100, 80, 46, 900, 14).x).toBe(46);
    expect(pillRect(892, 100, 80, 46, 900, 14).x).toBe(900 - a.w);
    // 药丸比整段绘图区还宽 → 贴左缘（不出现反向/负向夹取）
    expect(pillRect(400, 100, 900, 46, 900, 14).x).toBe(46);
  });

  it("pillRect：峰尖贴顶（σ 很小、峰很高）时不遮峰尖、改放峰尖下方", () => {
    const topY = 14;
    const a = pillRect(400, 20, 80, 46, 900, topY); // 上方放不下
    expect(a.y).toBe(20 + 14);
    expect(a.y).toBeGreaterThan(20); // 不遮峰尖
    // 上下都挤（极矮绘图区）→ 顶到绘图区上缘兜底
    const b = pillRect(400, 5, 80, 46, 900, topY);
    expect(b.y).toBeGreaterThanOrEqual(topY + 1);
  });
});

describe("公告", () => {
  it("sanitizeAnnouncement 把换行族换成空格（提交前拦截）", () => {
    expect(sanitizeAnnouncement("a\nb")).toBe("a b");
    expect(sanitizeAnnouncement("a\r\nb")).toBe("a b");
    expect(sanitizeAnnouncement("a\u2028b")).toBe("a b");
    expect(sanitizeAnnouncement("正常一行")).toBe("正常一行");
  });

  it("annPublishState 三态", () => {
    expect(annPublishState("草稿", "")).toEqual({ offline: false, noop: false });
    expect(annPublishState("", "线上")).toEqual({ offline: true, noop: false });
    expect(annPublishState("  ", "")).toEqual({ offline: false, noop: true });
  });
});

describe("消息推送口径", () => {
  it("quotaPart：已用/上限（剩）；上限 0 与 null 余额都读作不限", () => {
    expect(quotaPart("非紧急", 5, 3)).toBe("非紧急 已用 2/5（剩 3）");
    expect(quotaPart("非紧急", 0, null)).toBe("非紧急 不限");
    expect(quotaPart("紧急", 5, null)).toBe("紧急 不限");
  });

  it("notifyStatusText：quota_visible=false 不得读成「不限」", () => {
    const hidden = notifyStatusText({ enabled: false, configured: false, quota_visible: false, daily_max: 5, daily_remaining: null });
    expect(hidden).toContain("仅主管理员可见");
    const visible = notifyStatusText({
      enabled: true, type: "serverchan", secret_masked: "SCT1***", configured: true,
      quota_visible: true, daily_max: 5, daily_remaining: 3, urgent_daily_max: 3, urgent_daily_remaining: 2,
    });
    expect(visible).toContain("非紧急 已用 2/5（剩 3）");
    expect(visible).toContain("未必等于已送达");
  });

  it("notifyBody 只提交变化字段、密钥留空不提交", () => {
    const snap = notifySnapshot({ type: "", cooldown: 60, urgent_only: false, daily_max: 5, urgent_daily_max: 3 });
    const body = notifyBody(snap, { type: "serverchan", secret: "", cooldown: "60", dailyMax: "5", urgentMax: "3", urgentOnly: false });
    expect(body).toEqual({ type: "serverchan" });
    const withSecret = notifyBody(snap, { type: "serverchan", secret: " SCT123 ", cooldown: "60", dailyMax: "5", urgentMax: "3", urgentOnly: false });
    expect(withSecret.secret).toBe("SCT123");
  });
});

describe("邮件 / SMTP", () => {
  it("clean 把打码串/占位串当空处理", () => {
    expect(clean("a***@x.com")).toBe("");
    expect(clean("<未配置>")).toBe("");
    expect(clean("  real@x.com ")).toBe("real@x.com");
  });

  it("newSmtpId 形状与后端校验同口径", () => {
    const id = newSmtpId();
    expect(/^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/.test(id)).toBe(true);
    expect(id).not.toBe(newSmtpId());
  });

  it("smtpRowsFrom 不把脱敏值回填成可编辑 value", () => {
    const rows = smtpRowsFrom([{ id: "smtp-aaaa00000001", host: "a.io", port: 465, user: "a***@io", has_pass: true }]);
    expect(rows[0].user).toBe("");
    expect(rows[0].pass).toBe("");
    expect(rows[0].user0).toBe("a***@io");
  });

  it("collectSmtps 的 id 随行不随位（删中间行后剩第1、第3条）", () => {
    const rows = smtpRowsFrom([
      { id: "smtp-aaaa00000001", host: "a.io", port: 465, user: "a***@io", has_pass: true },
      { id: "smtp-bbbb00000002", host: "b.io", port: 587, user: "b***@io", has_pass: true },
      { id: "smtp-cccc00000003", host: "c.io", port: 465, user: "c***@io", has_pass: false },
    ]);
    rows.splice(1, 1);
    expect(collectSmtps(rows).map((e) => e.id)).toEqual(["smtp-aaaa00000001", "smtp-cccc00000003"]);
  });

  it("driftPlaceholder：改 host 后授权码占位改口、改回恢复", () => {
    const row = { host: "a.io", port: 465, user0: "a***@io", has_pass: true, pass: "" };
    expect(driftPlaceholder(row, "a.io", 465).pass).toBe("已配置，留空沿用");
    expect(driftPlaceholder(row, "evil-new.io", 465).pass).toContain("服务器已更换");
    expect(driftPlaceholder(row, "a.io", 465).pass).toBe("已配置，留空沿用");
  });

  it("mailStatusText：发信清单为空时必须说破", () => {
    expect(mailStatusText({ enabled: true, user: "u", admin_to: "a***@x", smtps: [] }))
      .toContain("无发信 SMTP，告警邮件一封都发不出去");
  });

  it("哨兵 <未配置> 不上屏：状态行与收件人 placeholder 都归一（A9）", () => {
    // sentinelText 只剥前导 "<"；打码串是要展示的，不碰
    expect(sentinelText("<未配置>")).toBe("");
    expect(sentinelText("a***@x")).toBe("a***@x");
    // 状态行：admin_to / user 为哨兵时写「未配置」，而不是把 <未配置> 当文案
    const st = mailStatusText({ enabled: true, user: "<未配置>", admin_to: "<未配置>", smtps: [{ host: "a.io" }] });
    expect(st).toContain("告警收件 未配置");
    expect(st).toContain("发件 未配置");
    expect(st).not.toContain("<");
    // placeholder：已配置给打码真值，未配置给可读引导
    expect(adminToPlaceholder({ admin_to: "a***@x.com" })).toBe("a***@x.com");
    expect(adminToPlaceholder({ admin_to: "<未配置>" })).toBe("尚未配置，填写管理员邮箱");
    expect(adminToPlaceholder({})).toBe("尚未配置，填写管理员邮箱");
  });

  it("driftPlaceholder：SMTP 发件账号的哨兵也不上屏", () => {
    const row = { host: "a.io", port: 465, user0: "", has_pass: false, pass: "" };
    expect(driftPlaceholder(row, "a.io", 465).user).toBe("留空沿用");
  });

  it("mailBody 只在 tableDirty 时提交 smtps", () => {
    const snap = { enabled: false, hasTo: true };
    const form = { enabled: true, adminTo: "", smtps: [] };
    expect(mailBody(snap, form, false)).toEqual({ enabled: true });
    expect(mailBody(snap, form, true)).toEqual({ enabled: true, smtps: [] });
  });
});

describe("健康 / 系统开关", () => {
  it("healthBody 只提交变化字段", () => {
    const snap = healthSnapshot({ account_verify: 1, probe_enable: 0, probe_time: "20:00", probe_interval: "1" });
    const body = healthBody(snap, { verify: true, probe: true, time: "20:00", interval: "1" });
    expect(body).toEqual({ probe_enable: 1 });
  });

  it("canDo：急停任意管理员、恢复与注册仅主管理员", () => {
    expect(canDo("global_pause", true, false)).toBe(true);
    expect(canDo("global_pause", false, false)).toBe(false);
    expect(canDo("global_pause", false, true)).toBe(true);
    expect(canDo("registration_pause", true, false)).toBe(false);
    expect(canDo("registration_pause", true, true)).toBe(true);
  });

  it("pauseHintText 按状态拼句", () => {
    expect(pauseHintText({ globalPause: true, regPause: true })).toContain("签到当前处于暂停状态");
    expect(pauseHintText({ globalPause: false, regPause: false })).toBe("");
  });

  it("switchesBody 只提交被改的那一个字段", () => {
    expect(switchesBody("global_pause", true)).toEqual({ global_pause: 1 });
    expect(switchesBody("registration_pause", false)).toEqual({ registration_pause: 0 });
  });
});
