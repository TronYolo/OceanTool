"""离线回归测试：python -m unittest discover -s tests -v"""
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import xarray as xr

from hycom_download import (
    DownloadConfig, Downloader, QCConfig, QCError, RemoteReader,
    VARIABLES, make_plans, spatial_slice, validate_data,
)


def fixture(key="T", times=None):
    if times is None:
        times = pd.date_range("2025-01-01", periods=8, freq="3h")
    hours = (pd.DatetimeIndex(times) - pd.Timestamp("2000-01-01")) / pd.Timedelta(hours=1)
    coords = {"time": ("time", np.asarray(hours), {"units": "hours since 2000-01-01 00:00:00", "calendar": "standard"}),
              "depth": [0.0, 10.0, 20.0], "lat": [22.0, 22.5], "lon": [120.0, 120.5, 121.0]}
    raw = np.full((len(times), 3, 2, 3), 1000, dtype=np.int16)
    raw[..., 0, 0] = -30000  # 陆地
    return xr.Dataset({VARIABLES[key].name: ( ("time", "depth", "lat", "lon"), raw,
                       {"_FillValue": np.int16(-30000), "scale_factor": 0.001, "add_offset": 20.0 if key == "T" else 0.0})}, coords=coords)


def metadata(ds, key):
    var = ds[VARIABLES[key].name]
    return {"coords": xr.Dataset(coords={d: ds[d] for d in var.dims}), "dtype": str(var.dtype), "dims": var.dims}


class FakeFactory:
    def __init__(self, ds, key="T", fail_large=False, bad_time=None, max_remote_steps=2):
        self.ds, self.key = ds, key
        self.fail_large, self.bad_time = fail_large, bad_time
        self.max_remote_steps = max_remote_steps
        self.calls = []
        self.transient_failures = 0
        self.closed = 0

    def __call__(self, url, timeout):
        factory = self

        class Reader:
            def request(self, operation, name, selection=None):
                if operation == "metadata":
                    return metadata(factory.ds, factory.key)
                span = selection["time"]
                factory.calls.append((span.start, span.stop))
                if factory.transient_failures:
                    factory.transient_failures -= 1
                    raise OSError("transient failure")
                if factory.fail_large and span.stop - span.start > factory.max_remote_steps:
                    raise TimeoutError("模拟服务端不接受大块")
                result = factory.ds.isel(selection).copy(deep=True)
                if factory.bad_time is not None and span.start <= factory.bad_time < span.stop:
                    result[name].values[factory.bad_time - span.start] = 0
                return result

            def close(self):
                factory.closed += 1

        return Reader()


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger("hycom.download").setLevel(logging.CRITICAL)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def config(self, **kwargs):
        args = dict(start="2025-01-01", end="2025-01-02", out_root=self.root,
                    variables=("T",), lon_range=(120, 121), lat_range=(22, 22.5),
                    depth_range=(0, 20), chunk_steps=4, max_attempts=2, backoff_seconds=0,
                    show_progress=False)
        args.update(kwargs)
        return DownloadConfig(**args)

    def test_calendar_months_2024_and_year_boundary(self):
        times = pd.date_range("2024-09-01 00:00", "2025-01-01 03:00", freq="3h")
        config = self.config(start="2024-09-30", end="2025-01-01 06:00")
        ds = fixture(times=times)
        p24 = make_plans(config, "T", 2024, "test/2024", metadata(ds, "T"))
        p25 = make_plans(config, "T", 2025, "test/2025", metadata(ds, "T"))
        self.assertEqual([p.size for p in p24], [8, 248, 240, 248])
        self.assertEqual(p25[0].size, 2)
        self.assertTrue(all(not len(p.missing_times) for p in p24 + p25))

    def test_missing_times_including_first_hours_block_month(self):
        ds = fixture(times=pd.date_range("2025-01-01 12:00", periods=4, freq="3h"))
        factory = FakeFactory(ds)
        downloader = Downloader(self.config(), reader_factory=factory)
        self.assertEqual(downloader.preview().iloc[0].missing_steps, 4)
        report = downloader.run()
        self.assertEqual(report.iloc[0].status, "failed")
        self.assertFalse(factory.calls)
        self.assertFalse(downloader.plan()[0].output.exists())

    def test_spatial_coverage_and_descending_axis(self):
        np.testing.assert_array_equal(np.arange(5)[spatial_slice(np.arange(5), (1.1, 2.5))], [1, 2, 3])
        axis = np.arange(5)[::-1]
        np.testing.assert_array_equal(axis[spatial_slice(axis, (1.1, 2.5))], [3, 2, 1])
        with self.assertRaises(ValueError):
            spatial_slice(axis, (-1, 2))

    def test_qc_per_timestep_zero_land_physical_range(self):
        ds = fixture()
        expected = metadata(ds, "T")["coords"]
        validate_data(ds, "T", expected, QCConfig())
        ds.water_temp.values[3] = 0  # 仅一个时刻坏，整块平均比例不一定能抓到
        with self.assertRaisesRegex(QCError, "raw==0"):
            validate_data(ds, "T", expected, QCConfig())
        u = fixture("U")
        u.water_u.values[:] = 0
        # 保留 U/V 零值筛查；需要接受真实零流速时，可显式关闭这项启发式。
        for key in ("U", "V"):
            flow = fixture(key)
            flow[VARIABLES[key].name].values[:] = 0
            with self.assertRaisesRegex(QCError, "raw==0"):
                validate_data(flow, key, metadata(flow, key)["coords"], QCConfig())
            validate_data(flow, key, metadata(flow, key)["coords"], QCConfig(raw_zero_limits={}))
        u.water_u.values[0, 0, 0, 0] = 20000
        with self.assertRaisesRegex(QCError, "物理范围"):
            validate_data(u, "U", metadata(u, "U")["coords"], QCConfig())

    def test_qc_empty_field_fill_value_and_wrong_coordinates(self):
        ds = fixture()
        expected = metadata(ds, "T")["coords"]
        ds.water_temp.values[0] = -30000
        with self.assertRaisesRegex(QCError, "空场"):
            validate_data(ds, "T", expected, QCConfig())
        ds = fixture().assign_coords(lon=[119, 120, 121])
        with self.assertRaisesRegex(QCError, "lon"):
            validate_data(ds, "T", expected, QCConfig())

    def test_adaptive_split_raw_roundtrip_and_cached_month(self):
        ds = fixture()
        factory = FakeFactory(ds, fail_large=True)
        downloader = Downloader(self.config(), reader_factory=factory)
        report = downloader.run()
        self.assertEqual(report.iloc[0].status, "complete", report.to_dict())
        output = downloader.plan()[0].output
        with xr.open_dataset(output, decode_cf=False) as actual:
            np.testing.assert_array_equal(actual.water_temp, ds.water_temp)
            self.assertEqual(actual.water_temp.dtype, np.int16)
            self.assertEqual(actual.water_temp.attrs, ds.water_temp.attrs)
        with xr.open_dataset(output) as actual:
            self.assertAlmostEqual(actual.water_temp.values[0, 0, 0, 1], 21.0)
            self.assertTrue(np.isnan(actual.water_temp.values[0, 0, 0, 0]))
        calls = len(factory.calls)
        self.assertEqual(downloader.run().iloc[0].status, "cached")
        self.assertEqual(len(factory.calls), calls)
        self.assertTrue(output.with_suffix(".complete.json").exists())

    def test_fail_single_time_then_resume_only_missing(self):
        factory = FakeFactory(fixture(), bad_time=2)
        downloader = Downloader(self.config(), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "incomplete")
        plan = downloader.plan()[0]
        self.assertFalse(plan.output.exists())
        failures = json.loads((plan.cache_dir / "failures.json").read_text(encoding="utf-8"))
        self.assertEqual(len(failures["failures"]), 1)
        factory.bad_time = None
        factory.calls.clear()
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        self.assertEqual(factory.calls, [(2, 3)])

    def test_transient_retry_and_no_false_disk_retry(self):
        factory = FakeFactory(fixture())
        factory.transient_failures = 1
        downloader = Downloader(self.config(), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        self.assertEqual(factory.calls[:2], [(0, 4), (0, 4)])
        with tempfile.TemporaryDirectory() as root:
            factory = FakeFactory(fixture())
            downloader = Downloader(self.config(out_root=root), reader_factory=factory)
            with patch.object(downloader, "_write_part", side_effect=OSError("disk full")):
                self.assertEqual(downloader.run().iloc[0].status, "failed")
            self.assertEqual(len(factory.calls), 1)

    def test_corrupt_part_is_redownloaded_without_stale_glob_merge(self):
        factory = FakeFactory(fixture(), bad_time=6)
        downloader = Downloader(self.config(), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "incomplete")
        plan = downloader.plan()[0]
        (plan.cache_dir / "part_00000_00004.nc").write_bytes(b"broken")
        (plan.cache_dir / "part_99999_99999.nc").write_bytes(b"stale unrelated part")
        factory.bad_time = None
        factory.calls.clear()
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        self.assertIn((0, 4), factory.calls)

    def test_changed_config_does_not_overwrite_existing_month(self):
        factory = FakeFactory(fixture())
        downloader = Downloader(self.config(), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        path = downloader.plan()[0].output
        original = path.read_bytes()
        altered = Downloader(self.config(lon_range=(120, 120.5)), reader_factory=factory)
        result = altered.run()
        self.assertEqual(result.iloc[0].status, "failed")
        self.assertIn("FileExistsError", result.iloc[0].detail)
        self.assertEqual(path.read_bytes(), original)

    def test_memory_limit_reduces_chunk_steps(self):
        factory = FakeFactory(fixture())
        downloader = Downloader(self.config(max_chunk_mb=72 / 1024**2), reader_factory=factory)
        self.assertEqual(downloader.plan()[0].chunk_steps, 2)

    def test_corrupt_completed_month_rebuilt_from_verified_parts(self):
        factory = FakeFactory(fixture())
        downloader = Downloader(self.config(), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        path = downloader.plan()[0].output
        path.write_bytes(b"corrupted after completion")
        factory.calls.clear()
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        self.assertFalse(factory.calls)
        with xr.open_dataset(path, decode_cf=False) as ds:
            self.assertEqual(ds.sizes["time"], 8)

    def test_interrupted_part_write_has_no_final_file(self):
        factory = FakeFactory(fixture())
        downloader = Downloader(self.config(), reader_factory=factory)
        plan = downloader.plan()[0]
        plan.cache_dir.mkdir(parents=True)
        path = plan.cache_dir / "part_00000_00004.nc"
        with patch.object(xr.Dataset, "to_netcdf", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                downloader._write_part(fixture().isel(time=slice(0, 4)), path, plan, 0, 4)
        self.assertFalse(path.exists())
        self.assertFalse(list(plan.cache_dir.glob("*.tmp")))

    def test_real_spawn_worker_local_netcdf_and_timeout(self):
        path = self.root / "source.nc"
        fixture().to_netcdf(path)
        reader = RemoteReader(str(path), timeout=20)
        self.addCleanup(reader.close)
        meta = reader.request("metadata", "water_temp")
        self.assertEqual(meta["dims"], ("time", "depth", "lat", "lon"))
        result = reader.request("data", "water_temp", {"time": slice(0, 2)})
        self.assertEqual(result.sizes["time"], 2)
        reader.close()
        reader.timeout = 0.000001
        with self.assertRaises(TimeoutError):
            reader.request("metadata", "water_temp")
        self.assertIsNone(reader.process)

    def test_persistent_outage_stops_source_without_request_storm(self):
        factory = FakeFactory(fixture())
        factory.transient_failures = 100
        downloader = Downloader(self.config(max_consecutive_remote_failures=3), reader_factory=factory)
        result = downloader.run()
        self.assertEqual(result.iloc[0].status, "failed")
        self.assertIn("SourceUnavailable", result.iloc[0].detail)
        self.assertEqual(len(factory.calls), 3)
        factory.transient_failures = 0
        self.assertEqual(downloader.run().iloc[0].status, "complete")

    def test_default_outage_limit_still_allows_split_to_single_step(self):
        ds = fixture(times=pd.date_range("2025-01-01", periods=16, freq="3h"))
        factory = FakeFactory(ds, fail_large=True, max_remote_steps=1)
        downloader = Downloader(self.config(end="2025-01-03", chunk_steps=16, max_attempts=4), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "complete")

    def test_separate_metadata_and_data_timeouts(self):
        actual = FakeFactory(fixture())
        timeouts = []

        def factory(url, timeout):
            timeouts.append(timeout)
            return actual(url, timeout)

        downloader = Downloader(self.config(metadata_timeout_seconds=300, timeout_seconds=180), reader_factory=factory)
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        self.assertEqual(timeouts, [300, 180])

    def test_progress_split_failure_resume_and_cached_month(self):
        bars = []

        def progress_factory(**kwargs):
            bar = RecordingProgress(**kwargs)
            bars.append(bar)
            return bar

        factory = FakeFactory(fixture(), fail_large=True, bad_time=2)
        downloader = Downloader(self.config(show_progress=True), reader_factory=factory,
                                progress_factory=progress_factory)
        self.assertEqual(downloader.run().iloc[0].status, "incomplete")
        bar = bars[-1]
        self.assertEqual((bar.n, bar.total), (8, 8))
        self.assertEqual([bar.postfix[k] for k in ("成功", "缓存", "失败")], [7, 0, 1])
        self.assertGreater(bar.postfix["重试"], 0)
        self.assertEqual(bar.postfix["状态"], "有缺失，未合并")
        self.assertEqual(bar.postfix["当前"], "01-01 21:00 UTC")
        self.assertTrue(bar.closed)
        self.assertFalse(downloader.plan()[0].output.exists())

        factory.bad_time = None
        self.assertEqual(downloader.run().iloc[0].status, "complete")
        self.assertEqual(bars[-1].n, 8)
        self.assertEqual([bars[-1].postfix[k] for k in ("成功", "缓存", "失败")], [1, 7, 0])
        self.assertEqual(bars[-1].postfix["状态"], "月文件已完成")
        self.assertTrue(bars[-1].closed)

        self.assertEqual(downloader.run().iloc[0].status, "cached")
        self.assertEqual(bars[-1].n, 8)
        self.assertEqual(bars[-1].postfix["缓存"], 8)
        self.assertTrue(bars[-1].closed)

    def test_progress_closes_without_false_completion_on_outage_or_interrupt(self):
        bars = []

        def progress_factory(**kwargs):
            bar = RecordingProgress(**kwargs)
            bars.append(bar)
            return bar

        factory = FakeFactory(fixture())
        factory.transient_failures = 100
        downloader = Downloader(self.config(max_consecutive_remote_failures=3),
                                reader_factory=factory, progress_factory=progress_factory)
        self.assertEqual(downloader.run().iloc[0].status, "failed")
        self.assertEqual(bars[-1].n, 0)
        self.assertEqual(bars[-1].postfix["状态"], "已停止，未完成")
        self.assertTrue(bars[-1].closed)
        factory.transient_failures = 0
        with patch.object(downloader, "_write_part", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                downloader.run()
        self.assertEqual(bars[-1].n, 0)
        self.assertEqual(bars[-1].postfix["状态"], "已中断，可续传")
        self.assertTrue(bars[-1].closed)


class RecordingProgress:
    """记录用户可见进度，用于检查重试、续传和中断的计数语义。"""

    def __init__(self, total, **kwargs):
        self.total, self.n, self.closed = total, 0, False
        self.postfix = {}

    def set_postfix(self, values, **kwargs):
        self.postfix = dict(values)

    def update(self, count):
        self.n += count
        assert self.n <= self.total, "拆块或重试重复推进进度"

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
