-- ============================================================================
-- 실거래 원장 스키마
--   trade            매매 실거래 현재 상태. 같은 거래 = 1행
--   rent             전월세 실거래 현재 상태. 같은 계약 = 1행
--   deal_change_log  값이 실제로 바뀐 행만 기록
--   collect_run      (수집종류, 구, 계약월) 단위 수집 시도 로그
--
-- 코드 컬럼에 char(n) 금지
--   - char(n) 은 공백 패딩 타입
--   - 원문 '' -> 공백 1칸 저장. NULL 판정 붕괴
--   - text + CHECK 로 길이와 문자종류 동시 강제
-- ============================================================================


-- -- 1. 매매 실거래 ----------------------------------------------------------
create table if not exists public.trade (
  id                  bigint generated always as identity primary key,

  -- 자연키
  apt_seq             text          not null,
  deal_date           date          not null,
  exclu_use_ar        numeric(9,4)  not null,
  floor               smallint      not null,
  deal_amount         integer       not null,

  -- 자연키 1개가 나타내는 거래 건수 추정치. 원본에 거래 ID 없음
  --   - 정상 행 n건     -> n. 해제 행은 거래가 아닌 상태 통지
  --   - 정상 0 + 해제만 -> 1. 해제할 거래 없으면 해제 통지도 없음
  -- 순번 부여 배제
  --   - 응답 순서 의존 -> 재수집마다 값 변동 -> 멱등성 붕괴
  trade_count         smallint      not null default 1,

  -- 정상 2건 이상 + 해제 통지 공존. 해제 대상 식별 불가 표시
  ambiguous_cancel    boolean       not null default false,

  -- 계약년월. API 조회 단위(DEAL_YMD)와 동일 축
  -- to_char 금지
  --   - to_char 는 STABLE
  --   - 생성 컬럼 표현식은 IMMUTABLE 필수 -> 42P17 로 DDL 실패
  --   - date_part + lpad 는 IMMUTABLE
  deal_ym             text generated always as (
                        lpad(date_part('year',  deal_date)::int::text, 4, '0') ||
                        lpad(date_part('month', deal_date)::int::text, 2, '0')
                      ) stored,

  -- 응답에 재출현하지 않은 행 표시
  --   - 자연키에 deal_amount 포함 -> 금액 정정은 새 행. 옛 행은 응답에서 소멸
  --   - 삭제 대신 표시. 이 UPDATE 가 트리거를 타서 정정 흔적 보존
  --   - 시세 / 판정 계산은 항상 where is_current
  is_current          boolean       not null default true,

  sgg_cd              text          not null,
  umd_cd              text,
  umd_nm              text,
  apt_nm              text          not null,
  apt_dong            text,
  jibun               text,
  road_nm             text,
  road_nm_bonbun      text,
  road_nm_bubun       text,
  build_year          smallint,

  -- 원문 보존. 로더의 boolean 파생 금지
  --   - 실측 고유값 2개 ('' 10,039 / 'O' 702)
  --   - 제3의 값 유입 시 CHECK 가 거부
  --   - boolean 파생 시 새 값이 조용히 '정상' 으로 접힘
  dealing_gbn         text,          -- '중개거래' | '직거래'
  cdeal_type          text,          -- 'O' = 해제. NULL = 정상

  -- 원문 2자리 연도 'YY.MM.DD'. 로더가 '20' 접두 조립
  --   - 실측 형식 예외 0건. rgst_date 9,290 / cdeal_day 702 전부 '99.99.99'
  --   - 등기일 < 거래일 불가 + 실거래 공개 2006년 이후 -> 19xx 해석 불가
  cdeal_day           date,
  rgst_date           date,

  sler_gbn            text,
  buyer_gbn           text,
  estate_agent_sgg_nm text,
  land_leasehold_gbn  text,

  -- 변경 감지 제외 대상 2칸
  first_seen_at       timestamptz   not null default now(),
  last_seen_at        timestamptz   not null default now(),

  constraint trade_cdeal_type_chk check (cdeal_type is null or cdeal_type = 'O'),
  constraint trade_sgg_cd_chk     check (sgg_cd ~ '^[0-9]{5}$'),
  constraint trade_umd_cd_chk     check (umd_cd is null or umd_cd ~ '^[0-9]{5}$')
);

comment on table  public.trade                  is '아파트 매매 실거래. 국토부 상세 API. 같은 거래 = 1행';
comment on column public.trade.deal_amount      is '거래금액. 단위 만원. API 원문 "110,000" -> 110000';
comment on column public.trade.cdeal_type       is '해제 여부. O = 해제. 통계 계산에서 제외';
comment on column public.trade.dealing_gbn      is '거래유형. 직거래는 시세 판정에서 제외';
comment on column public.trade.rgst_date        is '등기일자. API 원문은 2자리 연도 "24.02.22"';
comment on column public.trade.last_seen_at     is '최근 수집에서 이 행이 응답에 재출현한 시각';
comment on column public.trade.trade_count      is '거래 건수 통계는 count(*) 아닌 sum(trade_count)';
comment on column public.trade.ambiguous_cancel is '해제 대상 거래 식별 불가. 해제 우선 판정';

-- 자연키 unique 인덱스. upsert(ON CONFLICT) 의 동일 행 판정 근거
-- 강남구 36개월 전수 유일성 확인
--
-- 5칸 선정 기준: 거래 존속 기간 내 불변인 컬럼
--   - rgst_date              계약 수개월 뒤 채워짐
--   - cdeal_type / cdeal_day 해제 시점에 생성
--   - apt_dong               해제 행에서 공란
-- 위 셋 중 하나라도 키에 포함 시
--   - 같은 거래의 정상 행과 해제 행이 다른 키 -> 병합 불가
--   - 등기 발생일에 같은 거래가 신규 행으로 유입
create unique index if not exists trade_natural_key
  on public.trade (apt_seq, deal_date, exclu_use_ar, floor, deal_amount);

-- 조회용 인덱스 2개만. 검색 화면 쿼리 확정 전 증설 금지
create index if not exists trade_sgg_ym_idx   on public.trade (sgg_cd, deal_ym);
create index if not exists trade_apt_date_idx on public.trade (apt_seq, deal_date desc);


-- -- 2. 전월세 실거래 --------------------------------------------------------
-- 매매와 테이블 분리 근거: 응답 필드 집합 상이
--   - 해제(cdealType), 직거래(dealingGbn) 필드 자체 부재
--   - 도로명이 번지까지 단일 문자열
--   - 통합 시 절반이 NULL 인 컬럼 발생 + 파싱 규칙 상호 유입
create table if not exists public.rent (
  id                bigint generated always as identity primary key,

  apt_seq           text          not null,
  deal_date         date          not null,
  exclu_use_ar      numeric(9,4)  not null,
  floor             smallint      not null,
  deposit           integer       not null,
  monthly_rent      integer       not null,

  -- 매매와 동일 규칙. 전월세에 해제 개념 없음
  trade_count       smallint      not null default 1,

  -- to_char 금지. trade 주석 참조
  deal_ym           text generated always as (
                      lpad(date_part('year',  deal_date)::int::text, 4, '0') ||
                      lpad(date_part('month', deal_date)::int::text, 2, '0')
                    ) stored,

  is_current        boolean       not null default true,

  sgg_cd            text          not null,
  umd_nm            text,                     -- 전월세에 umdCd(법정동 코드) 부재
  apt_nm            text          not null,
  jibun             text,
  road_nm_full      text,                     -- '삼성로 212'. 번지 포함
  build_year        smallint,

  contract_type     text,                     -- 공백률 달마다 상이. 2026-07 2.1% / 2024-07 6.6%
  contract_term     text,
  pre_deposit       integer,
  pre_monthly_rent  integer,
  use_rr_right      text,                     -- 갱신요구권 사용 여부

  first_seen_at     timestamptz   not null default now(),
  last_seen_at      timestamptz   not null default now(),

  constraint rent_sgg_cd_chk check (sgg_cd ~ '^[0-9]{5}$')
);

comment on table  public.rent              is '아파트 전월세 실거래. 국토부 상세 API';
comment on column public.rent.deposit      is '보증금. 단위 만원';
comment on column public.rent.monthly_rent is '월세. 단위 만원. 0 = 순수 전세. 전세가율 모수는 이 조건만';
comment on column public.rent.pre_deposit  is '종전 보증금. 신규 계약에는 부재가 정상. 결측 아님';

create unique index if not exists rent_natural_key
  on public.rent (apt_seq, deal_date, exclu_use_ar, floor, deposit, monthly_rent);

create index if not exists rent_sgg_ym_idx   on public.rent (sgg_cd, deal_ym);
create index if not exists rent_apt_date_idx on public.rent (apt_seq, deal_date desc);


-- -- 3. 수집 실행 로그 -------------------------------------------------------
-- GitHub Actions 러너는 매 실행마다 신규 생성. 진행 상태의 파일 기록 불가
-- deal_change_log 보다 먼저 생성. 변경 이력이 이 테이블의 id 참조
create table if not exists public.collect_run (
  id             bigint      generated always as identity primary key,
  kind           text        not null check (kind in ('trade', 'rent')),
  lawd_cd        text        not null check (lawd_cd ~ '^[0-9]{5}$'),
  deal_ym        text        not null check (deal_ym ~ '^[0-9]{6}$'),

  -- running 은 기록. 잠금 아님
  --   - 완료 후에만 INSERT 시 죽은 시도의 흔적 소실
  --   - 이 칸으로 동시 실행 차단 금지
  --       기록은 크래시 후에도 존속 필요
  --       잠금은 크래시 즉시 해제 필요
  --       겸용 시 죽은 행이 재시도를 영구 차단
  --   - 차단은 아래 advisory lock 담당
  status         text        not null check (status in ('running', 'ok', 'error')),

  total_count    integer,                  -- API 가 알려준 전체 건수
  fetched_rows   integer,                  -- 실제 수신 후 적재한 행 수
  page_count     smallint,

  -- 같은 슬라이스 2회차 기대값: inserted 0 / updated 0 / unchanged N
  -- 부재 시 멱등성의 로그 확인 불가
  inserted_count  integer,
  updated_count   integer,
  unchanged_count integer,

  error_code     text,
  error_msg      text,
  started_at     timestamptz not null default now(),
  finished_at    timestamptz
);

create index if not exists collect_run_resume_idx
  on public.collect_run (kind, lawd_cd, deal_ym, started_at desc);

-- 동시 실행 차단: 테이블 제약 아닌 advisory lock
--   - 세션 종료 시 Postgres 가 자동 해제
--   - 타임아웃 상수 선정 불필요
--
-- 로더가 슬라이스 적재 트랜잭션 시작 시 호출
--   select pg_try_advisory_xact_lock(
--            lawd_cd::bigint * 10000000
--          + deal_ym::bigint * 10
--          + case when kind = 'rent' then 1 else 0 end);
--   false -> 같은 슬라이스를 다른 프로세스가 점유. 건너뜀
--
-- hashtext 금지
--   - int4. 슬라이스 1,800개에서 충돌 확률 0 아님
--   - 위 조립식은 deal_ym*10 최대 9,999,120 < 10^7. 자릿수 비중첩
--   - trade / 11680 / 202607 -> 116,802,026,070


-- -- 4. 변경 이력 ------------------------------------------------------------
create table if not exists public.deal_change_log (
  id             bigint      generated always as identity primary key,
  source_table   text        not null,

  -- 단일 (source_table, source_id) 는 FK 불가. 잘못된 id 를 DB 가 미차단
  -- nullable FK 2개 + CHECK 로 '정확히 한쪽만 채워짐' 강제
  --
  -- on delete restrict. cascade 금지
  --   - cascade 시 원본 삭제가 정정 / 해제 이력까지 동반 삭제
  --   - 원본 API 는 과거 상태 미제공. 이력은 복구 불가능
  --   - set null 은 아래 CHECK 3개와 양립 불가
  trade_id       bigint      references public.trade       (id) on delete restrict,
  rent_id        bigint      references public.rent        (id) on delete restrict,

  -- changed_at 과 started_at 범위 추정 시 회차 경계에서 오차
  -- 수집 배치 밖의 수기 UPDATE 는 NULL
  run_id         bigint      references public.collect_run (id) on delete set null,

  changed_fields text[]      not null,
  old_row        jsonb       not null,   -- 바뀌기 전 모습. 현재 모습은 원본 테이블
  changed_at     timestamptz not null default now(),

  constraint deal_change_log_source_chk       check (source_table in ('trade', 'rent')),
  constraint deal_change_log_one_target_chk   check ((trade_id is not null) <> (rent_id is not null)),
  constraint deal_change_log_source_match_chk check ((source_table = 'trade') = (trade_id is not null))
);

comment on table public.deal_change_log is
  '실거래 행의 값이 실제로 바뀐 순간만 기록. 정정, 해제 이력';

create index if not exists deal_change_log_trade_idx
  on public.deal_change_log (trade_id, changed_at desc) where trade_id is not null;

create index if not exists deal_change_log_rent_idx
  on public.deal_change_log (rent_id, changed_at desc) where rent_id is not null;

create index if not exists deal_change_log_run_idx
  on public.deal_change_log (run_id);


-- -- 5. 변경 이력 트리거 -----------------------------------------------------
-- 애플리케이션 아닌 트리거 채택 근거
--   - SQL Editor 수기 UPDATE 에도 이력 기록
--   - 기록 여부를 호출자의 성실성에 미위임
--
-- search_path 미고정 시 호출자의 search_path 를 따름
create or replace function public.log_deal_change()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
declare
  -- 수집 메타 2칸은 매 실행 변경. 비교 대상 제외
  old_j  jsonb := to_jsonb(old) - 'first_seen_at' - 'last_seen_at';
  new_j  jsonb := to_jsonb(new) - 'first_seen_at' - 'last_seen_at';
  fields text[];
begin
  -- order by k 필수
  --   - 부재 시 배열 순서가 실행마다 상이
  --   - 같은 성격의 이력이 다른 모양으로 축적
  select coalesce(array_agg(k order by k), '{}'::text[])
    into fields
    from jsonb_object_keys(new_j) as k
   where new_j -> k is distinct from old_j -> k;

  -- 이 조기 반환을 트리거 WHEN 절로 이동 금지
  --   - upsert SET 절의 last_seen_at = now() 로 WHEN 조건이 항상 참
  --   - 실제 필터는 여기 한 곳
  if array_length(fields, 1) is null then
    return new;
  end if;

  -- run_id 는 로더가 트랜잭션 시작 시 주입
  --   select set_config('stance.run_id', <collect_run.id>::text, true);
  --   세 번째 인자 true = 트랜잭션 로컬. 다음 트랜잭션으로 미전파
  --
  -- tg_table_name 사용. 트리거 인자 사용 시 오타가 잘못된 source 로 조용히 축적
  insert into public.deal_change_log
    (source_table, trade_id, rent_id, run_id, changed_fields, old_row)
  values (
    tg_table_name,
    case when tg_table_name = 'trade' then old.id end,
    case when tg_table_name = 'rent'  then old.id end,
    nullif(current_setting('stance.run_id', true), '')::bigint,
    fields,
    old_j
  );

  return new;
end;
$$;

drop trigger if exists trg_trade_change on public.trade;
create trigger trg_trade_change
  after update on public.trade
  for each row
  execute function public.log_deal_change();

drop trigger if exists trg_rent_change on public.rent;
create trigger trg_rent_change
  after update on public.rent
  for each row
  execute function public.log_deal_change();


-- -- 6. 권한 -----------------------------------------------------------------
-- GRANT 와 RLS 는 별개의 층. GRANT 에서 차단 시 RLS 는 미평가 (42501)
alter table public.trade           enable row level security;
alter table public.rent            enable row level security;
alter table public.deal_change_log enable row level security;
alter table public.collect_run     enable row level security;

-- 공개 데이터: 전체 읽기, 쓰기 차단
-- anon 포함 근거
--   - 익명 세션 발급 전 데이터 요청 구간 존재
--   - 그 시점의 역할은 authenticated 아닌 anon
drop policy if exists trade_public_read on public.trade;
create policy trade_public_read on public.trade
  for select to anon, authenticated using (true);

drop policy if exists rent_public_read on public.rent;
create policy rent_public_read on public.rent
  for select to anon, authenticated using (true);

-- Supabase 는 public 스키마 신규 테이블에 권한 자동 부여. 명시적 회수 필요
revoke all on table public.trade, public.rent,
                    public.deal_change_log, public.collect_run
  from anon, authenticated;

grant select on table public.trade to anon, authenticated;
grant select on table public.rent  to anon, authenticated;

-- 수집 스크립트 = service_role 키. RLS 우회하나 GRANT 는 별도 필요
--
-- all 아닌 3개만 부여
--   - 이 설계는 upsert 와 is_current 마킹만 사용. 로더에 DELETE 필요 지점 없음
--   - all 은 DELETE / TRUNCATE 포함
--   - 관리 목적 삭제는 SQL Editor(postgres 역할) 담당
--
-- revoke 선행 필수. grant 는 추가만 수행
--   - 이전 실행의 all 이 grant 재실행만으로는 미제거
revoke all on table public.trade, public.rent,
                    public.deal_change_log, public.collect_run
  from service_role;

grant select, insert, update on table public.trade, public.rent,
                                      public.deal_change_log, public.collect_run
  to service_role;
