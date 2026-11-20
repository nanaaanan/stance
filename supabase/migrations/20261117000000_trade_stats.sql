begin;

create function public.trade_stat_reason(
  p_is_current boolean,
  p_ambiguous_cancel boolean,
  p_cdeal_type text,
  p_dealing_gbn text
) returns text
language sql immutable
set search_path = ''
as $$
  select case
    when p_is_current = false then '정정됨'
    when p_ambiguous_cancel = true then '해제 불분명'
    when p_cdeal_type is not null then '해제'
    when p_dealing_gbn = '직거래' then '직거래'
    else '유효'
  end;
$$;

create table public.trade_group_stats (
  apt_seq text not null references public.complex (apt_seq),
  group_id text not null,
  group_label integer not null,
  valid_count integer not null check (valid_count >= 0),
  min_price integer,
  max_price integer,
  ratio_base_price numeric,
  excluded_ambiguous integer not null check (excluded_ambiguous >= 0),
  excluded_cancel integer not null check (excluded_cancel >= 0),
  excluded_direct integer not null check (excluded_direct >= 0),
  last_valid_ym text,
  window_start_ym text not null check (window_start_ym ~ '^[0-9]{6}$'),
  window_end_ym text not null check (window_end_ym ~ '^[0-9]{6}$'),
  refreshed_at timestamptz not null,
  primary key (apt_seq, group_id),
  check ((valid_count = 0) = (min_price is null)),
  check ((valid_count = 0) = (max_price is null)),
  check ((valid_count = 0) = (ratio_base_price is null)),
  check (min_price <= max_price)
);

create index trade_group_stats_min_price_idx
  on public.trade_group_stats (min_price);

create table public.trade_complex_stats (
  apt_seq text primary key references public.complex (apt_seq),
  valid_count integer not null check (valid_count >= 0),
  ungrouped_valid_count integer not null check (ungrouped_valid_count >= 0),
  households integer,
  approval_date date,
  turnover_exclusion text check (turnover_exclusion in (
    '명부 공유', '세대수 없음', '사용승인일 없음', '준공 2년 미만'
  )),
  window_start_ym text not null check (window_start_ym ~ '^[0-9]{6}$'),
  window_end_ym text not null check (window_end_ym ~ '^[0-9]{6}$'),
  refreshed_at timestamptz not null,
  check (ungrouped_valid_count <= valid_count),
  check (turnover_exclusion is not null
    or (households is not null and approval_date is not null))
);

create function public.refresh_trade_stats() returns jsonb
language plpgsql
security definer
set search_path = ''
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
  v_window_start_ym := to_char(v_month_start - interval '11 months', 'YYYYMM');
  v_window_end_ym := to_char(v_month_start, 'YYYYMM');

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

  delete from public.trade_group_stats;
  delete from public.trade_complex_stats;

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
        when registry.approval_date + interval '2 years'
          > v_month_start + interval '1 month' then '준공 2년 미만'
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

alter table public.trade_group_stats enable row level security;
alter table public.trade_complex_stats enable row level security;

create policy trade_group_stats_public_read on public.trade_group_stats
  for select to anon, authenticated using (true);
create policy trade_complex_stats_public_read on public.trade_complex_stats
  for select to anon, authenticated using (true);

revoke all on table public.trade_group_stats, public.trade_complex_stats
  from anon, authenticated, service_role;
grant select on table public.trade_group_stats, public.trade_complex_stats
  to anon, authenticated, service_role;

revoke execute on function public.trade_stat_reason(boolean, boolean, text, text)
  from public, anon, authenticated;
revoke execute on function public.refresh_trade_stats()
  from public, anon, authenticated;
grant execute on function public.trade_stat_reason(boolean, boolean, text, text)
  to service_role;
grant execute on function public.refresh_trade_stats() to service_role;

comment on table public.trade_group_stats is '시세 판정용 단지별 표시 그룹 매매 파생값';
comment on column public.trade_group_stats.ratio_base_price is
  '전세가율 분모 전용 가격. 거래 건수로 가중하며 반올림하지 않음';
comment on column public.trade_group_stats.refreshed_at is
  '파생값 계산 시각. 원장 확인 시각은 collect_run에서 읽음';
comment on table public.trade_complex_stats is '단지별 유효 매매 건수와 회전율 계산 재료';
comment on column public.trade_complex_stats.refreshed_at is
  '파생값 계산 시각. 원장 확인 시각은 collect_run에서 읽음';

commit;
