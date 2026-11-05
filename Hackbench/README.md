# hackbench 调度器测试

在 `Wisp/Hackbench/` 运行，读取同目录 `hacktest.json`。Python 3.12+，原生程序依赖 rt-tests 的 hackbench；本机已安装 `realtime-tests 2.11-1.fc44`，使用 `/usr/bin/hackbench`，无需重新构建或安装。本测试复用同级 Compile 和 Schbench 的 Python 接口，保留这三个目录的相对布局。

```sh
cd ~/Documents/code/Wisp/Hackbench
./hacktest                       # 当前调度器一次，不切换，不调用 sudo
./hacktest 1                     # 四种策略各一次，每轮独立随机排列
./hacktest 3                     # 三轮，共安排 12 次
./hacktest --groups 1            # 当前调度器，另一个组数的独立场景
./hacktest 3 --groups 8          # 八组场景，三轮
./hacktest --groups 1 --loops 10 --output ./bench/pilot-results
./hacktest --help
```

正常运行不打印进度或结果。无 n 时是当前策略的一次测试；n=1 则安排 default、scx_lavd、scx_bpfland、scx_rusty 各一次。多轮必要时只在开始前询问一次 sudo 密码，之后使用常驻监督进程，不续期、不模拟输入密码。首次运行先核对配置中的程序路径和完整 ops，其他机器参考 `hacktest.example.json`。

默认原生命令为：

```sh
hackbench -P -g 4 -f 20 -l 10000 -s 100
```

进程＋本地 Unix socket 是第一版的固定场景；每组有 20 个发送者和 20 个接收者，4 组共 160 个 worker，每个发送者循环向本组各接收者发送 100 字节消息。未传 `-p` 时就是本地 socket，不使用 TCP、实时调度、`-F/--fifo` 或内部多次重复。负载始终由普通用户执行，仅多轮调度器监督进程提权。

`workload.mode` 支持 `process`/`thread`，分别传 `-P`/`-T`；`communication` 支持 `socket`/`pipe`，后者传 `-p`。每批只使用一个组合，不自动遍历。`--groups`、`--loops`、`--output` 覆盖配置而不修改原文；最终生效配置保存到该批 `hacktest.json`，输入原文另存 `hacktest.input.json`。输出根目录中的每次调用都创建唯一批次。

参数及选择理由：

| 参数 | 当前值 | 用途 |
|---|---:|---|
| groups | 4 | 建立多任务 IPC 竞争；1/4/8 组应作为不同场景单独成批 |
| fds | 20 | 每组 20 个发送者、20 个接收者，沿用默认规模 |
| loops | 10000 | 增加固定通信工作量，降低过短任务启动噪声的影响 |
| datasize | 100 字节 | 固定小消息场景 |
| cpus | 4–13 | 当前执行环境可用的 10 个逻辑 CPU，门控 exec 前设置 affinity |
| cooldown_s | 30 | 各策略采用相同冷却间隔 |
| sample_interval_s | 1 | 资源、电源、温度采样周期；策略另约每 0.1 秒核验 |
| timeout_s | 300 | 异常运行的终止上限，不作为测试时长或调参手段 |

这些是起点，不是官方最优参数。应先做独立预实验观察默认策略耗时，必要时增加 loops，使正式单次约 10～30 秒，随后冻结配置。程序不会自动校准、缩短 loops、重试覆盖记录或按固定时长结束正常负载。1/4/8 组对应 40/160/320 个 worker，不能直接当成轻/中/重 CPU 利用率，也不能比较不同组数或 loops 的原始完成时间来给调度器排名。

启动前检查程序可执行性、`--help` 中的参数与版本、CPU 范围、整数溢出、RLIMIT_NOFILE、RLIMIT_NPROC 和当前 cgroup 的可见祖先 pids 限额。进程模式按每组描述符保守上界核对，线程模式按共享的全部描述符核对；记录 worker 数和 8 个框架辅助任务的预留量。不会自动提高任何系统限额。检查是瞬时的，`/proc` 在命名空间中可能只显示部分用户任务，因此不保证后续资源一定充足；创建或通信失败仍保留无效记录。

无 n 时接受当前外部 scx，结束不切换或停止它；未知当前策略使用 `current/`，结果与快照仍保存原始 ops。有 n 时先拒绝已运行的外部 scx，只管理本批自己启动的调度器，独立随机排列每轮四种策略并记录 seed/orders。一次初始授权后由常驻监督进程处理切换，经过 systemd-inhibit 持有覆盖整批的 `idle:sleep` 阻止锁；获取失败不开始负载，退出自动释放，不修改电源模式或屏幕锁设置。

切换后连续稳定 5 秒（可配置）再冻结策略快照；冻结后重载、回退或 ops/enable_seq 等变化都判无效，不重新建立基线。完整核验 state、root/ops、enable_seq、switch_all 和任务 ext.enabled。bpfland 沿用 `--primary-domain all`，属于显式参数场景；rusty 保持原参数，运行时错误按实际失败记录，不自动关闭其负载均衡。

整批保存配置、二进制 SHA256、原生帮助与版本、软件包信息（可得时）、CPU、运行内核、实际供电/模式和固定温度传感器。测试前后及采样时核对输入摘要、可用/在线 CPU 和策略，按现有框架核对电源；EPP 单独变化只作诊断。离散观测无法保证捕获任意短暂变化。

每次固定工作量自然结束，完整 stdout/stderr 合并保存。主要指标为 **hackbench_s**，即原生 `Time:`，越小越好。依据 rt-tests v2.11 源码，原生计时从所有 worker 创建并就绪后、发出 GO 前的 gettimeofday，到等待 worker 结束后的 gettimeofday；外层 **wall_s** 从门控 exec 前 SIGCONT 到 wait4 返回，包含启动和创建 worker 等开销，使用单调时钟。两者范围和时钟不同，不互相替代或强行改成相等。

除成功退出和可解析 Time 外，还核对原生回显的 mode、groups、fds、worker 数、loops 和消息大小，检查明确的通信/worker/信号错误。rt-tests 2.11 收到终止信号后仍可能打印 Time 并返回 0，部分 worker 错误也未完整传递为父进程退出码，所以不能只检查 exit_code。解析只接受唯一、有限、非负的 Time；短任务的 `0.000` 原样保留，不伪造更高精度，也不拿它当单条消息延迟。

正常结束、中断和超时均检查并清理本次自有进程组及独立 cgroup，不终止无关进程。超时后先终止本组、必要时强制清理并等待，原生退出码即使为 0 也无效。超时和普通运行失败在确认恢复 disabled 后可继续下一策略；监督进程丢失、无法恢复默认或用户中断则终止剩余安排，不补造未尝试行。无 n 时始终保留用户调度器。

结果位于 `bench/results/<UTC时间戳-PID>/`：

```text
snapshot.json                 # 固定输入、资源预检、程序版本与环境
hacktest.json                 # 最终生效配置
hacktest.input.json           # 用户配置原文
metadata.json                 # scope、seed/orders、最终状态、analysis
summary.csv                   # 每次尝试一行
scheduler-controller.log      # 多轮监督进程日志
run-001/<策略>/
    snapshot.json             # 本次稳定策略与实际命令
    hackbench.log             # 完整原生 stdout/stderr
    samples.csv               # 资源、温度、电源与策略样本
    attempt.json              # 结果、资源缺失、原生核验与超时证据
    scheduler.log             # 多轮 scx 的日志
```

CSV 使用以下固定字段，不重复铺开负载参数：

```csv
policy,run,valid,exit_code,wall_s,cpu_time_s,peak_memory_bytes,temp_start_c,temp_peak_sampled_c,hackbench_s,error
```

`run` 为轮号，无 n 时固定为 1。只有正常退出、原生核验通过且全程通用条件满足才为 valid；慢本身不剔除。CPU/内存覆盖独立 cgroup 的整个负载树，内存包含文件缓存和内核内存，不含 swap。缺少统计能力则留空并说明原因；固定传感器的 driver、label 和路径在 snapshot 中，温度单位 °C，峰值只取正式窗口内样本。

现有采样器未提供上下文切换、迁移和 CPU PSI，第一版不伪造这些计数，也不新增高频 perf 采集器；缺失原因写 attempt。能取得 CPU 时间时，另给出 CPU 秒 / wall_s / CPU 数的平均利用率辅助估算，不新增 CSV 主要指标，不作能耗结论。hackbench 包含 IPC 和内核数据处理等开销，不能描述成纯调度开销。

`metadata.json` 的 `analysis` 给出各策略有效/尝试次数、有效 hackbench_s 的中位数及最小–最大值。同轮双方有效且默认组时间大于 0 时，计算 `100×(默认−策略)/默认`；正值表示更快。单测不补跑默认组；采样点不是独立重复，三轮只作摸底，差异接近波动时不强行排名。没有原生请求 p99、唤醒延迟或 JSON，不额外制造这些数据。

退出码：0 表示安排完成且全部有效；1 表示批次失败或存在无效行；2 表示参数/启动预检失败；130 表示中断。

Clean 用法与前两种测试一致：

```sh
./Clean --dry-run
./Clean
./Clean 批次ID --dry-run
./Clean 批次ID1 批次ID2
./Clean --help
```

省略批次 ID 清除本测试的全部结果/验证记录，包括已登记 `--output` 目录里的自有批次。删除限定于 metadata 标记为 hackbench 的记录；预览不删除，未找到指定批次先报错，不部分删除。程序、配置、源码、构建以及 Compile/Schbench 数据均保留。每批配置副本随记录删除，固定配置不删。编译、schbench、hackbench 和各自 Clean 共用互斥锁，不能同时切换调度器或清理正在运行的实验。

复用接口：

| 接口 | 用途 |
|---|---|
| compilebench.cli.acquire_lock / scheduler_specs / build_environment | 三种测试及清理互斥、命令/ops 校验、受控环境 |
| compilebench.policy.Scheduler / policy_name | 当前策略观测、完整状态和 ext.enabled 核验 |
| compilebench.control.BatchScheduler | 一次授权、稳定等待、常驻监督、防挂起与收尾 |
| compilebench.workload.measure / BuildCgroup | 门控计时、采样、affinity、完整负载树统计与清理 |
| compilebench.system / Interrupts | 环境、摘要、温度/电源、原子保存和中断保护 |
| schbench_runner.cli.cpuset / finite / effective / append_error | 参数规范化、最终配置和错误合并 |
| schbench_runner.results.distribution | 独立运行结果的中位数及范围 |
| compilebench.cleanup.remove_entry | 删除已通过范围核验的记录条目 |

共享测量器仅新增可选 `timeout_s=None`，compiletest/schtest 原有调用继续无限等待正常负载结束；监督器补上 hacktest 的抑制锁描述，前两种测试的实际启动参数保留。不改已有实验配置，也不安装软件或修改系统设置。

验证命令为 `python3 -m unittest discover -s tests -v`，覆盖两种入口、随机完整轮、Time/失败解析、资源限额、输入变化、超时与中断清理、外部调度器保护、互斥和 Clean。实际短任务与回归记录在 `bench/validation/`，测试参数单独保存。真实四策略长测试按用户要求执行，替身和短任务不声称完成真实四策略验收。

本次在 Fedora 44、Linux `7.2.14-200.fc44.x86_64`、Python 3.14.8、hackbench 2.11 上验证：38 项 hacktest 专项测试、56 项 compiletest 兼容性回归和 39 项 schtest 兼容性回归通过。新增超时默认关闭，前两种测试没有启用超时。实际 CLI 中断和超时测试还检查了自有 worker 清理与无关进程保留。

原生验证记录为 `bench/validation/native-20261105T100519.147177Z/`。默认调度器下，4 组、20 fds、10000 loops 的独立预实验得到 `hackbench_s=3.302`、`wall_s≈3.324` 秒，低于 10～30 秒的工程目标；正式实验可先用 `--loops 40000` 另做预实验，确认后冻结，不自动改变任务文档的默认 10000。四种 process/thread × socket/pipe 小任务均通过；一次 0.15 秒异常超时正确记为无效，即使原生返回 0 也没有误判成功，结束 sched_ext 为 disabled。很短任务的原生 Time=0.000 已保留，不作性能成绩比较。

本环境有独立 cgroup CPU 统计，未启用 memory controller，内存峰值留空并记录原因。未运行真实四策略长测试，也没有把这些短探针声称为真实四策略验收。

原生行为依据：[rt-tests v2.11 hackbench.c](https://kernel.googlesource.com/pub/scm/utils/rt-tests/rt-tests/+/refs/tags/v2.11/src/hackbench/hackbench.c)、[任务文档引用的手册](https://kernel.googlesource.com/pub/scm/utils/rt-tests/rt-tests/+/61ba1aa3a0dbca5f6a8083daf6c068b05c080bb2/src/hackbench/hackbench.8)。
