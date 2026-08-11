// ============================================================
// RLS 스모크 테스트 (임시: 스키마 작업시 삭제 예정, 최소 확인절차)
//
// 확인하려는 것: 브라우저 - 인증 - DB쓰기 - 읽기 - 권한차단 (이 왕복 전체가 실제로 작동하는지)
//   1) 내가 넣은 행을 읽을 수 있는가               (select 정책)
//   2) 익명세션으로 DB에 행을 넣을 수 있는가        (insert 정책)
//   3) 다른 세션(시크릿 창)에서는 안 보이는가        (권한 차단)
//
// 3번 확인시 "임장 기록은 본인만 볼 수 있다"가 코드가 아니라
// 데이터베이스 수준에서 보장된다는 뜻이다.
// ============================================================

'use client'

import { useState } from 'react'
import { supabase } from '@/lib/supabase/client'

// DB에서 돌려받을 행의 모양을 실제 컬럼과 맞춤
type SmokeRow = {
  id: string
  note: string | null
  created_at: string
}

export function SmokeTest() {
  const [rows, setRows] = useState<SmokeRow[]>([])
  const [msg, setMsg] = useState<string>('')

  // 1) 목록 조회
  // RLS 덕분에 "내 행만" 이라는 조건을 코드에 쓰지 않아도 됨
  async function load() {
    const { data, error } = await supabase
      .from('_smoke')
      .select('id, note, created_at')
      .order('created_at', { ascending: false })

    if (error) {
      setMsg(`조회 실패: ${error.message}`)
      return
    }
    setRows(data ?? [])
    setMsg(`조회 성공: ${data?.length ?? 0}건`)
  }

  // 2) 행 추가
  // user_id 를 코드에서 넣지 않는 것에 주의
  // 테이블의 default auth.uid() 가 서버에서 자동으로 채움
  // 클라이언트가 user_id 를 정하게 두면 위조 가능
  async function insert() {
    const { error } = await supabase
      .from('_smoke')
      .insert({ note: `hello ${new Date().toLocaleTimeString()}` })

    if (error) {
      setMsg(`추가 실패: ${error.message}`)
      return
    }
    setMsg('추가 성공')
    await load() // 넣은 뒤 바로 다시 읽어서 화면 갱신
  }

  return (
    <div className="mt-6 rounded border border-neutral-300 p-4 text-sm">
      <p className="mb-2 font-semibold">RLS 스모크 테스트 (임시)</p>

      <div className="flex gap-2">
        <button onClick={load} className="rounded border px-3 py-1">
          목록 조회
        </button>
        <button
          onClick={insert}
          className="rounded bg-neutral-900 px-3 py-1 text-white"
        >
          행 추가
        </button>
      </div>

      {msg && <p className="mt-2 text-neutral-600">{msg}</p>}

      <ul className="mt-2 list-disc pl-5 text-neutral-700">
        {rows.map((r) => (
          <li key={r.id}>
            {r.note} — {new Date(r.created_at).toLocaleString()}
          </li>
        ))}
      </ul>
    </div>
  )
}
