import { describe, expect, it } from "vitest";
import {
  accountMatch,
  badgeOf,
  batchActions,
  byPhone,
  countLabel,
  deletedAtText,
  emptyText,
  filterGroup,
  groupAll,
  lastExecText,
  menuItems,
  menuItemsWithout,
  ownerMailText,
  ownerText,
  phoneMaskHit,
  prefText,
  pruneSelection,
  selectAllState,
  selectedIds,
  selectedPhones,
  sortedPending,
  stateCell,
  statsOf,
} from "./model";

interface AccountRecord {
  index: number;
  name: string;
  phone: string;
  phone_model: string;
  display_name: string;
  owner: string;
  owner_display: string;
  status: string;
  deleted: boolean;
  deleted_at: string;
  user_paused: boolean;
  time_pref: string | null;
  time_pref_edge: string | null;
  last_executor: { role: string; label: string } | null;
  has_phone_code: boolean;
}

function acc(over: Partial<AccountRecord> = {}): AccountRecord {
  return {
    index: 0,
    name: "A",
    phone: "138****8000",
    phone_model: "",
    display_name: "A",
    owner: "admin",
    owner_display: "管理员",
    status: "active",
    deleted: false,
    deleted_at: "",
    user_paused: false,
    time_pref: null,
    time_pref_edge: null,
    last_executor: null,
    has_phone_code: false,
    ...over,
  };
}

describe("分组与排序", () => {
  it("待处理组含待审核与已拒绝，待审核置顶、新提交在前、已拒绝沉底", () => {
    const rows = sortedPending([
      acc({ index: 0, status: "rejected" }),
      acc({ index: 1, status: "pending" }),
      acc({ index: 2, status: "active" }),
      acc({ index: 3, status: "pending" }),
      acc({ index: 4, status: "rejected" }),
    ]);
    expect(rows.map((r) => r.index)).toEqual([3, 1, 4, 0]);
  });

  it("正常组只含 status=active 且未软删；待删除组只含 deleted", () => {
    const accounts = [
      acc({ index: 0, status: "active" }),
      acc({ index: 1, status: "pending" }),
      acc({ index: 2, status: "active", deleted: true }),
    ];
    expect(groupAll(accounts, "active").map((a) => a.index)).toEqual([0]);
    expect(groupAll(accounts, "deleted").map((a) => a.index)).toEqual([2]);
  });
});

describe("检索", () => {
  // 列表 phone 是服务端已遮值；前端不重写遮罩公式，而是把遮罩串当通配模式用。
  it("完整手机号命中服务端遮罩串；空关键词全通过", () => {
    const a = acc({ name: "车站", phone: "138****8000", owner_display: "管理员" });
    expect(accountMatch(a, "13800138000")).toBe(true);
    expect(accountMatch(a, "车站")).toBe(true);
    expect(accountMatch(a, "138")).toBe(true); // 前缀子串
    expect(accountMatch(a, "8000")).toBe(true); // 后缀子串
    expect(accountMatch(a, "")).toBe(true);
    expect(accountMatch(a, "nope")).toBe(false);
    expect(accountMatch(a, "13900139000")).toBe(false); // 不同号不得命中
  });

  it("遮罩通配只按前后段锁定，中间号段任意", () => {
    expect(phoneMaskHit("138****8000", "13899999900")).toBe(false); // 后 4 位不符
    expect(phoneMaskHit("138****8000", "138 0013 8000")).toBe(true); // 分段输入取数字后比
    expect(phoneMaskHit("138****8000", "+8613800138000")).toBe(true); // +86 前缀归一到国内号
    expect(phoneMaskHit("138****8000", "8613800138000")).toBe(true); // 裸 86 前缀同归一
    expect(phoneMaskHit("138****8000", "1388000")).toBe(true); // 照可见数字拼接敲，也应命中
    expect(phoneMaskHit("138****8000", "138")).toBe(false); // 太短不按号码比
    expect(phoneMaskHit("13800138000", "13800138000")).toBe(false); // 无遮罩段即非模式
  });

  it("filterGroup 先按组归类再匹配", () => {
    const accounts = [
      acc({ index: 0, name: "甲", status: "active" }),
      acc({ index: 1, name: "乙", status: "active" }),
    ];
    expect(filterGroup(accounts, "active", "乙").map((a) => a.index)).toEqual([1]);
  });
});

describe("选中集（身份键 = 手机号）", () => {
  it("重排后同一选中项解析到同一手机号 / 其新 index", () => {
    const before = [
      acc({ index: 0, phone: "111****0001" }),
      acc({ index: 1, phone: "222****0002" }),
    ];
    expect(selectedPhones(before, { active: { "222****0002": true } }, "active")).toEqual(["222****0002"]);
    expect(selectedIds(before, { active: { "222****0002": true } }, "active")).toEqual([1]);
    const after = [
      acc({ index: 0, phone: "222****0002" }),
      acc({ index: 1, phone: "111****0001" }),
    ];
    expect(selectedPhones(after, { active: { "222****0002": true } }, "active")).toEqual(["222****0002"]);
    expect(selectedIds(after, { active: { "222****0002": true } }, "active")).toEqual([0]);
  });

  it("已不在列表的选中项不解析出目标（不回落成别人的 index）", () => {
    const accounts = [acc({ index: 0, phone: "111****0001" })];
    expect(selectedIds(accounts, { active: { "999****9999": true } }, "active")).toEqual([]);
    expect(byPhone(accounts, "999****9999")).toBeUndefined();
  });

  it("pruneSelection 丢掉已消失的选中项", () => {
    const accounts = [acc({ index: 0, phone: "111****0001" })];
    const pruned = pruneSelection(accounts, { active: { "111****0001": true, "999****9999": true } });
    expect(Object.keys(pruned.active)).toEqual(["111****0001"]);
  });

  it("全选态：整组勾满 checked；部分勾选 indeterminate；零行不 checked", () => {
    const accounts = [acc({ index: 0, phone: "111****0001" }), acc({ index: 1, phone: "222****0002" })];
    expect(selectAllState(accounts, { active: { "111****0001": true } }, "active", "")).toEqual({
      checked: false,
      indeterminate: true,
    });
    expect(
      selectAllState(accounts, { active: { "111****0001": true, "222****0002": true } }, "active", ""),
    ).toEqual({ checked: true, indeterminate: false });
    expect(selectAllState([], { active: {} }, "active", "")).toEqual({
      checked: false,
      indeterminate: false,
    });
  });
});

describe("统计与状态", () => {
  it("今日统计：成功/已签到计入成功，失败单列，跳过档与待签分开", () => {
    const accounts = [
      acc({ phone: "1" }),
      acc({ phone: "2" }),
      acc({ phone: "3" }),
      acc({ phone: "4" }),
      acc({ phone: "5" }),
    ];
    const states = { "1": "success", "2": "already", "3": "failed", "4": "no_task" };
    expect(statsOf(accounts, states)).toEqual({ success: 2, failed: 1, waiting: 1, skipped: 1 });
  });

  it("状态列 title 拼「状态 · 原因 · 耗时」，原因与状态名相同则不重复", () => {
    const withMsg = stateCell("1", { "1": "failed" }, { "1": "密码错误" }, { "1": 3.24 });
    expect(withMsg.title).toBe("签到失败 · 密码错误 · 耗时 3.2s");
    const dup = stateCell("1", { "1": "success" }, { "1": "签到成功" }, {});
    expect(dup.title).toBe("签到成功");
    expect(dup.tone).toBe("ok");
    expect(stateCell("2", {}, {}, {}).code).toBe("pending");
  });

  it("审核徽标：待审核 warn / 已拒绝 bad / 其余 ok", () => {
    expect(badgeOf("pending")).toEqual({ label: "待审核", tone: "warn" });
    expect(badgeOf("rejected")).toEqual({ label: "已拒绝", tone: "bad" });
    expect(badgeOf("active")).toEqual({ label: "正常", tone: "ok" });
  });
});

describe("展示文案", () => {
  it("自选时间带首尾标记；无偏好给破折号", () => {
    expect(prefText(acc({ time_pref: "06:30" }))).toBe("06:30");
    expect(prefText(acc({ time_pref: "06:30", time_pref_edge: "first" }))).toBe("最早 06:30");
    expect(prefText(acc({ time_pref: "06:30", time_pref_edge: "last" }))).toBe("最后 06:30");
    expect(prefText(acc({ time_pref: null }))).toBe("—");
  });

  it("归属：admin → 管理员；有 owner_display 直接展示", () => {
    expect(ownerText(acc({ owner: "admin", owner_display: "" }))).toBe("管理员");
    expect(ownerText(acc({ owner: "u@x.com", owner_display: "u***@x.com" }))).toBe("u***@x.com");
  });

  it("归属邮箱：邮箱形态直接用脱敏串，非邮箱回落归属名", () => {
    expect(ownerMailText(acc({ owner: "u***@x.com" }))).toBe("u***@x.com");
    expect(ownerMailText(acc({ owner: "admin", owner_display: "管理员" }))).toBe("管理员");
  });

  it("上次实领：null → 破折号；有 label → label；unknown 无 label → 旧数据", () => {
    expect(lastExecText(acc({ last_executor: null }))).toBe("—");
    expect(lastExecText(acc({ last_executor: { role: "user", label: "甲" } }))).toBe("甲");
    expect(lastExecText(acc({ last_executor: { role: "unknown", label: "" } }))).toBe("未标注（旧数据）");
  });

  it("删除时间取到分钟", () => {
    expect(deletedAtText(acc({ deleted_at: "2026-09-30T10:00:00" }))).toBe("2026-09-30 10:00");
  });
});

describe("空态与计数", () => {
  it("真空给组默认文案；筛出的空给「无匹配结果」", () => {
    expect(emptyText("active", [], [])).toBe("暂无账号，添加后即可开始自动签到");
    expect(emptyText("active", [acc()], [])).toBe("无匹配结果");
    expect(emptyText("active", [acc()], [acc()])).toBe("");
  });

  it("计数文案：无关键词只报总数；检索时补「匹配 / 共」", () => {
    expect(countLabel(0, 0, "")).toBe("");
    expect(countLabel(5, 5, "")).toBe("5 个");
    expect(countLabel(5, 2, "kw")).toBe("2 个匹配 / 共 5 个");
  });
});

describe("动作集", () => {
  it("行菜单按组给出；omit 可去掉窄屏已提到行外的主任务", () => {
    expect(menuItems("pending").map((m) => m.key)).toEqual(["approve", "reject", "edit", "remove"]);
    expect(menuItems("deleted").map((m) => m.key)).toEqual(["restore", "purge"]);
    expect(menuItems("active").map((m) => m.key)).toEqual([
      "move_up", "move_down", "signin", "edit", "remove",
    ]);
    expect(menuItemsWithout("pending", ["approve"]).map((m) => m.key)).toEqual(["reject", "edit", "remove"]);
  });

  it("批量动作集按组给出", () => {
    expect(batchActions("pending").map((b) => b.key)).toEqual(["approve", "reject"]);
    expect(batchActions("active").map((b) => b.key)).toEqual(["signin", "delete"]);
    expect(batchActions("deleted").map((b) => b.key)).toEqual(["restore", "purge"]);
  });
});
