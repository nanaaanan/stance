import { createBrowserClient } from '@supabase/ssr'

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!

// 브라우저 전용. 서버 컴포넌트와 Route Handler 에서 쓰지 말 것
//   - 세션은 쿠키에 저장. 이 클라이언트는 document.cookie 로 읽고 씀
//   - 서버는 요청과 응답의 쿠키를 다뤄야 해서 클라이언트를 따로 만듦
//   - 이 함수는 브라우저에서 인스턴스 하나를 재사용. 서버에서 공유하면 사용자 간 세션이 섞임
export const supabase = createBrowserClient(supabaseUrl, supabaseAnonKey)
