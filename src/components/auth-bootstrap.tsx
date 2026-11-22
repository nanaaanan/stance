// ============================================================
// 익명 세션 부트스트랩 - 앱 시작 시 딱 한번 해두는 준비작업
//
// 역할: 페이지가 열릴 때 딱 한 번, 방문자가 세션이 있는지 확인하고,
//       없으면 조용히 익명 세션을 발급.
//
// 왜 로그인 버튼이 아니라 자동 발급인가:
//   로그인부터 해야 기능들을 볼 수 있으면 사용자 이탈 가능성이 높다.
//   익명으로 먼저 쓰게 하고, 나중에 "기기를 바꿔도 유지하려면" 시점에
//   linkIdentity('google') 로 승격시킨다. 이때 user id 가 그대로 유지되므로
//   지금까지 쌓인 데이터가 그대로 따라온다.
//
// 루트 레이아웃에 합치지 말 것
//   - 레이아웃에 'use client' 를 붙이면 머리말, 바닥글까지 브라우저 번들에 포함
//   - 브라우저 전용 동작만 이 컴포넌트로 분리해 레이아웃에 1회 장착
// ============================================================

'use client'
// 이 지시어가 있어야 브라우저에서 실행되는 컴포넌트가 됨
//   next.js app router 는 기본이 서버 컴포넌트라서, useEffect/useState 같은
//   브라우저 기능을 쓰려면 반드시 이 한 줄이 필요.

import { useEffect } from 'react'
import { supabase } from '@/lib/supabase/client'

export function AuthBootstrap() {
  useEffect(() => {
    // useEffect 는 "화면이 그려진 뒤에 실행할 일"을 적는 곳
    // 두 번째 인자 [] 는 "처음 한 번만 실행" 이라는 뜻

    // async 함수를 즉시 실행하는 형태.
    // useEffect 자체는 async 를 붙일 수 없어서 안쪽에 따로 만든다.
    ;(async () => {
      // 1) 이미 세션이 있는가? (재방문자 / 새로고침)
      const {
        data: { session },
      } = await supabase.auth.getSession()

      if (session) {
        return // 있으면 새로 만들지 않는다. 만들면 기존 데이터와 연결이 끊김
      }

      // 2) 없으면 익명 세션 발급
      const { error } = await supabase.auth.signInAnonymously()

      if (error) {
        // 오류 원문(error.message)은 화면에 내보내지 않음
        //   - 내부 용어가 사용자에게 그대로 노출됨
        //   - 수집 창구는 콘솔 하나. 오류 추적 도구 도입 시 이 줄을 교체
        console.error('익명 세션 발급 실패:', error.message)
      }
    })()
  }, [])

  return null
}
