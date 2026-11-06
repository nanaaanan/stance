begin;

create table public.reb_complex (
  reb_complex_id text not null primary key,
  households integer,
  approval_date date,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  constraint reb_complex_id_chk check (reb_complex_id ~ '^[0-9]{14}$'),
  constraint reb_complex_households_chk check (households is null or households > 0)
);

comment on table public.reb_complex is '단지 명부의 단지 한 줄. 여러 거래 단지가 같은 명부 번호를 가리킬 수 있음';
comment on column public.reb_complex.households is '명부 한 단지의 총 세대수. 이 번호를 가리키는 단지가 여럿일 수 있다';
comment on column public.reb_complex.approval_date is '명부 사용승인일';

alter table public.reb_complex enable row level security;
create policy reb_complex_public_read on public.reb_complex
  for select to anon, authenticated using (true);
revoke all on table public.reb_complex from anon, authenticated, service_role;
grant select on table public.reb_complex to anon, authenticated;
grant select, insert, update on table public.reb_complex to service_role;

alter table public.complex
  add column if not exists reb_complex_id text,
  add column if not exists reb_fail_reason text,
  add column if not exists kapt_fail_reason text;

alter table public.complex
  add constraint complex_reb_complex_id_fkey foreign key (reb_complex_id)
    references public.reb_complex (reb_complex_id) on delete restrict,
  add constraint complex_reb_complex_id_not_empty_chk check (
    reb_complex_id is null or length(reb_complex_id) > 0
  ),
  add constraint complex_reb_fail_reason_not_empty_chk check (
    reb_fail_reason is null or length(reb_fail_reason) > 0
  ),
  add constraint complex_kapt_fail_reason_not_empty_chk check (
    kapt_fail_reason is null or length(kapt_fail_reason) > 0
  ),
  add constraint complex_reb_match_reason_chk check (
    reb_complex_id is null or reb_fail_reason is null
  ),
  add constraint complex_kapt_match_reason_chk check (
    kapt_code is null or kapt_fail_reason is null
  );

create index if not exists complex_reb_complex_id_idx on public.complex (reb_complex_id);
comment on column public.complex.reb_complex_id is '단지 명부 번호. 여러 apt_seq가 같은 번호를 가리킬 수 있음';
comment on column public.complex.reb_fail_reason is '단지 명부 층 매칭 실패 사유';
comment on column public.complex.kapt_fail_reason is 'K-apt 관리정보 층 매칭 실패 사유';

alter table public.complex_area
  drop constraint complex_area_status_chk,
  drop constraint complex_area_group_status_chk;
alter table public.complex_area
  add constraint complex_area_status_chk check (
    area_status in ('면적 매칭', '면적 ±0.5㎡ 밖', '면적 동점(모호)', 'K-apt 면적정보 없음', '면적 명부 없음')
  ),
  add constraint complex_area_group_status_chk check (
    (area_status in ('면적 ±0.5㎡ 밖', '면적 동점(모호)', '면적 명부 없음')) = (group_id is null)
  );
comment on table public.complex_area is '단지별 매매·전월세 실거래 면적과 공시가격 면적 명부 그룹의 대응';
comment on column public.complex_area.kapt_area is '공시가격 면적 명부 기준의 매칭 면적. 기존 칸 이름 유지';
comment on column public.complex_area.households is '공시가격 면적 명부 기준의 해당 면적 세대수. 명부 단지 전체 세대수와 다름';
comment on column public.complex_area.group_id is '공시가격 면적 명부 기준의 표시 그룹 식별자';
comment on column public.complex_area.group_areas is '공시가격 면적 명부 기준의 그룹 구성 면적을 오름차순 | 문자열로 저장';

commit;
