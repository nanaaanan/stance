"""도로명 매칭 규칙과 그 규칙이 사용하는 값 정규화.

K-apt CSV 전체를 읽어 구별 도로명 사전으로 파티션하고, 단지별 대표 거래 행을
골라 도로명 후보를 조회한다. 이름이나 지번 폴백은 사용하지 않는다.

road_key 를 바꿀 때는 scripts/recon.py 의 road_key 와 반환 문자열을 다시 대조한다.

실행
    set -a; source .env; set +a
    python3 scripts/matching.py
"""

import collections
import csv
import hashlib
import importlib.util
import pathlib
import sys
from decimal import Decimal, InvalidOperation


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
KAPT_CSV_PATH = ROOT / "data" / "seoul-apt-info.csv"
KAPT_CSV_SHA256_PREFIX = "b57914a8"
NORMALIZATION_NAMES = {
    1: "공백",
    2: "zero-pad",
    3: "부번 0",
    4: "전각/반각",
}
# 기여 측정에서 신규매칭이 있고 소실매칭이 없었던 변형만 운영 경로에 둔다.
ACTIVE_NORMALIZATION_VARIANTS = (3,)


def _validate_active_normalization_snapshot():
    """운영 변형을 판정한 K-apt 파일이 그대로인지 확인."""
    try:
        with open(KAPT_CSV_PATH, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()[:8]
    except OSError as exc:
        sys.exit(f"[중단] {KAPT_CSV_PATH.name} 을 읽지 못했습니다: {exc}")
    if actual != KAPT_CSV_SHA256_PREFIX:
        sys.exit("[중단] K-apt 파일이 바뀌었습니다. 정규화 기여를 다시 측정하세요.")


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


def _normalize_kapt_parts(road_nm, detail, variants=()) -> tuple[str, str]:
    """기여 측정용 변형을 K-apt 도로명과 상세주소에만 적용."""
    rn, de = str(road_nm or ""), str(detail or "")
    enabled = set(variants)

    if 4 in enabled:
        rn = rn.replace("\uff0d", "-").replace("\u3000", " ")
        de = de.replace("\uff0d", "-").replace("\u3000", " ")
    if 1 in enabled:
        rn = " ".join(part for part in rn.strip().split(" ") if part)
        de = " ".join(part for part in de.strip().split(" ") if part)
    if 2 in enabled and de.strip():
        tokens = de.strip().split("-")
        if all(token.strip().isdecimal() for token in tokens):
            de = "-".join(str(_int(token)) for token in tokens)
    if 3 in enabled:
        parts = de.strip().split("-")
        if len(parts) == 2 and parts[1] and set(parts[1]) == {"0"}:
            de = parts[0]
    return rn.strip(), de.strip()


def kapt_key(road_nm, detail, normalization_variants=()) -> str:
    """K-apt: '주소(도로명)' + '주소(도로상세주소)' -> '선릉로 221'."""
    rn, de = _normalize_kapt_parts(road_nm, detail, normalization_variants)
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
    def read_rows(cls):
        """서울 전체 CSV를 한 번 읽어 원본 행을 반환."""
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
        return rows

    @classmethod
    def from_rows(cls, rows: list, normalization_variants=()):
        """이미 읽은 행으로 지정 변형의 구별 도로명 사전을 생성."""
        by_gu, n = collections.defaultdict(dict), 0
        for r in rows:
            k = kapt_key(r.get("주소(도로명)"), r.get("주소(도로상세주소)"),
                         normalization_variants)
            if not k:
                continue
            gu = (r.get("주소(시군구)") or "").strip()
            by_gu[gu].setdefault(k, []).append(
                ((r.get("k-아파트코드") or "").strip(), (r.get("k-아파트명") or "").strip()))
            n += 1
        return cls(by_gu, len(rows), n)

    @classmethod
    def load(cls, normalization_variants=None):
        """서울 전체 CSV를 읽어 운영용 구별 도로명 사전을 생성."""
        if normalization_variants is None:
            _validate_active_normalization_snapshot()
            normalization_variants = ACTIVE_NORMALIZATION_VARIANTS
        return cls.from_rows(cls.read_rows(), normalization_variants)

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


def _match_state(index: KaptIndex, gu_names: set, rows: list) -> tuple[str, int]:
    """단지의 모든 구 파티션 후보를 합쳐 매칭 상태와 거래 건수를 반환."""
    head = min(rows, key=sort_key)
    key = road_key(head.get("road_nm"), head.get("road_nm_bonbun"),
                   head.get("road_nm_bubun"))
    candidates = set()
    if key:
        for gu_name in gu_names:
            candidates.update(index.lookup(gu_name, key) or ())
    if len(candidates) == 1:
        state = "matched"
    elif len(candidates) > 1:
        state = "ambiguous"
    else:
        state = "unmatched"
    return state, sum(r["trade_count"] for r in rows)


def _contribution(baseline: dict, compared: dict) -> dict:
    """한 변형의 상태를 원문 기준선과 직접 비교한 지표를 반환."""
    newly_matched = {
        apt for apt, (state, _) in baseline.items()
        if state != "matched" and compared[apt][0] == "matched"
    }
    lost_matched = {
        apt for apt, (state, _) in baseline.items()
        if state == "matched" and compared[apt][0] != "matched"
    }
    newly_ambiguous = {
        apt for apt, (state, _) in baseline.items()
        if state == "matched" and compared[apt][0] == "ambiguous"
    }
    return {
        "new": len(newly_matched),
        "lost": len(lost_matched),
        "ambiguous": len(newly_ambiguous),
        "deals": sum(compared[apt][1] for apt in newly_matched),
        "matched": sum(1 for state, _ in compared.values() if state == "matched"),
        "lost_apts": sorted(lost_matched),
    }


def _load_local_coverage():
    """레포의 coverage.py를 동명 외부 패키지와 구분해 로드."""
    path = HERE / "coverage.py"
    spec = importlib.util.spec_from_file_location("_stance_coverage", path)
    if spec is None or spec.loader is None:
        sys.exit(f"[중단] {path.name} 을 로드하지 못했습니다.")
    module = importlib.util.module_from_spec(spec)

    previous = sys.modules.get("matching")
    sys.modules["matching"] = sys.modules[__name__]
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("matching", None)
        else:
            sys.modules["matching"] = previous
    return module


def report_normalization_contribution():
    """36개월 거래에서 네 변형의 기준선 대비 단독 기여를 출력."""
    coverage = _load_local_coverage()
    start, end = coverage.month_range(coverage.DEFAULT_AS_OF, coverage.MONTHS_LONG)
    url, api_key = coverage.sb_config()
    districts = coverage.load_districts()
    rows_by_gu = {}
    per_apt = collections.defaultdict(list)
    apt_gu_names = collections.defaultdict(set)
    for sgg_cd, gu_name in districts:
        rows = coverage.fetch_district(sgg_cd, start, end, url, api_key)
        rows_by_gu[gu_name] = rows
        for row in rows:
            per_apt[row["apt_seq"]].append(row)
            apt_gu_names[row["apt_seq"]].add(gu_name)

    source_rows = KaptIndex.read_rows()
    variant_sets = {0: (), **{n: (n,) for n in NORMALIZATION_NAMES}}
    variant_sets["final"] = ACTIVE_NORMALIZATION_VARIANTS
    indexes = {name: KaptIndex.from_rows(source_rows, variants)
               for name, variants in variant_sets.items()}

    states = {}
    for name, index in indexes.items():
        states[name] = {
            apt: _match_state(index, apt_gu_names[apt], apt_rows)
            for apt, apt_rows in per_apt.items()
        }
    baseline = states[0]
    coverage_matched = 0
    for gu_name, rows in rows_by_gu.items():
        grouped = collections.defaultdict(list)
        for row in rows:
            grouped[row["apt_seq"]].append(row)
        coverage_matched += sum(
            1 for apt_rows in grouped.values()
            if match_complex(indexes[0], gu_name, apt_rows)[1]
        )

    baseline_matched = sum(1 for state, _ in baseline.values() if state == "matched")
    print("변형별 단독 기여 (단지 수, 사전 쪽 적용)")
    print(f"  baseline (모호 제외)                  {baseline_matched}")
    print(f"  baseline (coverage 정의, 모호 포함)  {coverage_matched}")
    print("                              신규매칭  소실매칭  신규모호  신규매칭분 거래건수  매칭")
    results = {}
    for number, name in NORMALIZATION_NAMES.items():
        result = _contribution(baseline, states[number])
        results[number] = result
        print(f"  변형 {number} {name:<10} {result['new']:>8} {result['lost']:>9}"
              f" {result['ambiguous']:>9} {result['deals']:>19} {result['matched']:>7}")

    final_result = _contribution(baseline, states["final"])
    active = "+".join(str(n) for n in ACTIVE_NORMALIZATION_VARIANTS)
    print(f"  최종 반영 조합 [{active}]       {final_result['new']:>8}"
          f" {final_result['lost']:>9} {final_result['ambiguous']:>9}"
          f" {final_result['deals']:>19} {final_result['matched']:>7}")

    labeled_results = [(f"변형 {n}", results[n]) for n in results]
    labeled_results.append(("최종 반영 조합", final_result))
    for label, result in labeled_results:
        if result["lost_apts"]:
            print(f"  {label} 소실 단지: {', '.join(result['lost_apts'])}")

    eligible = tuple(number for number, result in results.items()
                     if result["new"] > 0 and result["lost"] == 0)
    if eligible != ACTIVE_NORMALIZATION_VARIANTS or final_result["lost"]:
        sys.exit("[중단] 기여 측정 결과와 운영 정규화 변형이 다릅니다.")


if __name__ == "__main__":
    report_normalization_contribution()
