#!/usr/bin/env python3
"""매매와 전월세 파생값을 DB 함수로 한 번씩 갱신한다."""

import sys
import time

from collect import SupabaseError, _sb_config, _sb_request

# 매매를 먼저 갱신
#   - 전세가율 뷰는 창이 같은 두 표만 이음
#   - 앞이 실패하면 뒤를 부르지 않아 둘이 같은 창에 머묾
FUNCTIONS = ("refresh_trade_stats", "refresh_rent_stats")


def main() -> int:
    sb_url, sb_key = _sb_config()

    for name in FUNCTIONS:
        # 요청 한 번의 왕복 시간
        #   - 비교 대상은 각 함수의 DB 문장 제한 25초 (마이그레이션에서 함수 단위로 지정)
        started = time.monotonic()
        try:
            result = _sb_request("POST", f"rpc/{name}", sb_url, sb_key, body={})
        except SupabaseError as error:
            elapsed = time.monotonic() - started
            print(f"{name} status={error.status} {error.detail[:300]}", file=sys.stderr)
            print(f"elapsed_sec {elapsed:.1f}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - started

        print(name)
        for key, value in result.items():
            print(f"{key} {value}")
        print(f"elapsed_sec {elapsed:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
