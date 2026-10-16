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
import json
import os
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
   "CI runner 上靶场跑不起来（真登录 0 次）：同族 4 条全过、串行独占仍复现、本机全绿 ⇒ 差异在 runner 环境，未定论。见工单 zdvd。"
  ],
  [
   "skipUnless",
   "SIGKILL 与 /proc 查进程环境仅 POSIX 可用"
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
