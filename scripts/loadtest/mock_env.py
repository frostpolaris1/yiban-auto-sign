#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压测环境一键搭建/还原（**仅限测试机**）。

做四件事，全部幂等：
  1. 生成自签 CA 与服务器证书（SAN 覆盖所需易班域名），并导出登录页用 RSA 公钥；
  2. 改写 /etc/hosts：把所需域名双栈（127.0.0.1 + ::1）指向本机回环；
  3. 出站 443 兜底 REJECT：放行回环、拒绝其余，防止压测误连真实易班；
  4. 自检「零真实外联」并打印结论。

``--restore`` 反向还原第 2、3 步（证书目录按约定可保留），同样幂等。

安全约定：
  * 只应在测试机以 root 运行；脚本拒绝在非 Linux 上执行写操作（--dry-run 除外）。
  * 不写入任何真实凭据；域名列表来自参数（默认即易班公开域名，仅用于 hosts 改写）。
  * /etc/hosts 第一次改写前备份为 ``<base>/hosts.orig``，还原时按标记块删除。

用法：
  sudo python3 mock_env.py --base-dir /opt/yiban-loadtest
  sudo python3 mock_env.py --base-dir /opt/yiban-loadtest --restore
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys

DEFAULT_DOMAINS = [
    "oauth.yiban.cn",
    "f.yiban.cn",
    "api.uyiban.com",
    "c.uyiban.com",
    "app.uyiban.com",
]

HOSTS_BEGIN = "# yiban-loadtest-begin"
HOSTS_END = "# yiban-loadtest-end"

# 出站兜底规则（IPv4/IPv6 各两条：先放行回环，再拒绝其余 443）
IPT_OUT_MATCH = ["-p", "tcp", "--dport", "443"]


def run(cmd, dry_run=False, check=False):
    """执行外部命令；dry_run 时只打印。返回 (rc, stdout)。"""
    printable = " ".join(cmd)
    if dry_run:
        print(f"[dry-run] {printable}")
        return 0, ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        print(f"错误：找不到命令 {cmd[0]}", file=sys.stderr)
        return 127, ""
    if check and p.returncode != 0:
        print(f"命令失败（rc={p.returncode}）: {printable}\n{p.stderr.strip()}", file=sys.stderr)
    return p.returncode, p.stdout


# ---------------------------------------------------------------------------
# 证书
# ---------------------------------------------------------------------------
def _san_conf(domains):
    alt = ", ".join(f"DNS.{i + 1}:{d}" for i, d in enumerate(domains))
    return (
        "[v3_req]\n"
        "basicConstraints = CA:FALSE\n"
        "keyUsage = digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        f"subjectAltName = {alt}, IP.1:127.0.0.1, IP.2:::1\n"
    )


def ensure_certs(base_dir, domains, force=False, dry_run=False):
    """幂等生成 CA/服务器证书；返回证书路径 dict。"""
    ca_dir = os.path.join(base_dir, "ca")
    if not dry_run:
        os.makedirs(ca_dir, exist_ok=True)
    ca_key = os.path.join(ca_dir, "ca.key")
    ca_pem = os.path.join(ca_dir, "ca.pem")
    srv_key = os.path.join(ca_dir, "server.key")
    srv_pem = os.path.join(ca_dir, "server.pem")
    pub_pem = os.path.join(ca_dir, "pub.pem")
    san_cnf = os.path.join(ca_dir, "san.cnf")

    if not force and all(os.path.exists(p) for p in (ca_key, ca_pem, srv_key, srv_pem, pub_pem)):
        print(f"证书已存在，跳过生成：{ca_dir}")
        return {"ca": ca_pem, "cert": srv_pem, "key": srv_key, "pub": pub_pem}

    if not dry_run:
        with open(san_cnf, "w", encoding="utf-8") as f:
            f.write(_san_conf(domains))

    run(["openssl", "genrsa", "-out", ca_key, "2048"], dry_run=dry_run, check=True)
    run(["openssl", "req", "-x509", "-new", "-nodes", "-key", ca_key,
         "-sha256", "-days", "3650", "-subj", "/CN=yiban-loadtest-ca",
         "-out", ca_pem], dry_run=dry_run, check=True)
    run(["openssl", "genrsa", "-out", srv_key, "2048"], dry_run=dry_run, check=True)
    run(["openssl", "req", "-new", "-key", srv_key,
         "-subj", f"/CN={domains[0]}", "-out", os.path.join(ca_dir, "server.csr")],
        dry_run=dry_run, check=True)
    run(["openssl", "x509", "-req", "-in", os.path.join(ca_dir, "server.csr"),
         "-CA", ca_pem, "-CAkey", ca_key, "-CAcreateserial",
         "-days", "3650", "-sha256", "-extfile", san_cnf, "-extensions", "v3_req",
         "-out", srv_pem], dry_run=dry_run, check=True)
    # 登录页内嵌公钥可直接复用服务器 RSA 公钥（mock 从不解密，仅需合法 PEM）
    run(["openssl", "rsa", "-in", srv_key, "-pubout", "-out", pub_pem],
        dry_run=dry_run, check=True)
    if not dry_run:
        os.chmod(srv_key, 0o600)
        os.chmod(ca_key, 0o600)
        print(f"证书已生成：{ca_dir}")
    return {"ca": ca_pem, "cert": srv_pem, "key": srv_key, "pub": pub_pem}


# ---------------------------------------------------------------------------
# /etc/hosts
# ---------------------------------------------------------------------------
def build_hosts_block(domains):
    lines = [HOSTS_BEGIN]
    for d in domains:
        lines.append(f"127.0.0.1 {d}")
    for d in domains:
        lines.append(f"::1 {d}")
    lines.append(HOSTS_END)
    return "\n".join(lines) + "\n"


def _read_hosts(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def strip_hosts_block(text):
    """删除标记块（含标记行），返回剩余文本。"""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == HOSTS_BEGIN:
            skip = True
            continue
        if line.strip() == HOSTS_END:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "".join(out)


def apply_hosts(hosts_path, domains, backup_path, dry_run=False):
    """写入/更新标记块；幂等（已是目标内容则不动）。"""
    current = _read_hosts(hosts_path)
    block = build_hosts_block(domains)
    stripped = strip_hosts_block(current)
    if not stripped.endswith("\n") and stripped:
        stripped += "\n"
    desired = stripped + block
    if current == desired:
        print(f"{hosts_path}: 标记块已是目标内容，跳过")
        return False
    if not dry_run and not os.path.exists(backup_path):
        try:
            shutil.copy2(hosts_path, backup_path)
            print(f"{hosts_path} 原始内容已备份到 {backup_path}")
        except OSError as e:
            print(f"警告：备份 {hosts_path} 失败: {e}", file=sys.stderr)
    if dry_run:
        print(f"[dry-run] 写入 {hosts_path}：\n{block}", end="")
        return True
    tmp = hosts_path + ".tmp-loadtest"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(desired)
    os.replace(tmp, hosts_path)
    print(f"{hosts_path}: 已写入 {len(domains)} 个域名的双栈回环映射")
    return True


def restore_hosts(hosts_path, backup_path, dry_run=False):
    """删除标记块；幂等。若只剩空白可选恢复备份（优先保留人工追加内容）。"""
    current = _read_hosts(hosts_path)
    if HOSTS_BEGIN not in current:
        print(f"{hosts_path}: 无标记块，无需还原")
        return False
    stripped = strip_hosts_block(current)
    if dry_run:
        print(f"[dry-run] 从 {hosts_path} 删除标记块")
        return True
    tmp = hosts_path + ".tmp-loadtest"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(stripped)
    os.replace(tmp, hosts_path)
    print(f"{hosts_path}: 已删除标记块")
    return True


# ---------------------------------------------------------------------------
# iptables 出站兜底
# ---------------------------------------------------------------------------
def _ipt_cmd(ipv6):
    return "ip6tables" if ipv6 else "iptables"


def _rule_specs(ipv6):
    """返回 (放行回环规则, 其余 REJECT 规则)。"""
    loop_dst = "::1/128" if ipv6 else "127.0.0.0/8"
    accept = [*IPT_OUT_MATCH, "-d", loop_dst, "-j", "ACCEPT"]
    reject = [*IPT_OUT_MATCH, "-j", "REJECT"]
    return accept, reject


def _ipt_has(cmd, spec):
    rc, _ = run([cmd, "-C", "OUTPUT", *spec])
    return rc == 0


def apply_iptables(ipv6, dry_run=False):
    """幂等：放行回环 443，拒绝其余 443 出站。"""
    cmd = _ipt_cmd(ipv6)
    accept, reject = _rule_specs(ipv6)
    changed = False
    # ACCEPT 用 -I 插到最前，保证先于任何既有 REJECT 生效
    if not _ipt_has(cmd, accept):
        rc, _ = run([cmd, "-I", "OUTPUT", "1", *accept], dry_run=dry_run)
        if rc != 0 and not dry_run:
            print(f"警告：{cmd} 添加回环放行规则失败（可能无权限）", file=sys.stderr)
        else:
            changed = True
            print(f"{cmd}: 已放行回环 443 出站")
    else:
        print(f"{cmd}: 回环放行规则已存在")
    if not _ipt_has(cmd, reject):
        rc, _ = run([cmd, "-A", "OUTPUT", *reject], dry_run=dry_run)
        if rc != 0 and not dry_run:
            print(f"警告：{cmd} 添加 443 REJECT 规则失败（可能无权限）", file=sys.stderr)
        else:
            changed = True
            print(f"{cmd}: 已添加其余 443 出站 REJECT")
    else:
        print(f"{cmd}: 443 REJECT 规则已存在")
    return changed


def restore_iptables(ipv6, dry_run=False):
    """幂等删除本脚本添加的规则（按内容匹配，不误删他人规则）。"""
    cmd = _ipt_cmd(ipv6)
    removed = False
    for spec in _rule_specs(ipv6):
        if _ipt_has(cmd, spec):
            rc, _ = run([cmd, "-D", "OUTPUT", *spec], dry_run=dry_run)
            if rc == 0 or dry_run:
                removed = True
                print(f"{cmd}: 已删除规则 {' '.join(spec)}")
        else:
            print(f"{cmd}: 规则不存在，跳过 {' '.join(spec)}")
    return removed


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def selfcheck(domains, hosts_path, ipv6=True):
    """验证：域名解析到回环 + 兜底 REJECT 规则存在。返回 (ok, 明细行列表)。"""
    rows = []
    ok = True
    for d in domains:
        try:
            infos = socket.getaddrinfo(d, 443, proto=socket.IPPROTO_TCP)
            addrs = sorted({i[4][0] for i in infos})
        except OSError as e:
            rows.append(f"  [FAIL] 解析 {d} 失败: {e}")
            ok = False
            continue
        all_loop = bool(addrs) and all(_is_loopback(a) for a in addrs)
        rows.append(f"  [{'OK' if all_loop else 'FAIL'}] {d} -> {', '.join(addrs)}")
        ok = ok and all_loop
    for ipv6_flag in (False, True):
        cmd = _ipt_cmd(ipv6_flag)
        _, reject = _rule_specs(ipv6_flag)
        has = _ipt_has(cmd, reject)
        rows.append(f"  [{'OK' if has else 'FAIL'}] {cmd} 其余 443 REJECT 规则{'存在' if has else '缺失'}")
        ok = ok and has
    has_block = HOSTS_BEGIN in _read_hosts(hosts_path)
    rows.append(f"  [{'OK' if has_block else 'FAIL'}] {hosts_path} 标记块{'存在' if has_block else '缺失'}")
    ok = ok and has_block
    return ok, rows


def verify_zero_egress(domains, hosts_path, probe_ip="", timeout=3.0):
    """「零真实外联」自检：域名只解析到回环；可选探测目标 IP 的 443 被拒。"""
    print("== 零真实外联自检 ==")
    ok = True
    for d in domains:
        try:
            infos = socket.getaddrinfo(d, 443, proto=socket.IPPROTO_TCP)
            addrs = sorted({i[4][0] for i in infos})
        except OSError as e:
            print(f"  [FAIL] 解析 {d} 失败: {e}")
            ok = False
            continue
        loop_only = bool(addrs) and all(_is_loopback(a) for a in addrs)
        print(f"  [{'OK' if loop_only else 'FAIL'}] {d} -> {', '.join(addrs)}")
        ok = ok and loop_only
    if probe_ip:
        try:
            with socket.create_connection((probe_ip, 443), timeout=timeout):
                print(f"  [FAIL] 到 {probe_ip}:443 竟可连通——出站兜底未生效")
                ok = False
        except OSError as e:
            print(f"  [OK] 到 {probe_ip}:443 被拒绝/不可达（{e.__class__.__name__}）")
    else:
        print("  [SKIP] 未指定 --egress-probe-ip，跳过主动出站探测")
    print(f"== 自检结论：{'通过（域名均指向回环）' if ok else '未通过，请检查'} ==")
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mock_env.py",
        description="压测环境一键搭建/还原（仅限测试机；hosts + 自签证书 + 443 兜底）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--base-dir", default="/opt/yiban-loadtest",
                    help="证书/备份等持久文件目录")
    ap.add_argument("--hosts-file", default="/etc/hosts", help="hosts 文件路径")
    ap.add_argument("--domains", default=",".join(DEFAULT_DOMAINS),
                    help="需要映射到回环的域名（逗号分隔）")
    ap.add_argument("--restore", action="store_true", help="还原 hosts 与 iptables 改动")
    ap.add_argument("--no-ipv6", action="store_true", help="不做 IPv6 处理")
    ap.add_argument("--no-iptables", action="store_true", help="跳过 iptables 兜底")
    ap.add_argument("--force", action="store_true", help="强制重新生成证书")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不改系统")
    ap.add_argument("--check", action="store_true", help="只做自检，不做任何改动")
    ap.add_argument("--egress-probe-ip", default="",
                    help="可选：主动探测该 IP:443 应被拒绝（不写入仓库的临时值）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出自检明细")
    args = ap.parse_args(argv)

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        print("错误：--domains 为空", file=sys.stderr)
        return 2

    hosts_path = args.hosts_file
    backup_path = os.path.join(args.base_dir, "hosts.orig")

    if args.check:
        ok, rows = selfcheck(domains, hosts_path, ipv6=not args.no_ipv6)
        if args.json:
            print(json.dumps({"ok": ok, "rows": rows}, ensure_ascii=False, indent=2))
        else:
            print("\n".join(rows))
        return 0 if ok else 1

    is_linux = sys.platform.startswith("linux")
    if not is_linux and not args.dry_run:
        print("错误：hosts/iptables 操作仅支持 Linux；本机请用 --dry-run", file=sys.stderr)
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0 and not args.dry_run:
        print("错误：需要 root 权限（sudo）才能改写 hosts / iptables", file=sys.stderr)
        return 2

    if args.restore:
        print("== 还原压测环境 ==")
        restore_hosts(hosts_path, backup_path, dry_run=args.dry_run)
        if not args.no_ipv6:
            restore_iptables(True, dry_run=args.dry_run)
        if not args.no_iptables:
            restore_iptables(False, dry_run=args.dry_run)
        print("还原完成（证书目录按约定保留）")
        return 0

    print("== 搭建压测环境 ==")
    certs = ensure_certs(args.base_dir, domains, force=args.force, dry_run=args.dry_run)
    apply_hosts(hosts_path, domains, backup_path, dry_run=args.dry_run)
    if not args.no_iptables:
        apply_iptables(False, dry_run=args.dry_run)
        if not args.no_ipv6:
            apply_iptables(True, dry_run=args.dry_run)

    if not args.dry_run:
        verify_zero_egress(domains, hosts_path, probe_ip=args.egress_probe_ip)
        print("\n提示：压测结束后务必执行 --restore 还原 hosts 与 iptables。")
    print("证书路径：")
    for k, v in certs.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
