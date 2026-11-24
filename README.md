# Wisp

Workload-Informed Scheduling Policy

Wisp 通过基准测试比较 Linux 调度器在不同负载下的表现，并用 Collector 采集供模型使用的系统负载向量。当前仓库提供测试与采集入口。

## Mixbench

Mixbench 同时运行前台 schbench 和后台 sysbench CPU。前台按固定 RPS 发请求，记录实际 RPS 和请求 p99。后台持续运行 CPU 计算，记录每秒处理事件数。技术核验通过且前台请求 p99 不超过配置上限时，结果才记为 `valid=True`。

| 内容 | 位置 |
| --- | --- |
| 参数、版本、运行命令和结果判定 | [Mixbench 文档](Bench/Mixbench/README.md) |
| 参数模板 | [mixtest.example.json](Bench/Mixbench/mixtest.example.json) |
| 本机生效配置 | `Bench/Mixbench/mixtest.json` |
| schbench 构建方式 | [Schbench 文档](Bench/Schbench/README.md) |
| 每批实际参数、工具版本及性能结果 | 结果目录中的 `snapshot.json`、`metadata.json` 和 `summary.csv` |

前台 schbench 固定在提交 `24e32b8ef67cbafb0761aed68747d90a291767ae`，构建记录位于 `Bench/Schbench/bench/build/provenance.json`。本机后台使用 sysbench `1.0.20`。换机器时，可从示例建立本地配置，再按本机环境核对 CPU 范围和调度器路径。

在 Wisp 根目录运行 `./Bench/Mixbench/mixtest`，用当前调度器测一次。运行 `./Bench/Mixbench/mixtest 3 --rps 1000`，每轮随机测试四种调度器，共三轮。

## Finegraintest

[Finegraintest.py](Tests/Finegraintest.py) 复用本机 Mixbench 配置，对前台线程、后台线程和目标 RPS 做全组合测试。除表中覆盖项外，其余负载参数与判定规则继承 Mixbench。入口和队列模块的分工见 [Tests 目录说明](Tests/README.md)。

| 配置 | 值 |
| --- | --- |
| 前台 schbench worker 数 | 4、8、14 |
| 后台 sysbench 线程数 | 4、8、14 |
| 目标 RPS | 250、500、750、1000、1250、1500、1750、2000 |
| 每批轮数 | 3 轮，每轮随机测试四种调度器 |
| 正式负载时长 | 前后台并发，各运行 30 秒 |
| 冷却 | 每次尝试前 30 秒，批次之间额外休息 45 秒 |
| 调度事件采集 | 使用 `scheduler.bpf.o`，每次尝试结束后卸载探针，`runqueue_verified=false` |

全量共 **72 批、216 轮、864 次尝试**。每轮四种调度器的结果都保存后，终端显示进度。仅负载和等待约 15 小时 17 分钟，实际耗时还包括准备、调度器切换和收尾。启动时可能需要输入一次 sudo 密码，后续授权非交互。队列运行期间阻止熄屏和休眠，结束或按 Ctrl+C 后释放。

从 Wisp 根目录运行：

```sh
make -C Agent/Collector bpf      # 构建采集器和调度探针
./Tests/Finegraintest.py --check      # 检查配置和依赖
./Tests/Finegraintest.py --check-ebpf # 实跑一次，检查调度指标和探针卸载
./Tests/Finegraintest.py              # 运行全量队列
```

基准成绩写入 `summary.csv`，逐次负载向量另存为 `finegrain-workload.csv`，采样状态和元数据使用同一文件名前缀。新结果位于 `Bench/Mixbench/bench/results/finegraintest/`。本机已有的两批合并数据位于 `Agent/Collector/result/`。

## Collector 采集层

1. **主调用链**
   `main() → MonitorCreate() → MonitorLoopNs()`。循环每轮调用 `snap()`，通过 `MonitorSources` 调用各读取函数并组装 19 维向量，再调用 `sink(vector, &meta, userdata)` 交付样本。命令行入口传入的 `sink` 是 `write_sample()`。

2. **监测接口和数据**

   | 数据来源 | 指标 |
   | --- | --- |
   | `/proc/stat`、`/proc/pressure/{cpu,io}`、`/proc/meminfo` | CPU 使用率、每 CPU 可运行任务数、上下文切换率、I/O 等待比例离散程度、CPU 和 I/O 压力、可用内存 |
   | 前后台 cgroup 的 `cpu.stat` 和 `cpu.pressure` | 各自的 CPU 用量和 CPU 压力 |
   | `/sys/block/<设备>/inflight` | 指定设备正在处理的读写请求数 |
   | eBPF 探针更新的 BPF map | 唤醒率、唤醒延迟 p95、迁移率、每 CPU 切换次数方差，以及启用且验证后才采集的运行队列最大值 |

3. **扩展监测数据**

   在 [Indicator.def](Agent/Collector/Indicator.def) 末尾追加指标，填写名称、单位、作用范围和数据源，保留已有指标的位置。复用已有数据源时，在 [Monitor.c](Agent/Collector/Monitor.c) 对应的读取函数中填入新指标的值和状态。新增数据源时，在 [internal.h](Agent/Collector/internal.h) 的 `enum Source` 中声明，在 `MonitorSources` 中注册读取函数。若需要新的内核事件，再扩展 [scheduler.bpf.c](Agent/Collector/scheduler.bpf.c) 和 [Probe.c](Agent/Collector/Probe.c) 中的采集与读取逻辑。

4. **暴露给模型的接口**

   同进程接入时，模型层通过 [monitor.h](Agent/Collector/monitor.h) 将自己的 `MonitorSink` 回调传给 `MonitorLoopNs()`，每轮直接收到 `vector` 和包含时间戳、指标状态的 `meta`。模型也可以直接调用 `snap()` 自行决定采样时机。离线读取时，命令行采集器提供 `workload.csv`、`workload-status.jsonl` 和 `workload-meta.json`。字段说明见 [Collector 文档](Agent/Collector/README.md)。

## 调度器与版本

| 策略 | 版本或来源 |
| --- | --- |
| `default` | 禁用 sched_ext 后使用的 Linux 内核默认调度策略，版本随被测内核。本机核对时为 Fedora 44 的 `7.3.4-200.fc44.x86_64`，历史测试以批次快照为准。 |
| `scx_lavd` | `1.1.4`，来自 Fedora `scx-scheds-1.1.4-1.fc44.x86_64`。 |
| `scx_bpfland` | `1.1.4`，来自同一 Fedora 软件包。Mixbench 配置传入 `--primary-domain all`。 |
| `scx_rusty` | `1.1.4` 修复版，Cargo 安装记录固定在提交 `c8ab7bd73b98702e6c49264c774e1b8d48a75886`。 |

Rusty 的修复让负载均衡器跳过活跃任务表中失效的记录。如果任务已退出或记录槽位已被其他任务复用，旧记录可能被读成零负载，导致 Rusty 以 `Expected nonzero load transfer` 提前退出。补丁在任务退出时标记记录失效，并在均衡时检查记录所属轮次。[上游 PR #3936](https://github.com/sched-ext/scx/pull/3936) · [问题 #3876](https://github.com/sched-ext/scx/issues/3876)

本机自动切换使用 `/home/lucius/.local/scx-rusty-fixed/bin/scx_rusty`，配置项为 `schedulers.scx_rusty.command`。手动测试修复版时也使用这个完整路径。系统原版仍在 `/usr/bin/scx_rusty`。两个二进制都可能显示 `1.1.4`，复现实验时应同时记录二进制路径和修复版提交。

## 其他基准测试

[Compile](Bench/Compile/README.md) · [Schbench](Bench/Schbench/README.md) · [Hackbench](Bench/Hackbench/README.md)