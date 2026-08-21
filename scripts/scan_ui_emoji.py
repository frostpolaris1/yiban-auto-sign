# -*- coding: utf-8 -*-
"""渲染产物 UI-emoji 扫描：完整渲染三个模板，检测输出中是否残留界面级 emoji。
白名单：JS 注释行、数据契约比较（stt === '✅' 等）、图标系统说明注释。"""
import re
from jinja2 import Environment, FileSystemLoader

# 仅真正的 UI emoji/符号（不含箭头注释等）；范围写法显式、无歧义
PATTERNS = [
    "\U0001F300-\U0001FAFF",   # 各类象形 emoji（📅🗑️🔔 等）
    "\u2705|\u274C|\u26D4",     # ✅ ❌ ⛔
    "\u2796|\u23F8\uFE0F|\u23F9\uFE0F|\u23F8|\u23F9",  # ➖ ⏸️ ⏹️
    "\u26A0\uFE0F|\u26A0",      # ⚠️
    "\u2600\uFE0F|\u2600|\u263A",  # ☀️ 相关
    "\U0001F319|\U0001F534",    # 🌙 等
]
rx = re.compile("[" + "\U0001F300-\U0001FAFF" + "\u2705\u274C\u26D4\u2796\u23F8\u23F9\u26A0\u2600\u263A" + "]")
ALLOW = ("stt ===", "// ", "替代原 emoji")

env = Environment(loader=FileSystemLoader("web/templates"))
ctx = {
    "web_version": "t", "app_version": "0-test", "icp_info": "",
    "agreement_html": "<p>p</p>", "privacy_html": "<p>p</p>",
}
clean = True
for name in ["login.html", "user.html", "index.html"]:
    html = env.get_template(name).render(**ctx)
    hits = []
    for i, line in enumerate(html.splitlines(), 1):
        if rx.search(line):
            s = line.strip()
            if any(a in line for a in ALLOW):
                continue
            hits.append((i, s[:90]))
    print(name, "->", "CLEAN" if not hits else str(len(hits)) + " HITS")
    for i, s in hits[:8]:
        print("   line", i, repr(s))
    clean = clean and not hits
print("RESULT:", "ALL CLEAN" if clean else "REVIEW NEEDED")
