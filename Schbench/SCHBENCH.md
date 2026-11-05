# schbench 测试任务

## 目标与目录

新增 Python 3 入口，复用现有 compiletest。先读 `Compile/AGENTS.md`、`compiletest.json`及代码：通用配置/快照、策略核验、门控计时、采样、常驻提权监督、sleep 阻止锁、互斥、失败处理和 Clean 规则均沿用现有实现，编译专用的源码准备不适用。保持编译测试可用，README 列出实际复用接口。

代码、配置、测试、README 和 `bench/`放在 `Schbench/`，本文路径相对此目录，先进入后执行；本文档留在项目根目录。当前用静态实验建立“负载 → 调度器”映射，先满足约束再优化主要指标，不做加权总分。

## 入口与配置

| 命令 | 模式 | 执行范围 |
|---|---|---|
| `./schtest` | 尽力运行 | 当前调度器一次 |
| `./schtest n` | 尽力运行 | 四策略随机测试 n 轮 |
| `./schtest -R` | 固定请求率 | 当前调度器一次 |
| `./schtest -R n` | 固定请求率 | 四策略随机测试 n 轮 |

- n 为正整数。省略 n 与 n=1 不同；有 n 时，每轮独立随机排列 default、scx_lavd、scx_bpfland、scx_rusty，各一次，共 4×n 次，保存 seed/orders。
- 参数读自 `schtest.json`，键名风格参考 compiletest：程序路径、负载参数、CPU 范围、目标 RPS/容差、可选延迟上限、结果目录、冷却/采样间隔及调度器控制参数。
- 包装器 `-R`是模式开关，n 是轮数。目标 RPS 从配置或 `--rps VALUE`取得，传给原生 `schbench -R VALUE`；尽力模式不传原生 `-R/-A`。两种模式支持 `--output DIR`，保存最终生效配置。
- 示例起点为 `-m 1 -t 14 -r 60 -F 256 -n 5 -s 100`，按实际 CPU 和预实验确认。同一场景固定参数与 locking，核对版本后设 `-w 0 -z 0`，不清缓存；不使用校准/pipe 负载。这些是起点，不是最优参数。
- 正常运行不打印进度或结果；启动错误写 stderr。结果为 `bench/results/<唯一批次>/`。

## 运行流程与指标

1. **预检**：核对依赖、配置、权限和环境，保存整批配置/快照、程序版本与摘要、实际命令、统计单位和范围。
2. **策略准备**：无 n 时接受当前策略，不调用 sudo、不切换或停止用户调度器；有 n 时按 compiletest 的监督协议预检外部实例、授权一次，按随机顺序切换并等待稳定。
3. **单次运行**：统一冷却，保存本次固定快照；门控启动原生 schbench，并行资源/温度采样和策略核验，频率沿用 compiletest。仅从正式 schbench 的 exec 到退出记录 `wall_s`，准备和收尾不计入。
4. **记录**：保存退出码、完整 stdout/stderr、原生 JSON（支持时），解析最终平均 RPS、请求 p99、唤醒 p99。延迟用 µs，吞吐用请求/秒；不能以 RPS p50 替代平均值，不能平均多个窗口 p99。其他百分位保留原生结果。
5. **收尾**：停止本程序负载/采样并保存结果。无 n 保持用户策略；有 n 清理自己的调度器并核验 disabled。单次失败、继续条件及中断/监督丢失的终止规则沿用 compiletest，不伪造尚未尝试的记录。

评价分开处理：
- 固定请求率：预实验确定统一 RPS，主要看请求 p99，同时检查吞吐达标和唤醒 p99。未达 RPS 目标保留标注，不能仅凭低延迟判胜。
- 尽力运行：主要看平均 RPS，同时看请求/唤醒 p99；若配置延迟上限，先检查达标。两模式分别汇总，`wall_s`只作运行核查。
- `valid`要求正常退出、必需指标可解析且通用核验通过；性能未达目标不因此无效。资源统计范围和缺失处理沿用 compiletest；单测没有权限的辅助项留空说明。
- 同轮双方有效且满足约束时，按默认组计算延迟降低率或吞吐提升率；单测不补跑对照。统计各策略有效/尝试次数、达标次数、主要指标中位数和最小–最大值；不以每秒采样点充当重复实验。

## 文件与验收

- 整批保存 `snapshot.json`、`schtest.json`、`metadata.json`。模式、执行范围、seed/orders、命令和最终状态放元数据；目标 RPS、容差、可选延迟上限及达标情况放配置/`attempt.json`，不重复放 CSV。
- `run-NNN/<策略>/`保存 `snapshot.json`、`schbench.log`、`native.json`（支持时）、`samples.csv`、`attempt.json`；有准备过程时加 `prepare.log`。多轮 scx 有 `scheduler.log`，整批有 `scheduler-controller.log`。未知当前策略用 `current/`，原始 ops 仍记录。
- `summary.csv`每次尝试一行：沿用 compiletest 公共字段及含义，只新增三个核心指标；其他结果留在原生日志和 JSON。

```csv
policy,run,valid,exit_code,wall_s,cpu_time_s,peak_memory_bytes,temp_start_c,temp_peak_sampled_c,avg_rps,request_p99_us,wakeup_p99_us,error
```

- 汇总输出形式及 `./Clean`沿用 compiletest，不强制新增 comparison.csv。清理只涉及本测试数据；与 compiletest 协调互斥，避免并发切换或删除运行中结果。
- 先用短任务验证四种入口、随机轮、解析、失败记录及中断清理。真实验收：无 n 各一行、run=1、保持当前策略；n=1 各四行、四策略各一次、run=1、顺序与记录一致，正常完成均有效并最终 disabled。n 轮完整执行为 4n 行。固定请求率须先配置 RPS，达标与否如实记录。
- README 说明用法、配置、字段、复用接口、真实验证环境与限制，不将替身验证声称为真实验收。真实长测试按用户要求执行。

依据：[schbench 官方说明与源码](https://github.com/masoncl/schbench)。

