# sched_ext 内核编译测试

compiletest 的代码、配置、测试、说明文档和 `bench/` 数据统一放在 `SLIM/Compile/`；`SCHBENCH_TASK.md` 保留在 `SLIM/` 根目录。下文路径均相对于 `Compile/`。

使用 Python 3.9+ 和标准库，先进入目录，编辑其中的 `compiletest.json` 后运行：

```sh
cd ~/Documents/code/SLIM/Compile
./compiletest     # 当前调度器下测试一次，不切换调度器
./compiletest 1   # 一轮：四种策略各测试一次
./compiletest 3   # 三轮：每轮独立随机排列四种策略，共安排 12 次构建
```

只接受一个可选的正整数轮数。四种策略为 `default`、`scx_lavd`、`scx_bpfland`、`scx_rusty`。随机种子和每轮实际顺序保存在 `metadata.json` 的 `seed/orders`，汇总行按该顺序写入。正常运行不向终端输出进度或性能结果；启动错误可以写入标准错误，多轮模式必要时在开始前询问一次 sudo 密码。退出码为 0 表示全部有效，1 表示存在无效记录或批次失败，2 表示入口/配置错误，130 表示中断。

无参数模式使用用户当前调度器，不需要 sudo，也不启动或停止调度器。多轮模式要求开始时 sched_ext 为 disabled；发现外部 scx 时拒绝接管，用户须先自行停止。多轮模式只对调度器监督进程提权，配置准备、正式编译、采样与保存均以普通用户执行。开始前由同一 Python 父进程调用 `sudo -v`，随后用 `sudo -n` 启动本批专用监督进程；批次内不再调用 sudo，不依赖之后的凭据缓存、不保存密码。正常结束、错误、中断或父进程管道关闭时，监督进程停止自己启动的调度器；程序核验恢复 disabled。不会终止外部调度器。整批与 Clean 共用互斥锁。

多轮模式还使用 `systemd-inhibit --no-ask-password --what=idle:sleep --mode=block`，在同一次初始授权下让监督进程持有覆盖整批的临时自动挂起抑制锁；工作进程就绪意味着锁已取得。锁获取失败时不开始正式构建；正常结束、错误或中断后自动释放，不改写桌面电源配置。状态写入 `metadata.json` 的 `scheduler_controller.sleep_inhibition`。它不会关闭屏幕锁，界面仍可能要求解锁，但屏幕锁本身不暂停后台编译。参见 [systemd-inhibit](https://www.freedesktop.org/software/systemd/man/latest/systemd-inhibit.html)。

本机配置为 Linux 6.12.115、冻结的 defconfig、目标 `vmlinux`、`-j14`、30 秒冷却、1 秒采样。源码、配置、构建和结果依次位于 `bench/src/linux-6.12.115/`、`bench/config/kernel.config`、`bench/build/`、`bench/results/`。新环境可复制 `compiletest.example.json` 后填写路径；相对路径以配置文件所在目录为准。程序保留已有源码和冻结配置，不安装内核。

多轮使用配置中的 `schedulers`，为每个 scx 填写命令数组和精确的完整 `ops`。示例对应本机 1.1.4 版本，例如 `lavd_1.1.4_x86_64_unknown_linux_gnu`；换版本或架构后需更新。必须使用前台命令，配置和可执行程序应来自可信来源。监督进程仅接受预先登记命令的编号，不接受运行中提供的新命令。`scheduler_settle_s` 默认 5 秒，`scheduler_timeout_s` 默认 30 秒且必须大于前者；四种策略都等待状态持续稳定后再保存本次策略快照。启动稳定阶段的状态变化完整记录在元数据中；进入快照核验后再发生回退或重载，仍标为无效，不自动重试。此等待不能修复调度器自身的运行时错误。

本机 bpfland 命令已设为 `scx_bpfland --primary-domain all`，用于避开 1.1.4 默认 `auto` 模式在内部电源配置变化时主动重载的路径；见[该版本源码的 refresh_sched_domain](https://github.com/sched-ext/scx/blob/v1.1.4/scheds/rust/scx_bpfland/src/main.rs#L459)。本机旧日志显示自动主域为 `0x3fff`，包含全部 14 个 CPU；新参数固定使用 all 主域，但仍须视为明确指定参数的 bpfland，不能标成默认参数测试。配置和实际命令会进入新批次快照，不改写旧结果。此设置不保证避免其他原因造成的重载，enable_seq 核验仍保持严格；用户批次 `20261104T020508.875040Z-135196` 已完成四种策略各一次的真实 vmlinux 构建，四行均有效，最后恢复 disabled。无参数测试若由用户自行启动 bpfland，也需在启动命令中采用该参数。

两种模式共用以下测量流程：

1. 检查配置、源码、编译依赖和当前策略，保存整批参数、冻结配置、源码清单/摘要、编译器、可用 CPU、实际供电与模式和固定温度传感器。
2. 每次创建新的空 `make O=` 目录，复制同一份配置，执行 `olddefconfig` 并要求哈希不变；多轮模式切换并核验目标策略。
3. 用同样方式预读固定源码并按配置冷却，单调时钟只计正式 make 从门控 exec 到退出的耗时。约每秒采样资源和温度，约每 0.1 秒核验调度器状态。
4. 核对配置、源码、可用 CPU、供电/模式和策略是否变化，保存所有成功、失败和中断记录。多轮模式中单次加载或运行失败，若已确认恢复默认状态则继续剩余策略；恢复失败、监督进程丢失或中断时停止本批，未尝试的安排不伪造为已执行。

结果位于 `SLIM/Compile/bench/results/`，按轮号和策略区分：

```text
bench/results/<UTC时间戳-PID>/
├── snapshot.json              # 整批固定参数与初始环境
├── compiletest.json           # 参数原文
├── kernel.config              # 本批冻结内核配置
├── source-manifest.txt        # 固定源码输入清单
├── metadata.json              # 模式、轮数、seed/orders、最终状态、失败原因
├── summary.csv                # 每次尝试一行；完整多轮为 4×n 行
├── scheduler-controller.log   # 多轮模式的监督进程及调度器日志
├── run-001/<策略>/
│   ├── snapshot.json          # 本次目标策略和构建命令
│   ├── prepare.log
│   ├── build.log
│   ├── scheduler.log          # 本次 scx 的日志片段
│   ├── samples.csv
│   └── attempt.json
└── run-002/<策略>/...
```

整理目录时，原 `SLIM/bench/` 已整体迁入 `SLIM/Compile/bench/`，已有批次 ID、结果和构建产物均保留。历史快照、日志、配置来源记录及旧构建产物内部的绝对路径仍是当时的 `SLIM/bench/...`，查找对应文件时使用新的 `SLIM/Compile/bench/...` 路径。旧构建目录中也可能包含原路径的生成文件或链接；新测试会使用新路径和新建的空构建目录，无需复用旧构建目录。当前 `compiletest.json` 使用相对路径，随目录迁移后继续生效。

无参数模式只产生 `run-001/当前策略/`，单次结果也保留在 metadata 的 `result`。未知策略的日志目录使用 `current`，原始 ops 仍写入快照与结果。`policy` 为 default 或 scx 名称，快照另外保存完整的内核 `root/ops`；不以进程名称猜测 BPF 链接归属。scx 必须为 `switch_all=1`，运行内核须支持 sched_ext 和 `/proc/PID/sched` 的 `ext.enabled` 核验。

`summary.csv` 字段仍为 `policy, run, valid, exit_code, wall_s, cpu_time_s, peak_memory_bytes, temp_start_c, temp_peak_sampled_c, error`。多轮的 run 为 1 到 n；无参数调用的 run 固定为 1，独立调用不自动配对。只有编译成功且全程核验通过才为有效。耗时长本身不剔除。CPU/内存优先统计每次独立 cgroup 的完整进程树；内存包含其文件缓存和内核内存，不含 swap。没有委派权限时辅助指标留空，原因见 attempt.json。温度峰值取正式构建窗口内的采样最大值，详细电源快照和差异在 samples.csv。EPP 单独变化仅作诊断，不当作插拔电源。离散观测不能捕获任意短暂变化。

如需 cgroup 完整统计，可在支持用户委派的 systemd 环境运行以下命令。多轮时需保证其初始 sudo 授权可用，之后无需续期：

```sh
systemd-run --user --wait --pipe --collect \
  --property=Delegate=yes --property=DelegateSubgroup=manager \
  --working-directory="$PWD" "$PWD/compiletest"
```

在 `SLIM/Compile/` 目录以普通用户运行 `Clean`，无需 sudo。用法如下：

```sh
./Clean --dry-run          # 预览全部清理范围
./Clean                   # 删除全部记录及其独立配置
./Clean 批次ID --dry-run   # 只预览一个批次
./Clean 批次ID            # 只删除该批次及其独立配置
./Clean 批次ID1 批次ID2    # 一次删除多个指定批次
./Clean -c 配置文件.json --dry-run  # 使用指定配置文件预览
./Clean --help            # 查看参数说明
```

“批次ID”需要替换成 `bench/results/` 下的完整目录名，例如 `20261103T132723.853222Z-206509`，不要填写目录路径。可以先执行 `ls -1 bench/results` 查看已有批次，再运行 `./Clean 20261103T132723.853222Z-206509 --dry-run` 核对范围。未找到指定批次及其配置时会报错。

`--dry-run` 只预览，不删除。不带该选项会直接删除，不再询问确认。测试正在运行时拒绝清理。默认读取项目的 `compiletest.json`；`-c` 可指定其他配置，配置内相对路径按该配置文件所在目录解析。

省略批次ID时，清空配置指定的 `results_dir`、其同级 `validation/`，以及项目默认的 `bench/results/`、`bench/validation/` 和旧版 `results/`。指定批次时只删除其中同名的记录。结果目录内的 `kernel.config` 随该目录一起删除。对于 `bench/config/`（以及配置中 kernel_config 的同级目录）中的独立配置，Clean 精确识别 `批次ID.config/.json/.log`、`kernel-批次ID.config/.json/.log`、`config-批次ID.config/.json/.log` 或同名批次目录；不会用短前缀匹配其他实验。清理全部时也清理这些规则下带完整时间戳和 PID 的遗留配置。

非标准名称可在该批 `metadata.json` 的 `generated_configs` 中登记路径数组；绝对路径和相对该批次目录的路径均支持，删除范围限于配置目录。共享的冻结输入 `kernel.config` 及其来源记录 `kernel.json`、仍被保留实验引用的配置、源码与构建产物均保留。当前 compiletest 不会每次重建 `bench/config/kernel.config`，每批副本实际在结果目录中。Clean 与实验共用互斥锁；历史结果只有执行清理时才删除。本次 Clean 的 15 项专项测试通过，覆盖指定批次、关联/遗留配置、共享引用、越界拒绝和预览模式。

在 Fedora 44、Linux `7.2.14-200.fc44.x86_64`、Python 3.14.8 上运行 `python3 -m unittest discover -s tests -v`，56 项测试通过；本机三个 scx 均为 1.1.4。测试覆盖单次模式、随机完整轮、固定整批快照、失败后继续、中断清理、拒绝外部 scx，以及真实无特权监督进程在模拟凭据失效后继续三轮切换、拒绝再次授权、EOF/SIGINT 清理、启动稳定等待、未登记命令拒绝、挂起抑制锁覆盖整批和退出释放。

用户三轮批次 `20261104T034246.242650Z-265030` 的第一轮四条成绩有效，第二轮已经启动。系统日志确认：2026-11-04 11:57:53（北京时间）挂起，13:01:51 恢复；该时段只有批次开始的一次 sudo 会话，之后用户中断测试。此次加入自动挂起抑制，历史结果不改写。真实抑制锁获取和释放探针在 `bench/validation/inhibit-20261104T050903.372815Z/`，监督进程结合真实抑制锁的中断清理探针在 `bench/validation/controller-inhibit-20261104T051006.111655Z/`，均以普通用户短任务验证通过，没有触发真实挂起或加载调度器。此修正尚未重新完成真实三轮内核构建；可在默认状态重新运行 `./compiletest 3` 验证，共应有 12 行有效成绩、按保存顺序执行，结束 disabled。
