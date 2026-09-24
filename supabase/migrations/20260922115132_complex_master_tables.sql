-- ============================================================================
-- 단지 마스터 스키마
--   complex       화면이 읽는 단지 한 줄
--   complex_area  단지별 실거래 면적과 K-apt 면적 그룹의 대응
--
-- 기존 테이블을 바꾸거나 지우지 않는 확장 단계만 수행
-- ============================================================================


-- -- 1. 단지 마스터 ---------------------------------------------------------
create table if not exists public.complex (
  apt_seq       text        not null,
  sgg_cd        text        not null,
  apt_nm        text        not null,
  road_key      text,

  -- unique 금지. 41개 코드가 2개 이상의 apt_seq 에 연결됨
  kapt_code     text,
  kapt_key      text,
  match_source  text,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),

  constraint complex_pkey primary key (apt_seq),
  constraint complex_sgg_cd_chk check (sgg_cd ~ '^[0-9]{5}$'),
  constraint complex_road_key_not_empty_chk check (
    road_key is null or length(road_key) > 0
  ),
  constraint complex_kapt_code_not_empty_chk check (
    kapt_code is null or length(kapt_code) > 0
  ),
  constraint complex_kapt_key_not_empty_chk check (
    kapt_key is null or length(kapt_key) > 0
  ),
  constraint complex_match_source_not_empty_chk check (
    match_source is null or length(match_source) > 0
  ),
  constraint complex_kapt_match_fields_chk check (
    (kapt_code is null and kapt_key is null and match_source is null)
    or
    (kapt_code is not null and kapt_key is not null and match_source is not null)
  )
);

comment on table public.complex is
  '화면이 읽는 단지 마스터. 거래 행의 단지 속성은 수집 시점의 원본 스냅샷';
comment on column public.complex.sgg_cd is
  '이 단지의 거래가 가장 많이 조회된 구. 단지가 속한 구라는 뜻이 아님';
comment on column public.complex.road_key is
  'K-apt 매칭에 쓴 도로명 조인 키. 표시용 주소가 아님';


-- -- 2. 단지별 면적 ---------------------------------------------------------
create table if not exists public.complex_area (
  apt_seq       text         not null,
  exclu_use_ar  numeric(9,4) not null,
  area_status   text         not null,
  kapt_area     numeric(9,4),
  households    integer,
  group_id      text,
  group_label   integer,
  group_areas   text,
  first_seen_at timestamptz  not null default now(),
  last_seen_at  timestamptz  not null default now(),

  -- kapt_area 는 PK 로 쓸 수 없음. (apt_seq, kapt_area) 중복 7건 확인
  constraint complex_area_pkey primary key (apt_seq, exclu_use_ar),

  -- cascade 금지. 단지 한 줄 삭제가 면적 전체를 함께 지우면 안 됨
  constraint complex_area_apt_seq_fkey foreign key (apt_seq)
    references public.complex (apt_seq) on delete restrict,

  -- scripts/area.py 의 네 문자열과 글자까지 같아야 함
  constraint complex_area_status_chk check (
    area_status in (
      '면적 매칭',
      '면적 ±0.5㎡ 밖',
      '면적 동점(모호)',
      'K-apt 면적정보 없음'
    )
  ),
  constraint complex_area_kapt_area_match_chk check (
    (area_status = '면적 매칭') = (kapt_area is not null)
  ),
  constraint complex_area_households_match_chk check (
    (kapt_area is null) = (households is null)
  ),
  constraint complex_area_group_status_chk check (
    (area_status in ('면적 ±0.5㎡ 밖', '면적 동점(모호)')) = (group_id is null)
  ),
  constraint complex_area_group_label_match_chk check (
    (group_id is null) = (group_label is null)
  ),
  constraint complex_area_group_areas_match_chk check (
    (group_id is null) = (group_areas is null)
  ),
  constraint complex_area_group_id_not_empty_chk check (
    group_id is null or length(group_id) > 0
  ),
  constraint complex_area_group_areas_not_empty_chk check (
    group_areas is null or length(group_areas) > 0
  ),
  constraint complex_area_exclu_use_ar_positive_chk check (exclu_use_ar > 0),
  constraint complex_area_kapt_area_positive_chk check (
    kapt_area is null or kapt_area > 0
  ),
  constraint complex_area_households_positive_chk check (
    households is null or households > 0
  ),
  constraint complex_area_group_label_positive_chk check (
    group_label is null or group_label > 0
  )
);

comment on table public.complex_area is
  '단지별 실거래 면적과 K-apt 면적 그룹의 대응';
comment on column public.complex_area.group_label is
  '화면 표시용 정수 라벨. 계산에 사용하지 않음';
comment on column public.complex_area.group_areas is
  '그룹 구성 면적을 오름차순 | 문자열로 저장';


-- -- 3. 권한 ----------------------------------------------------------------
-- GRANT 와 RLS 는 별개의 층. GRANT 에서 차단되면 RLS 는 평가되지 않음
alter table public.complex      enable row level security;
alter table public.complex_area enable row level security;

drop policy if exists complex_public_read on public.complex;
create policy complex_public_read on public.complex
  for select to anon, authenticated using (true);

drop policy if exists complex_area_public_read on public.complex_area;
create policy complex_area_public_read on public.complex_area
  for select to anon, authenticated using (true);

-- 공개 데이터: 브라우저는 읽기만 허용
revoke all on table public.complex, public.complex_area
  from anon, authenticated;
grant select on table public.complex, public.complex_area
  to anon, authenticated;

-- 적재 역할: 삭제와 truncate 없이 읽기, 넣기, 고치기만 허용
revoke all on table public.complex, public.complex_area
  from service_role;
grant select, insert, update on table public.complex, public.complex_area
  to service_role;
