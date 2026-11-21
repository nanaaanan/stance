-- 매매 파생값 갱신 함수에만 문장 제한 25초
--   - API 로 부르면 authenticator 역할의 8초 제한 적용. 갱신이 8초를 넘는 날이 있음
-- 역할 단위로 풀지 않는 이유
--   - service_role 의 제한을 늘리면 수집과 적재의 모든 요청이 함께 풀림
-- 25초인 이유
--   - scripts/collect.py 의 요청 제한 30초보다 짧아야 DB 가 먼저 끊고 원인이 응답에 남음
alter function public.refresh_trade_stats() set statement_timeout = '25s';
