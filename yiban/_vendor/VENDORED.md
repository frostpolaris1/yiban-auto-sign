# Vendored：`yiban_protocol`

本目录是 `yiban/_vendor/yiban_protocol/`——从独立仓库
[`yiban-protocol`](https://github.com/frostpolaris1/yiban-protocol)（私有）原样同步的
洁净室协议库，**不是本仓的原创代码**。

| 项 | 值 |
|----|-----|
| 来源仓库 | `D:\code\yiban-protocol`（远端 `yiban-protocol`） |
| 来源 commit | `aab17fb`（2026-10-03） |
| 版本 | `0.1.0` |
| 许可证 | **MIT**（全文副本见同目录 `yiban_protocol/LICENSE`） |
| 同步方式 | **只从上游库同步覆盖本目录，禁止在本仓直接修改；需改动请去 yiban-protocol 仓库** |

## 为什么可以放进 AGPL 主仓

本主仓整体以 **AGPL-3.0** 分发；vendored 的 MIT 代码与 AGPL 兼容，只需保留其
版权与许可声明（即 `yiban_protocol/LICENSE` 与各文件头注）。合入后本目录内的
文件仍受 MIT 约束，主仓其余部分不受影响。

## 同步纪律

1. 上游库升级后，用「整目录覆盖」同步，不在本仓做增量编辑——`git status` 里本目录
   的任何 diff 都说明同步方式被破坏。
2. 本目录文件必须与上游逐字节一致（含 `__init__.py` 与 `LICENSE`）。
3. 主仓代码一律 `from yiban._vendor.yiban_protocol import ...` 引用，不复制其内部实现。
