export function SiteFooter() {
  return (
    <footer className="border-t">
      <div className="mx-auto w-full max-w-5xl space-y-1 px-4 py-6 text-xs text-muted-foreground">
        <p>데이터 출처: 국토교통부 아파트 실거래가 (공공데이터포털)</p>
        {/* 면책 문구는 확정 문구. 표현 변경 금지 */}
        <p>
          본 정보는 통계 기반 시뮬레이션 결과로 어떠한 투자 자문도 대체할 수
          없습니다
        </p>
      </div>
    </footer>
  )
}
