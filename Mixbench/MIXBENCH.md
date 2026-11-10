# 前台＋后台混合测试任务

## 目标与目录

新增 Python 3 入口，复用现有 compiletest/schtest/hacktest。先读 `COMPILE.md`、`SCHBENCH.md`、`HACKBENCH.md`及实际目录中的配置和代码：通用快照、策略核验、门控计时、采样、常驻提权监督、sleep 阻止锁、互斥、失败处理和 Clean 均沿用已有实现。直接组合原生程序或底层接口，不并行调用会各自切换策略、获取批次锁的测试入口；README 列出复用接口。

代码、配置、测试、README 和 `bench/`放在 `Mixbench/`，本文路径相对此目录，先进入后执行；本文档留在项目根目录。首版实现“固定请求率 schbench 前台＋sysbench CPU 后台”的稳态混合负载：**先满足前台约束，再优化后台吞吐**，不做加权总分。

## 入口与配置

| 命令 | 执行范围 |
|---|---|
| `./mixtest` | 当前调度器一次混合测试，不切换 |
| `./mixtest n` | 四策略随机测试 n 轮 |
| `./mixtest [n] --case front` | 前台单独运行，作为基线 |
| `./mixtest [n] --case back` | 后台单独运行，作为基线 |

- n 为正整数，省略 n 与 n=1 不同。有 n 时，每轮独立随机排列 default、scx_lavd、scx_bpfland、scx_rusty，各一次，共 4×n 次，保存 seed/orders。默认 `--case mixed`；每批一个 case、一组参数，不自动补跑基线。
- 参数读自 `mixtest.json`，风格参考已有配置：程序路径、前台参数/目标 RPS/容差/可选请求 p99 上限 `latency_limit_us`、后台线程数/计算量、时长、共享 CPU 范围、同步容差、结果目录、冷却/采样间隔、超时及调度器参数。支持 `--output DIR`、`--rps VALUE`、`--bg-threads N`，保存生效配置。
- 默认起点为 120 秒；前台 `-m 1 -t 14 -F 256 -n 5 -s 100 -r 120 -w 0 -z 0 -R <配置RPS>`；后台 `sysbench cpu --threads=14 --cpu-max-prime=20000 --time=120 --events=0 --rate=0 --warmup-time=0 --report-interval=1 run`。按本地版本 help 核对参数，记录版本与摘要；这些是预实验起点，不是最优参数。
- RPS 由独立预实验确定，同一场景四策略使用相同绝对值。线程数按实际 CPU 核对；如测试后台 7/14/28 线程，分别成批，不能逐策略调参。前后台共享同一 CPU 范围、普通优先级，不额外改变 nice、权重或配额；不清缓存，不启用自动调压、校准或周期清零。
- 正常运行不打印进度或结果；启动错误写 stderr。结果为 `bench/results/<唯一批次>/`。

## 运行流程与指标

1. **预检**：核对依赖、配置、权限和环境，保存整批配置/快照、程序版本与摘要、实际命令、统计单位和范围。
2. **策略准备**：无 n 时接受当前策略，不调用 sudo、不切换或停止用户调度器；有 n 时按 compiletest 的监督协议预检外部实例、授权一次，按随机顺序切换并等待稳定。每次混合测试只加载一次策略，前后台共同使用。
3. **单次运行**：统一冷却，保存本次固定快照；共同门控启动两个原生程序，运行相同时长并自然结束，不另加内部预热。并行资源/温度采样和策略核验，频率沿用 compiletest。`wall_s`为首个正式负载 exec 到最后一个退出，准备和收尾不计入；front/back case 只启动对应负载。
4. **记录**：保存完整输出、各自退出码和统计起止，解析前台最终平均 RPS、请求/唤醒 p99，以及后台 events/s、total events 和原生测量时长；不能以配置时长重算吞吐。同步核查见下；延迟用 µs，吞吐用请求/秒或事件/秒，不以 RPS p50 替代平均值，不平均多个窗口 p99。
5. **收尾**：任一负载失败、超时或中断，停止双方及其子进程、采样，保存失败记录。无 n 保持用户策略；有 n 清理自己的调度器并核验 disabled。继续条件和中断/监督丢失的终止规则沿用 compiletest，不伪造尚未尝试的记录。

评价分开处理：

- **同步核查**：记录两方 exec/退出和可观测的原生统计边界；启动错位、进程重叠比例（交集/较短进程时长）按预设容差判定，例如 ≤0.1 秒、≥99%。进程重叠不等于统计窗口一致，须核对版本计时行为，未观测边界注明。首版按近似同步区间解释；严重错位、未持续共置或计时口径无法核对时无效，不能混入长时间单独运行阶段。
- **前台约束**：平均 RPS 达到目标（按预设容差），且请求 p99≤延迟上限，同时报告唤醒 p99。无延迟上限时仅作探索，达标状态留空。阈值和 RPS 预先固定，不按结果反调。
- **后台目标**：前台满足约束时，主要比较 `back_events_s`，越高越好；CPU 时间和利用率不能替代有效工作量。吞吐不足、请求延迟超限或后台吞吐为零仍保存完整结果；请求 p99 超限按下文记为无效，并与技术失败区分。
- `valid`同时要求原有执行/核验通过和请求 p99 达标，具体规则见下文。mixed 的 `exit_code=0`仅表示双方正常退出，不保证 `valid=True`；原始退出码保存在 `attempt.json`。
- 资源统计覆盖双方完整进程树，优先用共同父 cgroup、两个子 cgroup；汇总取父组 CPU 时间和内存峰值，不能相加子组各自内存峰值。分侧诊断留在 samples/attempt，辅助项缺失沿用 compiletest 的留空说明规则。
- 同 case、同配置统计各策略有效/尝试次数、前台达标次数及主要指标中位数和最小–最大值。同轮双方有效且前台达标时，计算后台提升率 `100×(策略−默认)/默认`；未达标结果另列，零分母留空，单测不补跑对照。不把采样点当重复实验。
- 有同策略、同负载配置的单独运行基线时，可计算后台吞吐保留率（mixed events/s ÷ back events/s）和前台延迟放大（mixed 请求 p99 ÷ front 请求 p99）；缺基线或分母为零留空。本版不报告动态恢复时间或超限窗口，不能把原生累计周期输出当独立窗口。

## 文件与验收

### valid 判定与无效原因

- 正式 mixed 测试采用 **请求 `request_p99_us ≤ 30000 µs`（30 ms，包含等号）** 的延迟标准，配置项为 `latency_limit_us`，随本批配置冻结。同一场景四种调度器使用相同上限；自定义配置须核对该项，不能按结果反调。这里判定的是请求 p99，**不是 `wakeup_p99_us`**。
- **`valid=True` = 技术核验通过，且请求 p99 不超过配置上限**。程序异常退出、超时、中断、指标解析失败、同步不合格、策略/参数/环境核验失败等，仍记为无效；低 p99 不能抵消这些错误。请求 p99 缺失或非法时也不能判为达标。
- `summary.csv.error` 明确区分：`测试执行/核验错误: ...` 表示测试执行或核验出错；`请求 p99 超限: ...` 表示测得延迟超过上限。若两类问题同时存在，两者均保留；无法取得 p99 时写“请求 p99 缺失或非法”，不冒充测得超限。
- `attempt.json` 分别保存 `technical_valid` / `technical_error`、`latency_validity`（实际值、上限、是否达标）和 `invalid_reasons`：`technical_error`、`p99_exceeded`、`p99_unavailable`。因此可以区分“正常完成但 p99 超限”和“测试本身出错”。`metadata.json.validity_policy` 记录规则版本 `request-p99-validity-v1`。
- 平均 RPS 是否在目标容差内继续记录于 `constraints`；仅 RPS 不足或后台吞吐为零不单独使 `valid=False`，但前台未达标时不能作为满足前台约束的胜者。back 单独测试不适用请求延迟门槛；front 预实验显式设置 `latency_limit_us=null` 时允许探索运行，延迟达标状态留空。
- 所有无效结果及原始日志均保留。仅 p99 超限不会中止后续调度器或轮次；整批存在无效结果时入口返回非零状态。规则适用于后续新测试，历史 CSV 不自动回写。

### 唤醒 p99 的停止阶段筛选

- schbench 退出时会唤醒等待中的 worker，但固定版本的退出路径未刷新 `wake_time`；此前无任务时的睡眠可能计入 wakeup，抬高 p99。因此运行阶段评价统一排除 `stopping=1`，只保留记录时尚未收到结束通知的 `stopping=0`。
- 使用 `--observe-wakeup` 时，`summary.csv` 的 `wakeup_p99_us` 按上述保留样本重新计算 nearest-rank p99。四种调度器使用相同规则，不固定删除 14 条、不按行尾或延迟大小筛选；正常阶段的大值仍保留。
- 仅完整观测可以重算；缺失、溢出或没有运行阶段样本时，该指标留空并记为技术无效，不回退到原生全量值。
- 原始 `front-wakeup.csv`、原生日志和 JSON 保留审计；`front-wakeup-running.csv` 保存筛选结果，`attempt.json.wakeup_filter` 记录原生 p99、保留/排除数量及规则版本 `running-wakeup-v1`。未启用观测仍为原生口径，历史缺少逐条记录的结果不能重算或与筛选值混合排名。

- 整批保存 `snapshot.json`、`mixtest.json`、`metadata.json`。case、seed/orders、命令、统计范围/同步容差和最终状态放元数据；目标与达标状态放配置/`attempt.json`，不重复放 CSV。
- `run-NNN/<策略>/`保存 `snapshot.json`、`front.log`、`back.log`、`front-native.json`（支持时）、`samples.csv`、`attempt.json`；按 case 只生成实际负载文件。准备日志和 scheduler/controller 日志命名沿用已有测试。未知当前策略用 `current/`，原始 ops 仍记录。
- `attempt.json`保留各负载参数、退出码、统计起止/耗时、后台总工作量、同步证据及缺失原因。`summary.csv`每次尝试一行，公共字段含义沿用 compiletest，schbench 三字段均指前台；单独 case 中不适用的字段留空。

```csv
policy,run,valid,exit_code,wall_s,cpu_time_s,peak_memory_bytes,temp_start_c,temp_peak_sampled_c,avg_rps,request_p99_us,wakeup_p99_us,back_events_s,error
```

- 汇总输出及 `./Clean`沿用已有风格，只清本测试数据，保留程序、源码和共享配置；与其他测试协调互斥。
- 先用短任务验证入口、随机轮、解析、双负载并发/同步、一方失败/超时及清理。真实验收：`./mixtest`一行、run=1、保持当前策略；`./mixtest 1`四行、四策略各一次、run=1、顺序与记录一致，正常完成均技术有效并最终 disabled。完整 n 轮为 4n 行；front/back 验证相同行数及单负载行为。性能达标与否如实报告。
- README 说明用法、配置、字段、复用接口、RPS/SLO 预实验方法、同步近似及真实验证环境，不将替身验证声称为真实验收。真实长测试按用户要求执行。

依据：[Shenango](https://www.usenix.org/conference/nsdi19/presentation/ousterhout)、[Heracles](https://mast.stanford.edu/pubs/heracles_improving_resource_efficiency_at_scale/)、[schbench](https://github.com/masoncl/schbench)、[sysbench](https://github.com/akopytov/sysbench)。具体时长、线程和同步阈值为本项目预实验起点。

