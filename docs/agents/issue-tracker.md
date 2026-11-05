# Issue tracker: GitHub

任务和规格存放在 [MuZiJin701/seu-lecture-reserve-http 的 GitHub Issues](https://github.com/MuZiJin701/seu-lecture-reserve-http/issues)，使用 gh CLI。以下命令在本仓库根目录执行；写操作须在用户授权范围内进行。

## 仓库校验

首次访问 tracker 前，核对 `git remote -v`，再执行：

```bash
gh repo view --json nameWithOwner,url,hasIssuesEnabled,viewerPermission
```

目标必须为 `MuZiJin701/seu-lecture-reserve-http`，且 Issues 已启用。访问失败、目标不符或写权限不足时，报告具体原因并停止相应操作。下面的写命令显式指定仓库，避免写到 fork 上游。

## 常用操作

- 创建：`gh issue create --repo MuZiJin701/seu-lecture-reserve-http --title "..." --body-file <正文文件>`
- 读取：`gh issue view <编号> --repo MuZiJin701/seu-lecture-reserve-http --json number,title,body,labels,comments`
- 列表：`gh issue list --repo MuZiJin701/seu-lecture-reserve-http --state open --limit 100 --json number,title,body,labels,comments`；按需筛选，达到上限时继续分页核对。
- 更新正文：`gh issue edit <编号> --repo MuZiJin701/seu-lecture-reserve-http --body-file <正文文件>`
- 评论：`gh issue comment <编号> --repo MuZiJin701/seu-lecture-reserve-http --body-file <正文文件>`
- 标签：`gh issue edit <编号> --repo MuZiJin701/seu-lecture-reserve-http --add-label "..." --remove-label "..."`
- 关闭：先单独评论说明结论，再执行 `gh issue close <编号> --repo MuZiJin701/seu-lecture-reserve-http`。已关闭的任务也单独评论，避免依赖 `close --comment`。

多行正文通过文件传递，保留真实换行。任务只记录脱敏的需求、证据与验收条件，标签角色见 [triage-labels.md](triage-labels.md)。
技能要求“发布到 tracker”时创建 Issue；要求“读取相关 ticket”时读取对应 Issue。

## 任务关系

优先使用 GitHub 原生父子关系和阻塞依赖。先用当前 gh 的 `--help` 确认支持以下参数（gh 2.94+）：

- 创建子任务：创建时加 `--parent <父编号>`；关联已有任务：`gh issue edit <父编号> --repo MuZiJin701/seu-lecture-reserve-http --add-sub-issue <子编号>`。
- 创建阻塞：创建时加 `--blocked-by <编号>,<编号>`；关联已有阻塞：`gh issue edit <被阻塞编号> --repo MuZiJin701/seu-lecture-reserve-http --add-blocked-by <阻塞编号>`。
- 查未关闭阻塞数：`gh api repos/MuZiJin701/seu-lecture-reserve-http/issues/<编号> --jq .issue_dependencies_summary.blocked_by`。确认所有阻塞任务已关闭后才开始；字段缺失或请求失败时继续核对，不能按零处理。

CLI 或仓库不支持原生关系时，回退到正文链接：子任务顶部写 `Part of #<父编号>`，父任务维护带链接的任务清单；被阻塞任务顶部写 `Blocked by: #<编号>, #<编号>`。迁移旧正文关系前仍须核对未关闭阻塞。

## Pull requests as a triage surface

**PRs as a request surface: no.**
