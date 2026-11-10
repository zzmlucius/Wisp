# 前台＋后台混合调度器测试

在 `Wisp/Mixbench/` 运行，Python 3.12+、普通用户。保留同级 `Compile/compilebench_runner/` 和 `Schbench/schbench_runner/`。直接共同启动原生 schbench 与 sysbench，不并行调用其他测试入口。

## 本版参数与依赖

前台使用已有 `Schbench/bench/build/schbench` 及其 `provenance.json`；缺失时到 Schbench 执行 `./build-schbench`。本机已安装 sysbench 1.0.20；其他机器缺失时，Fedora 可自行执行 `sudo dnf install sysbench`。脚本不安装或升级依赖。

本版按 [MIXBENCH_PARAMS.md](MIXBENCH_PARAMS.md) 更新现有 `mixtest`，默认配置和示例配置一致：

| 参数 | 本版值 |
|---|---:|
| 前台目标 `rps` | 1000 请求/秒 |
| `rps_tolerance` | 0.02，即 ±2%（980–1020 请求/秒，含边界） |
| `latency_limit_us` | 请求 p99 ≤ 10000 µs，即 10 ms |
| 前后台各自运行时长 `runtime_s` | 60 秒，并发运行 |
| 后台 `threads` / `cpu_max_prime` | 14 / 20000 |

这是用户指定的首版待实测场景，不代表性能已经达标。前台保留原来的 14 个 worker；四种调度器共用同一参数和阈值，不自动调整。mixed 必须有有效的 RPS 和延迟上限；显式设为 `null` 时在启动前报错。front/back 可另批单独运行用于预实验和基线。

```sh
cd ~/Documents/code/Wisp/Mixbench
./mixtest --case front                    # 当前默认是固定 1000 RPS 的前台单独运行
./mixtest --case front --rps 1000          # 显式指定同一请求率
./mixtest --case back                     # 后台单独运行
```

需要尽力运行的前台预实验时，可先将配置的 `rps` 显式设为 `null`，再执行 `--case front`；mixed 此时拒绝启动，正式测试前恢复为 1000。命令行覆盖不会写回配置。其他候选参数分别成批，不能按各策略的结果反调阈值，也不自动补跑基线。

默认时长 60 秒，前台 `-m 1 -t 14 -r 60 -F 256 -n 5 -s 100 -R 1000 -w 0 -z 0`。关闭内部预热/周期清零/自动调压，`-i 61` 避免把周期 p99 当作独立最终分布。

本机后台实际命令为 `sysbench cpu --threads=14 --cpu-max-prime=20000 --time=60 --events=0 --rate=0 --report-interval=1 run`，自然运行到时长结束。本地 1.0.20 的 help 未声明 `--warmup-time`，实测传入会报 invalid option，因此不传该参数，原生 CPU 测试直接计时，不增加额外预热；若本地 help 支持该参数则显式传 `--warmup-time=0`。选择依据写入 `snapshot.json.versions.back.warmup`，实际命令写入 attempt。1 秒输出只保留日志，主要指标取最终 CPU speed 的 events/s；不对各秒数据求均值替代它。

当前可用 CPU 为 4–13，共 10 个；配置共享该范围。前台 14 worker、后台 14 线程保留任务规定的起点，存在超额并发，不意味着使用 14 个 CPU。若改为 7/14/28 个后台线程，应分别成批。两侧继承普通优先级，共用同一 CPU 范围，不改变 nice、权重或配额，不清缓存。迁移机器后先核对 CPU 范围。

## 运行

```sh
./mixtest                                # 当前策略一次 mixed，不切换、不调用 sudo
./mixtest 3                              # 每轮随机测试四策略，共 12 次
./mixtest 1 -1000                        # 四策略各测一次，统一目标 RPS=1000
./mixtest 1 -n 1500                      # 四策略各测一次，统一目标 RPS=1500
./mixtest 1 --case front                  # 四策略前台单独运行基线
./mixtest 1 --case back                   # 四策略后台单独运行基线
./mixtest 3 --rps 1000 --bg-threads 7 --output ./bench/results/scenario-7
```

省略轮数与 `1` 不同。默认 case=mixed；每批只有一个 case、一组固定参数。`-1000`、`-n 1000` 和 `--rps 1000` 等价，均指定前台目标 RPS；前面的 `1` 仍表示轮数。RPS 必须为正整数，省略时读取配置；`./mixtest -1000` 只测当前调度器一次。这里的 `-n` 属于 mixtest 的接口，原生 schbench 的 `-n 5` 参数仍保持配置值。

`--rps`、`--bg-threads`、`--output` 覆盖生效配置并保存，不修改输入文件。四策略使用同一目标 RPS，结果仍保存在 `bench/results/<唯一批次>/`。未设 RPS 的 front 是尽力模式；设 RPS 的 front 是固定请求率模式，两者不可直接混作基线。

每次统一冷却 30 秒；冷却、准备和收尾不计入 `wall_s`。`timeout_s=180` 是异常终止界限，不是负载工作时长。正常运行保持终端安静，结果保存到 `bench/results/<唯一批次>/`。多轮开始前须自行停止外部 scx；监督进程只在开始时询问一次 sudo 密码，此后常驻并持有整批 sleep/idle 阻止锁，批次内不再提权。单次保持用户当前调度器；多轮结束核验 disabled。

| 命令 | 尝试次数 | 正式负载时间约 |
|---|---:|---:|
| `./mixtest` | 当前策略 1 次 | 60 秒 |
| `./mixtest 1` | 四策略各 1 次 | 240 秒，4 分钟 |
| `./mixtest n` | 四策略各 n 次 | 240n 秒，4n 分钟 |
| `./mixtest 3` | 共 12 次 | 720 秒，12 分钟 |

每次前后台同时运行，因此不是 60+60 秒。整批实际时间还需加入每次冷却、准备、策略切换和收尾；脚本不增加内部重复或预实验次数。

## 同步与有效性

共享测量器先门控一个普通用户协调器；协调器在没有后台线程时 fork 两个原生子进程，全部 SIGSTOP 就绪后共同 SIGCONT。子进程在 exec 紧前写入单调时钟时间戳，退出由 waitpid 观测（约 10 ms 轮询，忙时可能更迟）。不是通过依次启动两个独立测试入口来近似并发。

`synchronization` 预先固定：

| 参数 | 默认 | 检查 |
|---|---:|---|
| `max_start_skew_s` | 0.1 秒 | 两个 exec 紧前时间戳的错位上限 |
| `min_overlap_ratio` | 0.99 | 进程运行区间交集 ÷ 较短区间 |
| `max_native_overhead_s` | 0.5 秒 | 非轮询额外预算；前台另计固定版本一次 1 秒结束检查周期，后台仍直接核对原生时长 |

**进程重叠不等于原生统计窗口一致。** 首版只支持已核对计时行为的 schbench 提交 `24e32b8ef67cbafb0761aed68747d90a291767ae`（同时核验来源记录与程序摘要）及 sysbench 1.0.20，并检查本地 help。schbench 创建消息线程后启动时长计时，结束后等待线程再打印累计分布；sysbench 在线程全部就绪时开始、worker join 后停止。原生绝对统计起止未直接观测，明确保存为缺失，按近似同步的稳态区间解释；不虚构精确共同窗口。

计时核验版本 `bounded_approximate_v2` 修正了将 schbench 配置的 60 秒当成原生实测统计时长的问题。固定源码在 `sleep_for_runtime` 内每轮检查结束条件，未结束时执行 `sleep(1)`；配置时长不能直接等同于进程退出时间。新规则把这一个已知轮询周期单独列出，前台预算为 `1.0 + max_native_overhead_s`，当前为 1.5 秒；后台没有这项轮询豁免，仍核对真实 `total time` 和固定时长。

例如前台运行 61.1 秒会留下轮询预算内的警告，运行 61.7 秒仍然无效，不能因为想得到有效结果继续放宽。该预算不能证明多出的时间全部属于收尾，也不能证明前后台的原生统计窗口精确一致；需要严格共同窗口时应另行增加原生边界观测。启动错位、进程重叠不足、超时、退出异常、策略/配置/供电变化继续判无效。负载命令、1000 RPS ±2%、请求 p99≤10 ms、60 秒时长和后台 14 线程均未因这次计时修正改变。

核验版本、来源和预算保存在 `metadata.json.timing_validation`、`snapshot.json.timing_validation` 及逐次 `attempt.json.synchronization`。前台字段明确标为 `configured_duration_s`，后台为 `native_duration_s`，原生绝对统计起止继续留空；`wall_s` 仍是实测进程窗口。历史结果不改写，前后版本的有效次数不能直接混为同一验收口径。性能是否达标继续单独记录，修正计时后不会自动将超限的前台变为达标。

启动严重错位、重叠不足、额外启动/收尾时间超限、版本计时口径不明、原生失败、必需指标缺失或通用核验失败，均为技术无效。短任务的启动/退出比例更大，验证时使用独立配置并保留其容差，不冒充 60 秒正式成绩。

`valid` 与前台是否达标分开：程序正常自然退出、指标可解析、同步和通用核验通过才技术有效。平均 RPS 在目标 ±容差内且请求 p99 ≤ 上限，前台才达标。唤醒 p99 作为辅助指标，不另加隐含阈值。性能未达标、后台吞吐为零仍保留有效结果；`attempt.json.constraints.met=false`，不通过剔除慢结果来排名。

任一原生负载失败，协调器立即停止另一侧。超时、Ctrl+C、TERM 由共享自有 session/cgroup 收尾；不处理无关进程。单次失败保留无效行；多轮仅在已核验恢复默认后继续，监督进程丢失、恢复失败或用户中断则停止，不伪造未尝试的行。

## 结果

整批保存 `mixtest.input.json`、生效 `mixtest.json`、`snapshot.json`、`metadata.json`、`summary.csv`。元数据记录 case、seed/orders、命令模板、统计口径、版本、同步容差、最终策略和汇总。逐次实际命令及原始退出码见 attempt。

`run-NNN/<策略>/` 包含 `snapshot.json`、`front.log`、`back.log`、支持时的 `front-native.json`、`samples.csv`、`attempt.json`；只生成该 case 实际运行的负载文件。`launch.json` 固定共同启动参数，`execution.json` 持续保存两侧 PID、时间戳、退出码及可得的分侧资源，协调器日志为 `prepare.log`。多轮另有 scheduler/controller 日志。未知外部策略使用 current 目录并保存原始 ops。

```csv
policy,run,valid,exit_code,wall_s,cpu_time_s,peak_memory_bytes,temp_start_c,temp_peak_sampled_c,avg_rps,request_p99_us,wakeup_p99_us,back_events_s,error
```

| 字段 | 含义 |
|---|---|
| `avg_rps` | 前台最终整次平均请求/秒；不使用 RPS p50 |
| `request_p99_us` / `wakeup_p99_us` | 前台最终累计请求/唤醒 p99，µs；不平均多个窗口 p99 |
| `back_events_s` | 后台原生最终 events/s，前台达标后越高越好 |
| `wall_s` | 首个正式原生 exec 紧前到最后一个退出观测，单调时钟秒；不含协调器准备/收尾 |
| `cpu_time_s` | 共同父 cgroup 的两侧完整进程树及协调器少量开销累计 CPU 秒 |
| `peak_memory_bytes` | 共同父 cgroup 峰值字节，包含缓存/内核内存，不含 swap；不相加子组峰值 |
| `temp_start_c` / `temp_peak_sampled_c` | 共同释放门控前的温度 / 正式负载窗口采样最高温，℃ |
| `exit_code` | mixed 双方均正常退出才为 0；两侧原始退出码放 attempt |

优先建立共同父 cgroup 与两个子 cgroup，父级汇总覆盖所有后代；没有权限或未启用控制器时辅助项留空并说明。资源采样遵循既有约每秒规则、策略核验约 0.1 秒，协调器检查两侧 affinity/nice。公共样本过滤到正式原生运行区间；父 cgroup 累积资源包含协调器少量开销，不伪装成可精确剥离的两侧成本。单独 case 不适用的 CSV 字段留空。

`metadata.json.analysis` 按策略记录有效/尝试数、前台达标/未达标数、各指标中位数和最小–最大值，并另列仅达标结果的主要指标。mixed 只有同轮双方都技术有效且前台达标，才计算 `100×(策略后台吞吐−默认后台吞吐)/默认后台吞吐`，零分母留空。先满足前台约束，再比较后台吞吐，不做加权分数；采样点不是重复实验。

可选 `baselines.front/back` 填此前 front/back 批次目录（相对配置文件或绝对路径）。只使用同策略、同侧参数/CPU/时长、同程序摘要和相同内核/供电环境的有效基线，计算 mixed 请求 p99 ÷ front 基线 p99 中位数，以及 mixed 后台吞吐 ÷ back 基线吞吐中位数。缺基线、不匹配或零分母均留空说明；不自动补跑，不计算动态恢复时间。

## 清理与复用

```sh
./Clean --dry-run                        # 预览全部 mixtest 记录
./Clean                                  # 清除本测试结果/验证记录
./Clean 20261106T000000.000000Z-12345      # 只清指定批次，替换为实际 ID
```

清理遵循 metadata 的 mixbench 标记及结果根目录索引，包括 `--output` 创建的记录；拒绝路径越界和符号链接，不删除其他测试、源码、程序或配置。被引用的 front/back 基线也是 mixtest 结果，清理它们后相关比率会留空。运行与 Clean 共用跨测试互斥锁。

| 复用接口 | 用途 |
|---|---|
| `compilebench_runner.cli.acquire_lock / scheduler_specs / build_environment` | 共用互斥、完整调度器命令/ops 校验、受控环境（额外固定 LC_ALL=C） |
| `compilebench_runner.policy.Scheduler / policy_name` | 当前策略与任务 ext.enabled 核验 |
| `compilebench_runner.control.BatchScheduler` | 一次授权、常驻监督、稳定等待、整批阻止挂起及恢复默认 |
| `compilebench_runner.workload.measure / SAMPLE_FIELDS` | 外层门控、原有资源/温度/供电采样与 session 收尾 |
| `compilebench_runner.metrics.BuildCgroup` | 父子资源统计；共享实现增加自有子树的递归存活核验与空组收尾 |
| `compilebench_runner.system / interrupts.Interrupts / cleanup.remove_entry` | 固定快照、摘要、原子保存、中断保护、安全删除 |
| `schbench_runner.cli.load_config / native_command / cpuset / finite / effective` | 前台原生配置校验与命令构造、参数规范化 |
| `schbench_runner.results.parse_results / distribution` | 整次前台指标解析与独立尝试的分布统计 |

共享测量器仅增加单调时钟起止证据字段，原有单负载计时不变。共享 cgroup 递归清理只作用于其自己创建的 UUID 目录；监督器增加 mixtest 的阻止挂起说明。

## 验证与环境

执行 `python3 -m unittest discover -s tests -v` 验证配置、三种 case、随机完整轮、技术有效性/前台达标分离、解析、同步失败、固定输入变化、单侧失败、超时、中断、子进程清理与 Clean。替身测试只能验证控制逻辑，不能宣称为四种真实调度器验收。

环境为 Fedora 44、Python 3.14.8，前台固定提交和 sysbench 1.0.20 实际程序可用。此前参数更新阶段的 37 项自动测试通过，并完成一次真实 default 策略下的 3 秒 mixed 冒烟测试：技术有效、父子 cgroup 已清理，前台未达标如实保留。记录见 `bench/validation/params-native-smoke/` 和 `params-tests/`。短测使用独立配置，仅将时长、冷却、采样和超时改为验证值；当时尚未执行 60 秒正式四策略测试，不能把短任务当作正式成绩；随后用户运行的三批正式结果保留在 bench/results，其中部分记录触发了旧版计时核验。

计时核验修正后共 40 项自动测试通过，另做一次真实 3 秒 mixed 冒烟验证，并只读重放既有三批的同步证据；记录在 `bench/validation/timing-v2/`。重放只说明新口径下同步检查的判定，不替代重新运行，也没有回写旧 CSV。真实短测技术有效且前台未达标，验证了两类判定仍分开；这次未执行新的正式四策略长测试。

计时核对依据：[本机固定版本 schbench 源码](https://github.com/masoncl/schbench/blob/24e32b8ef67cbafb0761aed68747d90a291767ae/schbench.c)、[sysbench 1.0.20 线程/统计计时](https://github.com/akopytov/sysbench/blob/1.0.20/src/sysbench.c)、[CPU speed 原生定义](https://github.com/akopytov/sysbench/blob/1.0.20/src/tests/cpu/sb_cpu.c)、[sysbench 参数说明](https://github.com/akopytov/sysbench#general-command-line-options)。
