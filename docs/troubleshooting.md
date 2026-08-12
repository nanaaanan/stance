# 트러블슈팅

이 프로젝트에서 실제로 예상치 못하게 막혔던 지점과 문제해결방법 기록

- 시점 : 코드 작성 후/선택 후
- 질문 : 왜 안됐고, 어떻게 고쳤나
- 핵심 : 원인 추적

---

## `permission denied for table _smoke` (42501, HTTP 403)

- **증상**: RLS 정책을 만들었는데도 insert/select 가 403
- **원인**: RLS 와 GRANT 는 별개의 층이다. RLS 는 "어느 행"을, GRANT 는 "이 테이블을 건드릴 수 있는가"를 정한다. GRANT 에서 막히면 RLS 는 평가되지도 않는다
- **해결**: `grant select, insert on table public.X to authenticated;`
- **재발 방지**: 새 테이블을 만들 때 GRANT 를 같은 SQL 블록에 넣는다. 공개 데이터는 anon+authenticated SELECT, 사용자 데이터는 authenticated 전체

## `relation "_smoke" already exists` (42P07)

- **증상**: 기존 SQL 아래에 GRANT를 붙여 재실행했더니 맨 위 create table에서 멈춤
- **원인**: SQL Editor 는 파일이 아니라 실행창이다. 편집창 전체를 위에서부터 다시 실행한다. 에러가 나면 그 뒤 문장은 실행되지 않는다
- **해결**: New query 로 빈 창을 열고 필요한 문장만 실행
- **재발 방지**: SQL은 중복되어도 무관하게 쓴다.(멱등성) 테이블 `create ... if not exists`, 정책 `drop policy if exists` 후 재생성, GRANT는 재실행 안전

## 로컬은 되는데 배포에서 세션이 안 잡힘

- **증상**: localhost 에서는 UUID 가 뜨는데 Vercel URL 에서는 "연결 중..." 에서 멈춤
- **원인**: Vercel 환경변수 미등록, 또는 등록했으나 재배포하지 않음
- **해결**: Settings &rarr; Environment Variables 에 Production/Preview/Development 3곳 모두 체크 &rarr; Deployments 에서 Redeploy
- **재발 방지**: 환경변수는 빌드 시점에 코드에 박힌다. 기존 배포에 소급 적용되지 않는다. 추가, 수정 후에는 항상 Redeploy

## `.env` 를 고쳤는데 반영되지 않음

- **증상**: 값을 바꿔도 이전 값으로 동작
- **원인**: Next.js 는 서버 시작 시점에 `.env` 를 읽는다
- **해결**: ⌃C 로 종료 후 `npm run dev` 재시작
- **재발 방지**: `.env` 를 만졌으면 무조건 재시작

## 전월세 `preDeposit` 이 전부 비어 보임

- **증상**: `numOfRows=10` 으로 확인했더니 종전 보증금이 100% 공백 &rarr; "이 필드는 못 쓴다"고 잘못 판단함.
- **원인**: 응답이 정렬돼 있고 앞부분에 미분류 건이 몰려 있다. 강남·동작 모두 앞 10건의 `contractType` 공백률이 정확히 100%
- **해결**: `numOfRows=1000` 으로 전수 확인 &rarr; 갱신 계약의 99.3% 가 채워져 있었다
- **재발 방지**: 표본을 볼 때 표본이 어떻게 정렬되는지를 먼저 의심한다. 첫 페이지는 표본이 아니다
