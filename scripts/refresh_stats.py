#!/usr/bin/env python3
"""매매 파생값을 DB 함수로 한 번 갱신한다."""

import sys

from collect import SupabaseError, _sb_config, _sb_request


def main() -> int:
    sb_url, sb_key = _sb_config()
    try:
        result = _sb_request("POST", "rpc/refresh_trade_stats", sb_url, sb_key, body={})
    except SupabaseError as error:
        print(f"status={error.status} {error.detail[:300]}", file=sys.stderr)
        return 1

    for key, value in result.items():
        print(f"{key} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
