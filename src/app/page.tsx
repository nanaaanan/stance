import Link from 'next/link'
import { JourneyMap } from '@/components/journey-map'
import { buttonVariants } from '@/components/ui/button'

export default function Home() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-10 px-4 py-12 md:py-16">
      <div className="flex flex-col gap-3 text-center">
        <h1 className="text-3xl font-bold md:text-4xl">
          집을 찾는 순서를 뒤집습니다
        </h1>
        <p className="text-muted-foreground">
          예산을 먼저 정하고, 그 다음에 집을 봅니다.
        </p>
      </div>

      <JourneyMap />

      <div className="flex justify-center">
        {/* prefetch 끔
            - 화면이 아직 없는 주소. 켜 두면 운영 환경에서 미리 불러오기 요청이 404
            - 온보딩 화면을 추가하는 작업에서 이 속성 제거 */}
        <Link
          href="/onboarding"
          prefetch={false}
          className={buttonVariants({ size: 'lg' })}
        >
          시작하기
        </Link>
      </div>
    </div>
  )
}
