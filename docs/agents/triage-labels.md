# Triage Labels

技能使用以下五种分诊角色；角色名与本仓库的标签名一致。

| 规范角色 | Tracker 标签 | 含义 |
| --- | --- | --- |
| needs-triage | needs-triage | 等待维护者评估 |
| needs-info | needs-info | 等待补充信息 |
| ready-for-agent | ready-for-agent | 规格完整，可交给代理执行 |
| ready-for-human | ready-for-human | 需要人工实施 |
| wontfix | wontfix | 不予实施 |

技能提到某种分诊角色时，使用表中的 Tracker 标签。

## 标签维护

按 [issue-tracker.md](issue-tracker.md) 校验仓库后，列出全部标签：

```bash
gh api --paginate 'repos/MuZiJin701/seu-lecture-reserve-http/labels?per_page=100' --jq '.[].name'
```

在配置初始化或用户已授权的维护中，只创建表内已配置且远程缺失的标签：

```bash
gh label create "<Tracker 标签>" --repo MuZiJin701/seu-lecture-reserve-http --description "<含义>"
```

保留已有标签的名称、颜色和说明。完成后重读远程，确认五种角色均有对应标签。更换标签名时先更新映射，避免创建同义标签。
