import Link from 'next/link'
import { StateMessage } from '@/components/state-message'
import { buttonVariants } from '@/components/ui/button'

// 설명 문구의 "아직 만들고 있는 화면"
//   - 헤더의 검색, 저장한 단지 링크가 지금 이 화면으로 옴 (site-header.tsx)
//   - 첫 화면의 시작하기 버튼도 같음 (app/page.tsx)
//   - 세 화면이 모두 생기면 이 부분 삭제
export default function NotFound() {
  return (
    <StateMessage
      heading="h1"
      title="이 주소에는 화면이 없습니다"
      description="주소가 맞지 않거나 아직 만들고 있는 화면입니다."
    >
      <Link href="/" className={buttonVariants()}>
        처음으로
      </Link>
    </StateMessage>
  )
}
