# Wisp
Workload-Informed Scheduling Policy

## 目录与入口

从 Wisp 根目录运行相应命令。各测试使用对应目录中的本地配置，默认把快照和结果写到该目录的 `bench/` 下。首次配置时，可以复制同目录的 `*.example.json` 并去掉文件名中的 `.example`。

| 功能 | 入口 | 说明 |
| --- | --- | --- |
| Compile | `./Bench/Compile/compiletest` | [Compile](Bench/Compile/README.md) |
| Mixbench | `./Bench/Mixbench/mixtest` | [Mixbench](Bench/Mixbench/README.md) |
| Schbench | `./Bench/Schbench/schtest` | [Schbench](Bench/Schbench/README.md) |
| Hackbench | `./Bench/Hackbench/hacktest` | [Hackbench](Bench/Hackbench/README.md) |
| 采集器 | `make -C Agent/Collector` | [Collector](Agent/Collector/README.md) |

四种测试的 Clean 入口位于各自目录，可加 `--dry-run` 预览。Finegraintest 使用下文的统一入口。

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

- `Bench/Compile/compiletest.json`（[配置模板](Bench/Compile/compiletest.example.json)）
- `Bench/Schbench/schtest.json`（[配置模板](Bench/Schbench/schtest.example.json)）
- `Bench/Hackbench/hacktest.json`（[配置模板](Bench/Hackbench/hacktest.example.json)）
- `Bench/Mixbench/mixtest.json`（[配置模板](Bench/Mixbench/mixtest.example.json)）

例如带轮数的 `./Bench/Compile/compiletest 3` 会按配置加载修复版。无参数的测试只使用当前调度器，
不会主动切换；手动启动 rusty 时也应使用上述完整路径：

```bash
sudo /home/lucius/.local/scx-rusty-fixed/bin/scx_rusty
```

原版 `/usr/bin/scx_rusty` 保留，直接运行 `scx_rusty` 时仍需留意 PATH 的解析结果。
安装成功不代表已验证运行稳定性，修复效果需通过新测试确认；历史结果保留原记录。

## Compile 配置维护记录

2026-11-11 对基础 Compile 配置做过一次工具链维护：新增 Rust 工具链后，更新 Rust 版本
和能力探测项，并验证两次 `olddefconfig` 稳定。旧基础配置保存在
`Bench/Compile/bench/config/kernel.before-rust-20261111.config`，差异及验证记录保存在
`Bench/Compile/bench/validation/config-repair-20261111/`。直接运行 compiletest 也使用整理后的基础配置。

## Finegraintest

从 Wisp 根目录运行 `./Finegraintest.py --check` 预检，运行 `./Finegraintest.py` 正式测试。RPS 为 250、500、750、1000、1250、1500、1750、2000，前后台线程数各取 4、8、14。共 72 批、216 轮、864 次尝试。每轮四种调度器都结束并写入结果后，终端显示进度。

前后台各运行 30 秒，每次冷却 30 秒，批间额外休息 45 秒。仅负载和等待合计约 15 小时 17 分钟，另计准备、切换和收尾。启动时可能输入一次 sudo 密码，之后自动续期。队列持有防熄屏和休眠锁，结束或 Ctrl+C 后释放。

采集器位于 `Agent/Collector`。如需重建，运行 `make -C Agent/Collector bpf`。可用 `./Finegraintest.py --check-ebpf` 实跑一次，核对 eBPF 指标和卸载。若终端预检提示 cgroup 不可委派，可从 Wisp 根目录运行 `systemd-run --user --scope -p Delegate=yes -p DelegateSubgroup=manager ./Finegraintest.py --check`。

新队列写入 `Bench/Mixbench/bench/results/finegraintest/`。已有两批测试的合并结果位于 [Agent/Collector/result](Agent/Collector/result/)。
