'use client'

import { useEffect } from 'react'
import { supabase } from '@/lib/supabase/client'

// 방문 즉시 익명 세션 발급. 로그인 버튼을 먼저 요구하지 않음
//   - 나중에 linkIdentity('google') 로 승격해도 user id 유지
//   - 익명 상태에서 쌓인 데이터가 승격 뒤에도 그대로 남음
//
// 루트 레이아웃에 합치지 말 것
//   - 레이아웃에 'use client' 를 붙이면 헤더, 푸터까지 브라우저 번들에 포함

// 화면 로드 1회당 발급 시도 1회
//   - 개발 모드의 StrictMode 는 effect 를 두 번 실행
//   - 두 실행이 모두 "세션 없음" 을 읽으면 익명 계정이 2개 생성됨
let started = false

export function AuthBootstrap() {
  useEffect(() => {
    if (started) {
      return
    }
    started = true
    ;(async () => {
      let result = await supabase.auth.getSession()

      // 발급 조건은 "오류 없이 세션 없음" 하나. 오류를 "세션 없음" 으로 처리하지 말 것
      //   - 통신 오류로 갱신 실패: SDK 는 쿠키를 유지한 채 session: null 과 error 를 반환
      //   - 갱신 도중 다른 탭이 토큰 교체: 저장소에 유효한 세션이 있어도 session: null 과 error 를 반환
      //   - 위 두 경우에 새로 발급하면 기존 방문자의 세션을 덮어씀
      //   - 갱신 토큰이 거절된 경우는 SDK 가 세션을 삭제. 재조회하면 오류 없이 세션 없음
      if (!result.data.session && result.error) {
        result = await supabase.auth.getSession()
      }

      if (result.data.session) {
        return
      }

      if (result.error) {
        console.error('세션 확인 실패:', result.error.message)
        return
      }

      const { error } = await supabase.auth.signInAnonymously()

      if (error) {
        // error.message 를 화면에 쓰지 말 것. 내부 용어가 사용자에게 노출됨
        console.error('익명 세션 발급 실패:', error.message)
      }
    })()
  }, [])

  return null
}
