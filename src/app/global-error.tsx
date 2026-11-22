'use client'

import { useEffect } from 'react'
import 'pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css'
import './globals.css'
import { StateMessage } from '@/components/state-message'
import { Button } from '@/components/ui/button'

type GlobalErrorProps = {
  error: Error & { digest?: string }
  retry: () => void
}

// 루트 레이아웃이 실패했을 때 레이아웃 대신 그려지는 화면
//   - html, body 를 직접 그림
//   - 전역 CSS 가 따라오지 않음. 글꼴과 globals.css 를 여기서 다시 불러옴
//   - 헤더, 푸터를 넣지 말 것. 실패한 레이아웃의 컴포넌트를 다시 실행하면 같은 오류 재발
export default function GlobalError({ error, retry }: GlobalErrorProps) {
  useEffect(() => {
    console.error(error)
  }, [error])

  return (
    <html lang="ko">
      <body className="flex min-h-dvh flex-col justify-center">
        <title>stance</title>
        <div role="alert">
          <StateMessage
            heading="h1"
            title="화면을 불러오지 못했습니다"
            description="앱에서 문제가 생겼습니다. 다시 시도해도 같으면 잠시 뒤에 열어 주세요."
          >
            <Button onClick={() => retry()}>다시 시도</Button>
          </StateMessage>
        </div>
      </body>
    </html>
  )
}
