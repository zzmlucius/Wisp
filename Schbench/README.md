# schbench 调度器测试

在 `Wisp/Schbench/` 中运行。Python 3.12+，正式负载使用普通用户；四策略批次额外需要 sudo、systemd-inhibit 和已安装的三个 scx。本目录复用同级 `Compile/compilebench/`，请保留两者的目录关系。

```sh
cd ~/Documents/code/Wisp/Schbench
./schtest                 # 尽力运行，当前调度器一次
./schtest 1               # 尽力运行，四策略各一次，随机顺序
./schtest 3               # 三轮，每轮独立随机排列，共 12 次
./schtest -R --rps 100    # 固定请求率，当前调度器一次；100 仅是命令示例
./schtest -R 1 --rps 100  # 固定请求率，四策略各一次
./schtest --help
```

原生 schbench 已在本目录构建。`build-schbench` 将官方源码固定在提交 `24e32b8ef67cbafb0761aed68747d90a291767ae`，构建到 `bench/build/schbench`，不会安装系统文件。重新构建可运行 `./build-schbench`，输出和来源分别保存到 `bench/build/prepare.log`、`provenance.json`。缺少源码时下载该固定提交，核对 `schbench.c` 的 SHA256 后构建；依赖 GCC、glibc 头文件和 pthread/libm。它与两种测试及 Clean 使用同一把互斥锁。

首次使用编辑 `schtest.json`；迁移机器时参考 `schtest.example.json`。路径相对于配置文件，入口从自身目录读取配置，支持从其他工作目录调用。程序的 `--help` 用于核对参数支持情况；该上游程序没有独立的版本选项，以来源提交和二进制 SHA256 标识版本。原生 JSON 的 `version` 是运行内核的 release，不当成 schbench 版本。

当前执行环境允许 CPU **4–13**，因此实际配置固定这 10 个 CPU 和 10 个 worker；示例保留任务文档的 14 worker 起点。正式场景可先按实际环境调整 `cpus` 和 `worker_threads`，然后保持所有策略一致。默认 `-m 1 -r 60 -F 256 -n 5 -s 100`、开启 locking、30 秒冷却、1 秒资源采样，约 0.1 秒检查策略。样例参数不是最优配置；本次短验证不构成场景校准。

`cpus` 是明确的范围，如 `4-13` 或 `0-3,6`，也接受冻结配置中的 CPU 整数数组。门控子进程在 exec 前设置 affinity，所有负载线程继承该范围；不限制调度器观察到的整个机器拓扑。配置 CPU 超出当前可用范围会在启动时报错。快照保存父进程可用 CPU、在线 CPU 和负载 affinity；运行期间改变会判无效。

固定请求率的正式目标需要先通过预实验确定；配置 `rps` 默认 `null`，避免擅自挑选正式负载。设置后可省略 `--rps`，例如 `./schtest -R 3`。包装器的 `-R` 是模式开关，原生实际命令是 `schbench -R VALUE`；尽力模式不传原生 `-R/-A`。`--rps` 只在固定模式允许使用。上游按 message_threads 整除请求率，因此要求目标可被消息线程数整除，避免静默降低目标。

两种模式均支持 `--output DIR`，DIR 相对于调用时的工作目录，是结果根目录，程序在下面创建唯一批次。最终生效配置存为该批 `schtest.json`，输入原文另存 `schtest.input.json`，不会覆盖用户配置。所有输出根目录登记在 `bench/results-index.json`，供 Clean 查找本测试自己的记录。

每次原生命令显式带 `-w 0 -z 0`，不清缓存，不使用自动 RPS、校准或 pipe 模式；固定 locking。`-i runtime+1` 避免无用的中间报告，最终统计仍为整次累计分布。记录并核对配置原文、最终配置、二进制和来源记录的摘要；从同一程序、同一场景开始测量。

无轮数时只借用当前调度器，既不调用 sudo，也不启动或停止用户调度器。未知 scx 仍按原始 ops 记录，目录为 `current/`。单次模式沿用 compiletest，不取得多轮专属的防挂起锁。

有轮数时，开始前拒绝已有外部 scx，由同一 Python 父进程先执行一次 `sudo -v`，再用 `sudo -n` 启动常驻监督进程。整个批次不再调用 sudo、不保存或模拟输入密码。监督进程经 systemd-inhibit 取得整批 `idle:sleep` 阻止锁，获取失败不开负载，退出自动释放；不改电源模式或屏幕锁设置。屏幕解锁提示不代表测试重新要求 sudo 授权。

四种策略每轮各一次，种子和实际排列存入 metadata。切换后需连续稳定 `scheduler_settle_s`（默认 5 秒），总加载时限 `scheduler_timeout_s`（默认 30 秒）。此后冻结完整 `state/root/ops/enable_seq/switch_all`，运行中同时核对负载任务的 `ext.enabled`。启动阶段的状态变化留证；冻结后回退或重新加载即无效，不重试、不更新基线。

已沿用本机 bpfland 的 `--primary-domain all`，避免已观察到的 auto 主域电源变化重载路径，结果属于显式参数实验。rusty 保持原命令：本机 1.1.4 曾反复出现 `Expected nonzero load transfer`，这是调度器运行时失败，包装器会记录无效并保留日志。它不会悄悄添加 `--no-load-balance` 来改变算法；启动稳定等待也不能保证避免运行时错误。

供电和桌面模式使用批次开始时实际读值；EPP 单独变化仅诊断。不会把鼠标等外设电池当作主机供电，也不以持续时间长剔除结果。离散检查无法保证捕获任意短暂变化。

每次失败保留一行；只有确认恢复 disabled 后才继续。恢复失败、监督进程丢失或中断则终止剩余安排，不补造未尝试行。正常、错误、Ctrl+C、SIGTERM 和控制管道 EOF 都清理本程序自己的负载、调度器与空 cgroup。无轮数时保持用户原策略。

结果保存在 `bench/results/<UTC时间戳-PID>/`：

```text
snapshot.json                 # 整批参数、环境、程序身份、传感器、初始策略
schtest.json                  # 最终生效配置，含模式与命令行覆盖
schtest.input.json            # 原始输入配置
metadata.json                 # seed/orders、命令、最终状态、失败原因、analysis
summary.csv                   # 每次尝试一行
scheduler-controller.log      # 多轮监督进程日志
run-001/<策略>/
    snapshot.json             # 已稳定的策略快照和本次实际命令
    schbench.log              # 完整 stdout 和 stderr 合并日志
    native.json               # 原生支持 -j 时保存，所有百分位保留
    samples.csv               # 资源、温度、电源和策略采样
    attempt.json              # 结果、指标来源、缺失原因与达标情况
    scheduler.log             # 多轮 scx 日志
```

正常运行不向终端打印进度或结果。启动错误可写 stderr，必要时多轮开始前询问一次 sudo 密码。退出码 0 表示已尝试的测试均有效且批次完成；1 表示有无效行或批次失败；2 表示参数或启动检查失败；130 表示中断。性能未达目标本身不会造成退出码 1。

`summary.csv` 字段如下：

| 字段 | 含义 |
|---|---|
| policy / run / valid | 策略名、轮号、实验是否有效 |
| exit_code | 原生 schbench 退出码；提前退出的调度器状态另外记入 error |
| wall_s | 门控 exec 前 SIGCONT 到 wait4 返回的单调时钟秒，含极少量唤醒/exec 开销；准备、冷却、切换和收尾在计时外，仅作运行核查 |
| cpu_time_s | 独立 cgroup 中整个负载树的累计 CPU 秒 |
| peak_memory_bytes | 独立 cgroup 的峰值字节，含文件缓存和内核内存，不含 swap |
| temp_start_c / temp_peak_sampled_c | 固定 CPU 传感器的起始温度和正式窗口内采样最高温，°C |
| avg_rps | 原生最终 average rps，请求/秒；绝不以 RPS p50 替代 |
| request_p99_us / wakeup_p99_us | 最终累计请求/唤醒延迟 p99，µs；不平均各窗口 p99 |
| error | 失败原因；详细缺失辅助指标原因见 attempt.json |

辅助指标不可用就留空并注明原因，不填 0。传感器路径、driver、label 和单位在整批 snapshot 中。若需要完整 cgroup CPU/内存统计，可在支持用户委派的 systemd 中运行：

```sh
systemd-run --user --wait --pipe --collect \
  --property=Delegate=yes --property=DelegateSubgroup=manager \
  --working-directory="$PWD" "$PWD/schtest"
```

`valid` 要求原生正常退出、三个必需指标可解析且所有通用核验通过。原生 JSON 支持时也核对最终 p99 与文本结果一致。是否达到 RPS 或延迟目标放在 `attempt.json` 的 `constraints`，不重复放入 CSV。

`rps_tolerance=0.05` 表示平均吞吐需落在目标 ±5% 内；可选 `request_p99_limit_us`、`wakeup_p99_limit_us` 是延迟上限。固定请求率主要看请求 p99，同时核对吞吐与唤醒延迟；尽力模式主要看平均 RPS，同时看两种延迟。有延迟上限时先满足约束，不做加权总分。

各策略有效/尝试次数、有效且达标次数、主要指标的中位数及最小–最大值存于 `metadata.json` 的 `analysis`。只有同轮默认组和目标组都有效且满足配置约束，才计算配对的延迟降低率或吞吐提升率；正值更好。单测不补跑对照，采样点不是独立重复，少量轮次可得出“尚不能区分”。两种模式分别生成批次和汇总。

清理用法与 compiletest 保持一致，但这里的 `Clean` 只处理 schtest 的数据：

```sh
./Clean --dry-run             # 预览全部 schtest 结果及验证记录
./Clean                      # 删除预览范围，不再询问确认
./Clean 批次ID --dry-run      # 预览指定批次
./Clean 批次ID1 批次ID2       # 清除指定批次
./Clean --help
```

批次 ID 使用完整目录名，不能填写路径。程序扫描配置结果目录、默认结果/验证目录和已登记的自定义输出根目录，只删除带本测试身份的批次；未知批次先报错，不会部分删除。符号链接目录及与程序、源码、构建、配置或 Compile 重叠的清理根目录会被拒绝。生成的每批配置副本随批次删除；固定 `schtest.json`、示例、程序、源码、来源记录和 Compile 数据保留。它与 compiletest、两边 Clean、build-schbench 共用 `acquire_lock()`，有测试运行就拒绝清理。

实际复用的接口：

| 现有接口 | 本测试用途 |
|---|---|
| compilebench.cli.acquire_lock / scheduler_specs / build_environment | 跨负载互斥、调度器命令与完整 ops 校验、受控环境 |
| compilebench.policy.Scheduler / policy_name | 只读策略观测、命名和任务 ext.enabled 核验 |
| compilebench.control.BatchScheduler | 一次授权、常驻监督、稳定等待、EOF/中断收尾和整批防挂起 |
| compilebench.workload.measure | 门控计时、0.1 秒策略检查、资源/温度/电源采样与进程树收尾 |
| compilebench.metrics.BuildCgroup | 独立负载树 CPU/内存统计，缺失原因记录 |
| compilebench.system / Interrupts | 环境快照、固定传感器、摘要、原子保存、中断保护 |
| compilebench.cleanup.remove_entry | 仅删除已校验的记录条目；schtest 自己限定清理范围 |

为共享测量器增加可选 `cpu_affinity`、`workload_label`，为监督器增加可选 `workload_name`；默认值保持 compiletest 原行为。schtest 不执行内核源码预读、olddefconfig 或内核构建。

验证环境是 Fedora 44、Linux `7.2.14-200.fc44.x86_64`、Python 3.14.8。自动测试命令为 `python3 -m unittest discover -s tests -v`；Compile 的兼容性回归在同级 Compile 目录用相同命令执行。测试包含四种入口、随机完整三轮、最终统计解析、配置/程序/CPU 变化、失败后继续、监督丢失终止、实际 CLI 中断清理，以及跨负载互斥和 Clean 范围。

39 项 schtest 专项测试和 56 项 compiletest 兼容性回归通过。共享监督进程的凭据失效模拟、EOF 收尾和挂起阻止锁由 compiletest 回归覆盖；未把这些替身测试当成真实四策略验收。

两种模式的原生 2 秒短任务在当前 default 调度器下均有效，固定请求率探针为 100 RPS 且达标；固定程序、2 worker、零冷却，仅用于验证运行链路。记录在 `bench/validation/native-20261104T125655.977656Z/`。本环境可取得 cgroup CPU 时间，但未启用 memory controller，峰值内存留空并注明原因。此前 JSON 解析核对失败的探针也保留在 validation 中。

自动测试记录同样见 `bench/validation/`。尚未执行真实四策略长测试。固定请求率的正式 RPS 和正式场景仍需预实验确定，现有 rusty 的运行时错误也尚未修复。

原生行为依据：[schbench 官方源码与说明](https://github.com/masoncl/schbench/tree/24e32b8ef67cbafb0761aed68747d90a291767ae)。
