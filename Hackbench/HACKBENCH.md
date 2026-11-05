# hackbench 测试任务

## 目标与目录

新增 Python 3 入口，复用已有 compiletest/schtest。先读 `Compile.md`、`SCHBENCH.md`和 实际代码：配置/快照、策略核验、门控执行、资源采样、常驻提权监督、sleep 阻止锁、互斥、失败处理和 Clean 均沿用现有接口，编译专用准备不适用。保持其他测试可用，README 列出复用接口。

代码、配置、测试、README 和 `bench/`放在 `Hackbench/`，本文路径相对此目录，先进入后执行；本文档留在项目根目录。用固定工作量的 IPC 竞争负载建立“负载 → 调度器”映射；先确认任务成功和策略有效，再比较完成时间，不做加权总分。

文件组织结构与Compile, SCHBENCH对齐

## 入口与配置

| 命令 | 执行范围 |
|---|---|
| `./hacktest` | 当前调度器测试一次，不切换 |
| `./hacktest n` | 四种调度器随机测试 n 轮，共 4×n 次 |
| `./hacktest n --groups G` | 指定通信组数，其余流程相同；也可省略 n 单测当前策略 |

- n 为正整数；省略 n 与 n=1 不同。有 n 时每轮独立随机排列 default、scx_lavd、scx_bpfland、scx_rusty，各运行一次，保存 seed/orders。
- 参数读自 `hacktest.json`，风格参考现有配置：程序路径、process/thread 模式、socket/pipe 通信、groups、fds、loops、datasize、CPU 范围、结果目录、冷却/采样间隔、超时及调度器控制参数。支持 `--output DIR`、`--groups G`、`--loops L`覆盖并保存实际配置。
- 默认使用进程＋本地 socket，原生命令为 `hackbench -P -g 4 -f 20 -l 10000 -s 100`。不设置实时调度或使用 `-F/--fifo`，负载以普通用户运行。
- 正常测试不打印进度或结果，启动错误写 stderr。结果为 `bench/results/<唯一批次>/`，每批只测一组负载配置，不额外套内部重复循环。

参数含义与选择：

| 参数 | 第一版设置 | 依据及用途 |
|---|---|---|
| 模式/通信 | process＋socket | 原生默认组合，先建立单一可复现的 IPC 场景；其他组合通过配置作为独立场景 |
| `-g` | 4 | 在本机约 14 个逻辑 CPU 上提供多任务竞争；后续分别测 1、4、8 组 |
| `-f` | 20 | 沿用默认每组 20 个发送者、20 个接收者；4 组共 160 个 worker |
| `-l` | 10000 | 增加固定通信工作量，降低极短运行对启动噪声的敏感性 |
| `-s` | 100 字节 | 沿用默认消息大小，先关注小消息频繁通信 |

这些数值是结合原生默认、公开测试用法和本机规模确定的起点，不是官方最优参数。先在独立预实验观察默认调度器耗时；若过短可增加 loops，使单次约 10～30 秒以便采样，此范围仅是工程目标。之后冻结 loops，正式四策略不得各自调参、自动缩短工作量或按固定时长杀死任务；超时仅用于终止异常并记失败。

1/4/8 组分别对应 40/160/320 个 worker，不等于轻/中/重 CPU 利用率，也不表示所有 worker 同时可运行。各组数单独成批和比较，不直接比较不同工作量的耗时。第一版无需自动遍历全部模式和组数。

## 运行流程与指标

1. **预检**：检查依赖、CPU 范围、参数、进程/文件描述符及 cgroup pids 限额，资源不足则说明，不擅自修改系统限额；保存整批配置/快照、内核、供电/模式、hackbench 软件包版本（可得时）、二进制摘要及实际命令。
2. **策略准备**：无 n 时接受外部当前策略，不调用 sudo、不切换或停止用户调度器；有 n 时按 compiletest 监督协议预检外部实例、授权一次，随机切换并等待稳定。策略核验及固定快照规则沿用现有实现。
3. **单次运行**：统一冷却、记录起始温度和本次快照；门控启动原生 hackbench，资源/温度采样和策略核验周期沿用 compiletest。固定工作量自然完成，不将“耗时越短”解释成减少实验内容。
4. **记录**：从门控 exec 到进程退出记录 `wall_s`，准备/冷却/收尾不计入；保存退出码和完整 stdout/stderr，解析原生 `Time:`为 `hackbench_s`，单位秒。原生计时与外层计时范围不同，按固定版本核对；不能互相替代或强行改成相等。
5. **收尾**：确认负载及 worker 已退出、保存记录。无 n 保持用户策略；有 n 清理自己的调度器并核验 disabled。中断/超时清理完整自有进程组或 cgroup；继续条件和监督丢失终止规则沿用 compiletest，不伪造尚未尝试的行。

- **主要指标**：相同模式、groups/fds/loops/datasize 和 CPU 范围下的 `hackbench_s`；`wall_s`保留作为端到端运行耗时和与现有框架一致的诊断字段。
- **辅助观察**：CPU 时间、进程树内存峰值、温度；已有采样支持时记录 CPU 利用率、上下文切换、迁移和 CPU PSI 到 samples/attempt，不为第一版强制增加高频 perf 跟踪或新增一套采集器。相关计数较少不自动等于性能更好。
- hackbench 同时包含 IPC 和内核数据处理开销，不描述为纯调度开销；不生成原生未提供的请求 p99、唤醒延迟或 JSON。不得把通信总时间当成单条消息延迟。
- `valid`要求正常退出、原生 Time 可解析、无明确 worker/通信失败且通用核验通过。异常、超时、参数/策略改变等保留无效行；慢本身不剔除。辅助项缺失留空并说明，不用 0 伪造，不偷偷重试覆盖。
- 统计有效/尝试次数、有效 `hackbench_s`的中位数和最小–最大值；同轮双方有效时算 `100×(默认−策略)/默认`，分母 0 留空。单测不补跑对照，不将采样点当成独立重复。3 轮只作摸底，差异接近波动时不强行排名。

## 文件与验收

- 整批保存 `snapshot.json`、`hacktest.json`、`metadata.json`，包括执行范围、seed/orders、配置、版本、单位/范围和最终状态；负载参数不重复铺进 CSV。
- `run-NNN/<策略>/`保存 `snapshot.json`、`hackbench.log`、`samples.csv`、`attempt.json`；有准备过程时加 `prepare.log`。多轮 scx 有 `scheduler.log`，整批有 `scheduler-controller.log`。未知当前策略用 `current/`，记录原始 ops。
- `summary.csv`每次尝试一行：沿用 compiletest 公共字段，只新增原生计时；详细参数、辅助计数、失败证据放 attempt/原生日志。

```csv
policy,run,valid,exit_code,wall_s,cpu_time_s,peak_memory_bytes,temp_start_c,temp_peak_sampled_c,hackbench_s,error
```

- 汇总方式及 `./Clean`沿用现有测试，不强制新增 comparison.csv；清理限本测试结果/验证记录，保留程序和共享配置。与 compiletest/schtest 协调互斥，避免并发切换或删除运行中数据。
- 先用小 loops 和输出样例验证两种入口、Time 解析、完整随机轮、失败/超时、中断及 worker 清理；短任务参数单独保存，不当正式成绩。
- 真实验收：`./hacktest`一行、run=1、保持当前策略；`./hacktest 1`四行、四策略各一次、run=1、顺序一致，正常完成均有效且最终 disabled。完整 n 轮为 4n 行。README 说明参数理由、用法、字段、复用接口和实际环境，不将替身验证声称为真实验收；真实长测试按用户要求执行。

## 参数参考

- [rt-tests 官方手册](https://kernel.googlesource.com/pub/scm/utils/rt-tests/rt-tests/+/61ba1aa3a0dbca5f6a8083daf6c068b05c080bb2/src/hackbench/hackbench.8)：模式、默认小消息参数及 Time 输出。
- [Intel LKP](https://github.com/intel/lkp-tests/blob/master/README.md)：对不同通信方式、process/thread 和并发规模分别配置测试；借鉴其变量划分，不直接套用服务器规模或修改系统设置。
- [LPC 2022 调度延迟实验](https://lpc.events/event/16/contributions/1273/attachments/975/1898/latency_LPC_sched_MC_2022.pdf)：使用 `-l 10000 -g 16`产生竞争背景；借鉴 loops 量级，不照搬其组数，也不将其中 cyclictest 延迟当作 hackbench 输出。

