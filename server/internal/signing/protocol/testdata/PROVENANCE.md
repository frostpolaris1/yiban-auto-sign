# 协议夹具来源

本目录的夹具复制自主仓 `tests/protocol/fixtures/`，复制时点
`develop` 的提交 `619edbef`（2026-10-25 20:19 +0800）。

## 来源分档

- **实拍脱敏**：结构与字段取自真实流量，个人数据已替换为虚构值。
  清单标 `captured` 的六项属此档。
- **纯合成**：无真实抓包，只作解析分支输入。
  清单标 `synthetic` 的六项属此档。
- **清单外五项**：`iframe_location.txt`、`usersure_form.txt`、
  `usersure_url.txt`、`sign_in_body.txt`、`sign_in_body_urlencoded.txt`。
  原清单没有来源字段。只作结构与编码回归，不引用为实拍证据。

## 已知冲突

`replay_manifest.json` 把 `authorize_page.html` 标为 `captured`。
`yiban/adapters/protocol_yiban/SPEC.md` §2.1 写它是合成页，原页未入仓。
本包只把它当**结构证据**，不声称字节级实拍。

清单本身 `replay_manifest.json` 是索引，不是平台响应；它的键名含
`challenge_ydclearance` 与 `challenge_fengkongcloud`，会命中挑战检测的强信号。
它不参与「正常响应」集合。

## 使用规则

1. 合成挑战页与会话失效页只喂解析分支，不得当平台已知响应。
2. 测试不触网；夹具是唯一输入。
3. 修改夹具必须同时更新来源说明与对应测试。
