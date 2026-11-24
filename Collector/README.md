# Wisp 采集层

`snap()` 输出固定顺序的 `double Vector[IND_COUNT]`。当前 schema v1 有 19 个指标，顺序、单位和范围见 [Indicator.def](Indicator.def)。不可用值为 `NAN`。`SampleMeta` 独立提供状态、单调时间戳、实际采样间隔和各指标的源观测窗口。每次尝试新建一个上下文。公开接口见 [monitor.h](monitor.h)。

```sh
make -C Collector
make -C Collector test
```

普通用户可运行基础采集。调用方链接 `Collector/bench/build/core/libwisp_monitor.a` 和 `-lm`。通过 `MonitorCreate()` 初始化上下文，随后直接调用 `snap()`，或通过 `MonitorLoop()` 定期接收向量。回调返回前使用本轮数据，跨回调使用时自行复制。调用方结束后调用 `MonitorDestroy()`。

Mixbench 在测试前构建采集器，在每次尝试的前后台进程进入独立 cgroup 后、放行前建立基线。采集进程位于两个负载 cgroup 之外。结果位于 `run-NNN/<策略>/`：

| 文件 | 内容 |
| --- | --- |
| `workload.csv` | 每行一个向量，前两列为 `timestamp_monotonic_ns,dt_s`，其后为 19 个值，缺失留空 |
| `workload-status.jsonl` | 同一时间戳、各列状态和读数窗口边界，列序与 schema 一致 |
| `workload-meta.json` | schema、单位、来源、探针状态、缺失计数和可用于训练的行号 |
| `collector.log` | 采集器启动和运行诊断 |

训练行号从表头之后的第一条记录开始计 1。仅标出有效混合尝试中、所有有效字段的源窗口都落在前后台共同存活区间内的行。边界使用现有 exec 前标记和较早的最后一次存活检查。它们描述进程范围，原生程序的内部计时边界没有被观测。每个模型仍应按自身所需指标检查缺失。原生请求 p99、RPS 和后台吞吐见 `summary.csv`。

可选配置放在 `Mixbench/mixtest.json` 的 `collector` 对象。配置 `"device": "nvme0n1"` 后读取该设备的瞬时在途读写请求。未指定设备时两列留空。cgroup 计数和 PSI 的可用性由当前用户的 cgroup 委派及内核决定。

可选 eBPF 构建需要 libbpf 开发文件、clang、bpftool 和内核 BTF：

```sh
make -C Collector bpf
```

将 `collector.bpf_object` 指向 `../Collector/bench/build/scheduler.bpf.o` 可尝试开启调度事件。运行时权限由内核和环境决定。加载、附着失败时返回缺失状态，并继续基础采集。延迟直方图按纳秒划分 64 个以 2 为底的桶，p95 输出目标桶上界，换算为微秒。每个 CPU 的事件累积值通过一次 per-CPU map 读取，跨 CPU 的计数不是同一瞬间冻结的。

只有在同一内核和所有待测调度器上确认 `update_rq_clock` 可附着、CO-RE 字段可读、`rq->nr_running` 语义符合预期后，才设置 `collector.runqueue_verified=true`。没有观察到所选 CPU 的新值时，运行队列列留空。

Fedora 44（内核 7.3.3）短混压已验证。普通进程作用域可取得全机、选定 CPU 和配置设备的基础指标。用户级 systemd 委派 cgroup 额外提供前后台 CPU 时间和 PSI，共取得 14 项。eBPF 对象可编译，本次用户权限下内核拒绝加载，5 项探针指标留空。openEuler 实机和运行队列语义待验证。
