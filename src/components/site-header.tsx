import Link from 'next/link'

const NAV_ITEMS = [
  { href: '/search', label: '검색' },
  { href: '/my', label: '저장한 단지' },
] as const

export function SiteHeader() {
  return (
    <header className="border-b">
      <div className="mx-auto flex h-14 w-full max-w-5xl items-center justify-between px-4">
        <Link href="/" className="text-base font-semibold">
          stance
        </Link>
        <nav aria-label="주요 메뉴">
          <ul className="flex items-center gap-4 text-sm">
            {NAV_ITEMS.map((item) => (
              <li key={item.href}>
                {/* prefetch 끔
                    - 화면이 아직 없는 주소. 켜 두면 운영 환경에서 미리 불러오기 요청이 404
                    - 해당 화면을 추가하는 작업에서 이 속성 제거 */}
                <Link
                  href={item.href}
                  prefetch={false}
                  className="text-muted-foreground hover:text-foreground"
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </header>
  )
}
