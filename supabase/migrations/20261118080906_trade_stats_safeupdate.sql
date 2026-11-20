-- 갱신 함수 교체만. 표, 권한, 판정 함수는 그대로
--   - create or replace = 소유자와 실행 권한 유지

begin;

create or replace function public.refresh_trade_stats() returns jsonb
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

commit;
