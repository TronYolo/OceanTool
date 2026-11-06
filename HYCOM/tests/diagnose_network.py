"""只读连通性诊断：DNS、TCP、TLS、THREDDS 和 OPeNDAP 元数据。"""
import socket
import ssl
import time
import multiprocessing as mp
import sys
from urllib.request import urlopen


def check(label, operation):
    start = time.monotonic()
    try:
        result = operation()
        print(f"{label}: OK ({time.monotonic() - start:.2f}s) {result}", flush=True)
    except Exception as exc:
        print(f"{label}: {type(exc).__name__} ({time.monotonic() - start:.2f}s) {exc}", flush=True)


def tcp():
    with socket.create_connection(("tds.hycom.org", 443), timeout=10) as conn:
        return conn.getpeername()


def tls():
    with socket.create_connection(("tds.hycom.org", 443), timeout=10) as conn:
        with ssl.create_default_context().wrap_socket(conn, server_hostname="tds.hycom.org") as secure:
            return secure.version()


def http(url):
    with urlopen(url, timeout=15) as response:
        data = response.read(16384)
        return {"status": response.status, "bytes_read": len(data), "type": response.headers.get("Content-Type")}


def backend_worker():
    import netCDF4
    import xarray as xr

    url = "https://tds.hycom.org/thredds/dodsC/ESPC-D-V02/t3z/2025"
    print("BACKEND: opening netCDF4 Dataset", flush=True)
    started = time.monotonic()
    with netCDF4.Dataset(url) as ds:
        print(f"BACKEND: netCDF4 opened ({time.monotonic() - started:.2f}s)", flush=True)
        for name in ("time", "depth", "lat", "lon"):
            print(f"BACKEND: reading coordinate {name}", flush=True)
            check(name, lambda name=name: {"shape": ds[name][:].shape})
        print("BACKEND: reading one water_temp value", flush=True)
        check("one raw value", lambda: str(ds["water_temp"][0, 0, 2500, 1500]))
    print("BACKEND: opening xarray Dataset", flush=True)
    started = time.monotonic()
    with xr.open_dataset(url, engine="netcdf4", decode_cf=False, cache=False) as ds:
        print(f"BACKEND: xarray opened ({time.monotonic() - started:.2f}s), sizes={dict(ds.sizes)}", flush=True)


def backend():
    process = mp.get_context("spawn").Process(target=backend_worker)
    process.start()
    process.join(timeout=90)
    if process.is_alive():
        print("BACKEND: timed out at the last printed stage (90s total)", flush=True)
        process.terminate()
        process.join(timeout=5)
    print(f"BACKEND: exit={process.exitcode}", flush=True)
    process.close()


if __name__ == "__main__":
    if "--backend" in sys.argv:
        backend()
        raise SystemExit(0)
    check("DNS", lambda: sorted({item[4][0] for item in socket.getaddrinfo("tds.hycom.org", 443, type=socket.SOCK_STREAM)}))
    check("TCP 443", tcp)
    check("TLS", tls)
    for label, url in [
        ("THREDDS catalog", "https://tds.hycom.org/thredds/catalog.html"),
        ("OPeNDAP DDS", "https://tds.hycom.org/thredds/dodsC/ESPC-D-V02/t3z/2025.dds"),
        ("OPeNDAP DAS", "https://tds.hycom.org/thredds/dodsC/ESPC-D-V02/t3z/2025.das"),
    ]:
        check(label, lambda url=url: http(url))
