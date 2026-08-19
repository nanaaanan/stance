-- ============================================================================
-- deal_change_log.run_id 를 PostgREST 요청 헤더에서 채운다.
--
-- 기존 log_deal_change() 는 set_config('stance.run_id', ...) 로 심어둔 값만 읽는다.
-- SQL Editor 에서 직접 고칠 때는 되지만, PostgREST 는 요청마다 새 세션이라
-- set_config 로 심은 값이 남아있지 않다. 그래서 수집 스크립트로 넣으면
-- run_id 가 항상 비어 있었다.
--
-- PostgREST 는 요청 헤더 전체를 트랜잭션 안에서 읽을 수 있게 해준다.
-- 수집 스크립트가 upsert 요청에 X-Run-Id 헤더를 붙이고, 트리거가 그 헤더를 읽는다.
-- 회차 번호를 구하는 부분만 바뀌고 나머지 로직은 그대로다.
-- ============================================================================

create or replace function public.log_deal_change()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
declare
  -- 이 두 칸은 매 실행 바뀜. 비교에서 뺌
  old_j  jsonb := to_jsonb(old) - 'first_seen_at' - 'last_seen_at';
  new_j  jsonb := to_jsonb(new) - 'first_seen_at' - 'last_seen_at';
  fields text[];
  hdr    text;
  run_id bigint;
begin
  -- order by k 필수
  --   - 없으면 배열 순서가 실행마다 달라짐
  --   - 같은 성격의 이력이 다른 모양으로 쌓임
  select coalesce(array_agg(k order by k), '{}'::text[])
    into fields
    from jsonb_object_keys(new_j) as k
   where new_j -> k is distinct from old_j -> k;

  -- 이 조기 반환을 트리거 WHEN 절로 옮기지 말 것
  --   - upsert SET 절의 last_seen_at = now() 로 WHEN 조건이 항상 참
  --   - 실제 필터는 여기 한 곳
  if array_length(fields, 1) is null then
    return new;
  end if;

  -- 수집 회차 번호를 두 군데서 찾는다
  --   SQL Editor 에서 직접 고칠 때 : set_config 로 심어둔 값
  --   수집 스크립트가 고칠 때      : 요청 헤더 X-Run-Id (헤더 이름은 소문자로 들어옴)
  --                                (PostgREST 는 요청마다 새 세션이라 set_config 를 못 씀)
  -- 숫자가 아닌 헤더가 오면 그냥 비운다. 여기서 형변환 오류로 적재 전체가 죽으면 안 됨
  hdr := nullif(current_setting('request.headers', true)::json ->> 'x-run-id', '');
  run_id := coalesce(
    nullif(current_setting('stance.run_id', true), '')::bigint,
    case when hdr ~ '^[0-9]+$' then hdr::bigint end
  );

  -- tg_table_name 사용. 트리거 인자를 쓰면 오타가 잘못된 source 로 조용히 쌓임
  insert into public.deal_change_log
    (source_table, trade_id, rent_id, run_id, changed_fields, old_row)
  values (
    tg_table_name,
    case when tg_table_name = 'trade' then old.id end,
    case when tg_table_name = 'rent'  then old.id end,
    run_id,
    fields,
    old_j
  );

  return new;
end;
$$;

-- 트리거 자체는 다시 만들 필요 없음. create or replace function 이면
-- 기존 트리거(trg_trade_change, trg_rent_change)가 새 함수를 그대로 씀
