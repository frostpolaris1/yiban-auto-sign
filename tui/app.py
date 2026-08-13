#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""易班自动签到 TUI 配置工具（服务器端）。

SSH 登录服务器后执行 `yiban`（或 `python3 -m tui`）即可打开面板：

- 左侧：账号列表（序号 / 状态 / 名称 / 手机号 / 设备型号）
  - 序号决定顺序模式下的打卡顺序（[ ] 上下调整）
  - 状态图标：⏳ 准备签到（今日未签到） ✅ 签到成功 ❌ 签到失败（来自 sign.log）
- 右上：签到日志（简化展示最近记录，自动刷新）
- 右下：设置区
  - 随机延迟开关（启动延迟 / 账号间隔，写入 .env）
  - 连通性检测（不登录，仅检查易班 API 可达性）
  - 服务器时间与签到状态

快捷键：A 添加  E 编辑  D 删除  [ ] 上下移  S 保存  Q 退出
"""

import argparse
import contextlib
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import ClassVar

import requests
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

# 共享模块（tui/ 与 scripts/ 同级）：加密模块 + SQLite 数据访问层
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import account_crypto  # noqa: E402
import db  # noqa: E402

# 默认路径（与 signin.py / run.sh 保持一致，可用参数覆盖）
ACCOUNTS_DEFAULT = os.environ.get("YIBAN_ACCOUNTS_FILE", "accounts.json")
LOG_DEFAULT = os.environ.get("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
ENV_DEFAULT = os.environ.get("YIBAN_ENV_FILE", ".env")

# 状态图标
ICON_PENDING = "⏳"  # 准备签到（今日未签到）
ICON_SUCCESS = "✅"  # 今日签到成功
ICON_FAILED = "❌"  # 今日签到失败（最终放弃）
ICON_RETRYING = "🔄"  # 重试中（队列重试放回队尾）
ICON_SKIPPED = "➖"  # 跳过（未在签到时间窗口等，非失败）

# 签到时间窗口（默认 06:30-07:50，与项目早操签到窗口一致；学校不同可修改）
SIGN_START = (6, 30)
SIGN_END = (7, 50)

# 随机延迟默认上限（与 signin.py 保持一致）
DEFAULT_START_DELAY_MAX = 60
DEFAULT_ACCOUNT_GAP_MAX = 10

# 解析 sign.log（行格式: [2026-08-07 06:40:04] [INFO] yiban: [手机号] ✅ 签到成功）
SIGN_LOG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) [\d:]+\] \[(\w+)\] (\w+): (.*)")
STATE_RE = re.compile(r"\[(\d+)\]\s*(✅|❌|🔄|➖)")


def parse_sign_log(path):
    """解析签到日志：返回 (今日各账号状态 dict, 最近日志行列表)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    states = {}
    recent = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = SIGN_LOG_RE.match(line.strip())
                if not m:
                    continue
                date, level, logger_name, msg = m.groups()
                if logger_name != "yiban" or level == "DEBUG":
                    continue
                recent.append(line.strip())
                if date == today:
                    sm = STATE_RE.search(msg)
                    if sm:
                        states[sm.group(1)] = sm.group(2)
    except OSError:
        pass
    return states, recent[-15:]


def load_env_int(env_path, key, default):
    """读取 .env 中的整数配置，缺失/非法回退默认值。"""
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    try:
                        return int(line.split("=", 1)[1])
                    except ValueError:
                        return default
    except OSError:
        pass
    return default


def write_env_int(env_path, key, value):
    """把整数配置写入 .env：value<=0 删除该行，>0 写入；保留其他行。"""
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    out = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    if value > 0:
        out.append(f"{key}={value}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


class AccountForm(ModalScreen):
    """账号编辑表单：添加新账号或编辑已有账号。"""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "取消")]

    def __init__(self, account=None, index=None):
        super().__init__()
        self.account = account or {}
        self.index = index  # None=添加，数字=编辑第 index+1 个账号

    def compose(self) -> ComposeResult:
        acc = self.account
        title = "➕ 添加账号" if self.index is None else f"✏️ 编辑账号 #{self.index + 1}"
        yield Container(
            Label(title, id="form-title"),
            Input(
                placeholder="名称（可选，不填默认 账号N）",
                value=acc.get("name", ""),
                id="name",
                max_length=20,
            ),
            Input(
                placeholder="手机号（必填）", value=acc.get("phone", ""), id="phone", max_length=20
            ),
            Input(
                placeholder="密码（必填）",
                value=acc.get("password", ""),
                id="password",
                password=True,
            ),
            Input(
                placeholder="设备型号（可选，如 Vivo-XXXX）",
                value=acc.get("phone_model", ""),
                id="phone_model",
                max_length=50,
            ),
            Input(
                placeholder="设备识别码（可选）",
                value=acc.get("phone_code", ""),
                id="phone_code",
                max_length=100,
            ),
            Label("提示：填写全部字段后按 Enter 或点击保存；按 Esc 取消", id="form-hint"),
            Horizontal(
                Button("保存", variant="success", id="save"),
                Button("取消", variant="default", id="cancel"),
                id="form-buttons",
            ),
            id="form",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        """Esc 取消，不保存任何修改。"""
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """任意输入框回车即保存，简化键盘操作。"""
        if event.input.id in ("name", "phone", "password", "phone_model", "phone_code"):
            self._save()

    def _save(self) -> None:
        phone = self.query_one("#phone", Input).value.strip()
        password = self.query_one("#password", Input).value.strip()
        if not phone or not password:
            self.notify("手机号和密码为必填项", severity="error", timeout=3)
            return
        self.dismiss(
            {
                "name": self.query_one("#name", Input).value.strip(),
                "phone": phone,
                "password": password,
                "phone_model": self.query_one("#phone_model", Input).value.strip(),
                "phone_code": self.query_one("#phone_code", Input).value.strip(),
            }
        )


class YibanTuiApp(App):
    """易班自动签到 TUI 主应用。"""

    TITLE = "易班自动签到"
    SUB_TITLE = "账号配置 · 日志 · 设置"

    # ---- 现代化配色（Tokyo Night 风格）----
    CSS = """
    $background: #16161e;
    $surface: #24283b;
    $panel: #1a1b26;
    $primary: #7aa2f7;
    $accent: #7dcfff;
    $success: #9ece6a;
    $warning: #e0af68;
    $error: #f7768e;
    $text: #c0caf5;
    $text-muted: #565f89;

    #main-row {
        height: 1fr;
    }
    #left-panel {
        width: 55%;
        height: 1fr;
        border: round $surface;
        padding: 0 1;
    }
    #right-panel {
        width: 45%;
        height: 1fr;
    }
    #log-box-wrap {
        height: 1fr;
    }
    #log-box {
        height: 1fr;
        border: round $surface;
        padding: 0 1;
        overflow-y: auto;
    }
    #settings-box {
        height: auto;
        border: round $surface;
        padding: 0 1;
        margin-top: 1;
    }
    #settings-box Label.section {
        text-style: bold;
        color: $accent;
    }
    .set-row {
        height: 3;
        align: left middle;
    }
    #clock {
        color: $text-muted;
        width: auto;
    }
    #sign-status {
        margin-left: 2;
        width: auto;
    }
    #ping-result {
        width: 1fr;
    }
    #delay-btn, #gap-btn {
        width: auto;
    }
    #delay-secs, #gap-secs {
        width: 8;
    }
    #form {
        width: 64;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
        margin: 2 6;
    }
    #form-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #form-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #form-buttons {
        height: auto;
    }
    #form-buttons Button {
        margin-right: 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("a", "add", "添加"),
        Binding("e", "edit", "编辑"),
        Binding("d", "delete", "删除"),
        Binding("[", "move_up", "上移"),
        Binding("]", "move_down", "下移"),
        Binding("m", "manual_sign", "手动签到"),
        Binding("s", "save", "保存"),
        Binding("q", "quit", "退出"),
    ]

    def __init__(
        self,
        config_path: str = ACCOUNTS_DEFAULT,
        log_path: str = LOG_DEFAULT,
        env_path: str = ENV_DEFAULT,
    ):
        super().__init__()
        self.config_path = config_path
        self.log_path = log_path
        self.env_path = env_path
        self.accounts = []  # [{'name','phone','password','phone_model','phone_code'}]
        self._editing_row = None  # 编辑模式目标行（None=添加模式）
        self.start_delay_max = 0  # 启动随机延迟上限（0=关闭）
        self.gap_max = 0  # 顺序模式账号间隔上限（0=关闭）
        self.states = {}  # {phone: '✅'|'❌'} 今日签到状态
        self._clock_timer = None
        self._log_timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            Container(DataTable(id="table", cursor_type="row"), id="left-panel"),
            Vertical(
                Container(
                    Label("📋 签到日志（最近）", classes="section"),
                    Static("", id="log-box"),
                    id="log-box-wrap",
                ),
                Container(
                    Label("⚙️ 设置", classes="section"),
                    Horizontal(
                        Label("启动延迟:"),
                        Button("关闭", id="delay-btn"),
                        Input(placeholder="秒数", id="delay-secs", type="integer"),
                        classes="set-row",
                    ),
                    Horizontal(
                        Label("账号间隔:"),
                        Button("关闭", id="gap-btn"),
                        Input(placeholder="秒数", id="gap-secs", type="integer"),
                        classes="set-row",
                    ),
                    Horizontal(
                        Button("连通性检测", id="ping-btn"),
                        Static("", id="ping-result"),
                        classes="set-row",
                    ),
                    Horizontal(
                        Static("", id="clock"), Static("", id="sign-status"), classes="set-row"
                    ),
                    id="settings-box",
                ),
                id="right-panel",
            ),
            id="main-row",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(" #", "状态", "名称", "手机号", "设备型号")
        # 随机延迟初始化（来自 .env）
        self.start_delay_max = load_env_int(self.env_path, "YIBAN_START_DELAY_MAX", 0)
        self.gap_max = load_env_int(self.env_path, "YIBAN_ACCOUNT_GAP_MAX", 0)
        self._refresh_settings_ui()
        self._load()
        # 定时刷新：时钟 1s，日志 10s
        self._clock_timer = self.set_interval(1.0, self._refresh_clock)
        self._log_timer = self.set_interval(10.0, self._refresh_log)
        self._refresh_clock()
        self._refresh_log()

    # ---- 数据加载与展示 ----
    def _load(self) -> None:
        """从 SQLite 加载全部账号（db 层已解密为明文）；数据库异常时明确提示而非崩溃。"""
        try:
            db.init_db(env_file=self.env_path)
            self.accounts = db.load_accounts()
        except Exception as e:
            self.accounts = []
            self.notify(f"数据库读取失败: {e}", severity="error", timeout=6)
        self._refresh_table()

    def _display_name(self, acc, index):
        """显示名称：用户输入的名称，未填写则用默认名 账号N。"""
        return acc.get("name") or f"账号{index + 1}"

    def _status_icon(self, acc):
        """状态图标：⏳ 准备签到 / ✅ 成功 / ❌ 最终失败 / 🔄 重试中 / ➖ 跳过（来自今日 sign.log）。"""
        return self.states.get(acc.get("phone", ""), ICON_PENDING)

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        for idx, acc in enumerate(self.accounts):
            table.add_row(
                str(idx + 1),
                self._status_icon(acc),
                self._display_name(acc, idx),
                acc.get("phone", ""),
                acc.get("phone_model", ""),
            )
        self.sub_title = (
            f"共 {len(self.accounts)} 个账号 · 顺序执行（队列重试） · "
            f"{os.path.basename(db._db_file)}"
        )

    # ---- 签到日志 ----
    def _refresh_log(self) -> None:
        _, recent = parse_sign_log(self.log_path)
        self.states, _ = parse_sign_log(self.log_path)
        # 状态可能变化，刷新列表图标
        if self.accounts:
            self._refresh_table()
        text = "\n".join(recent) if recent else "（暂无签到日志，等待定时任务执行…）"
        self.query_one("#log-box", Static).update(text)

    # ---- 服务器时间与签到状态 ----
    def _sign_status(self, now=None):
        """基于服务器时间计算签到状态。

        返回 (显示文本, markup 颜色)：
        - ⏳ 未到签到时间（每日 0 点 ~ 窗口开始）
        - 🔔 签到窗口（窗口内，绿色高亮）
        - ✅ 打卡时间已过（窗口结束后）
        - 🌙 今日无需打卡（周日）
        """
        now = now or datetime.now()
        if now.weekday() == 6:  # 周日
            return "🌙 今日无需打卡（周日）", "#565f89"
        start = now.replace(hour=SIGN_START[0], minute=SIGN_START[1], second=0, microsecond=0)
        end = now.replace(hour=SIGN_END[0], minute=SIGN_END[1], second=0, microsecond=0)
        if now < start:
            return f"⏳ 未到签到时间（{SIGN_START[0]:02d}:{SIGN_START[1]:02d} 开始）", "#7aa2f7"
        if now <= end:
            return f"🔔 签到窗口（~{SIGN_END[0]:02d}:{SIGN_END[1]:02d} 结束）", "#9ece6a"
        return "✅ 打卡时间已过", "#e0af68"

    def _refresh_clock(self) -> None:
        self.query_one("#clock", Static).update(
            f"服务器时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        text, color = self._sign_status()
        self.query_one("#sign-status", Static).update(f"[{color}]{text}[/{color}]")

    # ---- 设置区（随机延迟）----
    def _refresh_settings_ui(self) -> None:
        """同步设置区控件状态（延迟开关与秒数）。"""
        # 启动延迟开关
        dbtn = self.query_one("#delay-btn", Button)
        dsecs = self.query_one("#delay-secs", Input)
        if self.start_delay_max > 0:
            dbtn.label = "🟢 开启"
            dbtn.variant = "primary"
            dsecs.value = str(self.start_delay_max)
        else:
            dbtn.label = "🔴 关闭"
            dbtn.variant = "default"
            dsecs.value = ""
            dsecs.placeholder = f"秒数（默认{DEFAULT_START_DELAY_MAX}）"
        # 账号间隔开关
        gbtn = self.query_one("#gap-btn", Button)
        gsecs = self.query_one("#gap-secs", Input)
        if self.gap_max > 0:
            gbtn.label = "🟢 开启"
            gbtn.variant = "primary"
            gsecs.value = str(self.gap_max)
        else:
            gbtn.label = "🔴 关闭"
            gbtn.variant = "default"
            gsecs.value = ""
            gsecs.placeholder = f"秒数（默认{DEFAULT_ACCOUNT_GAP_MAX}）"
        self.sub_title = (
            f"共 {len(self.accounts)} 个账号 · 顺序执行（队列重试） · "
            f"{os.path.basename(db._db_file)}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delay-btn":
            self._toggle_delay("start")
        elif event.button.id == "gap-btn":
            self._toggle_delay("gap")
        elif event.button.id == "ping-btn":
            self._check_connectivity()

    def _toggle_delay(self, which) -> None:
        """启动延迟 / 账号间隔 开关切换（开启时填默认上限秒数，可改）。"""
        if which == "start":
            if self.start_delay_max > 0:
                self.start_delay_max = 0
                msg = "启动延迟: 关闭"
            else:
                self.start_delay_max = DEFAULT_START_DELAY_MAX
                msg = f"启动延迟: 开启（0~{DEFAULT_START_DELAY_MAX} 秒随机）"
        else:
            if self.gap_max > 0:
                self.gap_max = 0
                msg = "账号间隔: 关闭"
            else:
                self.gap_max = DEFAULT_ACCOUNT_GAP_MAX
                msg = f"账号间隔: 开启（0~{DEFAULT_ACCOUNT_GAP_MAX} 秒随机）"
        self._refresh_settings_ui()
        self.notify(f"{msg}（按 S 保存生效）", timeout=3)

    # ---- 连通性检测（不登录，仅检查易班 API 可达性）----
    @work(exclusive=True)
    async def _check_connectivity(self) -> None:
        result = self.query_one("#ping-result", Static)
        result.update("检测中…")
        try:
            resp = requests.get(
                "https://api.uyiban.com/base/c/auth/yiban",
                timeout=6,
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
                },
            )
            ok = resp.status_code < 500
            detail = f"HTTP {resp.status_code}"
        except Exception as e:
            ok = False
            detail = str(e)[:60]
        result.update(f"{'✅ 易班 API 可达' if ok else '❌ 不可达'}（{detail}）")

    # ---- 保存 ----
    def action_save(self) -> None:
        """保存账号到 SQLite（整表替换，敏感字段 db 层加密落盘），并把随机延迟写入 .env。

        ⚠️ 整表替换语义：与 web 后台并发使用时以最后一次保存为准（勿同时编辑）。
        """
        # 1. 账号配置：整表替换（事务内清空重插，保留 sort_order=列表顺序）
        db.replace_accounts(self.accounts)
        db.audit("tui", "tui_save", "", f"整表保存 {len(self.accounts)} 个账号")
        # 2. 随机延迟（开启时读秒数输入框，非法值回退默认；关闭写 0 即删除该行）
        if self.start_delay_max > 0:
            try:
                self.start_delay_max = max(
                    1, int(self.query_one("#delay-secs", Input).value or DEFAULT_START_DELAY_MAX)
                )
            except ValueError:
                self.start_delay_max = DEFAULT_START_DELAY_MAX
        write_env_int(self.env_path, "YIBAN_START_DELAY_MAX", self.start_delay_max)
        if self.gap_max > 0:
            try:
                self.gap_max = max(
                    1, int(self.query_one("#gap-secs", Input).value or DEFAULT_ACCOUNT_GAP_MAX)
                )
            except ValueError:
                self.gap_max = DEFAULT_ACCOUNT_GAP_MAX
        write_env_int(self.env_path, "YIBAN_ACCOUNT_GAP_MAX", self.gap_max)
        self._refresh_settings_ui()
        self.notify(
            f"已保存 {len(self.accounts)} 个账号 → yiban.db；"
            f"延迟: {'开' if self.start_delay_max > 0 else '关'}"
            f" | 间隔: {'开' if self.gap_max > 0 else '关'}（.env）",
            severity="information",
            timeout=4,
        )

    # ---- 账号操作 ----
    def action_add(self) -> None:
        """添加账号：记录当前为添加模式，保存后追加到列表。"""
        self._editing_row = None
        self.push_screen(AccountForm(), callback=self._on_form_result)

    def action_edit(self) -> None:
        row = self._current_row()
        if row is None:
            self.notify("请先选中要编辑的账号（↑↓ 选择）", severity="warning", timeout=3)
            return
        self._editing_row = row
        self.push_screen(
            AccountForm(account=self.accounts[row], index=row), callback=self._on_form_result
        )

    def action_delete(self) -> None:
        row = self._current_row()
        if row is None:
            self.notify("请先选中要删除的账号（↑↓ 选择）", severity="warning", timeout=3)
            return
        removed = self.accounts.pop(row)
        self.notify(f"已删除账号 {removed.get('phone', '')}（按 S 保存生效）", timeout=3)
        self._refresh_table()

    def action_move_up(self) -> None:
        """上移选中账号：改变阅读顺序与顺序模式打卡顺序。"""
        row = self._current_row()
        if row is None or row == 0:
            return
        self.accounts[row], self.accounts[row - 1] = self.accounts[row - 1], self.accounts[row]
        self._refresh_table()
        self._select_row(row - 1)

    def action_move_down(self) -> None:
        """下移选中账号。"""
        row = self._current_row()
        if row is None or row >= len(self.accounts) - 1:
            return
        self.accounts[row], self.accounts[row + 1] = self.accounts[row + 1], self.accounts[row]
        self._refresh_table()
        self._select_row(row + 1)

    def _select_row(self, row) -> None:
        table = self.query_one("#table", DataTable)
        if 0 <= row < len(self.accounts):
            table.move_cursor(row=row)

    def _current_row(self):
        table = self.query_one("#table", DataTable)
        if not self.accounts or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self.accounts):
            return table.cursor_row
        return None

    # ---- 手动签到（M 键：对选中账号立即执行一次签到）----
    def action_manual_sign(self) -> None:
        """手动签到选中账号：以子进程运行 signin.py --only 手机号。

        日志写入与 cron 相同路径（sign.log），状态图标随日志 10s 刷新自动更新。
        独立子进程：TUI 崩溃不影响签到执行。
        """
        row = self._current_row()
        if row is None:
            self.notify("请先选中要签到的账号（↑↓ 选择）", severity="warning", timeout=3)
            return
        phone = self.accounts[row].get("phone", "")
        if not phone:
            return
        # 项目根目录（tui 的上一级）
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(base, "scripts", "signin.py")
        env = dict(os.environ)
        # 单账号手动签到：关闭随机延迟，避免等待
        env["YIBAN_START_DELAY_MAX"] = "0"
        env["YIBAN_ACCOUNT_GAP_MAX"] = "0"
        # 解密 accounts.json 需要同一密钥：显式注入（--env 自定义路径时保证一致）
        if account_crypto.has_key(self.env_path) and not env.get("YIBAN_ACCOUNTS_KEY"):
            env["YIBAN_ACCOUNTS_KEY"] = account_crypto.load_key(self.env_path).hex()
        # 日志输出重定向到与 cron 相同的 sign.log（追加、行缓冲），
        # 否则日志被 DEVNULL 丢弃，TUI 日志区与状态图标无法更新
        log_fh = None
        with contextlib.suppress(OSError):
            # 日志文件不可写时回退丢弃，不影响签到执行
            log_fh = open(self.log_path, "a", encoding="utf-8", buffering=1)
        try:
            subprocess.Popen(
                [sys.executable, script, "--only", phone],
                cwd=base,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            self.notify(f"手动签到启动失败: {e}", severity="error", timeout=4)
            return
        self.notify(f"已触发 {phone} 手动签到（后台执行，详见右侧日志）", timeout=3)

    def _on_form_result(self, data) -> None:
        """表单关闭回调：data=None 表示取消。"""
        if data is None:
            return
        if self._editing_row is not None:
            self.accounts[self._editing_row] = data  # 编辑已有账号
            self._editing_row = None
        else:
            self.accounts.append(data)  # 添加新账号
        self._refresh_table()
        self.notify(
            f"{data['name'] or '账号' + str(len(self.accounts))} "
            f"({data['phone']}) 已就绪，按 S 保存到配置文件",
            timeout=3,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="易班自动签到 TUI 配置工具")
    parser.add_argument(
        "--config", default=ACCOUNTS_DEFAULT, help=f"账号配置文件路径（默认: {ACCOUNTS_DEFAULT}）"
    )
    parser.add_argument("--log", default=LOG_DEFAULT, help=f"签到日志路径（默认: {LOG_DEFAULT}）")
    parser.add_argument("--env", default=ENV_DEFAULT, help=f".env 路径（默认: {ENV_DEFAULT}）")
    args = parser.parse_args()
    YibanTuiApp(config_path=args.config, log_path=args.log, env_path=args.env).run()


if __name__ == "__main__":
    main()
