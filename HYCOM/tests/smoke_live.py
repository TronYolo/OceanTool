"""可选在线小样本验证。每变量仅 1 个时刻、表层、极小海域，不下载全年。"""
import logging
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hycom_download import DownloadConfig, Downloader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variables", nargs="+", choices=["T", "S", "U", "V"], default=["T", "S", "U", "V"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = DownloadConfig(
        start="2025-09-01", end="2025-09-01 03:00", variables=tuple(args.variables),
        out_root=Path(__file__).resolve().parents[1] / "verification" / "live_sample",
        lon_range=(120.40, 120.48), lat_range=(22.0, 22.04), depth_range=(0, 0),
        chunk_steps=1, max_attempts=1, timeout_seconds=180, metadata_timeout_seconds=300,
    )
    downloader = Downloader(config)
    print(downloader.preview().to_string(index=False))
    result = downloader.run()
    print(result.to_string(index=False))
    if not result.status.isin(["complete", "cached"]).all():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
