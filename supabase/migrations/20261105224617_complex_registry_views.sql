begin;

create view public.complex_registry_view
with (security_invoker = true)
as
with shared as (
  select
    apt_seq,
    reb_complex_id,
    case when reb_complex_id is not null
      then count(*) over (partition by reb_complex_id)
    end as reb_shared_count
  from public.complex
)
select
  shared.apt_seq,
  shared.reb_complex_id,
  shared.reb_shared_count,
  case when shared.reb_shared_count = 1 then registry.households end as households,
  registry.approval_date
from shared
left join public.reb_complex as registry using (reb_complex_id);

create view public.complex_area_registry_view
with (security_invoker = true)
as
select
  area.apt_seq,
  area.exclu_use_ar,
  area.group_id,
  registry.reb_shared_count,
  case when registry.reb_shared_count = 1 then area.households end as households
from public.complex_area as area
join public.complex_registry_view as registry using (apt_seq);

revoke all on table public.complex_registry_view, public.complex_area_registry_view
  from anon, authenticated, service_role;
grant select on table public.complex_registry_view, public.complex_area_registry_view
  to anon, authenticated, service_role;

comment on view public.complex_registry_view is
  '거래 단지별 명부 속성. 공유 명부의 세대수는 가리고 사용승인일은 유지';
comment on column public.complex_registry_view.apt_seq is '거래 단지 식별자';
comment on column public.complex_registry_view.reb_complex_id is '단지 명부 번호';
comment on column public.complex_registry_view.reb_shared_count is
  '잔존 행을 포함한 complex 전체에서 같은 명부 번호를 가리키는 단지 수. 번호가 없으면 NULL';
comment on column public.complex_registry_view.households is
  '명부 한 단지의 총 세대수. 명부 번호를 다른 단지와 공유하면 NULL';
comment on column public.complex_registry_view.approval_date is
  '명부 사용승인일. 명부 공유 여부와 무관하게 표시';

comment on view public.complex_area_registry_view is
  '거래 단지별 면적 세대수. 공유 명부의 세대수는 가리고 원천 값은 보존';
comment on column public.complex_area_registry_view.apt_seq is '거래 단지 식별자';
comment on column public.complex_area_registry_view.exclu_use_ar is '매매/전월세 실거래 전용면적';
comment on column public.complex_area_registry_view.group_id is '공시가격 면적 명부의 표시 그룹 식별자';
comment on column public.complex_area_registry_view.reb_shared_count is
  '잔존 행을 포함한 complex 전체에서 같은 명부 번호를 가리키는 단지 수. 번호가 없으면 NULL';
comment on column public.complex_area_registry_view.households is
  '공시가격 면적 명부 기준의 해당 면적 세대수. 명부 번호를 다른 단지와 공유하면 NULL';

comment on column public.reb_complex.households is
  '명부 한 단지의 총 세대수. 이 번호를 가리키는 단지가 여럿일 수 있다. 거래 단지 단위로 읽을 때는 complex_registry_view를 쓴다';
comment on column public.complex_area.households is
  '공시가격 면적 명부 기준의 해당 면적 세대수. 명부 단지 전체 세대수와 다름. 거래 단지 단위로 읽을 때는 complex_area_registry_view를 쓴다';

commit;
