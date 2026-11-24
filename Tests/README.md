# Finegraintest

- [Finegraintest.py](Finegraintest.py) 是运行入口，生成前台线程、后台线程和 RPS 的测试矩阵，配置 Collector 并启动测试。从 Wisp 根目录运行 `./Tests/Finegraintest.py`，加 `--check` 可先预检。
- [finegrain_queue.py](finegrain_queue.py) 是入口调用的队列模块，负责预检、sudo 授权、防熄屏和休眠、逐批运行 Mixbench、批间休息和保存进度。
