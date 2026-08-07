#!/usr/bin/env python3
"""易班自动签到 TUI 配置工具。

服务器端（SSH）运行的简易配置界面：
- 表单式输入账号：手机号、密码（掩码）、设备型号、设备识别码
- 一个账号的所有信息一次性输入，无需用符号分隔
- 多账号列表管理（添加 / 编辑 / 删除）
- 保存为 accounts.json，signin.py 会自动读取

用法：
    python3 -m tui [--config /path/to/accounts.json]

快捷键：
    A 添加账号   E 编辑选中账号   D 删除选中账号
    S 保存配置   Q 退出
"""

import argparse
import json
import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label

# 默认配置文件路径（与 signin.py 保持一致，可用环境变量覆盖）
ACCOUNTS_DEFAULT = os.environ.get('YIBAN_ACCOUNTS_FILE', 'accounts.json')


class AccountForm(ModalScreen):
    """账号编辑表单：添加新账号或编辑已有账号。"""

    BINDINGS = [Binding('escape', 'cancel', '取消')]

    def __init__(self, account=None, index=None):
        super().__init__()
        self.account = account or {}
        self.index = index  # None=添加，数字=编辑第 index+1 个账号

    def compose(self) -> ComposeResult:
        acc = self.account
        title = '➕ 添加账号' if self.index is None else f'✏️ 编辑账号 #{self.index + 1}'
        yield Container(
            Label(title, id='form-title'),
            Input(placeholder='手机号（必填）', value=acc.get('phone', ''),
                  id='phone', max_length=20),
            Input(placeholder='密码（必填）', value=acc.get('password', ''),
                  id='password', password=True),
            Input(placeholder='设备型号（可选，如 Vivo-XXXX）',
                  value=acc.get('phone_model', ''), id='phone_model', max_length=50),
            Input(placeholder='设备识别码（可选）', value=acc.get('phone_code', ''),
                  id='phone_code', max_length=100),
            Label('提示：填写全部字段后按 Enter 或点击保存；按 Esc 取消', id='form-hint'),
            Horizontal(
                Button('保存', variant='success', id='save'),
                Button('取消', variant='default', id='cancel'),
                id='form-buttons',
            ),
            id='form',
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'save':
            self._save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        """Esc 取消，不保存任何修改。"""
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """任意输入框回车即保存，简化键盘操作。"""
        if event.input.id in ('phone', 'password', 'phone_model', 'phone_code'):
            self._save()

    def _save(self) -> None:
        phone = self.query_one('#phone', Input).value.strip()
        password = self.query_one('#password', Input).value.strip()
        if not phone or not password:
            self.notify('手机号和密码为必填项', severity='error', timeout=3)
            return
        self.dismiss({
            'phone': phone,
            'password': password,
            'phone_model': self.query_one('#phone_model', Input).value.strip(),
            'phone_code': self.query_one('#phone_code', Input).value.strip(),
        })


class YibanTuiApp(App):
    """易班账号配置 TUI 主应用。"""

    TITLE = '易班自动签到 · 账号配置'
    SUB_TITLE = '服务器端 TUI 配置工具'

    CSS = '''
    #form {
        width: 64;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        margin: 2 6;
    }
    #form-title {
        text-style: bold;
        margin-bottom: 1;
    }
    Input {
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
    '''

    BINDINGS = [
        Binding('a', 'add', '添加'),
        Binding('e', 'edit', '编辑'),
        Binding('d', 'delete', '删除'),
        Binding('s', 'save', '保存'),
        Binding('q', 'quit', '退出'),
    ]

    def __init__(self, config_path: str = ACCOUNTS_DEFAULT):
        super().__init__()
        self.config_path = config_path
        self.accounts = []  # [{'phone', 'password', 'phone_model', 'phone_code'}, ...]
        self._editing_row = None  # 编辑模式目标行（None=添加模式）

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id='table', cursor_type='row')
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one('#table', DataTable)
        table.add_columns('手机号', '设备型号', '设备识别码')
        self._load()

    # ---- 数据加载与展示 ----
    def _load(self) -> None:
        """加载已有 accounts.json（若存在且合法）。"""
        self.accounts = []
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    self.accounts = [a for a in raw if isinstance(a, dict)]
            except (json.JSONDecodeError, OSError):
                self.notify(f'配置文件 {self.config_path} 解析失败，已从空列表开始',
                            severity='error', timeout=4)
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one('#table', DataTable)
        table.clear()
        for acc in self.accounts:
            code = acc.get('phone_code', '')
            display_code = code[:12] + ('…' if len(code) > 12 else '')
            table.add_row(acc.get('phone', ''), acc.get('phone_model', ''), display_code)
        self.sub_title = f'共 {len(self.accounts)} 个账号 | {os.path.basename(self.config_path)}'

    def action_save(self) -> None:
        """写入 accounts.json（UTF-8、缩进 2、保留中文）。"""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)
            f.write('\n')
        self.notify(f'已保存 {len(self.accounts)} 个账号 → {self.config_path}',
                    severity='information', timeout=3)

    # ---- 账号操作 ----
    def action_add(self) -> None:
        """添加账号：记录当前为添加模式，保存后追加到列表。"""
        self._editing_row = None
        self.push_screen(AccountForm(), callback=self._on_form_result)

    def action_edit(self) -> None:
        row = self._current_row()
        if row is None:
            self.notify('请先选中要编辑的账号（↑↓ 选择）', severity='warning', timeout=3)
            return
        # 记录编辑目标行：表单保存时按此行覆盖，不随光标移动变化
        self._editing_row = row
        self.push_screen(AccountForm(account=self.accounts[row], index=row),
                         callback=self._on_form_result)

    def action_delete(self) -> None:
        row = self._current_row()
        if row is None:
            self.notify('请先选中要删除的账号（↑↓ 选择）', severity='warning', timeout=3)
            return
        removed = self.accounts.pop(row)
        self.notify(f'已删除账号 {removed.get("phone", "")}（按 S 保存生效）', timeout=3)
        self._refresh_table()

    def _current_row(self):
        table = self.query_one('#table', DataTable)
        if not self.accounts or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self.accounts):
            return table.cursor_row
        return None

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
        self.notify(f'账号 {data["phone"]} 已就绪，按 S 保存到配置文件', timeout=3)


def main() -> None:
    parser = argparse.ArgumentParser(description='易班自动签到 TUI 配置工具')
    parser.add_argument('--config', default=ACCOUNTS_DEFAULT,
                        help=f'账号配置文件路径（默认: {ACCOUNTS_DEFAULT}）')
    args = parser.parse_args()
    YibanTuiApp(config_path=args.config).run()


if __name__ == '__main__':
    main()
