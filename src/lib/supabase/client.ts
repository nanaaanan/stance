// ============================================================
// 실행환경:
// 브라우저(클라이언트 컴포넌트)에서 쓰는 Supabase 연결 객체.
//
// 이 파일의 역할:
//   화면마다 "주소는 여기, 키는 이거" 를 반복하지 않기 위해
//   설정을 한 곳에 모아둔다. 앞으로는 이 파일에서 import 만 하면 된다.
//
// 목적:
//   클라이언트 컴포넌트에서 DB데이터 조회 및 auth상태 구독
//
// 서버에서 쓸 클라이언트는 별도로 만든다 (@supabase/ssr).
//   브라우저는 localStorage 로 세션을 들고 있고,
//   서버는 쿠키로 들고 있어야 해서 만드는 방식이 다르다.
// ============================================================

// 싱글톤 패턴 사용시 사용자 세션간 오염 발생 가능성 있음.
// 아래 패키지가 제공하는 함수 사용해서 독립 클라이언트 인스턴스로 반환.
import { createBrowserClient } from '@supabase/ssr'

// process.env 는 .env 파일의 값을 읽어오는 통로
// NEXT_PUBLIC_ 접두사가 붙은 것만 브라우저 코드에서 읽을 수 있음
// (접두사가 없으면 서버에서만 읽힘 - service_role 키를 지키는 장치)
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
// 끝의 ! 는 TypeScript 에게 "이 값은 절대 비어있지 않다"고 알려주는 표시
// (실제로 비어 있으면 런타임에 에러가 나므로, 배포 환경변수 누락을 빨리 알아챌 수 있음)

export const supabase = createBrowserClient(supabaseUrl, supabaseAnonKey)
