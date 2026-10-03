import { describe, expect, it } from "vitest";
import {
  DEFAULT_CAL_NOTE,
  SIGN_EVENTS_PATH,
  acceptAccounts,
  accountsKpiView,
  announcementView,
  calendarView,
  capacityRows,
  capacityText,
  clockView,
  distView,
  emptyState,
  noResultText,
  normalizeDaily,
  pauseView,
  pendingKpiView,
  pingView,
  rateKpiView,
  rateOf,
  slotsView,
  statusKind,
  statusLabel,
  trendView,
  usersKpiView,
} from "./model.js";

/* 数据看板口径单测。
   ⚠ 最核心的四段（`statusKind` / `normalizeDaily` / `acceptAccounts` / `rateOf`）另有
   `tests/test_dashboard_stats_caliber_js.py` 把 model.js 里的真函数抽到 Node 真跑——
   本文件补充边界用例与视图层（KPI / 图表 / 日历 / 运行状态）的口径。 */

const DAY = "2026-09-20";
const DAY2 = "2026-09-21";

const ROWS = [
  { day: DAY, status: "user_cancelled", cnt: 1, row_cnt: 2 },
  { day: DAY, status: "success", cnt: 1, row_cnt: 1 },
  { day: DAY2, status: "success", cnt: 1, row_cnt: 1 },
];

const ACCOUNTS = {
  total: 2,
  by_status: { user_cancelled: 1, success: 2 },
  by_day: [
    { day: DAY, status: "user_cancelled", accounts: 1 },
    { day: DAY, status: "success", accounts: 1 },
    { day: DAY2, status: "success", accounts: 1 },
  ],
};

function stateWith(over: Record<string, unknown> = {}) {
  return Object.assign(emptyState(), over);
}

describe("状态词表", () => {
  it("穷举分类：成功 / 失败 / 已知跳过 / 未知（未知码不落 skip）", () => {
    expect(statusKind("success")).toBe("success");
    expect(statusKind("already")).toBe("success");
    expect(statusKind("failed")).toBe("fail");
    expect(statusKind("skipped_window")).toBe("skip");
    expect(statusKind("")).toBe("unknown");
    expect(statusKind("status_from_2035")).toBe("unknown");
  });

  it("短名回落原文，不静默吞掉未知码", () => {
    expect(statusLabel("success")).toBe("成功");
    expect(statusLabel("status_from_2035")).toBe("status_from_2035");
    expect(statusLabel("")).toBe("未知");
  });
});

describe("事件口径 normalizeDaily", () => {
  it("按 row_cnt 打桶、按天聚合；不读 cnt（改成 999 结果不变）", () => {
    const out = normalizeDaily(ROWS);
    expect(out.map[DAY]).toEqual({ success: 1, fail: 0, skip: 2, unknown: 0, total: 3 });
    expect(out.map[DAY2].total).toBe(1);
    expect(out.days).toEqual([DAY, DAY2]);

    const mutated = ROWS.map((r) => ({ ...r, cnt: 999 }));
    expect(normalizeDaily(mutated).map).toEqual(out.map);
  });

  it("未知状态码单列成桶，与跳过分开", () => {
    const out = normalizeDaily([{ day: DAY, status: "brand_new", row_cnt: 4 }]);
    expect(out.map[DAY].unknown).toBe(4);
    expect(out.map[DAY].skip).toBe(0);
    expect(out.map[DAY].total).toBe(4);
  });

  it("空输入 / 缺 day 的行不产生桶", () => {
    expect(normalizeDaily([]).days).toEqual([]);
    expect(normalizeDaily([{ status: "success", row_cnt: 1 }]).days).toEqual([]);
  });
});

describe("账号口径 acceptAccounts", () => {
  it("total 与 by_status 原样透传（桶间可有交集），dayFinal 按日终态互斥", () => {
    const acc = acceptAccounts(ACCOUNTS);
    expect(acc.total).toBe(2);
    expect(acc.byStatus).toEqual(ACCOUNTS.by_status);
    expect(acc.dayFinal[DAY]).toEqual({ success: 1, fail: 0, skip: 1, unknown: 0, total: 2 });
    expect(acc.dayFinal[DAY2].success).toBe(1);
  });

  it("桶合计可大于总数（交集），不得相加冒充总数", () => {
    const acc = acceptAccounts(ACCOUNTS);
    const sliceSum = Object.values(acc.byStatus).reduce((n, v) => n + Number(v), 0);
    expect(sliceSum).toBeGreaterThan(acc.total);
  });

  it("缺字段时给出安全空值", () => {
    expect(acceptAccounts(undefined)).toEqual({ total: 0, byStatus: {}, dayFinal: {} });
  });
});

describe("成功率 rateOf", () => {
  it("分母只含 成功+失败：跳过与未知都不进", () => {
    expect(rateOf({ success: 8, fail: 2, skip: 5, unknown: 1 })).toBe(80);
    expect(rateOf({ success: 0, fail: 0, skip: 9, unknown: 3 })).toBeNull();
    expect(rateOf(null)).toBeNull();
  });
});

describe("今日成功率 KPI rateKpiView", () => {
  const now = new Date("2026-09-21T10:00:00");

  it("数据未回来时保持骨架（null）；已失败时给错误行且不被「无结果」盖掉", () => {
    expect(rateKpiView(stateWith({ signLoaded: false }), now)).toBeNull();
    const failed = rateKpiView(stateWith({ signLoaded: false, signFailed: true }), now);
    expect(failed.failed).toBe(true);
    expect(failed.value).toBe("—");
    expect(failed.sub).toBe("签到事件加载失败");
  });

  it("今日无了结尝试 → 空态文案，非灰字数字", () => {
    const v = rateKpiView(stateWith({ signLoaded: true, dailyMap: {} }), now);
    expect(v.empty).toBe(true);
    expect(v.value).toBe("—");
    expect(v.sub).toBe("今日暂无签到结果");
    expect(v.subCls).toBe("dash-muted");
  });

  it("有昨日对比时给价值、百分比与较昨日药丸", () => {
    const s = stateWith({
      signLoaded: true,
      dailyMap: {
        "2026-09-21": { success: 8, fail: 2, skip: 5, unknown: 0, total: 15 },
        "2026-09-20": { success: 1, fail: 3, skip: 0, unknown: 0, total: 4 },
      },
    });
    const v = rateKpiView(s, now);
    expect(v.value).toBe("80.0");
    expect(v.sup).toBe("%");
    expect(v.pill.cls).toBe("up");
    expect(v.pill.text).toBe("较昨日 +55%");
    expect(v.sub).toContain("跳过 5");
    expect(v.valueTitle).toContain("按事件");
  });

  it("无昨日数据 → 「无昨日对比」flat 药丸", () => {
    const s = stateWith({
      signLoaded: true,
      dailyMap: { "2026-09-21": { success: 1, fail: 0, skip: 0, unknown: 0, total: 1 } },
    });
    const v = rateKpiView(s, now);
    expect(v.pill.text).toBe("无昨日对比");
    expect(v.pill.cls).toBe("flat");
  });

  it("周末停签关闭时无结果文案改为「今日不签到」", () => {
    const sat = new Date("2026-09-19T10:00:00"); // 周六
    const s = stateWith({ weekendKnown: true, satSign: false });
    expect(noResultText(s, sat)).toBe("今日不签到（周六签到已关闭）");
    expect(noResultText(stateWith({ weekendKnown: true, satSign: true }), sat)).toBe("今日暂无签到结果");
  });
});

describe("容量 KPI", () => {
  const d = {
    capacity: {
      accounts: 2, accounts_max: 200,
      accounts_breakdown: { normal: 2, user_paused: 0, cred_paused: 0 },
      accounts_audit: 3,
      users: 1, users_max: 500,
    },
    capacity_estimate: { accounts_cap: 360, current_accounts: 2, potential_load: "—" },
  };

  it("账号卡消费 accounts_audit：副文案三分类不进审核数，审核数进 title 口径", () => {
    const v = accountsKpiView(d.capacity);
    expect(v.value).toBe("2");
    expect(v.sup).toBe("/200");
    expect(v.sub).toBe("正常 2 · 用户暂停 0 · 账密暂停 0");
    expect(v.subTitle).toContain("未通过审核");
    expect(v.pill.text).toBe("1%");
  });

  it("审计数为 0 时不提审核", () => {
    const cap = { ...d.capacity, accounts_audit: 0 };
    expect(accountsKpiView(cap).subTitle).toBe("均不含已删除账号");
  });

  it("上限 0 = 不限：pill 显示「未设上限」", () => {
    const v = usersKpiView({ users: 5, users_max: 0 });
    expect(v.pill.text).toBe("未设上限");
    expect(v.sub).toBe("未设上限");
  });

  it("失败时给出错误行且 pill 清空", () => {
    const v = accountsKpiView(null);
    expect(v.failed).toBe(true);
    expect(v.value).toBe("—");
    expect(v.pill).toBeNull();
  });

  it("容量行与估算行文案", () => {
    const rows = capacityRows(d);
    expect(rows.accounts).toContain("2 / 200");
    expect(rows.accounts).toContain("未通过审核 3 个未计入");
    expect(rows.estimate).toBe("容量 360 · 当前 2 · 潜在 —");
    expect(capacityRows(null)).toBeNull();
  });

  it("capacityText 无上限写作「/ 不限」", () => {
    expect(capacityText(3, 0, null, 0)).toBe("3 / 不限");
  });
});

describe("待处理账号 KPI", () => {
  it("待审核 + 已拒绝，deleted 行不计，有则告警强调", () => {
    const d = { accounts: [
      { status: "pending" }, { status: "rejected" }, { status: "active" }, { status: "pending", deleted: true },
    ] };
    const v = pendingKpiView(d);
    expect(v.value).toBe("2");
    expect(v.sub).toBe("待审核 1 · 已拒绝 1");
    expect(v.alert).toBe(true);
    expect(pendingKpiView({ accounts: [] }).alert).toBe(false);
    expect(pendingKpiView(null).failed).toBe(true);
  });
});

describe("趋势视图 trendView", () => {
  it("按事件桶堆叠；未知只在出现时入图，日均按天数", () => {
    const s = stateWith({ dailyMap: normalizeDaily(ROWS).map, dailyDays: normalizeDaily(ROWS).days });
    const v = trendView(s);
    expect(v.labels).toEqual(["09-20", "09-21"]);
    expect(v.datasets.map((d: { label: string }) => d.label)).toEqual(["成功", "失败", "跳过"]);
    expect(v.datasets[0].data).toEqual([1, 1]);
    expect(v.meta).toEqual([
      ["近 30 天成功", "2"], ["近 30 天失败", "0"], ["近 30 天跳过", "2"],
      ["日均签到事件（次）", "2"],
    ]);
  });

  it("有未知时插入未知序列与元数据（在日均之前）", () => {
    const s = stateWith({ dailyMap: { "2026-09-20": { success: 1, fail: 0, skip: 0, unknown: 3, total: 4 } }, dailyDays: ["2026-09-20"] });
    const v = trendView(s);
    expect(v.datasets.map((d: { label: string }) => d.label)).toEqual(["成功", "失败", "跳过", "未知"]);
    expect(v.meta[3]).toEqual(["近 30 天未知", "3"]);
    expect(v.meta[4][0]).toBe("日均签到事件（次）");
  });

  it("无数据返回 null", () => {
    expect(trendView(emptyState())).toBeNull();
  });
});

describe("分布视图 distView", () => {
  it("正数桶按数量降序；总数读后端去重终值而非桶合计", () => {
    const acc = acceptAccounts(ACCOUNTS);
    const s = stateWith({ statusAccounts: acc.byStatus, accountsTotal: acc.total });
    const v = distView(s);
    expect(v.data).toEqual([2, 1]);
    expect(v.labels).toEqual(["成功", "用户取消"]);
    expect(v.meta).toEqual([["签到账号总数", "2"], ["结果类型", "2"]]);
  });

  it("全零桶返回 null", () => {
    expect(distView(stateWith({ statusAccounts: { success: 0 } }))).toBeNull();
  });
});

describe("自选时间片视图 slotsView", () => {
  it("标签 / 人数 / 上限与最热门", () => {
    const v = slotsView([
      { label: "06:30", count: 3, cap: 10 },
      { label: "07:00", count: 5, cap: 8 },
    ]);
    expect(v.counts).toEqual([3, 5]);
    expect(v.caps).toEqual([10, 8]);
    expect(v.coverage).toBe("共 2 个可选时段");
    expect(v.meta[1]).toEqual(["最热门时段", "07:00（5 人）"]);
    expect(slotsView([])).toBeNull();
  });

  it("全部 0 人时「最热门」无定义，回落破折号", () => {
    const v = slotsView([
      { label: "06:30", count: 0, cap: 10 },
      { label: "07:00", count: 0, cap: 8 },
    ]);
    // 非空列表首片恒为 top；若不按 count>0 门控，会给出「06:30（0 人）」的假结论。
    expect(v.meta[1]).toEqual(["最热门时段", "—"]);
  });

  it("存在正数片时仍取人数最多的那片", () => {
    const v = slotsView([
      { label: "06:30", count: 0, cap: 10 },
      { label: "06:45", count: 2, cap: 8 },
      { label: "07:00", count: 1, cap: 8 },
    ]);
    expect(v.meta[1]).toEqual(["最热门时段", "06:45（2 人）"]);
  });
});

describe("签到热力图 calendarView", () => {
  const now = new Date("2026-09-21T10:00:00"); // 2026-09-21 是周一

  it("周一起始、前置/尾部空位与月份标签", () => {
    const v = calendarView(emptyState(), { month: null, now });
    expect(v.label).toBe("2026 年 9 月");
    const firstDay = v.cells.findIndex((c: { d?: number }) => c.d === 1);
    expect(firstDay).toBe(1); // 2026-09-01 是周二 → 1 个前置空位
    expect(v.cells.length).toBe(1 + 30 + (7 - ((1 + 30) % 7)) % 7);
  });

  it("染色读日终态 dayFinal：失败优先于成功，仅跳过 → is-none", () => {
    const s = stateWith({
      dayFinal: {
        "2026-09-10": { success: 0, fail: 1, skip: 0, unknown: 0, total: 1 },
        "2026-09-11": { success: 2, fail: 0, skip: 0, unknown: 0, total: 2 },
        "2026-09-12": { success: 0, fail: 0, skip: 1, unknown: 0, total: 1 },
      },
    });
    const v = calendarView(s, { month: null, now });
    const cell = (d: number) => v.cells.find((c: { d?: number }) => c.d === d);
    expect(cell(10).cls).toContain("is-fail");
    expect(cell(11).cls).toContain("is-ok");
    expect(cell(12).cls).toContain("is-none");
    expect(cell(10).title).toContain("失败 1");
  });

  it("今天格带 is-today，未来格带 is-future 且 title 说明", () => {
    const v = calendarView(emptyState(), { month: null, now });
    const cell = (d: number) => v.cells.find((c: { d?: number }) => c.d === d);
    expect(cell(21).cls).toContain("is-today");
    expect(cell(25).cls).toContain("is-future");
    expect(cell(25).title).toBe("未来日期（尚未签到）");
  });
});

describe("运行状态视图", () => {
  it("暂停徽标", () => {
    const v = pauseView({ global_pause: 1, registration_pause: 0 });
    expect(v.global.text).toBe("全局暂停中");
    expect(v.global.tone).toBe("bad");
    expect(v.reg.text).toBe("注册开放");
    expect(pauseView(null)).toBeNull();
  });

  it("时钟偏离档位（<=5 ok / <=60 warn / 其它 bad）", () => {
    const nowMs = 1_000_000_000_000;
    const base = { now: "2026-10-03 12:00:00", tz_offset_min: 480 };
    expect(clockView({ ...base, server_ts: nowMs / 1000 + 2 }, nowMs).tone).toBe("badge--ok");
    expect(clockView({ ...base, server_ts: nowMs / 1000 + 30 }, nowMs).tone).toBe("badge--warn");
    const bad = clockView({ ...base, server_ts: nowMs / 1000 + 120 }, nowMs);
    expect(bad.tone).toBe("badge--bad");
    expect(bad.text).toBe("偏差 +120 秒");
    expect(clockView({}, nowMs)).toBeNull();
  });

  it("公告空文本回落「暂无公告」", () => {
    expect(announcementView({ text: "  " }).empty).toBe(true);
    expect(announcementView({ text: "放假通知" }).text).toBe("放假通知");
  });

  it("连通性结果", () => {
    expect(pingView({ reachable: true }).text).toBe("可达");
    expect(pingView({ reachable: false, detail: "超时" }).detail).toBe("超时");
  });

  it("请求路径常量含 stage=sign（探针隔离的唯一防线）", () => {
    expect(SIGN_EVENTS_PATH).toBe("/api/admin/sign-events?days=30&stage=sign");
    expect(DEFAULT_CAL_NOTE).toContain("不含探针");
  });
});
