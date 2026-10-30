# Triage Labels

| 规范角色 | Tracker 标签 | 含义 |
| --- | --- | --- |
| needs-triage | needs-triage | 等待维护者评估 |
| needs-info | needs-info | 等待补充信息 |
| ready-for-agent | ready-for-agent | 规格完整，可交给代理执行 |
| ready-for-human | ready-for-human | 需要人工实施 |
| wontfix | wontfix | 不予实施 |

技能提及某种分诊角色时，使用对应的 Tracker 标签。

## 标签维护

在配置初始化或已获授权的标签维护中，先按 [issue-tracker.md](issue-tracker.md) 校验目标仓库，再列出标签：

```bash
gh label list --repo MuZiJin701/seu-lecture-reserve-http --limit 100 --json name,description,color
```

结果达到条数上限时继续分页核对。只创建上表已配置且远程缺失的标签：

```bash
gh label create "<Tracker 标签>" --repo MuZiJin701/seu-lecture-reserve-http --description "<含义>"
```

保留已有标签及其颜色、说明；完成后重读远程标签，确认五种角色均有对应标签。更换标签名时同步更新上表，避免另建同义标签。
