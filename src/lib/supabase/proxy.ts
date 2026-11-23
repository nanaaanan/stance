import { createServerClient } from '@supabase/ssr'
import { NextResponse, type NextRequest } from 'next/server'

// 이 함수에 접근 차단을 넣지 말 것
//   - 익명 세션은 화면이 뜬 뒤 브라우저에서 발급됨
//   - 첫 방문 요청은 항상 세션 없이 도착
export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request })

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll()
        },
        // 아래 세 단계를 줄이지 말 것
        //   - request 쪽 반영: 같은 요청을 이어받는 서버 컴포넌트가 새 토큰을 읽음
        //   - response 재생성: 바뀐 request 쿠키를 뒤 단계로 전달
        //   - headers 반영: 캐시 금지 헤더
        //   - 헤더가 빠지면 CDN 이 Set-Cookie 응답을 캐시해 다른 방문자에게 전달 가능
        setAll(cookiesToSet, headers) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          )
          response = NextResponse.next({ request })
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          )
          Object.entries(headers).forEach(([key, value]) =>
            response.headers.set(key, value),
          )
        },
      },
    },
  )

  // getClaims 를 getSession 으로 바꾸지 말 것
  //   - getSession 은 토큰 서명을 검증하지 않음
  // try/catch 를 지우지 말 것
  //   - 쿠키 안의 토큰이 깨져 있으면 SDK 가 AuthError 가 아닌 예외를 던짐
  //   - 예외가 밖으로 나가면 그 방문자는 쿠키를 지울 때까지 모든 화면에서 500
  //   - 오류 원문에 토큰 조각이 섞일 수 있어 이름만 기록
  try {
    await supabase.auth.getClaims()
  } catch (error) {
    console.error(
      '세션 갱신 중 예외:',
      error instanceof Error ? error.name : typeof error,
    )
  }

  return response
}
