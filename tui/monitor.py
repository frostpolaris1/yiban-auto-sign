"""实时性能监控模块（btop 简化版）。

在 TUI 配置工具中按 M 打开：
- CPU 使用率（总览 + 每核条形图）
- 内存 / 交换分区使用率
- 磁盘使用率与读写速率
- 网络上下行速率
- 负载 / 进程数 / 运行时长 / 内核版本

数据来源：psutil（每 2 秒刷新一次）。
"""

import os
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Static

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# 条形图宽度（字符数）
BAR_WIDTH = 24

# CPU 核数（展示每核条形图）
CPU_COUNT = os.cpu_count() or 1


def meter(value, width=BAR_WIDTH):
    """btop 风格条形图：█ 填充 + ░ 留白，按负载着色。"""
    value = max(0.0, min(100.0, value))
    filled = int(round(value / 100 * width))
    bar = '█' * filled + '░' * (width - filled)
    if value >= 90:
        color = 'red'
    elif value >= 70:
        color = 'yellow'
    else:
        color = 'green'
    return f'[{color}]{bar}[/{color}] {value:5.1f}%'


def human_bytes(n):
    """字节数格式化为人类可读。"""
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def format_uptime(seconds):
    """运行时长格式化为 天/时/分。"""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f'{days}天 {hours}时 {minutes}分'
    if hours:
        return f'{hours}时 {minutes}分'
    return f'{minutes}分'


class MonitorScreen(Screen):
    """实时性能监控页面（Esc 返回）。"""

    TITLE = '实时性能监控'
    BINDINGS = [Binding('escape', 'close', '返回')]

    CSS = '''
    #monitor {
        padding: 1 2;
    }
    #mon-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .meter-row {
        height: 1;
    }
    '''

    def __init__(self):
        super().__init__()
        self._last_time = time.time()
        self._last_net = None
        self._last_disk = None
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Container(
            Static('📊 实时性能监控（每 2 秒刷新，Esc 返回）', id='mon-title'),
            Static('', id='cpu-total'),
            Static('', id='cpu-cores'),
            Static('', id='mem'),
            Static('', id='swap'),
            Static('', id='disk'),
            Static('', id='net'),
            Static('', id='sys'),
            id='monitor',
        )

    def on_mount(self) -> None:
        if not HAS_PSUTIL:
            self.query_one('#cpu-total', Static).update(
                '⚠️ 未安装 psutil，无法监控系统性能\n'
                '请执行: pip3 install psutil')
            return
        # 热身：首次 cpu_percent 返回 0，先采样一次
        psutil.cpu_percent(interval=None, percpu=True)
        self.update_stats()
        self._timer = self.set_interval(2.0, self.update_stats)

    def action_close(self) -> None:
        """Esc 返回配置主界面。"""
        self.dismiss()

    def update_stats(self) -> None:
        """刷新一次全部监控数据。"""
        if not HAS_PSUTIL:
            return

        # CPU：总览 + 每核
        cpu_total = psutil.cpu_percent(interval=None)
        self.query_one('#cpu-total', Static).update(f'CPU 总览: {meter(cpu_total)}')
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        cores_txt = '  '.join(
            f'核{i + 1} {meter(c, BAR_WIDTH // CPU_COUNT if CPU_COUNT > 4 else 8)}'
            for i, c in enumerate(per_cpu))
        self.query_one('#cpu-cores', Static).update(cores_txt)

        # 内存 / 交换
        mem = psutil.virtual_memory()
        self.query_one('#mem', Static).update(
            f'内存: {meter(mem.percent)}  '
            f'({human_bytes(mem.used)} / {human_bytes(mem.total)})')
        swap = psutil.swap_memory()
        self.query_one('#swap', Static).update(
            f'交换: {meter(swap.percent)}  '
            f'({human_bytes(swap.used)} / {human_bytes(swap.total)})')

        # 网络 / 磁盘 IO 速率（与上次采样差分，放在磁盘行展示前计算）
        now = time.time()
        dt = max(now - self._last_time, 0.001)
        net = psutil.net_io_counters()
        if self._last_net is not None:
            rx = (net.bytes_recv - self._last_net.bytes_recv) / dt
            tx = (net.bytes_sent - self._last_net.bytes_sent) / dt
            self.query_one('#net', Static).update(
                f'网络: ↓ {human_bytes(rx)}/s   ↑ {human_bytes(tx)}/s')
        else:
            self.query_one('#net', Static).update('网络: 采样中…（2 秒后显示速率）')
        self._last_net = net

        io = psutil.disk_io_counters()
        if io is not None and self._last_disk is not None:
            rd = (io.read_bytes - self._last_disk.read_bytes) / dt
            wr = (io.write_bytes - self._last_disk.write_bytes) / dt
        else:
            rd = wr = 0.0
        self._last_disk = io
        self._last_time = now

        # 磁盘：根分区使用率 + 读写速率
        disk = psutil.disk_usage('/')
        self.query_one('#disk', Static).update(
            f'磁盘: {meter(disk.percent)}  '
            f'({human_bytes(disk.used)} / {human_bytes(disk.total)})  '
            f'IO: ↓ {human_bytes(rd)}/s ↑ {human_bytes(wr)}/s')

        # 系统概况：负载 / 进程数 / 运行时长 / 内核
        load = os.getloadavg()
        self.query_one('#sys', Static).update(
            f'负载: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}  |  '
            f'进程: {len(psutil.pids())}  |  '
            f'运行: {format_uptime(time.time() - psutil.boot_time())}  |  '
            f'内核: {os.uname().release}')
