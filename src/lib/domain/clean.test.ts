import { describe, expect, it } from 'vitest'
import {
  classifyTrade,
  selectValidTrades,
  summarizeTrades,
  type TradeForStats,
  type TradeReason,
} from './clean'

// 실제 수집 행. 래미안대치팰리스(11680-4394) 전용 84.97, 84.98, 84.99㎡
//   - 계약월 202503 부터 202606 까지 18행
//   - 값을 고치지 말 것. 아래 테스트의 기대값이 이 18행에 맞춘 고정값
type FixtureRow = TradeForStats & { deal_date: string }

function row(
  deal_date: string,
  deal_amount: number,
  dealing_gbn: string,
  cdeal_type: string | null = null,
): FixtureRow {
  return {
    deal_date,
    deal_ym: deal_date.slice(0, 4) + deal_date.slice(5, 7),
    deal_amount,
    trade_count: 1,
    is_current: true,
    ambiguous_cancel: false,
    cdeal_type,
    dealing_gbn,
  }
}

const RAEMIAN_85: FixtureRow[] = [
  row('2025-03-01', 407500, '중개거래'),
  row('2025-03-10', 407000, '중개거래', 'O'),
  row('2025-05-27', 435000, '중개거래'),
  row('2025-05-31', 447000, '중개거래'),
  row('2025-06-05', 370000, '직거래'),
  row('2025-06-24', 438000, '중개거래'),
  row('2025-06-27', 448000, '중개거래'),
  row('2025-06-27', 470000, '중개거래'),
  row('2025-07-22', 441000, '중개거래'),
  row('2025-10-14', 250000, '직거래'),
  row('2025-11-06', 475000, '중개거래'),
  row('2026-03-13', 415000, '중개거래'),
  row('2026-03-16', 419000, '중개거래'),
  row('2026-03-19', 405000, '중개거래'),
  row('2026-05-06', 439000, '중개거래'),
  row('2026-05-07', 425000, '중개거래'),
  row('2026-05-08', 430000, '중개거래'),
  row('2026-06-03', 440000, '중개거래'),
]

function trade(overrides: Partial<TradeForStats> = {}): TradeForStats {
  return {
    deal_ym: '202601',
    deal_amount: 100000,
    trade_count: 1,
    is_current: true,
    ambiguous_cancel: false,
    cdeal_type: null,
    dealing_gbn: '중개거래',
    ...overrides,
  }
}

const WINDOW = { startYm: '202601', endYm: '202612' }

describe('classifyTrade', () => {
  // 네 칸의 모든 조합 24개. DB 함수 trade_stat_reason 의 CASE 순서를 옮긴 표
  const cases: [boolean, boolean, string | null, string | null, TradeReason][] =
    [
      [false, false, null, null, '정정됨'],
      [false, false, null, '중개거래', '정정됨'],
      [false, false, null, '직거래', '정정됨'],
      [false, false, 'O', null, '정정됨'],
      [false, false, 'O', '중개거래', '정정됨'],
      [false, false, 'O', '직거래', '정정됨'],
      [false, true, null, null, '정정됨'],
      [false, true, null, '중개거래', '정정됨'],
      [false, true, null, '직거래', '정정됨'],
      [false, true, 'O', null, '정정됨'],
      [false, true, 'O', '중개거래', '정정됨'],
      [false, true, 'O', '직거래', '정정됨'],
      [true, true, null, null, '해제 불분명'],
      // 수집기 불변식(해제 불분명이면 cdeal_type 은 'O')을 어긴 행도 DB 와 같은 답
      [true, true, null, '중개거래', '해제 불분명'],
      [true, true, null, '직거래', '해제 불분명'],
      [true, true, 'O', null, '해제 불분명'],
      [true, true, 'O', '중개거래', '해제 불분명'],
      [true, true, 'O', '직거래', '해제 불분명'],
      [true, false, 'O', null, '해제'],
      [true, false, 'O', '중개거래', '해제'],
      [true, false, 'O', '직거래', '해제'],
      [true, false, null, '직거래', '직거래'],
      [true, false, null, '중개거래', '유효'],
      [true, false, null, null, '유효'],
    ]

  it.each(cases)(
    'is_current=%s ambiguous_cancel=%s cdeal_type=%s dealing_gbn=%s -> %s',
    (is_current, ambiguous_cancel, cdeal_type, dealing_gbn, expected) => {
      expect(
        classifyTrade({
          is_current,
          ambiguous_cancel,
          cdeal_type,
          dealing_gbn,
        }),
      ).toBe(expected)
    },
  )

  it("cdeal_type 은 'O' 가 아니어도 NULL 이 아니면 해제", () => {
    expect(classifyTrade(trade({ cdeal_type: '' }))).toBe('해제')
  })
})

describe('selectValidTrades', () => {
  it('분류가 유효인 거래만 원래 순서대로 남김', () => {
    const first = trade({ deal_amount: 1 })
    const noDealingGbn = trade({ deal_amount: 2, dealing_gbn: null })
    const input = [
      first,
      trade({ is_current: false }),
      trade({ ambiguous_cancel: true, cdeal_type: 'O' }),
      trade({ cdeal_type: 'O' }),
      trade({ dealing_gbn: '직거래' }),
      noDealingGbn,
    ]

    expect(selectValidTrades(input)).toEqual([first, noDealingGbn])
  })

  it('실제 행 18개에서 해제 1건과 직거래 2건을 뺀 15건', () => {
    expect(selectValidTrades(RAEMIAN_85).map((t) => t.deal_amount)).toEqual([
      407500, 435000, 447000, 438000, 448000, 470000, 441000, 475000, 415000,
      419000, 405000, 439000, 425000, 430000, 440000,
    ])
  })

  it('결과를 다시 넣어도 결과가 같음', () => {
    const once = selectValidTrades(RAEMIAN_85)

    expect(selectValidTrades(once)).toEqual(once)
  })

  it('받은 배열과 그 안의 행을 바꾸지 않음', () => {
    const input = structuredClone(RAEMIAN_85)
    const before = structuredClone(input)

    selectValidTrades(input)

    expect(input).toEqual(before)
  })
})

describe('summarizeTrades', () => {
  it('직거래를 빼면 최저가가 25.0억에서 40.5억으로 바뀜', () => {
    const window = { startYm: '202508', endYm: '202607' }
    const lowestIncludingDirect = Math.min(
      ...RAEMIAN_85.filter(
        (t) => t.deal_ym! >= window.startYm && t.deal_ym! <= window.endYm,
      ).map((t) => t.deal_amount),
    )

    expect(lowestIncludingDirect).toBe(250000)
    expect(summarizeTrades(RAEMIAN_85, window)).toEqual({
      validCount: 8,
      minPrice: 405000,
      maxPrice: 475000,
      excludedAmbiguous: 0,
      excludedCancel: 0,
      excludedDirect: 1,
    })
  })

  it('해제와 직거래를 각각 다른 칸에 셈', () => {
    expect(
      summarizeTrades(RAEMIAN_85, { startYm: '202503', endYm: '202607' }),
    ).toEqual({
      validCount: 15,
      minPrice: 405000,
      maxPrice: 475000,
      excludedAmbiguous: 0,
      excludedCancel: 1,
      excludedDirect: 2,
    })
  })

  it('기간의 시작 달과 끝 달을 포함함', () => {
    expect(
      summarizeTrades(RAEMIAN_85, { startYm: '202511', endYm: '202603' }),
    ).toMatchObject({ validCount: 4, minPrice: 405000, maxPrice: 475000 })
  })

  it('기간 밖의 거래는 어느 칸에도 세지 않음', () => {
    expect(
      summarizeTrades(RAEMIAN_85, { startYm: '202510', endYm: '202510' }),
    ).toEqual({
      validCount: 0,
      minPrice: null,
      maxPrice: null,
      excludedAmbiguous: 0,
      excludedCancel: 0,
      excludedDirect: 1,
    })
  })

  it('건수는 행 수가 아니라 trade_count 의 합', () => {
    const trades = [
      trade({ trade_count: 3, deal_amount: 90000 }),
      trade({ trade_count: 2, deal_amount: 95000 }),
      trade({ trade_count: 2, deal_amount: 80000, dealing_gbn: '직거래' }),
      trade({ trade_count: 3, deal_amount: 70000, dealing_gbn: '직거래' }),
      trade({ trade_count: 4, cdeal_type: 'O', ambiguous_cancel: true }),
      trade({ trade_count: 6, cdeal_type: 'O', ambiguous_cancel: true }),
      trade({ trade_count: 5, cdeal_type: 'O' }),
      trade({ trade_count: 1, cdeal_type: 'O' }),
    ]

    expect(summarizeTrades(trades, WINDOW)).toEqual({
      validCount: 5,
      minPrice: 90000,
      maxPrice: 95000,
      excludedAmbiguous: 10,
      excludedCancel: 6,
      excludedDirect: 5,
    })
  })

  it('기간 밖이면 분류와 무관하게 세지 않음', () => {
    const outside = ['202512', '202701'].flatMap((deal_ym) => [
      trade({ deal_ym }),
      trade({ deal_ym, dealing_gbn: '직거래' }),
      trade({ deal_ym, cdeal_type: 'O', ambiguous_cancel: true }),
      trade({ deal_ym, cdeal_type: 'O' }),
      trade({ deal_ym, is_current: false }),
    ])

    expect(summarizeTrades(outside, WINDOW)).toEqual({
      validCount: 0,
      minPrice: null,
      maxPrice: null,
      excludedAmbiguous: 0,
      excludedCancel: 0,
      excludedDirect: 0,
    })
  })

  it('정정된 행은 유효에도 제외 건수에도 넣지 않음', () => {
    const trades = [
      trade({ is_current: false, deal_amount: 10000 }),
      trade({ is_current: false, dealing_gbn: '직거래' }),
      trade({ deal_amount: 70000 }),
    ]

    expect(summarizeTrades(trades, WINDOW)).toEqual({
      validCount: 1,
      minPrice: 70000,
      maxPrice: 70000,
      excludedAmbiguous: 0,
      excludedCancel: 0,
      excludedDirect: 0,
    })
  })

  it('계약월이 없는 행은 건너뜀', () => {
    expect(summarizeTrades([trade({ deal_ym: null })], WINDOW).validCount).toBe(
      0,
    )
  })

  it('유효 거래가 없으면 두 가격이 모두 null', () => {
    expect(summarizeTrades([], WINDOW)).toEqual({
      validCount: 0,
      minPrice: null,
      maxPrice: null,
      excludedAmbiguous: 0,
      excludedCancel: 0,
      excludedDirect: 0,
    })
  })

  it('유효 거래만 다시 넣어도 건수와 두 가격이 같음', () => {
    const window = { startYm: '202503', endYm: '202607' }
    const all = summarizeTrades(RAEMIAN_85, window)
    const validOnly = summarizeTrades(selectValidTrades(RAEMIAN_85), window)

    expect(validOnly.validCount).toBe(all.validCount)
    expect(validOnly.minPrice).toBe(all.minPrice)
    expect(validOnly.maxPrice).toBe(all.maxPrice)
  })
})
