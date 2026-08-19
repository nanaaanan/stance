-- ============================================================================
-- 빈 값이 이미 채워진 값을 덮어쓰지 않게 한다.
--
-- 수집 스크립트는 PostgREST 로 upsert 를 보낸다. PostgREST 가 만드는 SQL 은
-- 이런 모양이라 payload 의 null 을 그대로 써버린다.
--
--   INSERT INTO trade (...) VALUES (...)
--   ON CONFLICT (자연키) DO UPDATE SET apt_dong = EXCLUDED.apt_dong, ...
--
-- 그래서 1회차에 등기일이 채워졌더라도, 2회차 응답에서 그 칸이 비어 있으면
-- null 로 덮여 사라진다. 덤으로 deal_change_log 에 "값이 바뀌었다" 는
-- 가짜 이력까지 쌓인다.
--
-- payload 에서 null 인 키를 빼는 방법은 쓸 수 없다.
--   - PostgREST 는 배열 안 객체의 키가 전부 같아야 한다 (PGRST102)
--   - 값 조합별로 요청을 쪼개면 묶어 보내는 의미가 없어진다
-- 그래서 클라이언트가 아니라 DB 에서 막는다.
--
-- BEFORE UPDATE 라서 이력 트리거(AFTER UPDATE)보다 먼저 돈다.
-- 되살린 값이 반영된 뒤에 이력을 비교하므로 가짜 이력도 생기지 않는다.
-- ============================================================================

create or replace function public.keep_filled_values()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
declare
  -- 한 번 채워지면 다시 비워질 일이 없는 칸들. trade 와 rent 를 합친 목록
  -- 그 테이블에 없는 칸은 자동으로 건너뛴다 (oldj -> k 가 null 이라 조건에 안 걸림)
  --
  -- 여기 없는 칸은 전부 NOT NULL 이다
  --   - is_current, trade_count, ambiguous_cancel, sgg_cd, apt_nm, last_seen_at
  --   - 빠뜨리면 조용히 넘어가지 않고 바로 에러가 난다
  protected constant text[] := array[
    'umd_cd','umd_nm','apt_dong','jibun',
    'road_nm','road_nm_bonbun','road_nm_bubun','road_nm_full','build_year',
    'dealing_gbn','cdeal_type','cdeal_day','rgst_date',
    'sler_gbn','buyer_gbn','estate_agent_sgg_nm','land_leasehold_gbn',
    'contract_type','contract_term','pre_deposit','pre_monthly_rent','use_rr_right'
  ];
  oldj jsonb := to_jsonb(old);
  newj jsonb := to_jsonb(new);
  k    text;
begin
  foreach k in array protected loop
    if (newj ->> k) is null and (oldj ->> k) is not null then
      newj := jsonb_set(newj, array[k], oldj -> k);
    end if;
  end loop;
  return jsonb_populate_record(new, newj);
end;
$$;

comment on function public.keep_filled_values() is
  '값이 있던 칸을 빈 값으로 덮지 않게 막는다. 위 목록의 칸은 채워지기만 하고 비워지지 않는다';

drop trigger if exists trg_trade_keep_filled on public.trade;
create trigger trg_trade_keep_filled
  before update on public.trade
  for each row execute function public.keep_filled_values();

drop trigger if exists trg_rent_keep_filled on public.rent;
create trigger trg_rent_keep_filled
  before update on public.rent
  for each row execute function public.keep_filled_values();

-- 값을 일부러 비우고 싶을 때는 이 트리거가 막는다.
-- 그럴 일이 생기면 아래처럼 잠깐 끄고 처리한다.
--   alter table public.trade disable trigger trg_trade_keep_filled;
--   ... 원하는 UPDATE ...
--   alter table public.trade enable  trigger trg_trade_keep_filled;
