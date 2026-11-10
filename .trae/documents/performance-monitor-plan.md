# 性能监控功能实施计划

## Context

后台管理中心需要新增本机性能监控配置与实时预览，Dotii 圆屏设备需要复用现有快照链路接收 CPU、内存、磁盘 IO、网络数据，并本地绘制最多四个汽车仪表风格的仪表。总开关控制设备页面是否出现，四个子开关分别控制前后台对应仪表；后台预览不受设备总开关影响。

## 1. 配置与本机采集

- 在 `bridge/codex_bridge.py` 的 `MODULE_DEFAULTS` 增加默认关闭的 `performance`，沿用现有 `/api/v1/admin/modules` 保存总开关；该开关不启动或停止采集。
- 新增 `bridge/performance_client.py`：
  - `PerformanceConfigStore` 持久化 `performance.json`，包含默认均为 `true` 的 `cpu_enabled`、`memory_enabled`、`disk_enabled`、`network_enabled`，严格校验布尔值并原子写入。
  - `PerformanceService` 仿照 `WeatherService` 使用 daemon 线程、`Lock`、`Event`，随 Bridge 启动和停止，每 1 秒通过 psutil 采集。
  - CPU 为系统总占用百分比，内存为已用百分比；磁盘为全系统读/写字节速率，网络为所有接口合计收/发字节速率。速率依据累计计数和 `monotonic` 时间差计算。
  - 首个 IO 样本只初始化计数，不发布伪速率；计数器回退、缺失数据和采集异常标记对应指标不可用。保留最近成功时间，超过 5 秒标记 stale，恢复后自动清除错误。
- 新增本机管理接口 `POST /api/v1/admin/performance/config`；在 overview 返回公开配置。总开关关闭时仍持续采集和预览，子开关只控制对应仪表显示。
- 在两份 packaging requirements 中加入相同版本的 psutil；检查现有 PyInstaller spec 的依赖收集，只有打包验证缺失时才调整 spec。

## 2. 快照数据契约

在 `BridgeServer.snapshot()` 动态合并配置与 `PerformanceService.snapshot()`，不修改 Codex 种子状态或 schema 版本。新增数据保持定长标量，适配现有 Wi-Fi/BLE 快照：

- `modules.performance: bool`
- `performance`：四个 `*_enabled`、`connected`、`stale`、`sampled_at_epoch`、`error`
- `performance.cpu` / `memory`：`available`、`percent`
- `performance.disk`：`available`、`read_bytes_per_sec`、`write_bytes_per_sec`
- `performance.network`：`available`、`rx_bytes_per_sec`、`tx_bytes_per_sec`

数值保持原始 bytes/s，不由后台格式化。CPU/内存量程固定 0–100；磁盘和网络各使用一个总速率指针，副文本分别显示读/写和收/发。总速率量程由前后台按相同四倍固定档位选择，保留约 25% 余量，立即升档、连续三个样本后降档；超顶仅截断指针，文字仍显示真实值，不增加额外量程配置。

## 3. 后台管理中心页面

修改 `bridge/web/index.html`、`app.js`、`styles.css`：

- 按现有 module tab/panel 模式新增“性能监控”导航和页面，包含设备显示总开关、四个独立开关、采样/异常状态及四个实时仪表卡片。
- 使用原生 SVG/CSS 绘制刻度和指针，不引入图表库；在现有 3 秒 overview 刷新中更新数值和指针。
- 为 `updateModule()` 增加 performance 特例：总开关只控制设备显示，不隐藏后台内容。四个子开关保存后立即隐藏/显示对应卡片；无有效数据时显示 `--`，不得将异常或首样本显示为 0。

## 4. Dotii 圆屏页面

- 在 `main/app_state.h` 增加性能总开关、四个子开关、连接/stale 状态及四项定长指标字段。
- 在 `main/connectivity.c` 扩展 `copy_module_config()` 和快照解析：缺字段默认关闭/不可用，检查数值有限且非负，CPU/内存钳制到 0–100；Wi-Fi 与 BLE 继续共用 `parse_snapshot()`。
- 新增 `main/performance_monitor.c/.h` 并登记 `main/CMakeLists.txt`。使用 LVGL 9.4 已有 Scale/needle 和标签本地绘制，不增加图片资源、线程或传输协议。
- 页面位于 358×358 安全区内，固定 CPU、内存、磁盘、网络顺序：1 项居中，2 项单行居中，3 项上二下一，4 项 2×2；0 项显示“未选择指标”。无效、离线或 stale 数据显示 `--` 和归零指针。
- 在 `main/state_ui.c` 将性能页追加为第 9 页，保持既有页面编号不变；同步 `page_enabled()`、`page_screen()`、`screen_enabled()`、页面点、启动构建和 `update_snapshot()`，复用现有每秒 UI 更新和关闭当前页后的自动跳转。
- 在 `main/app_state.c` 让有效性能数据参与 `app_state_has_real_data()`，保证性能页作为唯一数据页时也能退出等待屏。

## 5. 测试与验证

- 新增 `bridge/tests/test_performance_client.py`，mock psutil 和时钟，覆盖百分比边界、首样本、速率计算、计数器回退、异常恢复、stale、配置校验及总开关不影响采集。
- 更新 `test_codex_bridge_config.py`、`test_module_on_demand.py` 的完整 modules 断言和前端 ID 静态校验，并补快照聚合/配置接口测试。
- 执行：`.venv/bin/python -m unittest discover -s bridge/tests -v`、`.venv/bin/python -m compileall -q bridge`、`node --check bridge/web/app.js`。已知本机 CoreBluetooth 依赖失败单独说明，不与本功能混淆。
- 启动带 `--app-server` 的本地 Bridge，在浏览器验证 0–4 项仪表、总开关独立性、配置重启保持和实际本机数据刷新；检查带 token 的 snapshot 契约。
- 使用 ESP-IDF 构建固件并真机验证圆屏 0–4 项布局、开关同步、Wi-Fi/BLE 更新、断流显示和当前页关闭跳转。
- 构建后同步 `build/state_display.bin` 到 `firmware/state_display.bin`（bootloader/partition table 仅在有差异时复制），再用 Python 字节检索确认烧录包包含“性能监控”等特征字符串，避免只更新 build 产物。
