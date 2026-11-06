# ESPC-D-V02 下载流程

入口是 `Download_HYCOM.ipynb`，实现位于 `hycom_download.py`。原 notebook 原样保存在 `archive/Download_HYCOM.original.ipynb`。本次只调整下载环节，不修改转换和边界提取 notebook。

## 使用

1. 将 notebook 与 `hycom_download.py` 放在同一目录，从该目录或其父目录启动 Jupyter。
2. 安装 `requirements-download.txt` 中的依赖；本流程不需要 dask。
3. 修改 notebook 配置：时间、变量、区域、输出目录。日期使用 UTC，开始时间包含、结束时间不包含。
4. 运行预览单元，检查 `missing_steps`、起止时刻、预估数据量和路径。
5. 运行下载单元。中断或失败后再次运行；修改配置时先重新创建 `Downloader(config)`。

默认仍是原来的 2025 年 U/V 和区域，输出改到 notebook 同级的 `HYCOM_download_v2`，避免覆盖旧数据。下载 2024 年 9 月时设置 `start="2024-09-01", end="2024-10-01"`。`variables=("T", "S", "U", "V")` 可依次下载四个变量。

```python
from pathlib import Path
from hycom_download import DownloadConfig, Downloader

if __name__ == "__main__":  # Windows .py 脚本必须有此保护；notebook 可直接执行
    downloader = Downloader(DownloadConfig(
        start="2024-09-01", end="2024-10-01",
        out_root=Path("HYCOM_download_v2"),
        variables=("T", "S", "U", "V"),
    ))
    print(downloader.preview())
    print(downloader.run())
```

## 下载和恢复机制

- 从年度聚合读取真实时间轴，按日历月份选择；不再维护非闰年固定索引。支持跨年请求。
- 请求先裁切变量与空间，再读取时间块；公司慢网络默认 1 步，稳定后可调为 4/8/16 步；原始数组最多约 32 MiB。临时 QC 数组和进程传输还会额外用内存。单个时间步超过限制时明确报错，不做隐式超大请求。
- 每个块默认尝试 4 次，指数退避并加随机抖动；失败则二分为更小块，直到 1 个时刻。
- 子进程持有单个远端连接并复用，超时可终止、重连。元数据操作默认允许 300 秒，数据块操作默认允许 180 秒；异常日志包含最后读取阶段。串行网络读取，不对 netCDF4 使用多线程。
- 连续 32 次传输失败后停止本轮访问同一年度数据源，避免服务中断时仍遍历全年小块反复请求；该默认值给 16 步逐级拆到 1 步留出重试空间。下一轮运行可恢复。QC 失败不计入断网次数。可通过 `max_consecutive_remote_failures` 调整；notebook 的一步一块模式显式设为 8 次。
- 只保留通过 QC 的块；写临时文件、读回复检后原子替换。磁盘错误不会触发重新下载。
- 块目录包含请求指纹，区分区域、日期、数据源、变量和 QC 配置。合并只使用本次明确返回的有序块列表，不 glob 混入旧块。
- 合并逐块写入，不把整个月份加载到内存；完整校验后才发布月文件。
- 已完成月文件通过请求指纹和 SHA-256 凭证跳过。缺少凭证或文件变化时复检；本流程已完成但后来损坏的月文件可从缓存重建。
- 块大小变化不改变请求身份，但分块边界不同可能导致部分块重新下载；不承诺任意块大小切换都能完全复用。
- 每个变量、每个月显示时间步进度条，含百分比、已用时间、预计剩余时间、当前 UTC 数据时刻，以及成功/缓存/失败步数和重试次数。重试和拆块不会重复计数；100% 表示本月时间步已处理，仍须看最终状态是否完成合并。读取元数据时继续使用日志；`show_progress=False` 可关闭进度条。

## 有效性检查

1. 变量、维度顺序、形状、全部经纬度/深度/时间坐标和时间单位与请求一致。
2. 屏蔽 `_FillValue` / `missing_value`，每个时刻至少有一个有效值；允许陆地与深层缺测。
3. 全深度数据经 `scale_factor/add_offset` 解码后检查宽松物理范围：T 为 -5～50 ℃、S 为 0～50、U/V 为 -15～15 m/s。这些是可配置的工程筛查阈值，不是完整科学质量控制。
4. 对 T/S/U/V 检查最浅选中层：逐时刻计算有效点中原始打包值 `raw==0` 的比例，T/S 默认上限 20%，U/V 保留原流程的 1%。超过阈值判坏并进入重试/拆块流程；阈值可分别调整。零值筛查是启发式，真实零流速也可能触发它。
5. 单时刻永久失败时只写失败清单，不生成 NaN 占位、不插值，不发布该月文件。其他月份继续处理。

保留原始打包数值和 CF 属性，避免二次缩放。QC 使用临时解码值，月文件可以照常用 `xr.open_dataset(...)` 解码读取。

关于“全域为 20”：[官方 ESPC-D-V02 2025 温度元数据](https://tds.hycom.org/thredds/dodsC/ESPC-D-V02/t3z/2025.html) 中，`scale_factor=0.001`、`add_offset=20`，所以原始 `raw=0` 解码后正是 20 ℃。继续检查 raw 零值即可筛查此类整片异常，不另外添加“解码值等于 20 就判坏”的规则。该数据的缺测标记为 `-30000`，按 [CF 规范](https://cfconventions.org/Data/cf-conventions/cf-conventions-1.12/cf-conventions.html#missing-data) 应先识别缺测再缩放。陆地出现 20 ℃ 与缺测被替换为 raw 零值的情况相符，但仅凭现象无法确认出错环节；目前没有独立陆地掩膜，局部异常未超过零值比例阈值时不保证检出。

本地完整性检查无法保证服务端每个数值科学上正确。例如物理范围内的错误平场、部分未标记缺测仍需后续空间/时间诊断。`raw_zero_limits={}` 可以关闭零值比例启发式；`physical_ranges` 和 `min_valid_fraction` 可以按研究区域调整。

## 时间覆盖与兼容性

- 年度目录的实际首尾时刻不一定正好对应日历年。预览会列出请求内缺少的 3 小时时刻；存在缺口的月份报 `failed`，不悄悄跳过或宣称完整。
- 目前不会自动从相邻年度或逐日 archive 补取聚合中缺少的时刻。应先核对年度源的实际覆盖，再明确调整范围或另取缺少部分。
- 输出保持旧命名：`HYCOM_2025_monthly_T_qc/HYCOM_2025_water_temp_M09.nc`，S 使用 `salinity`，U/V 使用 `U`/`V`。
- 首尾只请求部分月份时，月文件仅包含该请求时段；完整性针对请求范围。请求指纹记录起止时间，改成整月请求时不会误复用局部文件。
- 旧流程月文件/块无请求指纹，不能证明下载区域和校验规则相同，不自动采用或覆盖。使用新的输出目录；后续 Process notebook 的 `data_dir` 指向相应新目录即可。
- 不支持两个下载进程同时写同一个输出目录；不同任务使用独立根目录。

## 输出与诊断

```text
HYCOM_download_v2/
  download_report.json                  # 每个月更新一次的执行报告
  HYCOM_2025_monthly_U_qc/
    HYCOM_2025_U_M09.nc                 # 全部请求数据通过 QC 后才存在
    HYCOM_2025_U_M09.complete.json      # 请求指纹、校验和、来源和 QC 配置
    .parts/M09_<请求指纹>/
      part_00000_00016.nc               # 已验证的原始数据块
      part_00016_00032.split.json       # 记录拆块，续传优先复用子块
      failures.json                    # 未下载成功的具体时刻与原因
```

`complete`：本次完成；`cached`：已验证并跳过；`incomplete`：单时刻仍失败；`failed`：覆盖、配置、网络或文件错误。`run()` 返回报告而不因一个月失败终止整个任务，自动化调用应检查返回报告的状态。

缓存不会自动删除，便于恢复损坏月文件。Ctrl+C 或进程被强杀后可能有唯一命名的 `.tmp` 残留；它们不会被当成有效块或月文件。

## 验证

在 HYCOM 目录运行离线测试：

```text
python -m unittest discover -s tests -v
```

覆盖真实日历分月、跨年、缺失时刻、升降序坐标、逐时刻 QC、U/V 原始零值筛查及显式关闭、CF 打包数值往返、超时、重试、自动拆块、单时刻故障续传、缓存损坏、原子写入中断、同名文件保护、持续断网，以及进度条在重试/拆块/续传/中断时的计数与关闭。

可选在线测试：`python -X utf8 tests/smoke_live.py`，每变量只下载一个时刻、表层、极小海域，输出位于 `verification/live_sample`。此测试需要服务器可达，元数据超时 300 秒，数据超时 180 秒；加 `--variables T` 可只验证温度。

2026-09-24 验证记录：19 项离线测试通过；notebook 代码语法、导入和配置单元检查通过。本次进度条和 U/V 零值检查调整未运行在线测试。此前在线检查曾成功读取 T 元数据，也出现过元数据超时；完整下载流程尚未通过实网验证，不能仅凭超时判断是本机网络还是远端服务导致。

参考：[HYCOM ESPC-D-V02 数据说明](https://www.hycom.org/dataserver/espc-d-v02/global-analysis)、[年度 U 聚合目录](https://tds.hycom.org/thredds/catalogs/ESPC-D-V02_u3z.html)、[xarray open_dataset / CF 解码](https://docs.xarray.dev/en/stable/generated/xarray.open_dataset.html)。
