"""Fixed, read-only AKShare SDK worker; parent owns its strict process timeout."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from datetime import date
from importlib.metadata import version


def main() -> int:
    if len(sys.argv) not in {4, 5} or not re.fullmatch(r"\d{6}", sys.argv[1]):
        return 2
    symbol = sys.argv[1]
    timeout = float(sys.argv[4]) if len(sys.argv) == 5 else 12.0
    if not 0 < timeout <= 120:
        return 2
    start, end = date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
    if not 0 <= (end - start).days <= 366:
        return 2
    # Third-party progress/stdout must not corrupt the JSON channel; no cookie,
    # browser profile, account credential or user material is supplied to this SDK.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import akshare as ak
            frame = ak.stock_zh_a_hist(
                symbol=symbol, period="daily", start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), adjust="", timeout=timeout,
            )
        rows = [{str(key): str(value) for key, value in row.items()}
                for row in frame.to_dict(orient="records")]
        print(json.dumps({"sdk_version": version("akshare"), "upstream": "EASTMONEY",
                          "operation": "stock_zh_a_hist", "adjust": "", "rows": rows},
                         ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        from requests import exceptions

        failure = "INVALID_RESPONSE"
        if isinstance(exc, ImportError):
            failure = "CAPABILITY_UNAVAILABLE"
        elif isinstance(exc, exceptions.Timeout):
            failure = "TIMEOUT"
        elif isinstance(exc, exceptions.ConnectionError):
            failure = "NETWORK"
        elif isinstance(exc, exceptions.HTTPError):
            status = exc.response.status_code if exc.response is not None else 0
            failure = (
                "RATE_LIMITED" if status == 429
                else "ACCESS_RESTRICTED" if status in {401, 403}
                else "NETWORK" if status >= 500 else "INVALID_RESPONSE"
            )
        # Never return an untrusted exception message, URL or response body.
        print(json.dumps({"failure_class": failure, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
