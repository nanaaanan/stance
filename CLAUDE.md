# CLAUDE.md

서울 아파트 실거래 기반 프롭테크 웹앱. 1인 풀스택 개발.

## 서비스 개요

집을 찾는 순서를 뒤집는다: **관점 진단(거주 vs 자산) → 예산 확정 → 임장 단지 탐색**

## 스택

- Next.js (App Router) + TypeScript
- Supabase (Postgres + Auth + PostGIS) — 인증은 **익명 세션 기본, 구글은 승격**
- Tanstack Query + Zustand + React Hook Form
- Tailwind + shadcn/ui
- Vercel (main 머지 = 자동 배포)
- 배치: 로컬 스크립트 + GitHub Actions cron
- 테스트: Vitest (예산 계산 / 적합도 판정 / 정제 규칙 3영역만)

## 명령어

```bash
npm run dev        # 개발 서버
npm run build      # 프로덕션 빌드 (푸시 전 반드시 통과)
npm run lint       # ESLint
npm run test       # Vitest (도입 후)
```

## 디렉토리

```
src/app/           라우트 (/, /onboarding, /onboarding/result, /search, /complex/[id], /my, /my/visit/[id], /my/compare)
src/components/    UI 컴포넌트
src/lib/           supabase 클라이언트, 계산 로직(순수 함수)
config/            regulation.json / market.json / assumptions.json
scripts/           수집·정제 배치 (표준 라이브러리 우선)
docs/api/          REST Client(.http)
docs/decisions/    ADR(선택전,판단)
docs/troubleshooting/  벌어진일(선택후,문제해결)
docs/data/         schema.md, 매핑표
docs/journal/      일일 의사결정 로그
data/              matching-failures.csv 등 산출물
```

## 아키텍처 규칙

- **계산은 SQL이 아니라 앱 레이어에서.** DSR·한도는 TS 순수 함수로 계산 → 정수 하나를 쿼리 인자로 전달 → DB는 `WHERE price <= ?` 단순 비교만 (인덱스 100% 활용)
- **정책 값은 코드가 아니라 `config/*.json`.** 시행일 필드 유지
- **관점(거주/자산) 분기는 컴포넌트 분기가 아니라 설정 데이터.** `if (viewpoint === ...)`를 화면마다 쓰지 않는다
- **온보딩 단계 상태는 URL 쿼리가 소유.** Zustand는 세션 내 임시 UI 상태 전용
- **모바일 필터는 시트 닫힘(onClose)에 1회만 요청.** PC는 onChange

## 금지 (하드 룰)

- 서비스키·시크릿 하드코딩 금지. `.env`는 커밋하지 않는다 (`.env.example`만)
- data.go.kr 서비스키는 **디코딩 키**를 쓴다 (인코딩 키는 이중 인코딩되어 에러 30)
- 매칭 실패를 추측으로 메우지 않는다 → `NULL` + "정보 미확인" 표시
- 화면에 검증되지 않은 통계·근거 없는 사실 주장을 넣지 않는다
- "배정 초등학교"라고 쓰지 않는다 → "가장 가까운 초등학교"
- 화면에 "폰지","투기" 단어를 직접 노출하지 않는다
- 판정 라벨을 새로 만들지 않는다. 임계값이 필요하면 **사용자 예산**을 기준선으로 쓴다
- 크롤링 금지 (호가,매물 수집 전면 폐기)
- 기능 스펙을 데이터 확인 전에 미리 쓰지 않는다

## 데이터 핵심 사실

- 매매는 **상세 API** `getRTMSDataSvcAptTradeDev` (기본 API에는 `aptSeq`가 없다)
- `DEAL_YMD`는 6자리 단일 월. 1콜 = 1개월. `numOfRows=1000`, 30tps → 0.2초 스로틀
- 매매↔전월세 조인 = `aptSeq`. 실거래↔K-apt 조인 = **도로명 단독**
- **정제 필수:** 해제(`cdealType='O'`) 중복 제거 → 직거래(`dealingGbn='직거래'`) 플래그 → 파생값 계산 시 직거래 제외
- (단지×면적) 조합의 **34%는 최근 12개월 거래가 없다.** 판정 배지를 붙이지 않는다

## ADR

결정한 즉시 `docs/decisions/`에 주제와 템플릿만 생성해준다.
템플릿: `## 맥락  ## 검토한 선택지  ## 결정  ## 포기한 것`

## 커밋 컨벤션

`type(scope): (첫 줄)무엇을 했는지 한 줄 요약 + (본문)왜 그렇게 했는지/어떤 문제를 풀었는지`

- type: feat / fix / chore / docs / refactor / test / style
- scope: auth / search / complex / onboarding / budget / visit / data / infra
- **커밋 메시지 주 언어는 한글, 템플릿만 생성해주고 멈출 것**
