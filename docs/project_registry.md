# ProjectRegistry

`<runtime_root>/config/projects.json` 是服务启动时一次性加载的只读项目注册表：

以下内容是完全虚构的公开示例。实际仓库路径、GitLab 地址、项目名称和 Bundle ID 只属于部署数据，不得写入源码仓库的 `config/`、测试或文档。

```json
{
  "schema_version": 1,
  "projects": [
    {
      "project": "ExampleApp",
      "display_name": "Example App",
      "repository_path": "/path/to/ExampleApp",
      "gitlab": {"host": "git.example.com", "project_path": "mobile/example-app"},
      "allowed_bundle_ids": ["com.example.coverage"]
    }
  ]
}
```

`project` 是数据库稳定标识；`display_name` 只用于展示。仓库路径必须是绝对的真实 Git worktree 根。GitLab host/project path 与 project 都必须唯一；Bundle ID 白名单非空、去重、精确匹配。未知字段、路径穿越、无效仓库和重复定义会阻止服务启动。

HTTP、Cycle、Build、Run 均不能覆盖仓库路径、GitLab API 地址或 Bundle 白名单。删除注册项目后，其既有 Cycle 会以 `project_not_registered` 明确失败，不会猜测其他仓库。
