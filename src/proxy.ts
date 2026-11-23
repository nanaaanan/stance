import type { NextRequest } from 'next/server'
import { updateSession } from '@/lib/supabase/proxy'

export async function proxy(request: NextRequest) {
  return updateSession(request)
}

export const config = {
  // matcher 를 변수나 함수로 빼지 말 것
  //   - 빌드 때 정적으로 분석되는 값. 상수가 아니면 무시되고 모든 요청에서 proxy 실행
  matcher: [
    '/((?!_next/static|_next/image|favicon\\.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)',
  ],
}
