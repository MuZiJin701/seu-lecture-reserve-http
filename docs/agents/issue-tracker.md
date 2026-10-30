# Issue tracker: GitHub

任务和规格存放在 [MuZiJin701/seu-lecture-reserve-http 的 GitHub Issues](https://github.com/MuZiJin701/seu-lecture-reserve-http/issues)。
使用 gh CLI；以下命令在本仓库根目录执行。

## 仓库校验

首次访问 tracker 前，用 `git remote -v` 核对远程，并执行：

```bash
gh repo view --json nameWithOwner,url,hasIssuesEnabled,viewerPermission
```

目标必须为 `MuZiJin701/seu-lecture-reserve-http`，且 Issues 已启用；访问失败时先报告原因并停止 tracker 操作。
下面的写命令显式指定本仓库，避免 fork 或默认仓库配置将任务写到上游。写操作须在用户授权范围内进行。

## 常用操作

- 创建：`gh issue create --repo MuZiJin701/seu-lecture-reserve-http --title "..." --body-file <文件>`
- 读取：`gh issue view <编号> --repo MuZiJin701/seu-lecture-reserve-http --json number,title,body,labels,comments`
- 列表：`gh issue list --repo MuZiJin701/seu-lecture-reserve-http --state open --json number,title,body,labels,comments`；按需加 `--label` 或更改 `--state`。
- 更新正文：`gh issue edit <编号> --repo MuZiJin701/seu-lecture-reserve-http --body-file <文件>`
- 评论：`gh issue comment <编号> --repo MuZiJin701/seu-lecture-reserve-http --body-file <文件>`
- 标签：`gh issue edit <编号> --repo MuZiJin701/seu-lecture-reserve-http --add-label "..." --remove-label "..."`
- 关闭：先按上面的评论命令记录结论，再执行 `gh issue close <编号> --repo MuZiJin701/seu-lecture-reserve-http`。已关闭任务仍先单独评论，避免 `close --comment` 丢弃说明。

多行正文通过文件传递，保留真实换行。标签角色见 [triage-labels.md](triage-labels.md)。
技能要求“发布到 tracker”时创建 Issue；要求“读取相关 ticket”时读取对应 Issue。
任务正文只记录脱敏的需求、证据与验收条件。

## 任务关系

优先使用 GitHub 原生父子关系和阻塞依赖（以下参数由 gh 2.94+ 提供）：

- 创建子任务：在创建命令中加 `--parent <父任务编号>`；关联已有任务：`gh issue edit <父任务编号> --repo MuZiJin701/seu-lecture-reserve-http --add-sub-issue <子任务编号>`。
- 创建阻塞依赖：在创建命令中加 `--blocked-by <编号>,<编号>`；关联已有阻塞：`gh issue edit <被阻塞任务编号> --repo MuZiJin701/seu-lecture-reserve-http --add-blocked-by <阻塞任务编号>`。
- 查询未关闭阻塞数：`gh api repos/MuZiJin701/seu-lecture-reserve-http/issues/<编号> --jq .issue_dependencies_summary.blocked_by`。只在确认所有阻塞任务已关闭后开始；字段缺失或请求失败时继续核对依赖，不按零处理。

旧版 CLI 或仓库不支持原生关系时，回退到正文链接：子任务顶部写 `Part of #<父任务编号>`，
父任务维护带链接的任务清单；被阻塞任务顶部写 `Blocked by: #<编号>, #<编号>`。
已有正文关系在迁移为原生关系前仍须核对，避免漏掉未关闭的阻塞。

## Pull requests as a triage surface

**PRs as a request surface: no.**
