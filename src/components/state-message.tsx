import type { ReactNode } from 'react'

type StateMessageProps = {
  title: string
  description: string
  heading?: 'h1' | 'h2'
  children?: ReactNode
}

// title, description 에 기본값을 두지 말 것
//   - 내용이 없는 이유를 화면마다 직접 쓰게 하는 장치
//   - 기본 문구가 생기면 원인이 다른 결측이 같은 문장으로 표시됨
export function StateMessage({
  title,
  description,
  heading: Heading = 'h2',
  children,
}: StateMessageProps) {
  return (
    <div className="mx-auto flex w-full max-w-md flex-col items-center gap-2 px-4 py-16 text-center">
      <Heading className="text-lg font-semibold">{title}</Heading>
      <p className="text-sm text-muted-foreground">{description}</p>
      {children ? <div className="mt-4">{children}</div> : null}
    </div>
  )
}
