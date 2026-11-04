| 接口 | 用户态怎么获取 | 能观察什么 | 对选择调度器的作用 |
|---|---|---|---|
| **procfs：全机统计** | `/proc/stat`、`/proc/meminfo`、`/proc/vmstat` | 各核 CPU 时间、可运行线程数、上下文切换次数、内存及换页情况 | 判断 CPU 饱和度、并行程度、各核负载差异 |
| **procfs：线程统计** | `/proc/<pid>/task/<tid>/{stat,status}` | CPU 时间、线程状态、自愿/非自愿上下文切换等 | 区分持续计算、频繁阻塞和被抢占行为 |
| **cgroup v2** | `/sys/fs/cgroup/<组>/` 下的 `cpu.stat`、`memory.stat`、`io.stat` 等 | 一组进程的 CPU 用量、内存和块 I/O 统计 | 将一个应用及其子进程作为整体观察 |
| **PSI** | `/proc/pressure/{cpu,memory,io}`，或 cgroup 中的 `*.pressure` | 任务因 CPU、内存、I/O 资源不足而停顿的时间比例 | 区分“资源用得多”和“已经出现资源竞争” |
| **调度统计 schedstat** | `/proc/<pid>/task/<tid>/schedstat` | 累计运行时间、运行队列等待时间、运行次数 | 获取较粗粒度的调度等待信息 |
| **perf_events** | `perf_event_open()`；实验时可用 `perf stat` | cycles、instructions、cache misses、page faults、迁移等 | 补充计算效率、缓存行为和迁移特征 |
| **tracepoints + eBPF** | 挂接调度事件，在内核聚合后由用户态读取 | 唤醒频率、等待时间分布、运行片段长度、迁移等 | 提取与调度策略直接相关的行为特征 |
| **taskstats / delay accounting** | Generic Netlink | 每任务 CPU 等待、同步块 I/O、换入、内存回收等延迟 | 更明确地区分时间耗在何处 |