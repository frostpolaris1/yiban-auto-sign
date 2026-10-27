# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""影子运行对账工具（P8-E）：旧栈与新栈按账号逐日终态对比。

输入是两份中间 JSONL（本工具不直连两库，不绑任何驱动；从两侧库/状态文件
提取中间件的命令由运维 runbook 持有，属过程稿不入库）：

- 旧侧行：{"phone", "day", "status", "task_state", "task_result",
  "account_status"}。`status`＝旧栈按日状态文件的终态词（与
  yiban/core/status.py ALL_STATUSES 同词）；`status` 缺失或为空时按
  `task_state` 派生（done→ok、failed→failed、skipped→soft、其余→未定）。
  `account_status` 仅随明细留痕，不参与分类（终态词已覆盖暂停语义）。
- 新侧行：{"phone", "day", "state", "result"}。两列＝新库 sign_tasks
  同名列（state 词表见 server/internal/core/status/status.go，result 词表见
  server/internal/signing/contract/contract.go 的 Outcome 与 worker 的
  wait_* 等待词；结算分档见 server/internal/app/worker/worker.go
  settleOutcome）。

等价类（对账单位是类，不是词面；词面原样进明细供人工判读）：
- ok＝已签成（旧 success/already；新 success+already_done）；
- soft＝有结论、当日不再动（旧 no_task/no_position；新 skipped+no_target 等）；
- window＝窗口族（旧 skipped_window/skipped_norange；新 wait_window_open/
  wait_window_missing——新栈窗口未开是退回排队，日终快照里即该形态）；
- cred_fail＝凭据错（旧 paused 熔断；新 failed+credential_invalid）；
- failed＝其余失败；other＝新栈无对应概念的旧管理侧词（user_cancelled/
  supplementing/global_paused，新栈缺该维度，只能人工判读）；
- undecided＝未定态；unknown＝词表外。

分类（每 phone×day 一格）：
- match：两侧等价类相同；
- old_ok_new_failed：旧 ok、新 failed；
- window_mismatch：window 类恰在一侧；
- status_word_diff：两侧都有结论、类不同（不含上两类，含 unknown 侧）；
- one_side_no_row：账号两侧都有、当日行只在一侧；
- new_missing / old_missing：账号只在旧侧 / 新侧出现；
- undecided：任一侧未定态，计数不计差异（快照时点未收尾属正常）。

隐私：输出一律掩码手机号（复用 yiban.core.masking.mask_phone，同一实现）；
--json / --csv 明细落盘 0600。中间 JSONL 本身含明文手机号，只允许落本地
运行目录，不入仓库。

退出码：0＝对账完成（差异是发现，不是工具失败）；2＝用法/输入错误
（缺必填字段、坏 JSON、坏 mapping）。
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yiban.core.masking import mask_phone

#: 旧侧状态词 → 等价类。词表＝yiban/core/status.py ALL_STATUSES＋队列态
#: done/claimed/stolen（done 只知「已了结」，分不清 success/already/no_task，
#: 按成功类计；claimed/stolen/pending/retrying 都不是当日终态）。
_OLD_CLASS = {
    "success": "ok", "already": "ok", "done": "ok",
    "no_task": "soft", "no_position": "soft", "skipped": "soft",
    "skipped_window": "window", "skipped_norange": "window",
    "paused": "cred_fail",
    "failed": "failed",
    "user_cancelled": "other", "supplementing": "other", "global_paused": "other",
    "retrying": "undecided", "pending": "undecided",
    "claimed": "undecided", "stolen": "undecided",
    "": "undecided",
}
#: 新侧 state=skipped 时的 result 词 → 等价类（settleOutcome 的
#: 「有结论但不该告警」档）；表外词按 soft 计。
_NEW_SKIPPED_CLASS = {
    "no_target": "soft", "invalid_target": "soft", "photo_missing": "soft",
    "challenge": "soft", "device_unbound": "soft",
}
#: 新栈窗口等待词（worker.go wait_* 家族中窗口相关的两个）。
_WINDOW_WAITS = ("wait_window_open", "wait_window_missing")
_CLASSES = ("ok", "soft", "window", "cred_fail", "failed",
            "other", "undecided", "unknown")
#: 旧侧 task_state → 派生词（status 缺失时的降级口径，见模块说明）。
_OLD_STATE_FALLBACK = {"done": "done", "failed": "failed",
                       "skipped": "skipped"}


def _old_norm(row, mapping):
    """旧侧行 → (等价类, 词面)。词面优先取 status；缺失按 task_state 派生。"""
    word = str(row.get("status") or "").strip()
    if not word:
        word = _OLD_STATE_FALLBACK.get(
            str(row.get("task_state") or "").strip(), "pending")
    return mapping.get(word, _OLD_CLASS.get(word, "unknown")), word


def _new_norm(row, mapping):
    """新侧行 → (等价类, 词面)。state 定分支，result 定词面。"""
    state = str(row.get("state") or "").strip()
    result = str(row.get("result") or "").strip()
    if state == "success":
        return "ok", (result or "success")
    if state == "skipped":
        cls = mapping.get(result) or _NEW_SKIPPED_CLASS.get(result, "soft")
        return cls, (result or "skipped")
    if state == "failed":
        if result == "credential_invalid":
            return "cred_fail", result
        return "failed", (result or "failed")
    if state in ("pending", "running"):
        if result in _WINDOW_WAITS:
            return "window", result
        return "undecided", (result or state)
    return "unknown", (result or state)


def _index(rows):
    """行列表 → {phone: {day: row}}；必填字段缺失即用法错误。"""
    out = {}
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 行不是 JSON 对象")
        phone = str(row.get("phone") or "").strip()
        day = str(row.get("day") or "").strip()
        if not phone or not day:
            raise ValueError(f"第 {i} 行缺必填字段 phone/day")
        out.setdefault(phone, {})[day] = row
    return out


def _detail(category, phone, day, old, new, old_map, new_map):
    o = None
    if old is not None:
        cls, word = _old_norm(old, old_map)
        o = {"class": cls, "status": word,
             "task_state": str(old.get("task_state") or ""),
             "task_result": str(old.get("task_result") or ""),
             "account_status": str(old.get("account_status") or "")}
    n = None
    if new is not None:
        cls, word = _new_norm(new, new_map)
        n = {"class": cls, "state": str(new.get("state") or ""),
             "result": word}
    return {"category": category, "phone": mask_phone(phone), "day": day,
            "old": o, "new": n}


def _compare(old_rows, new_rows, old_map, new_map):
    """全量对比 → (counts, diffs)。counts 含 match 与 undecided；diffs 只装差异。"""
    old_idx, new_idx = _index(old_rows), _index(new_rows)
    counts = {"match": 0, "old_ok_new_failed": 0, "window_mismatch": 0,
              "status_word_diff": 0, "one_side_no_row": 0,
              "new_missing": 0, "old_missing": 0, "undecided": 0}
    diffs = []
    for phone in sorted(set(old_idx) | set(new_idx)):
        o_days, n_days = old_idx.get(phone, {}), new_idx.get(phone, {})
        for day in sorted(set(o_days) | set(n_days)):
            old, new = o_days.get(day), n_days.get(day)
            if old is not None and new is None:
                # 新侧整个账号无行＝账号级缺席；有账号但缺当日行＝单日缺席。
                cat = "new_missing" if not n_days else "one_side_no_row"
            elif old is None and new is not None:
                cat = "old_missing" if not o_days else "one_side_no_row"
            else:
                ocls, _ow = _old_norm(old, old_map)
                ncls, _nw = _new_norm(new, new_map)
                if "undecided" in (ocls, ncls):
                    counts["undecided"] += 1
                    continue
                if ocls == ncls:
                    counts["match"] += 1
                    continue
                if ocls == "ok" and ncls == "failed":
                    cat = "old_ok_new_failed"
                elif (ocls == "window") != (ncls == "window"):
                    cat = "window_mismatch"
                else:
                    cat = "status_word_diff"
            counts[cat] += 1
            diffs.append(_detail(cat, phone, day, old, new, old_map, new_map))
    return counts, diffs


def _load_mapping(path):
    """--mapping 文件 → (old 词表, new 词表)。类名不在词表内即用法错误。

    形状：{"old": {词: 类}, "new": {result 词: 类}}；两键都可缺省。
    new 表只在 state=skipped 分支生效（其余分支的类由 state 结构性决定）。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("mapping 根必须是对象")
    tables = []
    for side in ("old", "new"):
        table = data.get(side) or {}
        if not isinstance(table, dict):
            raise ValueError(f"mapping.{side} 必须是对象")
        for word, cls in table.items():
            if cls not in _CLASSES:
                raise ValueError(
                    f"mapping.{side}[{word!r}] 的类 {cls!r} 不在词表内")
        tables.append({str(k).strip(): v for k, v in table.items()})
    return tuple(tables)


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as e:
                raise ValueError(f"{path} 第 {i} 行不是合法 JSON：{e}") from e
    return rows


def _write_json(path, counts, diffs):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"counts": counts, "diffs": diffs}, f,
                  ensure_ascii=False, indent=1)
        f.write("\n")


def _write_csv(path, diffs):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(("category", "phone", "day",
                         "old_class", "old_status", "old_task_state",
                         "old_task_result", "account_status",
                         "new_class", "new_state", "new_result"))
        for d in diffs:
            o, n = d["old"] or {}, d["new"] or {}
            writer.writerow((d["category"], d["phone"], d["day"],
                             o.get("class", ""), o.get("status", ""),
                             o.get("task_state", ""), o.get("task_result", ""),
                             o.get("account_status", ""),
                             n.get("class", ""), n.get("state", ""),
                             n.get("result", "")))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="影子运行对账：旧栈与新栈按账号逐日终态对比（输出零手机号明文）")
    parser.add_argument("--old", required=True, help="旧侧终态 JSONL（必填）")
    parser.add_argument("--new", required=True, help="新侧终态 JSONL（必填）")
    parser.add_argument("--day", default="", help="只对比该日（YYYY-MM-DD，缺省全量）")
    parser.add_argument("--json", default="", help="明细 JSON 输出路径（0600）")
    parser.add_argument("--csv", default="", help="明细 CSV 输出路径（0600）")
    parser.add_argument(
        "--mapping", default="",
        help='等价类补充映射 JSON：{"old": {词: 类}, "new": {result 词: 类}}；'
             "缺省词表已覆盖两侧全部在册状态词，只用于新增词")
    args = parser.parse_args(argv)
    try:
        old_map, new_map = (_load_mapping(args.mapping)
                            if args.mapping else ({}, {}))
        old_rows = _load_jsonl(args.old)
        new_rows = _load_jsonl(args.new)
    except (OSError, ValueError) as e:
        print(f"拒绝执行：{e}", file=sys.stderr)
        return 2
    if args.day:
        old_rows = [r for r in old_rows if str(r.get("day")) == args.day]
        new_rows = [r for r in new_rows if str(r.get("day")) == args.day]
    try:
        counts, diffs = _compare(old_rows, new_rows, old_map, new_map)
    except ValueError as e:
        print(f"拒绝执行：{e}", file=sys.stderr)
        return 2
    if args.json:
        _write_json(args.json, counts, diffs)
    if args.csv:
        _write_csv(args.csv, diffs)
    head = " ".join(f"{k}={v}" for k, v in counts.items())
    print(f"影子对账完成：{head}；差异 {len(diffs)} 条")
    for d in diffs:
        o, n = d["old"] or {}, d["new"] or {}
        print(f"  [{d['category']}] {d['phone']} {d['day']}"
              f"  old={o.get('class', '-')}/{o.get('status', '-')}"
              f"  new={n.get('class', '-')}/{n.get('result', '-')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
