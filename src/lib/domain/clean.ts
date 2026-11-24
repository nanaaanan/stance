import type { Database } from '@/types/database.types'

type TradeRow = Database['public']['Tables']['trade']['Row']

// DB 함수 public.trade_stat_reason 과 같은 문자열, 같은 순서
//   - 원본은 supabase/migrations 의 trade_stat_reason
//   - 한쪽만 바꾸면 화면의 배지 건수와 trade_group_stats 의 제외 건수가 어긋남
export type TradeReason = '정정됨' | '해제 불분명' | '해제' | '직거래' | '유효'

export type TradeFlags = Pick<
  TradeRow,
  'is_current' | 'ambiguous_cancel' | 'cdeal_type' | 'dealing_gbn'
>

export type TradeForStats = TradeFlags &
  Pick<TradeRow, 'deal_ym' | 'deal_amount' | 'trade_count'>

// 계약월 6자리(YYYYMM). 양 끝 포함
export type TradeWindow = {
  startYm: string
  endYm: string
}

export type TradeSummary = {
  validCount: number
  minPrice: number | null
  maxPrice: number | null
  excludedAmbiguous: number
  excludedCancel: number
  excludedDirect: number
}

export function classifyTrade(trade: TradeFlags): TradeReason {
  // 조건 순서를 바꾸지 말 것. 처음 맞는 하나가 결과
  //   - 해제 불분명인 행은 cdeal_type 도 'O'. 해제를 먼저 보면 해제 불분명이 0건이 됨
  //   - 해제된 직거래는 해제로 분류. 직거래를 먼저 보면 제외 건수의 칸이 바뀜
  if (!trade.is_current) {
    return '정정됨'
  }
  if (trade.ambiguous_cancel) {
    return '해제 불분명'
  }
  if (trade.cdeal_type !== null) {
    return '해제'
  }
  if (trade.dealing_gbn === '직거래') {
    return '직거래'
  }
  return '유효'
}

export function selectValidTrades<T extends TradeFlags>(
  trades: readonly T[],
): T[] {
  return trades.filter((trade) => classifyTrade(trade) === '유효')
}

export function summarizeTrades(
  trades: readonly TradeForStats[],
  window: TradeWindow,
): TradeSummary {
  const summary: TradeSummary = {
    validCount: 0,
    minPrice: null,
    maxPrice: null,
    excludedAmbiguous: 0,
    excludedCancel: 0,
    excludedDirect: 0,
  }

  for (const trade of trades) {
    if (
      trade.deal_ym === null ||
      trade.deal_ym < window.startYm ||
      trade.deal_ym > window.endYm
    ) {
      continue
    }

    // 건수는 행 수가 아니라 trade_count 의 합
    //   - 자연키가 같은 거래 n건이 DB 에 1행으로 저장됨
    switch (classifyTrade(trade)) {
      case '유효':
        summary.validCount += trade.trade_count
        summary.minPrice =
          summary.minPrice === null
            ? trade.deal_amount
            : Math.min(summary.minPrice, trade.deal_amount)
        summary.maxPrice =
          summary.maxPrice === null
            ? trade.deal_amount
            : Math.max(summary.maxPrice, trade.deal_amount)
        break
      case '해제 불분명':
        summary.excludedAmbiguous += trade.trade_count
        break
      case '해제':
        summary.excludedCancel += trade.trade_count
        break
      case '직거래':
        summary.excludedDirect += trade.trade_count
        break
      // 정정됨은 어느 칸에도 넣지 않음. trade_group_stats 에 대응하는 칸이 없음
      case '정정됨':
        break
    }
  }

  return summary
}
