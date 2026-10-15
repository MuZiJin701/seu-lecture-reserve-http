# Issue tracker: GitHub

任务和规格存放在 [MuZiJin701/seu-lecture-reserve-http 的 GitHub Issues](https://github.com/MuZiJin701/seu-lecture-reserve-http/issues)。
使用 gh CLI；以下命令在本仓库根目录执行。

## 常用操作

- 创建：`gh issue create --title "..." --body-file <文件>`
- 读取：`gh issue view <编号> --json number,title,body,labels,comments`
- 列表：`gh issue list --state open --json number,title,body,labels`
- 更新正文：`gh issue edit <编号> --body-file <文件>`
- 评论：`gh issue comment <编号> --body-file <文件>`
- 标签：`gh issue edit <编号> --add-label "..." --remove-label "..."`
- 关闭：`gh issue close <编号> --comment "..."`

多行正文通过文件传递，保留真实换行。标签角色见 [triage-labels.md](triage-labels.md)。
技能要求“发布到 tracker”时创建 Issue；要求“读取相关 ticket”时读取对应 Issue。
任务正文只记录脱敏的需求、证据与验收条件。

## 任务关系

本次配置使用的 gh 2.45.0 不支持 --parent / --add-sub-issue。
拆分任务时，在子任务正文顶部写 `Part of #<父任务编号>`，
并在父任务中维护带链接的任务清单。
依赖关系用正文顶部的 `Blocked by: #<编号>` 记录；所有阻塞任务关闭后才可执行。

## Pull requests as a triage surface

**PRs as a request surface: no.**
