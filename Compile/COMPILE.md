# 项目目标与当前范围

本文件及 compiletest 相关代码、配置、测试、文档与 `bench/` 数据统一位于 `SLIM/Compile/`。本文路径相对于该目录；先进入 `Compile/` 再执行命令。`SCHBENCH_TASK.md` 保留在 `SLIM/` 根目录。

在 Fedora 上测量内核编译性能，后续在 openEuler 复测。使用 Python 3，优先标准库，复用调度器观测、快照和资源测量逻辑。

## 入口与流程

- `./compiletest`：当前调度器下只测试一次。允许借用外部 scx，不调用 sudo，不切换或停止用户调度器。
- `./compiletest n`：n 为正整数；每轮独立随机排列 default、scx_lavd、scx_bpfland、scx_rusty，四种各构建一次，共安排 4×n 次。保存随机种子和实际顺序。
- 参数读自 `compiletest.json`：源码、冻结配置、目标、并行度、结果/构建目录、冷却/采样间隔，以及多轮的调度器命令、完整 ops、加载超时和稳定等待时间。
- 结果放在 `bench/results/<批次>/`，按轮次和策略保存。正常运行不打印进度或结果；启动错误可以写标准错误，多轮必要时在正式构建前询问一次 sudo 密码。

## 实验约束

1. 检查源码、配置、依赖和权限；保存整批参数原文、冻结内核配置、源码清单与摘要、实际编译器、可用 CPU、供电/模式和固定温度传感器。
2. 每次使用新建的空 `make O=` 目录和同一份配置，关闭编译缓存；olddefconfig 后要求配置哈希不变。预读同一源码输入，使用相同冷却间隔。
3. 多轮切换后等待状态稳定，再保存本次策略快照。加载与稳定等待不计时，启动变化保留证据；之后再回退或重载则无效，不重试或重新建立基线。
4. 核验 state、root/ops、enable_seq、switch_all 和任务 ext.enabled。默认组 disabled；scx 组 enabled、完整 ops 精确匹配且 switch_all=1。
5. wall_s 仅覆盖单次正式 make 从门控 exec 到退出。约每秒采样资源、同一 CPU 温度和电源，约每 0.1 秒核验策略。CPU/内存优先用每次独立 cgroup 覆盖完整编译树；注明内存包含缓存，辅助指标缺失留空并说明原因。
6. 测试前后及采样时核对参数；源码、配置、可用 CPU、供电/模式或策略条件变化则无效。EPP 仅作诊断；离散观测不能保证捕获任意短暂变化。耗时长本身不剔除。

## 权限与收尾

- 普通用户执行配置准备、正式编译、采样和保存；只有多轮调度器监督进程提权。
- 多轮开始前拒绝已有外部 scx，不接管或停止它。开始前由同一 Python 父进程授权一次并启动常驻监督进程，批次内不再调用 sudo、不保存密码。
- 多轮监督进程通过 systemd-inhibit 持有整批 idle:sleep 阻止锁，避免离开电脑后自动挂起暂停测试。获取失败不开始构建，退出自动释放，不修改电源模式或屏幕锁设置；无需额外授权，状态写入元数据。
- 正常结束、错误、中断和管道 EOF 后停止自己的调度器并核验 disabled。只启动事先登记的命令，只清理本脚本自己的进程与空 cgroup。
- 单次加载/运行失败保留无效行，确认已恢复默认后继续；恢复失败、监督进程丢失或用户中断则终止，不伪造尚未尝试的行。
- 只构建内核，不安装或修改启动配置。保留源码、配置和构建产物。
- 保留 `./Clean` 清除测试记录与验证日志，支持指定批次；同时删除该实验生成的独立配置，保留源码、共享冻结配置及其来源记录、其他实验仍引用的配置和构建产物。整批和 Clean 共用互斥锁，支持预览清理范围。

## 文件与验收

- 整批保存 snapshot.json、compiletest.json、kernel.config、source-manifest.txt、metadata.json（含模式、seed/orders、最终状态、失败原因）。
- `run-NNN/<策略>/` 保存本次 snapshot.json、build.log、prepare.log、samples.csv、attempt.json；多轮 scx 另有 scheduler.log，整批有 scheduler-controller.log。未知当前策略使用 current 子目录，原始 ops 仍保存。
- summary.csv 每次尝试一行，字段保持 `policy, run, valid, exit_code, wall_s, cpu_time_s, peak_memory_bytes, temp_start_c, temp_peak_sampled_c, error`。无参数恰一行、run=1；多轮完成安排时 4×n 行，run=1..n。只有编译成功且全程核验通过才有效。
- 先用短任务验证两种入口、随机完整轮、固定快照、失败记录、授权过期模拟和中断清理。真实四策略验收运行 `./compiletest 1`：四行、四策略各一次、run=1、均有效，顺序与保存的排列一致，结束 disabled。
- README 说明用法、字段和实际验证环境与限制，不将替身测试声称为真实四策略内核验收。
