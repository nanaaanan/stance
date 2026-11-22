import type { Metadata } from 'next'
// 글꼴 CSS 를 globals.css 보다 먼저 불러옴
//   - dynamic-subset: 글자 범위별 조각 92개. 화면에 쓰인 글자의 조각만 내려받음
//   - 통짜 파일(2.0MB)을 next/font/local 로 쓰면 첫 방문마다 전부 내려받음
import 'pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css'
import './globals.css'
import { AuthBootstrap } from '@/components/auth-bootstrap'
import { SiteFooter } from '@/components/site-footer'
import { SiteHeader } from '@/components/site-header'

export const metadata: Metadata = {
  title: 'stance',
  description: '집을 찾는 순서를 거꾸로',
}

export default function RootLayout({ children }: LayoutProps<'/'>) {
  return (
    <html lang="ko">
      <body className="flex min-h-dvh flex-col">
        <AuthBootstrap />
        <SiteHeader />
        <main className="flex-1">{children}</main>
        <SiteFooter />
      </body>
    </html>
  )
}
