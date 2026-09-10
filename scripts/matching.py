"""도로명 매칭 규칙과 그 규칙이 사용하는 값 정규화.

K-apt CSV 전체를 읽어 구별 도로명 사전으로 파티션하고, 단지별 대표 거래 행을
골라 도로명 후보를 조회한다. 이름이나 지번 폴백은 사용하지 않는다.

road_key 를 바꿀 때는 scripts/recon.py 의 road_key 와 반환 문자열을 다시 대조한다.
"""

import collections
import csv
import pathlib
import sys
from decimal import Decimal, InvalidOperation


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
KAPT_CSV_PATH = ROOT / "data" / "seoul-apt-info.csv"


def _int(v) -> int:
    """'00221' -> 221. 빈 값이나 숫자가 아니면 0."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def road_key(rn, bonbun, bubun) -> str:
    """실거래: '선릉로' + '00221' + '00000' -> '선릉로 221'

    scripts/recon.py 의 road_key() 와 문자 단위로 같은 결과를 내야 함
      - 규칙이 갈라지면 recon.py 로 낸 강남구 실측(39.3%/84.3%)과 대조 불가
    실거래는 본번이 zero-pad 되어 오고, CSV 는 원문 그대로라 여기서 맞춤
    부번이 0 이면 붙이지 않음
    """
    b, s = _int(bonbun), _int(bubun)
    if not str(rn).strip() or not b:
        return ""
    return f"{str(rn).strip()} {b}" + (f"-{s}" if s else "")


def kapt_key(road_nm, detail) -> str:
    """K-apt: '주소(도로명)' + '주소(도로상세주소)' -> '선릉로 221'."""
    rn, de = str(road_nm or "").strip(), str(detail or "").strip()
    return f"{rn} {de}" if rn and de else ""


def area_key(v) -> str:
    """면적을 기존 조합 키나 표시 그룹 ID 에 넣을 문자열로 돌려줌.

    기존 조합은 원본 자릿수를 유지한다.
    표시 그룹은 coverage.py 의 display_groups()가 정규화한 최소 면적을 넘긴다.
    ROUND() 단독 그룹핑은 .5 경계 오분류 실측으로 폐기됐다.
    """
    try:
        return str(Decimal(str(v)))
    except (InvalidOperation, TypeError):
        return ""


def sort_key(r) -> tuple:
    """대표 도로명을 고를 때 쓰는 결정적 정렬 순서.

    첫 행을 고정하는 것은 recon.py 의 setdefault 와 같은 성격
    정렬을 명시하는 이유는 recon.py 와의 일치가 아니라
    이 스크립트가 실행할 때마다 같은 값을 낸다는 것 자체
      - 도로명이 갈리는 단지는 강남구 512개 중 4개(0.78%)
      - 다만 건수 가중으로는 205/10,237 = 2.00%p 까지 움직일 수 있음
    """
    return (str(r.get("deal_date") or ""), area_key(r.get("exclu_use_ar")),
            _int(r.get("floor")),
            road_key(r.get("road_nm"), r.get("road_nm_bonbun"), r.get("road_nm_bubun")))


class KaptIndex:
    """서울 전체 K-apt 행을 구별 도로명 키로 파티션한 조회 사전."""

    def __init__(self, by_gu: dict, source_rows: int, key_rows: int):
        self._by_gu = by_gu
        self.source_rows = source_rows
        self.key_rows = key_rows
        self.gu_count = len(by_gu)

    @classmethod
    def load(cls):
        """서울 전체 CSV를 읽어 구별 도로명 조회 사전을 생성."""
        rows, decoded = [], False
        # 디코딩이 조용히 성공할 수 있어, 예외 여부가 아니라 컬럼 존재로 확인
        for enc in ("cp949", "utf-8-sig", "euc-kr", "utf-8"):
            try:
                with open(KAPT_CSV_PATH, encoding=enc, newline="") as f:
                    rows = list(csv.DictReader(f))
            except (UnicodeDecodeError, LookupError):
                continue
            if rows and "주소(도로명)" in rows[0]:
                decoded = True
                break
        if not decoded:
            sys.exit(f"[중단] {KAPT_CSV_PATH.name} 을 읽지 못했습니다. 인코딩과 컬럼명을 확인하세요.")

        by_gu, n = collections.defaultdict(dict), 0
        for r in rows:
            k = kapt_key(r.get("주소(도로명)"), r.get("주소(도로상세주소)"))
            if not k:
                continue
            gu = (r.get("주소(시군구)") or "").strip()
            by_gu[gu].setdefault(k, []).append(
                ((r.get("k-아파트코드") or "").strip(), (r.get("k-아파트명") or "").strip()))
            n += 1
        return cls(by_gu, len(rows), n)

    def lookup(self, gu_name: str, key: str):
        """구 파티션 안에서 이미 구성된 사전만 조회."""
        return self._by_gu.get(gu_name, {}).get(key)


def match_complex(index: KaptIndex, gu_name: str, rows: list) -> tuple:
    """단지의 대표 도로명 키, 후보 목록, 매칭 상태를 반환."""
    head = min(rows, key=sort_key)
    key = road_key(head.get("road_nm"), head.get("road_nm_bonbun"), head.get("road_nm_bubun"))
    candidates = index.lookup(gu_name, key) if key else None
    if candidates:
        status = "matched"
    elif not key:
        status = "no_key"
    else:
        status = "no_entry"
    return key, candidates, status
