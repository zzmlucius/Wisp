# Wisp
Workload-Informed Scheduling Policy

## rusty 修复版

四种测试的自动切换模式统一使用独立安装的 rusty 修复版：

```text
/home/lucius/.local/scx-rusty-fixed/bin/scx_rusty
```

该程序为 `scx_rusty 1.1.4`，Cargo 安装记录中的固定提交为
`c8ab7bd73b98702e6c49264c774e1b8d48a75886`，包含上游
[PR #3936](https://github.com/sched-ext/scx/pull/3936)，修复失效任务记录可能导致的
`Expected nonzero load transfer` 退出问题。版本号仍为 1.1.4；新实验应注明该修复提交。

以下配置的 `schedulers.scx_rusty.command` 均指向上述完整路径，`ops` 保持
`rusty_1.1.4_x86_64_unknown_linux_gnu`：

- [Compile/compiletest.json](Compile/compiletest.json)
- [Schbench/schtest.json](Schbench/schtest.json)
- [Hackbench/hacktest.json](Hackbench/hacktest.json)
- [Mixbench/mixtest.json](Mixbench/mixtest.json)

例如带轮数的 `./compiletest 3` 会按配置加载修复版。无参数的测试只使用当前调度器，
不会主动切换；手动启动 rusty 时也应使用上述完整路径：

```bash
sudo /home/lucius/.local/scx-rusty-fixed/bin/scx_rusty
```

原版 `/usr/bin/scx_rusty` 保留，直接运行 `scx_rusty` 时仍需留意 PATH 的解析结果。
安装成功不代表已验证运行稳定性，修复效果需通过新测试确认；历史结果保留原记录。
