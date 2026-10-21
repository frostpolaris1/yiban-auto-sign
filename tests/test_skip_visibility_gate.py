# -*- coding: utf-8 -*-
"""跳过显性化门：跳过面必须有门，"跳过 ≠ 绿"（AGENTS.md §十三.30；140 §六-14）。

标签：A · 判据：覆盖显性化

**本门挡的缺陷形状**：一条检查因环境不满足而**跳过**，整套却没跑却报绿。
`pyproject.toml` 的 `addopts = "-ra --tb=short"` 只让 skip 在**输出里可见**，它
**不阻止**"整套没跑却报绿"——可见 ≠ 有门。实测（140 §五 S13）：全仓 **144 个真实
skip 点、53 文件、350 条用例、23 族**，其中 6 族**在任何一轨都不跑**（本地权威 root
轨实测亦 `SKIPPED`），而它们挡的是部署归属、状态目录不可写、锁目录属主、生产机防呆、
真 SIGKILL 收场这类**要紧**的性质。

**本门钉的性质（两条）**：
  ① **跳过面不得静默变化**：全仓每个 `(文件, 跳过标记, 理由)` 三元组都必须在
     `KNOWN_SKIP_SITES` 里**显式登记**；新增一处、删掉一处、改一次理由，都要回来改这张表
     ——改表的人因此必须看一眼"这次跳过的是什么"。
  ② **永不运行的族必须显式登记**（`NEVER_RUNS_IN_CI`）：它们在本仓是**声明**，不是
     推断。登记必须带工单号与理由；族被修好（跳过点消失）后条目自动陈旧 ⇒ 红，
     逼人删条目。

**本门不挡什么（如实登记，别把它当万全）**：
  · 它**不**自动发现"新出现的一个永不运行的族"——判"某族在 CI 上到底跑没跑"需要
    运行期清单（`plo2` 的 runner 侧"本应运行的检查清单"），本门只看**静态跳过面**。
  · 它**不**判断某条跳过是否**正当**。它只保证"跳过是看得见的、被登记过的"。
  · 跳过理由是动态表达式时，本门登记为 `DYNAMIC`（理由本身仍可见：它在那条断言里）。
    它**不**要求理由必须是字面量——那会误伤 `raise unittest.SkipTest(msg)` 这类正当写法。

对应实现：tests/ 下全部用例文件（AST 扫描）；`pyproject.toml` 的 `addopts`。
关键断言：钉"跳过面逐枚登记、登记不许陈旧、永不运行的族在库内有名有姓"三条性质；
    不钉任何用例行为。
依赖：只读文件文本与 AST；不跑子进程、不触网、不改任何落点。整文件在本机执行，无 skip。
"""
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(BASE, "tests")

#: 跳过标记的识别词：`Call` 的末段名（`skipUnless(...)` / `SkipTest(...)` 等）。
_SKIP_MARKERS = frozenset({
    "skipUnless", "skipIf", "skip", "skipTest", "SkipTest", "importorskip",
})
#: 理由不可静态取到时占位（理由本身仍在断言里可见，故这不是"不可见"）。
DYNAMIC = "\x00DYNAMIC"

# ---------------------------------------------------------------------------
# 登记表（本仓跳过面的冻结名册）
# ---------------------------------------------------------------------------
#: `{文件: [[标记, 理由], ...]}`。由 `work/gen_skip_registry.py` 从现量树生成一次；
#: 之后**只准人来改**：任何增删改都要在这张表上落笔并给理由。
KNOWN_SKIP_SITES = json.loads(r"""
{
 "tests/test_account_ops_reentry.py": [
  [
   "skipUnless",
   "node 不可用：跳过账号写操作防重入的 JS 行为测试"
  ]
 ],
 "tests/test_audit_transaction.py": [
  [
   "skipUnless",
   "真占写锁重演练为 opt-in（要等满 9×busy_timeout≈135s，占全量墙钟 92%）：设 YIBAN_DRILL_REAL_DB_LOCK=1 按需运行"
  ]
 ],
 "tests/test_backup_db_path.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipIf",
   "需要 sqlite3 命令（一致性快照与 integrity_check 判据）"
  ]
 ],
 "tests/test_backup_docker_hardening.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ]
 ],
 "tests/test_backup_e2e.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "需要 find"
  ],
  [
   "skipTest",
   "需要系统 gpg"
  ]
 ],
 "tests/test_backup_flag_parsing.py": [
  [
   "skipTest",
   "无 bash 环境"
  ]
 ],
 "tests/test_backup_hardening.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipIf",
   "需要 sqlite3"
  ]
 ],
 "tests/test_backup_require_encrypt.py": [
  [
   "skipTest",
   "无 bash 环境"
  ]
 ],
 "tests/test_backup_sentinel.py": [
  [
   "SkipTest",
   "需要 bash 与 python3（Git Bash/WSL）"
  ]
 ],
 "tests/test_calendar_state_visibility.py": [
  [
   "skipUnless",
   "node 不可用：跳过前端状态行/日期格的 JS 行为用例"
  ]
 ],
 "tests/test_claims_reap_e2e.py": [
  [
   "skipUnless",
   "SIGKILL 模拟崩溃仅 POSIX 可用"
  ]
 ],
 "tests/test_config_loader.py": [
  [
   "skipTest",
   "\u0000DYNAMIC"
  ]
 ],
 "tests/test_config_registry_gate.py": [
  [
   "skipTest",
   "文件系统不接受非法 UTF-8 名（本用例面向 POSIX 检出）"
  ],
  [
   "skipUnless",
   "本用例需要 git CLI（门按跟踪集判生产面）"
  ]
 ],
 "tests/test_dashboard_stats_caliber_js.py": [
  [
   "skipUnless",
   "node 不可用：跳过数据看板双口径聚合的 JS 行为测试"
  ]
 ],
 "tests/test_db_integrity.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "需要 rsync 或 scp"
  ]
 ],
 "tests/test_delay_ack_frontend.py": [
  [
   "skipUnless",
   "node 不可用：跳过受门禁提交的前端行为测试"
  ],
  [
   "skipUnless",
   "node 不可用：跳过清空收件人的前端行为测试"
  ]
 ],
 "tests/test_deploy_deps_sync.py": [
  [
   "skipIf",
   "需要 bash"
  ]
 ],
 "tests/test_deploy_gate_wiring.py": [
  [
   "skipIf",
   "bash 不在 PATH：仅 bash -n 语法检查那条跳过（CI/WSL 有 bash）"
  ],
  [
   "skipIf",
   "bash 不在 PATH：口令交接判据跳过（CI/WSL 有 bash）"
  ],
  [
   "skipIf",
   "bash 不在 PATH：两代镜像灌数脚本的活体模拟跳过（CI/WSL 有 bash）"
  ]
 ],
 "tests/test_deploy_paths_and_restore_verdict.py": [
  [
   "skipIf",
   "需要 bash"
  ],
  [
   "skipTest",
   "\u0000DYNAMIC"
  ],
  [
   "skipTest",
   "/opt 不可写，无法模拟生产机特征"
  ],
  [
   "skipUnless",
   "生产机防呆判定依赖 uname -s=Linux"
  ]
 ],
 "tests/test_deploy_prod_artifacts.py": [
  [
   "skipIf",
   "需要 bash"
  ],
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "\u0000DYNAMIC"
  ],
  [
   "skipTest",
   "宿主文件系统不保留 POSIX mode（Windows 开发机）"
  ],
  [
   "skipTest",
   "当前是 root 而宿主没有 setpriv：造不出非 root 位格"
  ],
  [
   "skipTest",
   "需要 git"
  ],
  [
   "skipTest",
   "需要 git 取暂存检出的名册"
  ],
  [
   "skipTest",
   "需要 root（WSL）+ 保留 POSIX mode 的文件系统"
  ],
  [
   "skipTest",
   "需要以 root 运行（WSL）才能触发 M01 前置门"
  ],
  [
   "skipTest",
   "需要系统 gpg"
  ]
 ],
 "tests/test_dev_verify_entry.py": [
  [
   "skipTest",
   "嵌套运行：跳过拉起入口脚本的用例"
  ],
  [
   "skipUnless",
   "--ci 在 Windows 侧被上游拒绝（信息不同），只在 POSIX 侧验证"
  ]
 ],
 "tests/test_drill_redline_e2e.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "宿主不允许建符号链接"
  ],
  [
   "skipTest",
   "宿主文件系统不保留 POSIX mode（C2 断言 0600，无法实跑）"
  ]
 ],
 "tests/test_env_export_line_divergence.py": [
  [
   "skipTest",
   "无 bash 环境"
  ]
 ],
 "tests/test_env_line_break_frontend.py": [
  [
   "skipUnless",
   "需要 node 真跑前端纯函数（取不到则整类 skip）"
  ]
 ],
 "tests/test_fallback_gates.py": [
  [
   "skipTest",
   "跨进程 flock 仅 POSIX 可用（Windows 上取不到锁）"
  ],
  [
   "skipTest",
   "跨进程 flock 仅 POSIX 可用（Windows 上探测退回 False）"
  ]
 ],
 "tests/test_global_pause_chain_e2e.py": [
  [
   "skipIf",
   "持锁构造需要 fcntl 锁后端（Windows 腿走同一判码但无从构造忙态）"
  ],
  [
   "skipUnless",
   "全链用例需要 bash（Git Bash 即可）"
  ]
 ],
 "tests/test_live_claim_cross_process_contention_e2e.py": [
  [
   "skipUnless",
   "真 SIGKILL 与独立进程争用仅 POSIX 可用"
  ]
 ],
 "tests/test_locks.py": [
  [
   "skipUnless",
   "跨进程 flock 仅 POSIX 可用；Windows 退化为进程内锁"
  ],
  [
   "skipUnless",
   "跨进程 flock 断言仅 POSIX 可用"
  ]
 ],
 "tests/test_logs_by_date.py": [
  [
   "skipTest",
   "宿主 ICU 不认 Etc/GMT±N 时区名，无法在本机重放该时区"
  ],
  [
   "skipTest",
   "宿主未同时认下两个时区，无法比对时区无关性"
  ],
  [
   "skipUnless",
   "node 不可用：跳过日志页日期校验的 JS 行为测试"
  ]
 ],
 "tests/test_migrations_fail_closed.py": [
  [
   "skipIf",
   "root 无视权限位，chmod 反例不成立"
  ]
 ],
 "tests/test_multi_executor_drill_e2e.py": [
  [
   "skipTest",
   "真因（账号集没铺满执行体）已修；本挂起只为不推高 CI 墙钟（i9gl）而留，删本 if ＋ 名册条目即恢复。见工单 zdvd。"
  ],
  [
   "skipUnless",
   "SIGKILL 与 /proc 查进程环境仅 POSIX 可用"
  ],
  [
   "skipUnless",
   "真监督进程与执行体子进程仅 POSIX 可用"
  ]
 ],
 "tests/test_notify_ledger.py": [
  [
   "mark.skipif",
   "本平台无跨进程文件锁（fcntl/msvcrt 均不可用），无法验证文件锁串行化"
  ]
 ],
 "tests/test_only_single_process.py": [
  [
   "skipUnless",
   "进程组 kill 仅 POSIX 有语义"
  ]
 ],
 "tests/test_path_env_read_gate.py": [
  [
   "skipUnless",
   "需要 bash（Git Bash / WSL）"
  ]
 ],
 "tests/test_path_single_definition.py": [
  [
   "skipIf",
   "需要 bash（Git Bash / WSL）"
  ]
 ],
 "tests/test_pause_flag_truthiness_e2e.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "需要 bash（Git Bash/WSL）"
  ]
 ],
 "tests/test_pr_template_gate.py": [
  [
   "skipUnless",
   "git 不可用：跳过 PR 模板守卫"
  ]
 ],
 "tests/test_run_entry_hardening.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ]
 ],
 "tests/test_run_sh_logging.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "pty 仅 POSIX"
  ]
 ],
 "tests/test_run_sh_stderr_timestamps.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ]
 ],
 "tests/test_run_sh_workers.py": [
  [
   "skipIf",
   "root 无视权限位"
  ],
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "\u0000DYNAMIC"
  ],
  [
   "skipTest",
   "当前文件系统不强制读权限位"
  ]
 ],
 "tests/test_runsh_env_parse.py": [
  [
   "skipTest",
   "无 bash 环境"
  ]
 ],
 "tests/test_runtime_data_layout.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "本机没有单元文件声明的服务账号（install -o 会失败）"
  ],
  [
   "skipTest",
   "需要 root（WSL）+ 保留 POSIX mode 的文件系统"
  ],
  [
   "skipTest",
   "需要 setpriv 造非 root 位格"
  ]
 ],
 "tests/test_schedule_edge_limit_js.py": [
  [
   "skipUnless",
   "node 不可用：跳过滑块量程的 JS 行为测试"
  ]
 ],
 "tests/test_schedule_retry.py": [
  [
   "SkipTest",
   "无 bash 环境"
  ]
 ],
 "tests/test_scheduler_exception_surface.py": [
  [
   "SkipTest",
   "无 bash 环境"
  ],
  [
   "skipTest",
   "宿主不保留 POSIX mode（Windows 开发机）"
  ],
  [
   "skipTest",
   "宿主不保留 POSIX 属主/权限（Windows 开发机）"
  ],
  [
   "skipTest",
   "需要 root（WSL）才能构造属主为他人的目录"
  ]
 ],
 "tests/test_scheduler_gate.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipTest",
   "宿主文件系统不保留 POSIX mode（Windows 开发机）"
  ]
 ],
 "tests/test_scripts_gate_hardening.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ],
  [
   "skipIf",
   "需要 git 判断忽略规则"
  ]
 ],
 "tests/test_second_round_rc_contract.py": [
  [
   "SkipTest",
   "真信号杀演练为 opt-in（重演练、真实时序，曾 TZ=UTC 偶发红）：设 YIBAN_DRILL_REAL_SIGKILL=1 按需运行"
  ],
  [
   "SkipTest",
   "真信号杀演练需要 POSIX SIGKILL"
  ],
  [
   "SkipTest",
   "需要 bash（Git Bash/WSL）"
  ]
 ],
 "tests/test_sentinel_db_readonly.py": [
  [
   "SkipTest",
   "需要 bash 与 python3（Git Bash/WSL）"
  ]
 ],
 "tests/test_shared_facts_gate.py": [
  [
   "skipUnless",
   "需要 bash（Git Bash / WSL）"
  ],
  [
   "skipUnless",
   "需要 bash 与 git（Git Bash / WSL）"
  ]
 ],
 "tests/test_skip_visibility_gate.py": [
  [
   "skipUnless",
   "需要 bash（Git Bash / WSL）"
  ]
 ],
 "tests/test_sign_round_guards.py": [
  [
   "skipTest",
   "本平台无 fcntl：运行锁探测走降级路径，无此判据"
  ],
  [
   "skipTest",
   "本平台无 fcntl：运行锁探测走降级路径，无此拒因"
  ]
 ],
 "tests/test_smtp_identity_frontend.py": [
  [
   "skipUnless",
   "node 不可用：跳过 SMTP 行身份的前端行为测试"
  ],
  [
   "skipUnless",
   "node 不可用：跳过额度显示口径的前端行为测试"
  ]
 ],
 "tests/test_state_file_writes.py": [
  [
   "skipUnless",
   "umask 与 POSIX 模式位在 Windows 上不生效"
  ]
 ],
 "tests/test_time_field_norm.py": [
  [
   "skipUnless",
   "node 不可用：跳过时间字段范围校验的 JS 行为测试"
  ]
 ],
 "tests/test_users_exit_surface_frontend.py": [
  [
   "skipUnless",
   "node 不可用：跳过 Task 2-9b 前端出口真跑"
  ]
 ],
 "tests/test_vue_build_reproducible.py": [
  [
   "skipUnless",
   "需要 bash（Git Bash / WSL）"
  ]
 ],
 "tests/test_web_mask_email_parity.py": [
  [
   "skipUnless",
   "node 不可用：跳过前后端邮箱脱敏口径对拍"
  ]
 ],
 "tests/test_web_rate_limit_concurrency_e2e.py": [
  [
   "skipUnless",
   "真 HTTP 服务 + gunicorn 仅 POSIX 且装了 gunicorn 时可用"
  ]
 ],
 "tests/test_web_vue_sources_tracked.py": [
  [
   "skipUnless",
   "git 不可用：跳过 Vue 源码入库守卫"
  ]
 ],
 "tests/test_work_accounts_selection.py": [
  [
   "skipUnless",
   "node 不可用：跳过账号页选中身份键的 JS 行为测试"
  ]
 ],
 "tests/test_workers_domain_single_source.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ]
 ],
 "tests/test_yiban_fallback_sh.py": [
  [
   "skipIf",
   "需要 bash（Git Bash/WSL）"
  ]
 ]
}
""")

#: 在任何一轨 CI 上都不跑的族（140 §五 S13 的 A 族名册）。值是 (族名, 理由, 工单)。
#: 这些是**声明**：把"某族在 CI 上恒跳"这一事实留在库内（普查稿是过程稿、不入库，
#: 只靠它登记会让事实随文档消失）。
NEVER_RUNS_IN_CI = {
    "tests/test_deploy_prod_artifacts.py": (
        "M01 部署归属门族（install.sh 拒 root 执行可写检出）",
        "三条 pytest 轨全在 ubuntu-latest 非 root；实测 uid1000 下 chattr/chown/写 /opt "
        "全 EPERM ⇒ 族族恒跳",
        "1ffr",
    ),
    "tests/test_run_sh_workers.py": (
        "状态目录不可写 ⇒ run.sh 拒运行族",
        "靠 chattr +i 注入；非 root 下 EPERM ⇒ 恒跳",
        "1ffr",
    ),
    "tests/test_scheduler_exception_surface.py": (
        "锁目录属主为他人 ⇒ run.sh 在 flock 之前拒运行族",
        "靠 chown 注入；非 root 下 EPERM ⇒ 恒跳",
        "1ffr",
    ),
    "tests/test_deploy_paths_and_restore_verdict.py": (
        "pull-prod-backup 生产机防呆族",
        "判据含 /opt 特征；CI runner 上不成立 ⇒ 恒跳",
        "1ffr",
    ),
    "tests/test_second_round_rc_contract.py": (
        "真 SIGKILL 整链收场族",
        "opt-in（默认不跑）；文件自称主证据 ⇒ 默认轨上等于没有",
        "plo2",
    ),
}


def _dotted(node):
    """把 `a.b.c` 形式的属性访问还原成点串。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _reason(node):
    """取跳过理由：第一个字符串常量实参；取不到回 `DYNAMIC`。"""
    for a in list(node.args) + [k.value for k in node.keywords]:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    return DYNAMIC


def scan_source(src):
    """返回该源码里的跳过点集合 `{(标记, 理由)}`。"""
    out = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        tail = dotted.split(".")[-1]
        if dotted.startswith(("pytest.mark.", "mark.")) and tail in ("skip", "skipif"):
            out.add(("mark." + tail, _reason(node)))
        elif tail in _SKIP_MARKERS and not dotted.startswith(("pytest.mark.", "mark.")):
            out.add((tail, _reason(node)))
    return out


def scan_tests():
    """扫 `tests/` 下全部用例件，返回 `({相对路径: {(标记, 理由)}}, 解析失败文件)`。

    第二个返回值供 **fail-closed** 用：解析不了的文件**不许静默跳过**——跳过等于把它的
    跳过点从判定面里悄悄摘掉（AGENTS.md §十三.29"门看的东西 ＝ 实际改的东西吗"）。
    调用方必须断言它为空。
    """
    found, unparsed = {}, []
    for dirpath, _dirs, files in os.walk(TESTS_DIR):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, BASE).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                sites = scan_source(src)
            except (SyntaxError, UnicodeDecodeError):
                unparsed.append(rel)
                continue
            if sites:
                found[rel] = sites
    return found, unparsed


def _as_sets(registry):
    return {rel: {tuple(x) for x in sites} for rel, sites in registry.items()}


class SkipSurfaceRegisteredTest(unittest.TestCase):
    """跳过面逐枚登记，且登记不许陈旧。"""

    def test_无未登记的跳过点(self):
        found, unparsed = scan_tests()
        self.assertEqual(unparsed, [],
                         "这些用例件解析不了，跳过面判定已静默缩小（漏扫＝门有洞）：%s"
                         % unparsed)
        known = _as_sets(KNOWN_SKIP_SITES)
        unregistered = {}
        for rel, sites in found.items():
            extra = sites - known.get(rel, set())
            if extra:
                unregistered[rel] = sorted(extra)
        self.assertEqual(
            unregistered, {},
            "这些跳过点没有登记进 `KNOWN_SKIP_SITES`：%s。\n"
            "跳过不是绿（AGENTS.md §十三.30）。请在此表登记，并给出一条理由说明"
            "'这条跳过的是什么、为什么可以跳'；若它本不该跳，就修它。" % unregistered)

    def test_名册无陈旧条目(self):
        found, unparsed = scan_tests()
        self.assertEqual(unparsed, [],
                         "这些用例件解析不了，跳过面判定已静默缩小（漏扫＝门有洞）：%s"
                         % unparsed)
        known = _as_sets(KNOWN_SKIP_SITES)
        stale = {}
        for rel, sites in known.items():
            gone = sites - found.get(rel, set())
            if gone:
                stale[rel] = sorted(gone)
        self.assertEqual(
            stale, {},
            "`KNOWN_SKIP_SITES` 里有条目已不再命中（跳过点被删/理由被改）：%s。\n"
            "陈旧的名册会让这道门只看一半，必须同批订正。" % stale)

    def test_扫空不得当绿(self):
        found, unparsed = scan_tests()
        self.assertEqual(unparsed, [], "这些用例件解析不了：%s" % unparsed)
        total = sum(len(v) for v in found.values())
        self.assertGreater(
            total, 0,
            "一个跳过点都没扫到：判定面已失效（本仓现量有近百个），门在空转")
        self.assertGreater(len(KNOWN_SKIP_SITES), 0, "登记表为空：门在空转")


class NeverRunsRegisteredTest(unittest.TestCase):
    """在任何一轨都不跑的族，必须在库内有名有姓。"""

    def test_永不运行的族仍存在且仍带跳过点(self):
        found, unparsed = scan_tests()
        self.assertEqual(unparsed, [], "这些用例件解析不了：%s" % unparsed)
        for rel, (family, why, ticket) in sorted(NEVER_RUNS_IN_CI.items()):
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                            "登记为'永不运行'的族 %s 已不存在——条目要删" % rel)
            self.assertTrue(
                found.get(rel),
                "%s 已不再有任何跳过点，说明这族已经跑得起来了：请把它从 "
                "`NEVER_RUNS_IN_CI` 删掉（族名 %r）。留着陈旧声明会让"
                "'哪些门其实没跑'这张表失真" % (rel, family))
            self.assertTrue(ticket, "%s 的登记缺工单号" % rel)
            self.assertTrue(why, "%s 的登记缺理由" % rel)

    def test_永不运行的族也必须在跳过面名册里(self):
        known = _as_sets(KNOWN_SKIP_SITES)
        missing = sorted(set(NEVER_RUNS_IN_CI) - set(known))
        self.assertEqual(
            missing, [],
            "这些族登记为'永不运行'，却不在跳过面名册 `KNOWN_SKIP_SITES` 里：%s"
            "（两张表必须自洽）" % missing)

    def test_本批修好的族不得留在永不运行表里(self):
        """`test_web_font_closure.py` 本批已改成"缺 fonttools 时挂替身包照跑"。

        它原先在任何一轨都不跑（本地权威 root 轨实测 `SKIPPED [3]`），而它挡的是
        "位置参数 outdir 被整目录 rmtree"这类删除事故。修好后它既不该在
        `NEVER_RUNS_IN_CI` 里，也不该再有跳过点——这条正面钉住"修好就是修好"。
        """
        rel = "tests/test_web_font_closure.py"
        self.assertNotIn(rel, NEVER_RUNS_IN_CI, "该族已修好，不该再登记为永不运行")
        self.assertNotIn(
            rel, scan_tests()[0],
            "该族不该再有跳过点：它现在缺 fonttools 时挂替身包，任何轨都真的跑")


ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")


def _func_source(text, name):
    """切出 bash 函数完整定义（列 1 的 `name() {` 起、到列 1 的 `}`）。空串=没抽到。"""
    m = re.search(r"^%s\(\) \{\s*(?:#.*)?$" % re.escape(name), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^\}$", rest, re.M)
    return "" if end is None else "%s() {\n%s\n}\n" % (name, rest[:end.start()])


def _bash_array_items(text, name):
    """抽出 `name=(...)` 的条目（去引号、跳过注释/空行）。空列表=没抽到。"""
    m = re.search(r"^%s=\(\s*$" % re.escape(name), text, re.M)
    if m is None:
        return []
    rest = text[m.end():]
    end = re.search(r"^\)\s*$", rest, re.M)
    if end is None:
        return []
    out = []
    for line in rest[:end.start()].splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.strip('"').strip("'"))
    return out


@unittest.skipUnless(shutil.which("bash"), "需要 bash（Git Bash / WSL）")
class RuntimeSkipPolicyTest(unittest.TestCase):
    """工单 1ffr：运行期跳过必须**点名**，安全类跳过不得静默计入绿。

    静态门 `SkipSurfaceRegisteredTest` 只看"源码里有几个跳过点"；它回答不了
    "这一轮**实际**跳过了哪些用例"。本类钉运行期那一半：
      ① 汇总必须列出跳过的 classname / 理由（最多 5 条 + 总数）；
      ② `--ci`/全量模式下未登记的跳过判红，登记过的（NEVER_RUNS_IN_CI 族与
         opt-in 平台条件）显式豁免；
      ③ 豁免表与 `NEVER_RUNS_IN_CI` 对账——两张表各说各话时红。
    """

    def setUp(self):
        self.text = open(ENTRY, encoding="utf-8").read()
        self.paths = _bash_array_items(self.text, "SKIP_EXEMPT_PATHS")
        self.reasons = _bash_array_items(self.text, "SKIP_EXEMPT_REASONS")

    def _run_policy(self, xml, strict):
        fn = _func_source(self.text, "skip_policy_check")
        self.assertIn("skip_policy_check() {", fn, "抽不出 skip_policy_check")
        arr = ["SKIP_EXEMPT_PATHS=("] + ["    '%s'" % p for p in self.paths] + [")"]
        arr += ["SKIP_EXEMPT_REASONS=("] + ["    '%s'" % r for r in self.reasons] + [")"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.xml")
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                # 真实 pytest 报告有 testsuites/testsuite 根；这里补上同一形状，
                # 否则多个 testcase 会构成非法 XML（解析器拒绝，判据测不到）。
                f.write("<testsuites><testsuite>%s</testsuite></testsuites>\n" % xml)
            script = "\n".join(arr) + "\n" + fn + '\nskip_policy_check "$1" "$2" "$3"\n'
            return subprocess.run([shutil.which("bash"), "-c", script, "x", path,
                                   "1" if strict else "0", sys.executable],
                                  capture_output=True, text=True, timeout=60)

    @staticmethod
    def _case(classname, reason):
        return ('<testcase classname="%s" name="t" time="0.1">'
                '<skipped type="pytest.skip" message="%s"/></testcase>\n'
                % (classname, reason))

    def test_exemptions_cover_every_never_running_family(self):
        missing = sorted(set(NEVER_RUNS_IN_CI) - set(self.paths))
        self.assertEqual(
            missing, [],
            "这些族登记为'永不运行'，但 dev-verify 的运行期豁免表没有它们：%s\n"
            "运行期策略会把它们判红（或反之静默放过）——两张表必须自洽" % missing)
        self.assertTrue(self.paths, "SKIP_EXEMPT_PATHS 为空：豁免表没抽到或已删")

    def test_unregistered_skip_is_named_and_fails_in_strict_mode(self):
        xml = self._case("tests.test_whatever.Cls", "环境不满足，悄悄跳过")
        r = self._run_policy(xml, strict=True)
        self.assertNotEqual(r.returncode, 0, "未登记的跳过在全量/--ci 必须判红")
        out = r.stdout + r.stderr
        self.assertIn("tests.test_whatever.Cls", out, "必须点名跳过的用例")
        self.assertIn("环境不满足，悄悄跳过", out, "必须点名跳过理由")
        non_strict = self._run_policy(xml, strict=False)
        self.assertEqual(non_strict.returncode, 0,
                         "fast 档只要求可见，不判红（它不是门禁）")

    def test_registered_skip_is_exempt(self):
        for py in sorted(NEVER_RUNS_IN_CI):
            cls = py[:-3].replace("/", ".")
            with self.subTest(py=py):
                r = self._run_policy(self._case(cls + ".Cls", "已登记的平台条件"), True)
                self.assertEqual(r.returncode, 0,
                                 "已登记的族不得判红：%s\n%s" % (py, r.stdout))
        r = self._run_policy(
            self._case("_pytest.unittest", "真信号杀演练为 opt-in：设 X=1 按需运行"), True)
        self.assertEqual(r.returncode, 0, "按理由登记的 opt-in 跳过必须豁免：\n" + r.stdout)

    def test_the_summary_lists_at_most_five_names_plus_total(self):
        xml = "".join(self._case("tests.test_whatever.C%d" % i, "r%d" % i)
                      for i in range(8))
        r = self._run_policy(xml, strict=False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("total=8", r.stdout, "汇总必须给总数")
        self.assertIn("另 3 条", r.stdout, "超过 5 条时只列表并报剩余数")
        self.assertEqual(r.stdout.count("  - tests.test_whatever."), 5,
                         "最多列 5 条")

    def test_missing_report_is_not_green_in_strict_mode(self):
        """报告缺失 = 跳过面不可见（§30）：strict 档必须红，不许当"没有跳过"。"""
        fn = _func_source(self.text, "skip_policy_check")
        script = "SKIP_EXEMPT_PATHS=()\nSKIP_EXEMPT_REASONS=()\n" + fn + \
                 '\nskip_policy_check /nonexistent/report.xml 1\n'
        r = subprocess.run([shutil.which("bash"), "-c", script],
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, "报告缺失在 strict 档必须判红")

    def test_both_modes_write_a_skip_report(self):
        """接线：全量/fast 与 --ci 的 pytest 都必须产出可解析的跳过报告。"""
        self.assertIn('--junitxml="$SKIP_XML"', self.text,
                      "全量/fast 的 pytest 没有产出跳过报告")
        self.assertIn('SKIP_XML=$(mktemp)', self.text,
                      "全量/fast 的报告路径必须每次新建（不落仓库）")
        m = re.search(r"^run_ci\(\) \{(.*?)^\}", self.text, re.M | re.S)
        self.assertIsNotNone(m, "抽不出 run_ci")
        self.assertIn('--junitxml="$CI_SKIP_XML"', m.group(1),
                      "--ci 的快集也必须产出跳过报告")
        self.assertIn('skip_policy_check "$CI_SKIP_XML" 1 "$py"', m.group(1),
                      "--ci 必须对未登记跳过判红")


if __name__ == "__main__":
    unittest.main(verbosity=2)
