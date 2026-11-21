#!/usr/bin/env python3
"""매매 파생값을 DB 함수로 한 번 갱신한다."""

import sys
import time

from collect import SupabaseError, _sb_config, _sb_request


def main() -> int:
    sb_url, sb_key = _sb_config()

    # 요청 한 번의 왕복 시간
    #   - 비교 대상은 이 함수의 DB 문장 제한 25초 (마이그레이션에서 함수 단위로 지정)
    started = time.monotonic()
    try:
        result = _sb_request("POST", "rpc/refresh_trade_stats", sb_url, sb_key, body={})
    except SupabaseError as error:
        elapsed = time.monotonic() - started
        print(f"status={error.status} {error.detail[:300]}", file=sys.stderr)
        print(f"elapsed_sec {elapsed:.1f}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started

    for key, value in result.items():
        print(f"{key} {value}")
    print(f"elapsed_sec {elapsed:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
