# Git 快速上手（GIT QUICKSTART）

面向不熟悉 Git 的协作者。完整的协作流程与分支规范见 [WORKFLOW.md](./WORKFLOW.md)，贡献须知见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 首次准备

```bash
git clone git@github.com:frostpolaris1/yiban-auto-sign.git   # GitHub
# 或 git clone git@gitee.com:frostpolaris/yiban-auto-sign.git # Gitee
cd yiban-auto-sign

# 配置身份（提交信息署名用）
git config user.name "你的名字"
git config user.email "你的邮箱"
```

## 日常命令速查

| 想做什么 | 命令 |
|---|---|
| 查看当前分支与状态 | `git status` / `git branch` |
| 查看最近提交 | `git log --oneline -10` |
| 切换分支 | `git switch <分支名>` |
| 新建分支 | `git switch -c feat/你的功能名` |
| 暂存并提交 | `git add <文件>` → `git commit -m "fix(login): 空密码导致 500"` |
| 推送 | `git push -u origin <分支名>`（首次）/ `git push` |
| 拉取更新 | `git pull --ff-only` |
| 合并分支 | `git switch <目标分支>` → `git merge <来源分支>` |
| 查看远程 | `git remote -v` |
| 打标签 | `git tag -a v0.18.0 -m "说明"` → `git push --tags` |

## 与项目分支模型对应

- `server-web`：开发主干（日常功能提交到这里）
- `main`：生产 / 发布分支（tag 从这里打，部署走 main）
- `feat/*`、`fix/*`：短期分支，从 `server-web` 拉，合回后删除
- `hotfix/*`：生产紧急修复，从 `main` 拉

## 提交信息规范（Conventional Commits）

格式：`<type>(<scope>): <subject>`，例如 `feat(calendar): 新增签到日历面板`。

常用 type：`feat`（新功能）`fix`（修复）`docs`（文档）`refactor`（重构）`perf`（性能）`test`（测试）`chore`（杂项）`release`（发布）。

> 可选：启用本地校验 `git config core.hooksPath scripts/git-hooks`，提交时自动检查格式。

## 常见问题

**误提交了？**
```bash
git reset --soft HEAD~1   # 撤销最近一次提交（改动保留在暂存区）
git reset --hard HEAD~1   # 撤销并丢弃改动（谨慎！）
```

**想改上次提交信息？**
```bash
git commit --amend -m "新的信息"   # 仅未推送时使用；已推送需 force push
```

**合并冲突了？**
1. `git status` 查看冲突文件
2. 编辑文件解决冲突（保留 `<<<<<<<` / `=======` / `>>>>>>>` 之间的正确内容并删除标记）
3. `git add <文件>` → `git commit`（或 `git merge --continue`）

**改了文件但想暂时收起来？**
```bash
git stash        # 暂存改动
git stash pop    # 恢复
```

**推送被拒绝（远端有更新）？**
```bash
git pull --rebase   # 把本地提交重放到远端最新之上，再 git push
```
