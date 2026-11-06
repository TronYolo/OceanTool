"""ESPC-D-V02 区域下载：真实时间分月、可续传分块、QC、原子写入。

仅支持 time/depth/lat/lon 的 T/S/U/V。时间为 UTC，区间为 [start, end)。
下载保留源文件的打包整数和 CF 属性；QC 时才解码，避免二次 scale/offset。
"""
from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Callable
from uuid import uuid4

import netCDF4
import numpy as np
import pandas as pd
import xarray as xr
from tqdm.auto import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

LOG = logging.getLogger("hycom.download")
DIMS = ("time", "depth", "lat", "lon")
VERSION = "espc-download-v1"
BASE_URL = "https://tds.hycom.org/thredds/dodsC/ESPC-D-V02"


@dataclass(frozen=True)
class Variable:
    endpoint: str
    name: str
    file_label: str
    physical_range: tuple[float, float]


VARIABLES = {
    "T": Variable("t3z", "water_temp", "water_temp", (-5.0, 50.0)),
    "S": Variable("s3z", "salinity", "salinity", (0.0, 50.0)),
    "U": Variable("u3z", "water_u", "U", (-15.0, 15.0)),
    "V": Variable("v3z", "water_v", "V", (-15.0, 15.0)),
}


@dataclass(frozen=True)
class QCConfig:
    # 保守的物理范围，可针对研究区域覆盖默认值。
    physical_ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    # 逐时刻检查最浅选中层的有效点中 raw==0 比例；U/V 保留旧版 1% 判据。
    raw_zero_limits: dict[str, float] = field(default_factory=lambda: {"T": 0.20, "S": 0.20, "U": 0.01, "V": 0.01})
    min_valid_fraction: float = 0.0  # 0 表示至少一个有效点；陆地比例不作固定假设

    def __post_init__(self):
        if not 0 <= self.min_valid_fraction <= 1:
            raise ValueError("min_valid_fraction 必须在 [0, 1] 内")
        for key, value in self.raw_zero_limits.items():
            if key not in VARIABLES or not 0 <= value <= 1:
                raise ValueError("raw_zero_limits 必须为 T/S/U/V 到 [0, 1] 阈值的映射")
        for key, bounds in self.physical_ranges.items():
            if key not in VARIABLES or len(bounds) != 2 or not np.all(np.isfinite(bounds)) or bounds[0] >= bounds[1]:
                raise ValueError("physical_ranges 的变量或上下限无效")


@dataclass(frozen=True)
class DownloadConfig:
    start: str
    end: str
    out_root: Path | str
    variables: tuple[str, ...] = ("U", "V")
    lon_range: tuple[float, float] = (116.0, 123.0)
    lat_range: tuple[float, float] = (20.5, 27.5)
    depth_range: tuple[float, float] = (0.0, 3800.0)
    chunk_steps: int = 1  # 公司慢网络默认一步一块；网络稳定后可调为 4/8/16
    max_chunk_mb: float = 32.0  # 原始数组上限；QC/序列化另需数倍临时内存
    max_attempts: int = 4  # 每个块（包括拆小后的块）的总尝试次数
    max_consecutive_remote_failures: int = 32  # 允许默认 16 步逐级拆到 1 步后再判断持续断网
    timeout_seconds: float = 180.0  # 数据块读取的硬超时（含重连时的打开操作）
    metadata_timeout_seconds: float = 300.0  # 首次打开和读取完整坐标单独留足时间
    backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    compression_level: int = 1
    show_progress: bool = True
    base_url: str = BASE_URL
    qc: QCConfig = field(default_factory=QCConfig)

    def __post_init__(self):
        if utc_timestamp(self.start) >= utc_timestamp(self.end):
            raise ValueError("start 必须早于 end（end 不包含在下载范围中）")
        if utc_timestamp(self.start) < pd.Timestamp("2024-08-10"):
            raise ValueError("此下载器仅支持 2024-08-10 起的 ESPC-D-V02")
        if not self.variables or len(set(self.variables)) != len(self.variables) or any(k not in VARIABLES for k in self.variables):
            raise ValueError("variables 必须是非空、不重复的 T/S/U/V 序列")
        for name in ("chunk_steps", "max_attempts", "max_consecutive_remote_failures"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("max_chunk_mb", "timeout_seconds", "metadata_timeout_seconds"):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if not 0 <= self.compression_level <= 9:
            raise ValueError("compression_level 必须在 0 到 9 之间")
        if min(self.backoff_seconds, self.max_backoff_seconds) < 0:
            raise ValueError("重试等待时间不能为负数")
        for name in ("lon_range", "lat_range", "depth_range"):
            bounds = getattr(self, name)
            if len(bounds) != 2 or not np.all(np.isfinite(bounds)) or bounds[0] > bounds[1]:
                raise ValueError(f"{name} 必须是有限且递增的范围；不支持跨日期变更线")


def utc_timestamp(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("时间不能为空")
    return result.tz_convert("UTC").tz_localize(None) if result.tzinfo else result


class QCError(ValueError):
    """数据内容未通过校验；可重试下载，但不能直接合并。"""


class SourceUnavailable(OSError):
    """持续网络故障，停止本轮访问同一数据源，留待下次续传。"""


def _remote_worker(pipe, url: str):
    """网络访问隔离在可终止子进程中，避免 netCDF-C 长时间卡住 notebook。"""
    ds = None
    try:
        while True:
            request = pipe.recv()
            if request is None:
                return
            try:
                if ds is None:
                    pipe.send((None, "打开远端数据集（含坐标索引）"))
                    ds = xr.open_dataset(url, engine="netcdf4", decode_cf=False, cache=False)
                    pipe.send((None, "远端数据集已打开"))
                operation, varname, selection = request
                if operation == "metadata":
                    var = ds[varname]
                    coords = {}
                    for dim in DIMS:
                        pipe.send((None, f"读取 {dim} 坐标"))
                        coords[dim] = ds[dim].load()
                    payload = {
                        "coords": xr.Dataset(coords=coords),
                        "dtype": str(var.dtype), "dims": var.dims,
                    }
                else:
                    pipe.send((None, f"读取 {varname} 数据块"))
                    payload = ds[[varname]].isel(selection).transpose(*DIMS).load()
                    # 只传已加载数组，不把后端句柄带回父进程。
                    payload = xr.Dataset(
                        {varname: payload[varname]},
                        coords={d: payload[d] for d in DIMS}, attrs=ds.attrs,
                    )
                pipe.send((True, payload))
            except Exception as exc:
                pipe.send((False, f"{type(exc).__name__}: {exc}"))
                if ds is not None:
                    ds.close()
                ds = None
    except (EOFError, BrokenPipeError):
        pass
    finally:
        if ds is not None:
            ds.close()
        pipe.close()


class RemoteReader:
    """单连接复用；失败重连。串行请求，避免 netCDF4 线程安全问题。"""

    def __init__(self, url: str, timeout: float):
        self.url, self.timeout = url, timeout
        self.process = self.pipe = None

    def request(self, operation: str, varname: str, selection=None):
        if self.process is None:
            ctx = mp.get_context("spawn")
            self.pipe, child = ctx.Pipe()
            self.process = ctx.Process(target=_remote_worker, args=(child, self.url), daemon=True)
            self.process.start()
            child.close()
        try:
            self.pipe.send((operation, varname, selection))
            deadline = time.monotonic() + self.timeout
            stage = "等待读取进程响应"
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.pipe.poll(remaining):
                    raise TimeoutError(f"远端操作超过 {self.timeout:g}s，最后阶段：{stage}；{self.url}")
                ok, payload = self.pipe.recv()
                if ok is None:
                    stage = payload
                    LOG.debug("远端进度：%s", stage)
                    continue
                if not ok:
                    raise OSError(payload)
                return payload
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            # 无论服务端是否卡住，都能在有限时间内结束。
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.process.close()
            self.pipe.close()
            self.process = self.pipe = None


def spatial_slice(axis, bounds) -> slice:
    """支持升/降序坐标；向外取最近一层，完整覆盖指定范围。"""
    values = np.asarray(axis, dtype=float)
    if values.ndim != 1 or not len(values) or not np.all(np.isfinite(values)):
        raise ValueError("空间坐标必须是有限的一维数组")
    delta = np.diff(values)
    if len(values) > 1 and not (np.all(delta > 0) or np.all(delta < 0)):
        raise ValueError("空间坐标必须严格单调")
    descending = len(values) > 1 and values[0] > values[-1]
    ascending = values[::-1] if descending else values
    lo, hi = bounds
    if lo < ascending[0] or hi > ascending[-1]:
        raise ValueError(f"请求范围 {bounds} 超出坐标范围 {ascending[0], ascending[-1]}；经度须采用源数据坐标制")
    i = max(0, int(np.searchsorted(ascending, lo, side="right")) - 1)
    j = min(len(values) - 1, int(np.searchsorted(ascending, hi, side="left")))
    if descending:
        i, j = len(values) - 1 - j, len(values) - 1 - i
    return slice(i, j + 1)


def decoded_times(coords: xr.Dataset) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(xr.decode_cf(coords[["time"]])["time"].values)


@dataclass
class MonthPlan:
    key: str
    url: str
    year: int
    month: int
    selection: dict
    coords: xr.Dataset
    dtype: str
    missing_times: pd.DatetimeIndex
    signature: str
    output: Path
    cache_dir: Path
    chunk_steps: int

    @property
    def size(self):
        return self.coords.sizes["time"]


def make_plans(config: DownloadConfig, key: str, year: int, url: str, meta: dict) -> list[MonthPlan]:
    if set(meta["dims"]) != set(DIMS) or len(meta["dims"]) != 4:
        raise ValueError(f"{key} 维度不支持：{meta['dims']}")
    coords = meta["coords"]
    dates = decoded_times(coords)
    if dates.hasnans or not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError("服务器时间轴有空值、重复或乱序，请检查数据源")
    if not np.all(dates == dates.floor("3h")):
        raise ValueError("源时间不在 UTC 三小时间隔上，请检查是否选错数据源")
    space = {d: spatial_slice(coords[d], getattr(config, f"{d}_range")) for d in DIMS[1:]}
    small = coords.isel(space)
    bytes_per_step = np.prod([small.sizes[d] for d in DIMS[1:]]) * np.dtype(meta["dtype"]).itemsize
    if bytes_per_step > config.max_chunk_mb * 1024**2:
        raise ValueError("单个时间步超过 max_chunk_mb；请缩小区域或增加内存上限")
    chunk_steps = min(config.chunk_steps, max(1, int(config.max_chunk_mb * 1024**2 / bytes_per_step)))
    start, end = utc_timestamp(config.start), utc_timestamp(config.end)
    plans = []
    for month in range(1, 13):
        month_start = pd.Timestamp(year=year, month=month, day=1)
        a, b = max(start, month_start), min(end, month_start + pd.offsets.MonthBegin(1))
        if a >= b:
            continue
        expected = pd.date_range(a.ceil("3h"), b, freq="3h", inclusive="left")
        if not len(expected):
            continue
        indices = np.flatnonzero((dates >= a) & (dates < b))
        i = int(indices[0]) if len(indices) else 0
        j = int(indices[-1]) + 1 if len(indices) else 0
        subset = small.isel(time=slice(i, j))
        missing = expected.difference(dates[i:j])
        signature_data = {
            "version": VERSION, "url": url, "variable": key,
            "start": str(a), "end": str(b), "qc": asdict(config.qc),
            "dtype": meta["dtype"],
            "coords": {d: subset[d].values.tolist() for d in DIMS},
            "time_attrs": dict(subset.time.attrs),
        }
        signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True, default=str).encode()).hexdigest()
        folder = Path(config.out_root) / f"HYCOM_{year}_monthly_{key}_qc"
        label = VARIABLES[key].file_label
        output = folder / f"HYCOM_{year}_{label}_M{month:02d}.nc"
        plans.append(MonthPlan(key, url, year, month, {**space, "time": slice(i, j)}, subset,
                               meta["dtype"], missing, signature, output,
                               folder / ".parts" / f"M{month:02d}_{signature[:16]}", chunk_steps))
    return plans


def validate_data(ds: xr.Dataset, key: str, expected: xr.Dataset, qc: QCConfig) -> dict:
    """检查每个时刻、全部深度的数值；统计不把陆地 FillValue 当成有效数据。"""
    spec = VARIABLES[key]
    if spec.name not in ds or tuple(ds[spec.name].dims) != DIMS:
        raise QCError(f"缺少变量 {spec.name} 或维度顺序错误")
    for dim in DIMS:
        if dim not in ds.coords or not np.array_equal(ds[dim].values, expected[dim].values):
            raise QCError(f"{dim} 坐标与请求不一致")
    for attr in ("units", "calendar"):
        default = "standard" if attr == "calendar" else None
        if ds.time.attrs.get(attr, default) != expected.time.attrs.get(attr, default):
            raise QCError(f"time.{attr} 与请求不一致")
    raw = np.asarray(ds[spec.name].values)
    if raw.shape != tuple(expected.sizes[d] for d in DIMS) or not raw.shape[0]:
        raise QCError("数组形状错误或没有时间步")
    attrs = ds[spec.name].attrs
    valid = np.isfinite(raw)
    for attr in ("_FillValue", "missing_value"):
        for value in np.asarray(attrs.get(attr, []), dtype=float).ravel():
            valid &= raw != value
    if np.any(np.isinf(raw)):
        raise QCError("检测到无穷值")
    counts = valid.reshape(len(raw), -1).sum(axis=1)
    fractions = counts / np.prod(raw.shape[1:])
    bad = (counts == 0) | (fractions < qc.min_valid_fraction)
    if np.any(bad):
        raise QCError(f"空场/有效比例不足，块内时间索引 {np.flatnonzero(bad).tolist()}")
    scale = float(attrs.get("scale_factor", 1.0))
    offset = float(attrs.get("add_offset", 0.0))
    physical = raw.astype(np.float64) * scale + offset
    lo, hi = qc.physical_ranges.get(key, spec.physical_range)
    out_of_range = valid & (~np.isfinite(physical) | (physical < lo) | (physical > hi))
    if out_of_range.any():
        raise QCError(f"{int(out_of_range.sum())} 个有效值超出物理范围 [{lo}, {hi}]")
    k = int(np.argmin(ds.depth.values))
    surface_valid = valid[:, k]
    denominator = surface_valid.sum(axis=(1, 2))
    zero_fraction = np.divide(((raw[:, k] == 0) & surface_valid).sum(axis=(1, 2)), denominator,
                              out=np.zeros(len(raw), dtype=float), where=denominator > 0)
    limit = qc.raw_zero_limits.get(key)
    if limit is not None and np.any(zero_fraction > limit):
        raise QCError(f"最浅选中层 raw==0 比例 {zero_fraction.max():.1%} 超过 {limit:.1%}（疑似坏场）")
    return {"min_valid_fraction": float(fractions.min()), "max_raw_zero_fraction": float(zero_fraction.max())}


def atomic_json(path: Path, payload: dict):
    temp = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class MonthProgress:
    """按已处理的时间步计数；失败和成功分开显示，重试不重复推进进度。"""

    def __init__(self, plan: MonthPlan, enabled: bool, factory=tqdm):
        self.dates = decoded_times(plan.coords)
        self.stats = {"成功": 0, "缓存": 0, "失败": 0, "重试": 0}
        self.current = ""
        self.bar = factory(total=plan.size, desc=f"{plan.key} {plan.year}-{plan.month:02d} 已处理时间步",
                           unit="步", disable=not enabled, mininterval=0.5, miniters=1,
                           dynamic_ncols=True, leave=True)
        self.stage("检查缓存")

    def stage(self, state: str, index: int | None = None):
        if index is not None:
            self.current = self.dates[index].strftime("%m-%d %H:%M UTC")
        self.bar.set_postfix({"当前": self.current, **self.stats, "状态": state}, refresh=True)

    def advance(self, count: int, kind: str, index: int):
        self.stats[kind] += count
        self.stage("继续下载" if kind != "失败" else "已记录失败", index)
        self.bar.update(count)

    def retry(self, index: int, attempt: int, total: int):
        self.stats["重试"] += 1
        self.stage(f"重试 {attempt}/{total}", index)

    def close(self):
        self.bar.close()


class Downloader:
    def __init__(self, config: DownloadConfig, *, reader_factory: Callable = RemoteReader,
                 sleep: Callable = time.sleep, progress_factory: Callable = tqdm):
        self.config, self.reader_factory, self.sleep = config, reader_factory, sleep
        self._plans = None
        self._remote_failures = 0
        self.progress_factory = progress_factory

    def _pause(self, attempt: int):
        delay = min(self.config.max_backoff_seconds, self.config.backoff_seconds * 2**attempt)
        if delay:
            self.sleep(delay * random.uniform(0.8, 1.2))

    def _metadata(self, reader, name):
        for attempt in range(self.config.max_attempts):
            try:
                return reader.request("metadata", name)
            except (OSError, TimeoutError, EOFError) as exc:
                reader.close()
                LOG.warning("读取元数据失败 %s/%s：%s", attempt + 1, self.config.max_attempts, exc)
                if attempt + 1 == self.config.max_attempts:
                    raise
                self._pause(attempt)

    def plan(self) -> list[MonthPlan]:
        """只读服务器坐标；不拉取四维数据，也不创建输出文件。"""
        if self._plans is not None:
            return self._plans
        config = self.config
        first = utc_timestamp(config.start).year
        last = (utc_timestamp(config.end) - pd.Timedelta(nanoseconds=1)).year
        plans = []
        for key in config.variables:
            for year in range(first, last + 1):
                url = f"{config.base_url.rstrip('/')}/{VARIABLES[key].endpoint}/{year}"
                reader = self.reader_factory(url, config.metadata_timeout_seconds)
                try:
                    LOG.info("读取 %s %s 年时间和空间坐标：%s", key, year, url)
                    meta = self._metadata(reader, VARIABLES[key].name)
                    plans.extend(make_plans(config, key, year, url, meta))
                finally:
                    reader.close()
        self._plans = plans
        return plans

    def preview(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "variable": p.key, "month": f"{p.year}-{p.month:02d}", "steps": p.size,
            "missing_steps": len(p.missing_times), "chunk_steps": p.chunk_steps,
            "raw_MB": round(np.prod(list(p.coords.sizes.values())) * np.dtype(p.dtype).itemsize / 1024**2, 2),
            "first_time": str(decoded_times(p.coords)[0]) if p.size else "",
            "last_time": str(decoded_times(p.coords)[-1]) if p.size else "",
            "output": str(p.output),
        } for p in self.plan()])

    def _valid_file(self, path: Path, plan: MonthPlan, a: int, b: int) -> bool:
        if not path.exists():
            return False
        try:
            with xr.open_dataset(path, engine="netcdf4", decode_cf=False, cache=False) as ds:
                if ds.attrs.get("hycom_request_id") != plan.signature:
                    return False
                if ds.sizes.get("time") != b - a:
                    return False
                for i in range(a, b, plan.chunk_steps):
                    j = min(i + plan.chunk_steps, b)
                    validate_data(ds.isel(time=slice(i - a, j - a)), plan.key,
                                  plan.coords.isel(time=slice(i, j)), self.config.qc)
            return True
        except (OSError, ValueError, RuntimeError, KeyError, IndexError) as exc:
            LOG.warning("缓存校验失败 %s：%s", path.name, exc)
            return False

    def _write_part(self, ds: xr.Dataset, path: Path, plan: MonthPlan, a: int, b: int):
        temp = path.with_name(path.name + f".{uuid4().hex}.tmp")
        try:
            ds.attrs["hycom_request_id"] = plan.signature
            ds.attrs["hycom_source"] = plan.url
            # 原始整数 + CF 属性原样保存，禁止 xarray 自动使用旧 encoding 再次打包。
            for var in ds.variables.values():
                var.encoding = {}
            name = VARIABLES[plan.key].name
            ds.to_netcdf(temp, engine="netcdf4", encoding={name: {
                "zlib": self.config.compression_level > 0, "complevel": self.config.compression_level,
                "_FillValue": ds[name].attrs.pop("_FillValue", None),
            }})
            if not self._valid_file(temp, plan, a, b):
                raise QCError("写入后的块校验失败")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _download_span(self, reader, plan: MonthPlan, a: int, b: int, failures: list,
                       progress: MonthProgress | None = None) -> list[Path]:
        path = plan.cache_dir / f"part_{a:05d}_{b:05d}.nc"
        if self._valid_file(path, plan, a, b):
            LOG.info("复用 %s %02d月 [%d:%d]", plan.key, plan.month, a, b)
            if progress is not None:
                progress.advance(b - a, "缓存", b - 1)
            return [path]
        split_marker = path.with_suffix(".split.json")
        error = ""
        if not split_marker.exists() or b - a == 1:
            for attempt in range(self.config.max_attempts):
                if progress is not None:
                    if attempt:
                        progress.retry(a, attempt + 1, self.config.max_attempts)
                    else:
                        progress.stage(f"下载 {a + 1}–{b}/{plan.size}", a)
                try:
                    selection = {**plan.selection, "time": slice(plan.selection["time"].start + a,
                                                                 plan.selection["time"].start + b)}
                    ds = reader.request("data", VARIABLES[plan.key].name, selection)
                    self._remote_failures = 0
                    validate_data(ds, plan.key, plan.coords.isel(time=slice(a, b)), self.config.qc)
                except (OSError, TimeoutError, EOFError, QCError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    reader.close()
                    if not isinstance(exc, QCError):
                        self._remote_failures += 1
                        if self._remote_failures >= self.config.max_consecutive_remote_failures:
                            raise SourceUnavailable(f"连续 {self._remote_failures} 次远端请求失败，暂停本轮数据源：{plan.url}；最后错误：{error}") from exc
                    LOG.warning("%s %02d月 [%d:%d] 尝试 %d/%d 失败：%s", plan.key, plan.month,
                                a, b, attempt + 1, self.config.max_attempts, error)
                    if attempt + 1 < self.config.max_attempts:
                        if progress is not None:
                            progress.stage("等待退避重试", a)
                        self._pause(attempt)
                else:
                    # 磁盘错误不当作网络错误反复下载。
                    self._write_part(ds, path, plan, a, b)
                    if progress is not None:
                        progress.advance(b - a, "成功", b - 1)
                    LOG.info("完成 %s %02d月 [%d:%d] / %d", plan.key, plan.month, a, b, plan.size)
                    return [path]
        if b - a > 1:
            mid = (a + b) // 2
            atomic_json(split_marker, {"split_at": mid, "reason": error})
            return (self._download_span(reader, plan, a, mid, failures, progress)
                    + self._download_span(reader, plan, mid, b, failures, progress))
        failures.append({"time": str(decoded_times(plan.coords)[a]), "index": a, "reason": error})
        if progress is not None:
            progress.advance(1, "失败", a)
        return []

    def _merge(self, parts: list[Path], plan: MonthPlan):
        """逐块复制到月文件，内存不随月份长度增长；不依赖 dask/open_mfdataset。"""
        temp = plan.output.with_name(plan.output.name + f".{uuid4().hex}.tmp")
        name = VARIABLES[plan.key].name
        try:
            with netCDF4.Dataset(temp, "w") as target:
                offset = 0
                for part in parts:
                    with netCDF4.Dataset(part) as source:
                        source.set_auto_maskandscale(False)
                        if offset == 0:
                            target.setncatts({k: source.getncattr(k) for k in source.ncattrs()})
                            for dim, size in source.dimensions.items():
                                target.createDimension(dim, None if dim == "time" else len(size))
                            for varname, var in source.variables.items():
                                attrs = {k: var.getncattr(k) for k in var.ncattrs()}
                                fill = attrs.pop("_FillValue", False)
                                options = {"zlib": self.config.compression_level > 0,
                                           "complevel": self.config.compression_level} if varname == name else {}
                                out = target.createVariable(varname, var.dtype, var.dimensions, fill_value=fill, **options)
                                out.setncatts(attrs)
                                out.set_auto_maskandscale(False)
                                if "time" not in var.dimensions:
                                    out[:] = var[:]
                        n = len(source.dimensions["time"])
                        for varname, var in source.variables.items():
                            if "time" in var.dimensions:
                                target[varname][offset:offset + n] = var[:]
                        offset += n
                target.setncattr("hycom_complete", 1)
                target.setncattr("hycom_downloader", VERSION)
            if not self._valid_file(temp, plan, 0, plan.size):
                raise QCError("合并后时间、坐标或内容校验失败")
            os.replace(temp, plan.output)
            self._write_receipt(plan)
        finally:
            temp.unlink(missing_ok=True)

    def _write_receipt(self, plan):
        atomic_json(plan.output.with_suffix(".complete.json"), {
            "request_id": plan.signature, "sha256": file_sha256(plan.output),
            "steps": plan.size, "source": plan.url, "qc": asdict(self.config.qc),
        })

    def _month_complete(self, plan) -> bool:
        if not plan.output.exists():
            return False
        receipt = plan.output.with_suffix(".complete.json")
        owned = False
        if receipt.exists():
            try:
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                owned = saved["request_id"] == plan.signature
                if owned and saved["sha256"] == file_sha256(plan.output):
                    return True
            except (ValueError, KeyError):
                pass
        # 无凭证/文件变化时完整复检，不能仅凭时间步数跳过。
        if self._valid_file(plan.output, plan, 0, plan.size):
            self._write_receipt(plan)
            return True
        try:
            with xr.open_dataset(plan.output, engine="netcdf4", decode_cf=False) as existing:
                owned = existing.attrs.get("hycom_request_id") == plan.signature
        except (OSError, ValueError, RuntimeError):
            # 损坏到无法打开时，只有本次请求的完成凭证才能确认文件归属。
            pass
        if not owned:
            raise FileExistsError(f"同名月文件来自旧流程或不同请求，保留原文件；请换 out_root 或先移走：{plan.output}")
        return False

    def run(self) -> pd.DataFrame:
        """保留重试日志，同时避免终端日志覆盖进度条。"""
        with logging_redirect_tqdm(tqdm_class=tqdm):
            return self._run()

    def _run(self) -> pd.DataFrame:
        """失败月份继续保留分块；无 NaN 占位、不把不完整月份标为完成。"""
        results = []
        plans = self.plan()
        reader, active_url = None, None
        unavailable = {}
        try:
            for plan in plans:
                row = {"variable": plan.key, "month": f"{plan.year}-{plan.month:02d}",
                       "output": str(plan.output), "status": "", "detail": ""}
                plan.output.parent.mkdir(parents=True, exist_ok=True)
                progress = None
                try:
                    if len(plan.missing_times):
                        raise ValueError(f"数据源缺少 {len(plan.missing_times)} 个请求时刻，示例 {list(plan.missing_times[:4].astype(str))}；请核对年度聚合覆盖范围")
                    progress = MonthProgress(plan, self.config.show_progress, self.progress_factory)
                    if self._month_complete(plan):
                        row["status"] = "cached"
                        progress.advance(plan.size, "缓存", plan.size - 1)
                        progress.stage("整月已验证")
                        LOG.info("整月已验证，跳过 %s", plan.output.name)
                    else:
                        plan.cache_dir.mkdir(parents=True, exist_ok=True)
                        if plan.url in unavailable:
                            raise SourceUnavailable(unavailable[plan.url])
                        if active_url != plan.url:
                            if reader is not None:
                                reader.close()
                            reader = self.reader_factory(plan.url, self.config.timeout_seconds)
                            active_url = plan.url
                            self._remote_failures = 0
                        parts, failures = [], []
                        for a in range(0, plan.size, plan.chunk_steps):
                            parts.extend(self._download_span(reader, plan, a, min(a + plan.chunk_steps, plan.size), failures, progress))
                        atomic_json(plan.cache_dir / "failures.json", {"failures": failures})
                        if failures:
                            row.update(status="incomplete", detail=f"{len(failures)} 个时刻失败；见 {plan.cache_dir / 'failures.json'}")
                            progress.stage("有缺失，未合并")
                        else:
                            progress.stage("正在合并和复检")
                            self._merge(parts, plan)
                            row["status"] = "complete"
                            progress.stage("月文件已完成")
                except (OSError, ValueError, RuntimeError) as exc:
                    if isinstance(exc, SourceUnavailable):
                        unavailable[plan.url] = str(exc)
                    row.update(status="failed", detail=f"{type(exc).__name__}: {exc}")
                    if progress is not None:
                        progress.stage("已停止，未完成")
                    LOG.error("%s %s：%s", plan.key, row["month"], row["detail"])
                except KeyboardInterrupt:
                    if progress is not None:
                        progress.stage("已中断，可续传")
                    raise
                finally:
                    if progress is not None:
                        progress.close()
                results.append(row)
                # 每个月保存当前进度；中途 Ctrl+C 仍可从分块继续。
                atomic_json(Path(self.config.out_root) / "download_report.json", {"results": results})
        finally:
            if reader is not None:
                reader.close()
        result = pd.DataFrame(results)
        if not result.empty and result.status.isin(["failed", "incomplete"]).any():
            LOG.warning("存在未完成月份；修正问题后重新运行，已验证分块会复用。")
        return result
