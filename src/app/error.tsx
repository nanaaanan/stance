'use client'

import { useEffect } from 'react'
import { StateMessage } from '@/components/state-message'
import { Button } from '@/components/ui/button'

type ErrorScreenProps = {
  error: Error & { digest?: string }
  retry: () => void
}

// error.message 를 화면에 쓰지 말 것
//   - 서버에서 난 오류는 운영 환경에서 일반 문구로 바뀌어 옴
//   - 브라우저에서 난 오류는 원문 그대로 옴. 내부 용어가 사용자에게 노출됨
export default function ErrorScreen({ error, retry }: ErrorScreenProps) {
  useEffect(() => {
    console.error(error)
  }, [error])

  return (
    <div role="alert">
      <StateMessage
        heading="h1"
        title="화면을 불러오지 못했습니다"
        description="앱에서 문제가 생겼습니다. 다시 시도해도 같으면 잠시 뒤에 열어 주세요."
      >
        <Button onClick={() => retry()}>다시 시도</Button>
      </StateMessage>
    </div>
  )
}
