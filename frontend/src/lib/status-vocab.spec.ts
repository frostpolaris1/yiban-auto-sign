import { describe, expect, it } from "vitest";
import { STATUS_VOCAB, statusField } from "./status-vocab.js";
import { STATUS_LABEL, statusLabel as chartLabel } from "../dashboard/model.js";
import { STATE_ICON, STATE_TEXT, STATE_TONE } from "../accounts/model.js";
import { statusLabel } from "../logs/format";

/* 状态词表单源守卫：三页（仪表盘图表 / 账号表 / 日志徽标）必须从
   lib/status-vocab.js 取词，不得各留一份。Python 侧总账见
   tests/test_yiban_status_single_source.py（钉键集合 == yiban.status.ALL_STATUSES）。 */

const CODES = Object.keys(STATUS_VOCAB);

describe("STATUS_VOCAB（唯一事实源）", () => {
  it("覆盖全部 12 个状态码，每条含 full/short/icon/tone", () => {
    expect(CODES.sort()).toEqual(
      [
        "already", "failed", "global_paused", "no_position", "no_task", "paused",
        "pending", "retrying", "skipped_norange", "skipped_window", "success",
        "user_cancelled",
      ].sort(),
    );
    for (const k of CODES) {
      const e = STATUS_VOCAB[k];
      expect(typeof e.full, k).toBe("string");
      expect(e.full.length, k).toBeGreaterThan(0);
      expect(typeof e.short, k).toBe("string");
      expect(e.short.length, k).toBeGreaterThan(0);
      expect(typeof e.icon, k).toBe("string");
      expect(["ok", "bad", "warn", "info", "muted"]).toContain(e.tone);
    }
  });

  it("statusField 对未知码返回调用方兜底值（信息不丢）", () => {
    expect(statusField("brand_new_code", "short", undefined)).toBeUndefined();
    expect(statusField("brand_new_code", "short", "brand_new_code")).toBe("brand_new_code");
  });
});

describe("三页消费同源（图表 short / 账号表 full+icon / 日志徽标 short）", () => {
  it("仪表盘图表短名 === STATUS_VOCAB[k].short", () => {
    for (const k of CODES) expect(STATUS_LABEL[k], k).toBe(STATUS_VOCAB[k].short);
    expect(chartLabel("success")).toBe("成功");
    expect(chartLabel("status_from_2035")).toBe("status_from_2035");
  });

  it("账号表文案/图标 === STATUS_VOCAB[k].full/icon", () => {
    for (const k of CODES) {
      expect(STATE_TEXT[k], k).toBe(STATUS_VOCAB[k].full);
      expect(STATE_ICON[k], k).toBe(STATUS_VOCAB[k].icon);
    }
  });

  it("账号表语气档 = 规范化 tone（info 收成 muted，账号页 CSS 无 info 档）", () => {
    for (const k of CODES) {
      const want = STATUS_VOCAB[k].tone === "info" ? "muted" : STATUS_VOCAB[k].tone;
      expect(STATE_TONE[k], k).toBe(want);
    }
  });

  it("日志徽标文案 === short，语气档 === tone", () => {
    for (const k of CODES) {
      const got = statusLabel("sign", k);
      expect(got.label, k).toBe(STATUS_VOCAB[k].short);
      expect(got.tone, k).toBe(STATUS_VOCAB[k].tone);
      expect(got.raw, k).toBe(k);
    }
  });

  it("三页未知码兜底语义不变", () => {
    expect(chartLabel("")).toBe("未知");
    expect(statusLabel("sign", "nope")).toEqual({ label: "nope", tone: "muted", raw: "nope" });
    expect(statusLabel("sign", "")).toEqual({ label: "未知", tone: "muted", raw: "" });
  });
});
