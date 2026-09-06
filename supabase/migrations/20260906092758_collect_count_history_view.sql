-- 수집에 성공한 관측 중 첫 값과 값이 바뀐 시점만 보여 준다.
-- NULL 도 관측값으로 비교하며, running/error 는 관측에서 제외한다.
create or replace view public.collect_count_history
with (security_invoker = true)
as
with observations as (
  select
    kind,
    lawd_cd,
    deal_ym,
    started_at,
    id as run_id,
    total_count,
    lag(total_count) over observation_order as prev_total_count,
    row_number() over observation_order = 1 as is_first_observation
  from public.collect_run
  where status = 'ok'
  window observation_order as (
    partition by kind, lawd_cd, deal_ym
    order by started_at, id
  )
), changes as (
  select *
  from observations
  where is_first_observation
     or total_count is distinct from prev_total_count
)
select
  kind,
  lawd_cd,
  deal_ym,
  started_at,
  run_id,
  total_count,
  prev_total_count,
  is_first_observation,
  -- 반복 관측을 뺀 다음에 구간의 끝을 구한다. 마지막 구간은 NULL 이다.
  lead(started_at) over (
    partition by kind, lawd_cd, deal_ym
    order by started_at, run_id
  ) as valid_until
from changes;

-- 내부 운영 로그. 자동 부여되거나 이전 실행에 남은 권한을 먼저 회수한다.
revoke all on table public.collect_count_history from anon, authenticated;
revoke all on table public.collect_count_history from service_role;
grant select on table public.collect_count_history to service_role;
