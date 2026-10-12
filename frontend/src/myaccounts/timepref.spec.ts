import { describe, expect, it } from "vitest";
import {
  DISABLED_HINT,
  FIRST_SLOT_TIP,
  LAST_SLOT_TIP,
  LOAD_ERROR_TEXT,
  SLOT_FULL_PCT,
  SLOT_OFF_NOTE,
  collapseLabel,
  collapsedByDefault,
  edgeTip,
  estimateText,
  slotClass,
  slotPctText,
  slotTitle,
  type TimePrefData,
  type TimeSlot,
} from "./timepref";

function slot(over: Partial<TimeSlot> = {}): TimeSlot {
  return { slot_min: 390, label: "06:30", pct: 10, ...over };
}

function data(over: Partial<TimePrefData> = {}): TimePrefData {
  return {
    has_account: true,
    window: "06:30 ~ 07:50",
    slots: [slot({ slot_min: 390, label: "06:30" }), slot({ slot_min: 395, label: "06:35" })],
    pref_slot: null,
    allowed: true,
    ...over,
  };
}

describe("slotClass（优先级：不可选 > 选中 > 满员 > 部分裁剪）", () => {
  it("各态与优先级", () => {
    expect(slotClass(slot({ disabled: true }), null)).toBe("slot--off");
    expect(slotClass(slot({ slot_min: 395 }), 395)).toBe("slot--on");
    expect(slotClass(slot({ pct: SLOT_FULL_PCT }), null)).toBe("slot--full");
    expect(slotClass(slot({ pct: 120 }), null)).toBe("slot--full");
    expect(slotClass(slot({ edge_note: "前 2 分钟被保留" }), null)).toBe("slot--partial");
    expect(slotClass(slot(), null)).toBe("");
  });

  it("禁用态压过选中与满员（不可选就不该显示选中）", () => {
    expect(slotClass(slot({ disabled: true, slot_min: 395 }), 395)).toBe("slot--off");
    expect(slotClass(slot({ disabled: true, pct: 100 }), null)).toBe("slot--off");
  });

  it("选中压过满员（自己选中的满员时段要显示为已选）", () => {
    expect(slotClass(slot({ slot_min: 390, pct: 100 }), 390)).toBe("slot--on");
  });
});

describe("槽位文案", () => {
  it("热度文案：不可选报『已保留』，其余报百分比", () => {
    expect(slotPctText(slot({ disabled: true, pct: 99 }))).toBe("已保留");
    expect(slotPctText(slot({ pct: 0 }))).toBe("已选 0%");
    expect(slotPctText(slot({ pct: 100 }))).toBe("已选 100%");
  });

  it("title 只在不可选/部分裁剪时给出", () => {
    expect(slotTitle(slot({ disabled: true }))).toBe(SLOT_OFF_NOTE);
    expect(slotTitle(slot({ edge_note: "前 2 分钟被保留" }))).toBe("前 2 分钟被保留，选中后将在可用部分为你签到");
    expect(slotTitle(slot())).toBe("");
  });
});

describe("edgeTip（仅选中首/尾时提醒；部分裁剪优先）", () => {
  const d = data({
    slots: [slot({ slot_min: 390 }), slot({ slot_min: 395 }), slot({ slot_min: 400 })],
  });

  it("未选中（或非首尾）不给提醒", () => {
    expect(edgeTip(d, 0, 3)).toBe("");
    expect(edgeTip({ ...d, pref_slot: 395 }, 1, 3)).toBe("");
  });

  it("选中首尾给对应文案", () => {
    expect(edgeTip({ ...d, pref_slot: 390 }, 0, 3)).toBe(FIRST_SLOT_TIP);
    expect(edgeTip({ ...d, pref_slot: 400 }, 2, 3)).toBe(LAST_SLOT_TIP);
  });

  it("部分裁剪的说明优先于首尾文案", () => {
    const withEdge = data({
      pref_slot: 390,
      slots: [slot({ slot_min: 390, edge_note: "前 2 分钟被保留" })],
    });
    expect(edgeTip(withEdge, 0, 1)).toBe("前 2 分钟被保留，选中后将在可用部分为你签到");
  });

  it("未开启时前缀『未开启：』（选择会保存但不生效）", () => {
    expect(edgeTip({ ...d, allowed: false, pref_slot: 390 }, 0, 3)).toBe("未开启：" + FIRST_SLOT_TIP);
  });
});

describe("estimateText（三支：已生效不显示 / 有预计 / 只有说明）", () => {
  it("已生效自选时不显示预计行", () => {
    expect(estimateText({ ...data(), pref: true, estimated: "07:00" })).toEqual({ text: "", hidden: true });
  });

  it("有预计值时拼接说明与未开启后缀", () => {
    expect(estimateText({ ...data(), estimated: "07:00", estimate_note: "（按当前队列）" })).toEqual({
      text: "预计签到时段：07:00（按当前队列）",
      hidden: false,
    });
    expect(estimateText({ ...data(), allowed: false, estimated: "07:00" })).toEqual({
      text: "预计签到时段：07:00（自选未开启，按自动分配）",
      hidden: false,
    });
  });

  it("无预计值但有说明时只显示说明；两者都无则隐藏", () => {
    expect(estimateText({ ...data(), estimate_note: "窗口未开启" })).toEqual({ text: "窗口未开启", hidden: false });
    expect(estimateText({ ...data() })).toEqual({ text: "", hidden: true });
  });
});

describe("折叠与固定文案", () => {
  it("未开启时默认收起", () => {
    expect(collapsedByDefault(false)).toBe(true);
    expect(collapsedByDefault(true)).toBe(false);
    expect(collapseLabel(true)).toBe("展开配置");
    expect(collapseLabel(false)).toBe("收起");
  });

  it("固定文案（与 legacy 模板/组件逐字一致）", () => {
    expect(DISABLED_HINT).toBe("功能未开启：你的选择会保存，但暂不生效");
    expect(LOAD_ERROR_TEXT).toBe("签到时间加载失败，请重试");
  });
});
