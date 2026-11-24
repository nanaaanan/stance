begin;

-- 12개월 창의 끝을 이번 달에서 지난달로 옮김
--   - 수집이 이번 달 계약분을 받지 않아 창의 마지막 달이 항상 비어 있었음
-- statement_timeout 을 정의 안에 다시 적는 이유
--   - create or replace 는 소유자와 권한만 유지. SET 설정은 새 정의의 값으로 교체
--   - 빠지면 API 역할의 8초 제한으로 돌아감
create or replace function public.refresh_trade_stats() returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '25s'
as $$
declare
  v_month_start date;
  v_window_start_ym text;
  v_window_end_ym text;
  v_refreshed_at timestamptz;
  v_conflict record;
  v_result jsonb;
begin
  lock table public.trade_group_stats, public.trade_complex_stats in exclusive mode;

  v_refreshed_at := now();
  v_month_start := date_trunc('month', v_refreshed_at at time zone 'Asia/Seoul')::date;
  v_window_start_ym := to_char(v_month_start - interval '12 months', 'YYYYMM');
  v_window_end_ym := to_char(v_month_start - interval '1 month', 'YYYYMM');

  select area.apt_seq, area.group_id into v_conflict
  from public.complex_area as area
  where area.group_id is not null
  group by area.apt_seq, area.group_id
  having count(distinct area.group_label) > 1
  order by area.apt_seq, area.group_id
  limit 1;

  if found then
    raise exception 'group_label 충돌: apt_seq=%, group_id=%',
      v_conflict.apt_seq, v_conflict.group_id;
  end if;

  -- where true 삭제 금지
  --   - API 경유 호출 = authenticator 세션 = pg-safeupdate 적재
  --   - WHERE 없는 DELETE 는 21000 으로 거부
  --   - TRUNCATE 는 ACCESS EXCLUSIVE 라 갱신 중 읽기 차단
  delete from public.trade_group_stats where true;
  delete from public.trade_complex_stats where true;

  -- 집계, 두 INSERT, 요약을 한 문장으로 유지
  --   - plpgsql 은 문장마다 새 스냅샷
  --   - 나누면 사이에 커밋된 수집 정정이 한쪽 표에만 반영
  with base as materialized (
    select
      trade.apt_seq, area.group_id, trade.is_current,
      trade.deal_ym, trade.deal_amount, trade.trade_count,
      complex.apt_seq is not null as has_complex,
      trade.deal_ym between v_window_start_ym and v_window_end_ym as in_window,
      public.trade_stat_reason(
        trade.is_current, trade.ambiguous_cancel, trade.cdeal_type, trade.dealing_gbn
      ) as reason
    from public.trade as trade
    left join public.complex as complex on complex.apt_seq = trade.apt_seq
    left join public.complex_area as area
      on area.apt_seq = trade.apt_seq and area.exclu_use_ar = trade.exclu_use_ar
  ), labels as (
    select area.apt_seq, area.group_id,
      case when min(area.group_label) = max(area.group_label)
        then min(area.group_label) end as group_label
    from public.complex_area as area
    where area.group_id is not null
    group by area.apt_seq, area.group_id
  ), g as (
    insert into public.trade_group_stats (
      apt_seq, group_id, group_label, valid_count,
      min_price, max_price, ratio_base_price,
      excluded_ambiguous, excluded_cancel, excluded_direct, last_valid_ym,
      window_start_ym, window_end_ym, refreshed_at
    )
    select b.apt_seq, b.group_id, labels.group_label,
      coalesce(sum(b.trade_count) filter (where b.in_window and b.reason = '유효'), 0),
      min(b.deal_amount) filter (where b.in_window and b.reason = '유효'),
      max(b.deal_amount) filter (where b.in_window and b.reason = '유효'),
      sum(b.deal_amount::numeric * b.trade_count)
        filter (where b.in_window and b.reason = '유효')
        / nullif(sum(b.trade_count) filter (where b.in_window and b.reason = '유효'), 0),
      coalesce(sum(b.trade_count) filter (where b.in_window and b.reason = '해제 불분명'), 0),
      coalesce(sum(b.trade_count) filter (where b.in_window and b.reason = '해제'), 0),
      coalesce(sum(b.trade_count) filter (where b.in_window and b.reason = '직거래'), 0),
      max(b.deal_ym) filter (where b.reason = '유효'),
      v_window_start_ym, v_window_end_ym, v_refreshed_at
    from base as b
    join labels on labels.apt_seq = b.apt_seq and labels.group_id = b.group_id
    where b.has_complex
    group by b.apt_seq, b.group_id, labels.group_label
    having bool_or(b.is_current)
    returning apt_seq
  ), totals as (
    select b.apt_seq,
      sum(b.trade_count) as valid_count,
      coalesce(sum(b.trade_count) filter (where b.group_id is null), 0) as ungrouped_valid_count
    from base as b
    where b.has_complex and b.in_window and b.reason = '유효'
    group by b.apt_seq
  ), c as (
    insert into public.trade_complex_stats (
      apt_seq, valid_count, ungrouped_valid_count, households, approval_date,
      turnover_exclusion, window_start_ym, window_end_ym, refreshed_at
    )
    select complex.apt_seq,
      coalesce(totals.valid_count, 0), coalesce(totals.ungrouped_valid_count, 0),
      registry.households, registry.approval_date,
      case
        when registry.reb_shared_count > 1 then '명부 공유'
        when registry.households is null then '세대수 없음'
        when registry.approval_date is null then '사용승인일 없음'
        -- 경계는 창 끝 다음 달 1일. 창이 지난달에서 끝나므로 이번 달 1일
        when registry.approval_date + interval '2 years' > v_month_start then '준공 2년 미만'
      end,
      v_window_start_ym, v_window_end_ym, v_refreshed_at
    from public.complex as complex
    join public.complex_registry_view as registry on registry.apt_seq = complex.apt_seq
    left join totals on totals.apt_seq = complex.apt_seq
    returning apt_seq, valid_count, ungrouped_valid_count
  )
  select jsonb_build_object(
    'window_start_ym', v_window_start_ym,
    'window_end_ym', v_window_end_ym,
    'refreshed_at', v_refreshed_at,
    'group_rows', (select count(*) from g),
    'complex_rows', (select count(*) from c),
    'valid_count', (select coalesce(sum(valid_count), 0) from c),
    'ungrouped_valid_count', (select coalesce(sum(ungrouped_valid_count), 0) from c),
    'trades_without_complex', (
      select coalesce(sum(trade_count), 0) from base where not has_complex and is_current
    )
  ) into v_result;

  return v_result;
end;
$$;

create table public.rent_group_stats (
  apt_seq text not null references public.complex (apt_seq),
  group_id text not null,
  jeonse_count integer not null check (jeonse_count >= 0),
  ratio_base_deposit numeric,
  renewal_count integer not null check (renewal_count >= 0),
  renewal_pre_deposit_avg numeric,
  renewal_deposit_avg numeric,
  window_start_ym text not null check (window_start_ym ~ '^[0-9]{6}$'),
  window_end_ym text not null check (window_end_ym ~ '^[0-9]{6}$'),
  refreshed_at timestamptz not null,
  primary key (apt_seq, group_id),
  check ((jeonse_count = 0) = (ratio_base_deposit is null)),
  check ((renewal_count = 0) = (renewal_pre_deposit_avg is null)),
  check ((renewal_count = 0) = (renewal_deposit_avg is null))
);

create function public.refresh_rent_stats() returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '25s'
as $$
declare
  v_month_start date;
  v_window_start_ym text;
  v_window_end_ym text;
  v_refreshed_at timestamptz;
  v_result jsonb;
begin
  lock table public.rent_group_stats in exclusive mode;

  -- 창 계산은 refresh_trade_stats 와 같은 식. 다르면 뷰가 두 표를 잇지 못함
  v_refreshed_at := now();
  v_month_start := date_trunc('month', v_refreshed_at at time zone 'Asia/Seoul')::date;
  v_window_start_ym := to_char(v_month_start - interval '12 months', 'YYYYMM');
  v_window_end_ym := to_char(v_month_start - interval '1 month', 'YYYYMM');

  delete from public.rent_group_stats where true;

  -- 갱신계약 재료의 조건
  --   - 종전 월세 0 은 원본에서 빈 값으로 옴. DB 에는 NULL. = 0 으로만 비교하면 거의 다 빠짐
  --   - trade_count = 1 만 사용. 종전 값은 자연키 밖이라 여러 계약이 한 행으로 합쳐지면
  --     값이 갈린 칸이 NULL 이 되고, 빈 값의 NULL 과 구별 불가
  with base as materialized (
    select
      rent.apt_seq, area.group_id,
      rent.deposit, rent.monthly_rent, rent.pre_deposit, rent.trade_count,
      complex.apt_seq is not null as has_complex,
      coalesce(
        rent.contract_type = '갱신' and rent.monthly_rent = 0 and rent.trade_count = 1
          and rent.pre_deposit > 0 and coalesce(rent.pre_monthly_rent, 0) = 0,
        false
      ) as is_renewal_pair
    from public.rent as rent
    left join public.complex as complex on complex.apt_seq = rent.apt_seq
    left join public.complex_area as area
      on area.apt_seq = rent.apt_seq and area.exclu_use_ar = rent.exclu_use_ar
    where rent.is_current
      and rent.deal_ym between v_window_start_ym and v_window_end_ym
  ), g as (
    insert into public.rent_group_stats (
      apt_seq, group_id, jeonse_count, ratio_base_deposit,
      renewal_count, renewal_pre_deposit_avg, renewal_deposit_avg,
      window_start_ym, window_end_ym, refreshed_at
    )
    select b.apt_seq, b.group_id,
      coalesce(sum(b.trade_count) filter (where b.monthly_rent = 0), 0),
      sum(b.deposit::numeric * b.trade_count) filter (where b.monthly_rent = 0)
        / nullif(sum(b.trade_count) filter (where b.monthly_rent = 0), 0),
      count(*) filter (where b.is_renewal_pair),
      avg(b.pre_deposit::numeric) filter (where b.is_renewal_pair),
      avg(b.deposit::numeric) filter (where b.is_renewal_pair),
      v_window_start_ym, v_window_end_ym, v_refreshed_at
    from base as b
    where b.has_complex and b.group_id is not null
    group by b.apt_seq, b.group_id
    returning jeonse_count, renewal_count
  )
  select jsonb_build_object(
    'window_start_ym', v_window_start_ym,
    'window_end_ym', v_window_end_ym,
    'refreshed_at', v_refreshed_at,
    'group_rows', (select count(*) from g),
    'jeonse_count', (select coalesce(sum(jeonse_count), 0) from g),
    'renewal_count', (select coalesce(sum(renewal_count), 0) from g),
    'rent_count', (select coalesce(sum(trade_count), 0) from base),
    'grouped_rent_count', (
      select coalesce(sum(trade_count), 0) from base where has_complex and group_id is not null
    ),
    'rents_without_complex', (
      select coalesce(sum(trade_count), 0) from base where not has_complex
    ),
    'ungrouped_rent_count', (
      select coalesce(sum(trade_count), 0) from base where has_complex and group_id is null
    )
  ) into v_result;

  return v_result;
end;
$$;

-- 전세가율은 저장하지 않고 두 표에서 읽을 때 나눔
--   - 두 표는 따로 갱신. 저장한 비율은 한쪽만 갱신되면 틀린 값
-- 창 조건을 join 에 두는 이유
--   - 창이 다른 달의 전세 값이 매매 창의 값처럼 보이지 않게 전월세 쪽 칸 전부를 NULL 로
create view public.group_jeonse_ratio_view
with (security_invoker = true)
as
select
  trade.apt_seq,
  trade.group_id,
  rent.ratio_base_deposit / nullif(trade.ratio_base_price, 0) as jeonse_ratio,
  rent.jeonse_count,
  trade.valid_count as trade_valid_count,
  trade.window_start_ym,
  trade.window_end_ym,
  trade.refreshed_at as trade_refreshed_at,
  rent.refreshed_at as rent_refreshed_at
from public.trade_group_stats as trade
left join public.rent_group_stats as rent
  on rent.apt_seq = trade.apt_seq
  and rent.group_id = trade.group_id
  and rent.window_start_ym = trade.window_start_ym
  and rent.window_end_ym = trade.window_end_ym;

alter table public.rent_group_stats enable row level security;

create policy rent_group_stats_public_read on public.rent_group_stats
  for select to anon, authenticated using (true);

revoke all on table public.rent_group_stats, public.group_jeonse_ratio_view
  from anon, authenticated, service_role;
grant select on table public.rent_group_stats, public.group_jeonse_ratio_view
  to anon, authenticated, service_role;

revoke execute on function public.refresh_rent_stats()
  from public, anon, authenticated;
grant execute on function public.refresh_rent_stats() to service_role;

comment on table public.rent_group_stats is '단지별 표시 그룹 전월세 파생값. 전세가율 분자와 갱신계약의 보증금 변화';
comment on column public.rent_group_stats.ratio_base_deposit is
  '전세가율 분자 전용. 월세 0 인 계약의 보증금을 거래 건수로 가중하며 반올림하지 않음';
comment on column public.rent_group_stats.renewal_count is
  '갱신이고 지금과 종전 모두 월세가 없으며 한 행이 계약 한 건인 것의 수';
comment on column public.rent_group_stats.refreshed_at is
  '파생값 계산 시각. 원장 확인 시각은 collect_run에서 읽음';

-- 두 갱신 함수에 문장 제한이 남아 있지 않으면 적용 전체를 실패시킴
do $$
begin
  if (
    select count(*)
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('refresh_trade_stats', 'refresh_rent_stats')
      and p.proconfig @> array['statement_timeout=25s']
  ) <> 2 then
    raise exception '갱신 함수의 statement_timeout 설정 누락';
  end if;
end;
$$;

commit;
