import { Skeleton } from '@/components/ui/skeleton'

export function LoadingState() {
  return (
    <div
      role="status"
      className="mx-auto flex w-full max-w-2xl flex-col gap-3 px-4 py-16"
    >
      <span className="sr-only">불러오는 중</span>
      <Skeleton className="h-7 w-2/3" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-1/2" />
    </div>
  )
}
