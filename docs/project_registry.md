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
      "compiler_repository_names": ["ExampleApp", "ExampleApp2"],
      "gitlab": {"host": "git.example.com", "project_path": "mobile/example-app"},
      "allowed_bundle_ids": ["com.example.coverage"]
    }
  ]
}
```

`project` 是数据库稳定标识；`display_name` 只用于展示。仓库路径必须是绝对的真实 Git worktree 根。GitLab host/project path 与 project 都必须唯一；Bundle ID 白名单非空、去重、精确匹配。未知字段、路径穿越、无效仓库和重复定义会阻止服务启动。

`compiler_repository_names` 是可选的、按项目隔离的编译仓库目录名白名单；省略时默认只允许登记仓库根目录名。显式配置必须是非空、不重复的目录名称列表，不能包含绝对路径、路径分隔符、`.` 或 `..`。它不改变服务端读取 Git 的 `repository_path`。分析器从能与固定 Git 路径对应的编译路径中确定一次根目录，随后批量剥离同一目录前缀；前缀包含目录边界，不按部分名称匹配。多个有效切分或第二个有效编译根会失败，未登记的目录名不自动推断。名单随启动快照传入分析子进程，并计入分析结果标识，修改后需重启服务；已有报告不会因新配置被直接改写，可通过正常 retry 重新分析。

HTTP、Cycle、Build、Run 均不能覆盖仓库路径、GitLab API 地址或 Bundle 白名单。删除注册项目后，其既有 Cycle 会以 `project_not_registered` 明确失败，不会猜测其他仓库。

新 Debug 包以仓库相对路径导出覆盖率，按 Build 固定 Git 树直接查找；`compiler_repository_names` 只用于兼容旧绝对路径包。仓库相对路径不需要登记打包 checkout 的目录名。
