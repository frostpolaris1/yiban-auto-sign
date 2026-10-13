import { describe, expect, it } from "vitest";
import {
  anchorText,
  auditDetailText,
  auditStateText,
  auditStatusQuery,
  auditTone,
  dataAtText,
  indeterminateText,
  knownDebtLines,
  type AuditState,
  type AuditStatusPayload,
} from "./audit-status";

function payload(over: Partial<AuditStatusPayload> = {}): AuditStatusPayload {
  return {
    ok: true,
    state: "healthy",
    verdict_code: 0,
    reason: "",
    cached: true,
    data_at: "2026-10-13 00:00:07",
    snapshot_stale: false,
    chain_ok: true,
    broken: 0,
    anchor_status: "ok",
    anchor: {
      path_state: "ok",
      lines: 4,
      old_format_lines: 2,
      unparsable_lines: 0,
      max_id: 4,
      max_ts: "2026-09-01 00:00:31",
    },
    indeterminate: { present: false, reason: "" },
    known_debt: [],
    write_failures: 0,
    empty_hash_rows: 0,
    note: "",
    ...over,
  };
}

describe("auditStatusQuery", () => {
  it("无查询参数", () => {
    expect(auditStatusQuery()).toBe("/api/admin/audit-status");
  });
});

describe("三态：互斥、各不相同、无法定论不得显示成健康", () => {
  const states: AuditState[] = ["healthy", "tampered", "indeterminate"];

  it("三态各有独立标签", () => {
    const labels = states.map(auditStateText);
    expect(new Set(labels).size).toBe(3);
    expect(auditStateText("healthy")).toBe("健康");
    expect(auditStateText("tampered")).toBe("检出篡改");
    expect(auditStateText("indeterminate")).toBe("无法定论");
  });

  it("三态色调互不相同：健康绿、篡改红、无法定论黄", () => {
    expect(auditTone("healthy")).toBe("ok");
    expect(auditTone("tampered")).toBe("bad");
    expect(auditTone("indeterminate")).toBe("warn");
    expect(new Set(states.map(auditTone)).size).toBe(3);
  });

  it("未知状态按「无法定论」显示（不猜、不当健康）", () => {
    expect(auditStateText("weird")).toBe("无法定论");
    expect(auditTone("weird")).toBe("warn");
  });
});

describe("anchorText：校验到哪个锚点", () => {
  it("有定点基准时给时刻与行号", () => {
    expect(anchorText(payload())).toContain("2026-09-01 00:00:31");
    expect(anchorText(payload())).toContain("第 4 行");
  });

  it("无可定点基准时如实写明", () => {
    const p = payload({ anchor: { ...payload().anchor!, max_id: null, max_ts: null } });
    expect(anchorText(p)).toContain("没有可作定点基准的行");
  });

  it("锚点读不出时如实写明状态", () => {
    const p = payload({ anchor: { ...payload().anchor!, path_state: "missing", lines: 0 } });
    expect(anchorText(p)).toContain("missing");
  });

  it("无锚点信息时不留白", () => {
    expect(anchorText(payload({ anchor: null }))).toContain("无");
  });
});

describe("dataAtText：缓存结果必须自报数据时刻", () => {
  it("有快照时给时刻", () => {
    expect(dataAtText(payload())).toContain("2026-10-13 00:00:07");
  });

  it("无快照时明说没有", () => {
    expect(dataAtText(payload({ cached: false, data_at: null }))).toContain("无");
  });

  it("超龄时把「已超龄」写出来（自检停摆不许只换个日期继续绿）", () => {
    const text = dataAtText(payload({ snapshot_stale: true }));
    expect(text).toContain("已超龄");
    expect(text).toContain("2026-10-13 00:00:07");
    expect(dataAtText(payload())).not.toContain("已超龄");
  });
});

describe("indeterminateText / knownDebtLines", () => {
  it("没有「判不出来」的区间时回空串", () => {
    expect(indeterminateText(payload())).toBe("");
  });

  it("有「判不出来」的区间时把原因带出来", () => {
    const p = payload({
      state: "indeterminate",
      verdict_code: 2,
      indeterminate: { present: true, reason: "锚点文件含非法 UTF-8 字节" },
    });
    expect(indeterminateText(p)).toContain("判不出来的区间");
    expect(indeterminateText(p)).toContain("非法 UTF-8");
  });

  it("已知欠账逐行给出，且与三态分开（健康态也照样有欠账行）", () => {
    const p = payload({
      known_debt: [{
        code: "old_format_anchor_lines",
        label: "锚点老格式行（2026-08-28 前）",
        count: 2,
        reason: "这些行不带行间链",
      }],
    });
    const lines = knownDebtLines(p);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("已知欠账");
    expect(lines[0]).toContain("2 行");
    expect(lines[0]).toContain("2026-08-28");
    // 欠账不改变三态：健康仍是健康
    expect(auditStateText(p.state)).toBe("健康");
    expect(knownDebtLines(payload())).toEqual([]);
  });
});

describe("auditDetailText", () => {
  it("无异常时回空串", () => {
    expect(auditDetailText(payload())).toBe("");
  });

  it("断链/空 hash/欠账各带一条", () => {
    const text = auditDetailText(payload({
      chain_ok: false, broken: 2, empty_hash_rows: 1, write_failures: 3, note: "附加诊断",
    }));
    expect(text).toContain("断链 2 处");
    expect(text).toContain("空 hash 行 1 条");
    expect(text).toContain("写入欠账 3 次");
    expect(text).toContain("附加诊断");
  });
});
