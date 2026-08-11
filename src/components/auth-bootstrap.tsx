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
// 우선 UUID 를 화면에 그대로 출력.(배포 환경까지 연결됐다는 증거 확인용)
//   앱 셸 작업 시 화면 출력은 제거하고 훅으로 옮길 예정
// ============================================================

'use client'
// 이 지시어가 있어야 브라우저에서 실행되는 컴포넌트가 됨
//   next.js app router 는 기본이 서버 컴포넌트라서, useEffect/useState 같은
//   브라우저 기능을 쓰려면 반드시 이 한 줄이 필요.

import { useEffect, useState } from 'react'
import { supabase } from '@/lib/supabase/client'

export function AuthBootstrap() {
  // uid: 화면에 표시할 사용자 UUID. 아직 모를 때는 null
  const [uid, setUid] = useState<string | null>(null)
  // err: 실패했을 때 이유를 화면에 띄우기 위한 상태
  //      콘솔만 찍으면 배포 환경에서 원인 못 찾음.
  const [err, setErr] = useState<string | null>(null)

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
        setUid(session.user.id)
        return // 있으면 새로 만들지 않는다. 만들면 기존 데이터와 연결이 끊김
      }

      // 2) 없으면 익명 세션 발급
      const { data, error } = await supabase.auth.signInAnonymously()

      if (error) {
        setErr(error.message)
        return
      }

      setUid(data.user?.id ?? null)
    })()
  }, [])

  if (err) {
    return <p className="text-xs text-red-500">세션 오류: {err}</p>
  }

  return (
    <p className="text-xs text-neutral-400">session: {uid ?? '연결 중...'}</p>
  )
}
