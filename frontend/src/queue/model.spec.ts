import { describe, expect, it } from "vitest";
import {
  FOCUS_TARGET,
  type OpRequest,
  type QueueRow,
  type RowView,
  WIDE_MIN,
  closeDrawer,
  deleteRequest,
  drawerForm,
  eventsQuery,
  openDrawer,
  pendingQuery,
  queueQuery,
  reviewRequest,
  retryRequest,
  rowView,
  summaryView,
  type QueueSummaryData,
} from "./model";

/* 队列页（lcpx 试点）模型判据。测不变式，不测编舞：
   · 三组待办各自由**服务端**过滤分页；本层只钉「每组查询指向正确端点与参数」；
   · 两种服务端行形状（/api/accounts pending 组行、/api/queue 行）归一成同一视图行，
     且手机号/归属一律取服务端已脱敏值，本层不做任何遮罩加工；
   · 行内操作（通过/驳回/删除/重试）指向**既有** ops 端点；完整手机号只允许出现在
     重试请求体里（来源是详情接口），绝不进 URL；
   · 抽屉状态机：开＝有目标且 open，关＝不再渲染（open=false），换目标＝替换；
   · 抽屉两形态：≤720px 全屏（size 100%），>720px 侧滑 480px；焦点目标＝关闭侧
     （与 ep-confirm「焦点落取消侧」同一安全口径）。 */

function pendingApiRow(over: Partial<QueueRow> = {}): QueueRow {
  return {
    index: 2,
    name: "e2e-pending-acct",
    phone: "137****7003",
    phone_model: "Redmi K70",
    display_name: "e2e-pending-acct",
    owner: "admin***@e2e",
    owner_display: "管理员添加",
    status: "pending",
    reject_reason: "",
    time_pref: "06:45",
    ...over,
  };
}

function queueApiRow(over: Partial<QueueRow> = {}): QueueRow {
  return {
    index: 0,
    name: "e2e-user-acct",
    phone: "138****8001",
    phone_model: "iPhone 14",
    display_name: "e2e-user-acct",
    owner: "e2e***@example.com",
    owner_display: "e2e-user@example.com",
    status: "active",
    reject_reason: "",
    state: "failed",
    state_msg: "密码错误",
    state_time: "06:42:00",
    plan_at: "06:47",
    time_pref: "",
    ...over,
  };
}

describe("三组待办的查询构造（服务端过滤分页）", () => {
  it("待审核组走既有 /api/accounts 分组分页", () => {
    expect(pendingQuery(20, 0)).toBe("/api/accounts?group=pending&limit=20&offset=0");
    expect(pendingQuery(50, 60)).toBe("/api/accounts?group=pending&limit=50&offset=60");
  });

  it("失败/未签组走只读聚合 /api/queue，bucket 二值", () => {
    expect(queueQuery("failed", 20, 0)).toBe("/api/queue?bucket=failed&limit=20&offset=0");
    expect(queueQuery("unsigned", 100, 40)).toBe("/api/queue?bucket=unsigned&limit=100&offset=40");
  });

  it("抽屉近 30 天事件按账号下标取，天数显式下发", () => {
    expect(eventsQuery(0)).toBe("/api/queue/0/events?days=30");
    expect(eventsQuery(12, 7)).toBe("/api/queue/12/events?days=7");
  });
});

describe("rowView：两种服务端行归一（脱敏值透传，不二次加工）", () => {
  it("pending 组行：审核徽标按 status，保留自选时段", () => {
    const v = rowView(pendingApiRow(), "pending");
    expect(v.group).toBe("pending");
    expect(v.phone).toBe("137****7003");
    expect(v.badgeText).toBe("待审核");
    expect(rowView(pendingApiRow({ status: "rejected", reject_reason: "识别码不匹配" }), "pending").badgeText).toBe("已拒绝");
  });

  it("queue 失败行：状态文案取词表 short，失败消息与计划时刻透传", () => {
    const v = rowView(queueApiRow(), "failed");
    expect(v.group).toBe("failed");
    expect(v.stateText).toBe("失败");
    expect(v.stateMsg).toBe("密码错误");
    expect(v.planAt).toBe("06:47");
  });

  it("queue 未签行：无当日结论 → 待签，计划时刻可缺", () => {
    const v = rowView(queueApiRow({ state: "pending", state_msg: "待生成", plan_at: null }), "unsigned");
    expect(v.group).toBe("unsigned");
    expect(v.stateText).toBe("待签");
    expect(v.planAt).toBe("");
  });
});

describe("今日摘要视图", () => {
  it("业务日＋星期＋窗口＋账号数＋失败数，一一对应服务端字段", () => {
    const d: QueueSummaryData = {
      today: "2026-10-24",
      weekday: "周六",
      window: "06:30 ~ 07:50",
      counts: { pending: 2, rejected: 1, active: 1032, failed: 3, unsigned: 8 },
    };
    const v = summaryView(d);
    expect(v.dateText).toBe("2026-10-24 周六");
    expect(v.windowText).toBe("窗口 06:30 ~ 07:50");
    expect(v.accountCount).toBe(1032);
    expect(v.failedCount).toBe(3);
    expect(v.pendingCount).toBe(3); // 待审核 2 ＋ 已拒绝 1
  });
});

describe("行内操作：指向既有 ops 端点", () => {
  it("通过/驳回 → POST /api/accounts/<index>/review，body 带脱敏 phone", () => {
    const approve: OpRequest = reviewRequest(pendingApiRow(), "approve");
    expect(approve.method).toBe("POST");
    expect(approve.path).toBe("/api/accounts/2/review");
    expect(approve.body).toEqual({ action: "approve", phone: "137****7003" });

    const reject = reviewRequest(pendingApiRow(), "reject", "识别码不匹配");
    expect(reject.body).toEqual({ action: "reject", phone: "137****7003", reason: "识别码不匹配" });
  });

  it("删除（软删）→ DELETE /api/accounts/<index>", () => {
    const req = deleteRequest(queueApiRow());
    expect(req.method).toBe("DELETE");
    expect(req.path).toBe("/api/accounts/0");
    expect(req.body).toEqual({ phone: "138****8001" });
  });

  it("重试 = 详情取完整号后 POST /api/signin：完整号只在请求体，不进 URL", () => {
    const req = retryRequest(queueApiRow(), { account: { phone: "13800138001" } });
    expect(req.method).toBe("POST");
    expect(req.path).toBe("/api/signin");
    expect(req.body).toEqual({ phone: "13800138001" });
    expect(req.path.includes("13800138001")).toBe(false);
    // 详情缺失（号码不完整）时拒绝发请求，而不是把脱敏串发出去
    expect(retryRequest(queueApiRow(), undefined)).toBeNull();
    expect(retryRequest(queueApiRow(), { account: { phone: "138****8001" } })).toBeNull();
  });
});

describe("抽屉状态机", () => {
  it("开＝open 且目标为该行；换目标＝替换；关＝不再渲染", () => {
    const a = rowView(queueApiRow(), "failed");
    const b = rowView(pendingApiRow(), "pending");
    let s = openDrawer({ open: false, target: null }, a);
    expect(s.open).toBe(true);
    expect(s.target).toEqual(a);
    s = openDrawer(s, b);
    expect(s.open).toBe(true);
    expect(s.target).toEqual(b);
    s = closeDrawer(s);
    expect(s.open).toBe(false);
    expect(s.target).toEqual(b); // 目标保留供退出过渡，open 决定是否渲染
  });
});

describe("抽屉两形态与焦点口径", () => {
  it("≤720px 全屏（size 100%），>720px 侧滑 480px", () => {
    expect(drawerForm(360)).toEqual({ size: "100%", cls: "obj-drawer--full" });
    expect(drawerForm(720)).toEqual({ size: "100%", cls: "obj-drawer--full" });
    expect(drawerForm(WIDE_MIN)).toEqual({ size: "480px", cls: "obj-drawer--side" });
    expect(drawerForm(1920)).toEqual({ size: "480px", cls: "obj-drawer--side" });
  });

  it("初始焦点目标＝抽屉关闭钮（破坏面同 ep-confirm 落安全侧）", () => {
    expect(FOCUS_TARGET).toBe(".el-drawer__close-btn");
  });
});

describe("RowView 形状稳定性", () => {
  it("视图行不携带服务端原始大对象字段（只留渲染所需）", () => {
    const v: RowView = rowView(queueApiRow(), "failed");
    const keys = Object.keys(v).sort();
    expect(keys).toEqual([
      "badgeText", "group", "index", "model", "name", "owner", "phone",
      "planAt", "rejectReason", "state", "stateMsg", "stateText", "status", "timePref",
    ]);
  });
});
