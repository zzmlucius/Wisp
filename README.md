# Wisp
Workload-Informed Scheduling Policy

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

- [Compile/compiletest.json](Compile/compiletest.json)
- [Schbench/schtest.json](Schbench/schtest.json)
- [Hackbench/hacktest.json](Hackbench/hacktest.json)
- [Mixbench/mixtest.json](Mixbench/mixtest.json)

例如带轮数的 `./compiletest 3` 会按配置加载修复版。无参数的测试只使用当前调度器，
不会主动切换；手动启动 rusty 时也应使用上述完整路径：

```bash
sudo /home/lucius/.local/scx-rusty-fixed/bin/scx_rusty
```

原版 `/usr/bin/scx_rusty` 保留，直接运行 `scx_rusty` 时仍需留意 PATH 的解析结果。
安装成功不代表已验证运行稳定性，修复效果需通过新测试确认；历史结果保留原记录。

## 夜间顺序测试

在 Wisp 根目录以普通用户运行：

```bash
./nighttest
```

依次执行以下四批任务，每批四种调度器各测试 3 轮，总计 48 次尝试：

```bash
./Mixbench/mixtest 3 -500
./Mixbench/mixtest 3 -1000
./Mixbench/mixtest 3 -1500
./Mixbench/mixtest 3 -2000
```

入口复用上述命令的 Python 实验接口，在同一个普通用户 Python 进程中顺序执行。
初次认证、每 30 秒的 `sudo -n -v` 刷新和每批监督进程提权均由该父进程发起，
兼容按终端或父进程区分的 sudo 认证缓存。只在启动时可能输入一次密码；
队列设置 `WISP_SUDO_NONINTERACTIVE=1`，后续认证与提权禁止交互，认证失败会报错而不会等待密码。
单批已运行的提权监督进程仍沿用原有机制，不依赖缓存续期；后台刷新停止或失效后不再启动新批次。
schbench 和 sysbench 仍以普通用户运行，不保存密码、不修改 sudoers。

各批结果仍在 `Mixbench/bench/results/`；队列日志和
`status.json` 位于 `bench/overnight/<批次>/`，其中列出每批结果目录、退出状态和日志路径。
仅 p99 超限或其他非致命无效记录不会阻止后续 RPS 组；启动失败、监督进程异常、
轮次不完整、未恢复 disabled 或用户中断则停止后续任务并保留记录。

启动前自行停止外部 scx，保持供电方式和电源模式一致。运行时可以锁屏；保持终端打开，
不要关机、手动挂起或合盖。每批沿用原有的 sleep 阻止锁。Ctrl+C 会沿用测试收尾并停止刷新。
所有带前台的 Mixbench 测试（包括 nighttest）当前使用原版 schbench，统计全部唤醒样本，
不执行末条或 `stopping` 筛选，也不采集末条观测。旧 `--observe-wakeup` 参数仅兼容接受，
不启用观测或筛选。历史数据保留原统计口径，详见
[Mixbench/README.md](Mixbench/README.md#原生全量唤醒统计)。

nighttest 仅运行上述四组 Mixbench，不执行 Compile 测试或内核配置预检。

### Compile 配置维护记录

2026-11-11 对基础 Compile 配置做过一次工具链维护：新增 Rust 工具链后，更新 Rust 版本
和能力探测项，并验证两次 `olddefconfig` 稳定。旧基础配置保存在
`Compile/bench/config/kernel.before-rust-20261111.config`，差异及验证记录保存在
`Compile/bench/validation/config-repair-20261111/`。直接运行 compiletest 也使用整理后的基础配置。

## midnighttest：固定前14/后14的四档 RPS

在 Fedora 44 GNOME 桌面终端中，以普通用户执行：

```bash
./midnighttest --check   # 只检查并保存配置，短暂取得并释放防熄屏/休眠锁，不输入密码
./midnighttest           # 正式执行；仅启动时可能要求一次 sudo 密码
```

顺序为 500、1000、1500、2000 RPS，每档四种调度器各 3 轮，共 4 批、48 次测试。
每批固定前台 14 worker、后台 14 线程、60 秒负载；采用当前原版 schbench 全量唤醒统计。
其他负载参数、CPU 范围、请求 p99 上限来自 Mixbench 配置，四批单独冻结副本，
不改写原始 mixtest.json。每次测试前至少冷却 60 秒（输入配置更长则沿用），批间再休息
120 秒。默认仅负载和冷却的下限约 102 分钟，另需调度器切换、准备和收尾时间。
这采用固定休息窗口，不保证每次开始温度完全相同，实际温度保留在各次结果中。

本队列集中保存到 `Mixbench/bench/results/midnighttest/<UTC时间戳-PID>/`：

- `results/前台-后台-RPS-供电_时间戳-PID/`：每批原始结果、CSV、日志与快照。
- `inputs/14-14-RPS-供电.json`：每档冻结配置；`logs/14-14-RPS-供电.log`：该批入口日志。
- `status.json`：队列进度及各批实际路径；`preflight.json`：配置、程序摘要、环境和检查结果。
- `scheduler-preflight/`：正式运行前，逐一加载、停止三种 scx 的检查日志，不计入正式成绩。

整批由同一个普通用户 Python 进程运行，复用 nighttest 的初次认证和每 30 秒非交互刷新。
后续 sudo 均使用 `-n`，不保存密码、不修改 sudoers。GNOME 的 idle/suspend 抑制及
logind 的 idle/sleep 阻止锁覆盖预检、测试和所有休息间隔，自动熄屏不会在批间恢复；
完成、Ctrl+C 或正常错误退出时释放，桌面原来的自动熄屏设置随即恢复。
该入口需要 `python3-gobject`；缺失时可用 `sudo dnf install python3-gobject` 安装。

启动前检查四组原生命令、程序与来源摘要、调度器路径、CPU 可用范围、温度传感器、
测量 cgroup、至少 1 GiB 空间和防熄屏/休眠接口。正式启动在一次授权后验证各 scx 可加载和退出，
通过后才开始四档测试。`--check` 不加载调度器，相关能力由正式启动的调度器预检核验。
整批持有跨测试锁，并与 nighttest 共用队列锁；避免中途 Clean 或另一个测试修改记录。
批间检查冻结文件、供电/电源模式和 CPU 可用范围。保持同一供电方式及电源模式、终端会话打开，
不要关机或手动挂起。普通 p99 超限等非致命无效结果会保留并继续后续 RPS；调度器失联、
认证失败、输入变化、轮次不完整或未恢复 disabled 时停止后续任务并写明原因。

## Finegraintest

从 Wisp 根目录执行 `./Finegraintest.py --check` 生成并核查私有配置。正式运行用 `./Finegraintest.py`。

- 前台 worker 和后台线程独立取 4、8、14。第一批矩阵的 RPS 取 250、500、750、1000。共 36 个负载配置，每组默认调度器、LAVD、bpfland、rusty 各三轮，每轮调度器顺序随机，共 432 次尝试。原生 schbench 的 `-R 0` 会进入尽力模式。
- schbench 和 sysbench 都运行 30 秒。采集样本的共同运行区间按两侧重叠范围标记。其他负载参数、CPU 范围和判定阈值读自 `Mixbench/mixtest.json`。
- 每次冷却 30 秒，批次间额外休息 45 秒。运行和等待合计约 7 小时 38 分钟，另有准备、切换与收尾时间。
- 正式队列复用一次性 sudo 认证和自动非交互续期，持有整批防熄屏和休眠锁。结束或 Ctrl+C 后释放。请从有 cgroup 委派的 Fedora GNOME 终端启动。若普通终端预检提示不可委派，可以用 `systemd-run --user --scope -p Delegate=yes -p DelegateSubgroup=manager ./Finegraintest.py --check`，正式运行时去掉 `--check`。
- 结果在 `Mixbench/bench/results/finegraintest/<队列ID>/results/`。每个尝试的 19 维向量写入 `finegrain-workload.csv`，时间窗和采集状态写入同名前缀的 JSON 和 JSONL。原生测试指标在该配置的 `summary.csv`。采集进程随每次尝试关闭。Finegraintest 使用现有 `Collector/bench/build/scheduler.bpf.o`，由队列父进程用同一份授权发起 `sudo -n`。采集器附着后降回普通用户写 CSV，探针随每次尝试关闭。正式运行前可执行 `./Finegraintest.py --check-ebpf`，授权后以默认调度器实跑一场，核对四项调度事件有值以及探针关闭。运行队列探针保持禁用。普通 `--check` 仍无需 sudo。

第二半使用 `./Finegraintest2.py`，RPS 档位为 1250、1500、1750、2000。共 36 批、432 次尝试。每组跑完四种调度器并写入结果后，终端显示该组轮数和全队列进度，共 108 轮。结果在 `Mixbench/bench/results/finegraintest2/<队列ID>/results/`。支持 `--check` 和 `--check-ebpf`。
