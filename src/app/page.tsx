import { AuthBootstrap } from '@/components/auth-bootstrap'
import { SmokeTest } from '@/components/smoke-test'

export default function Home() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-2xl flex-col justify-center gap-4 p-6">
      <h1 className="text-2xl font-bold">
        예산을 먼저 정하고, 그 다음에 집을 봅니다.
      </h1>
      <p className="text-sm to-neutral-500">
        서울 25개 구 아파트 실거래 기반, 국토교통부 공공데이터
      </p>
      <p className="text-xs to-neutral-400">마지막 데이터 갱신 : - (수집 전)</p>
      <AuthBootstrap />
      <SmokeTest />
    </main>
  )
}
