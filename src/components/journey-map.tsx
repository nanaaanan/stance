import { ArrowUp, ChevronRight } from 'lucide-react'
import journey from '../../config/journey.json'

// 단계 이름과 순서를 이 파일에 적지 말 것
//   - 출처는 config/journey.json 하나
//   - 예산 결과 화면, 저장 목록 화면의 지도가 같은 파일을 읽음
//   - 번호는 배열 위치에서 계산. JSON 에 번호 필드 없음
const steps = journey.steps.map((step, index) => ({
  ...step,
  order: index + 1,
}))
const stanceSteps = steps.filter((step) => step.byStance)
const outsideSteps = steps.filter((step) => !step.byStance)

const CAPTIONS: Record<string, string | undefined> = {
  viewpoint: '왜 사나',
  budget: '얼마까지 가능한가',
  search: '그 안의 단지',
  visit: '가서 본다',
}

const USUAL_START_KEY = 'search'

export function JourneyMap() {
  return (
    <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
      <section
        aria-labelledby="journey-stance"
        className="rounded-lg border-2 border-primary p-5 md:p-6"
      >
        <h2 id="journey-stance" className="text-sm font-semibold">
          stance 가 함께 갑니다
        </h2>
        {/* role="list" 를 지우지 말 것
            - Tailwind 기본 스타일이 list-style 을 없앰
            - Safari 의 VoiceOver 는 list-style 이 없는 목록을 목록으로 읽지 않음 */}
        <ol role="list" className="mt-5 grid gap-4 md:grid-cols-4 md:gap-3">
          {stanceSteps.map((step, index) => (
            <li
              key={step.key}
              className="relative flex flex-wrap items-center gap-x-3 gap-y-1 md:flex-col md:flex-nowrap md:text-center"
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground">
                {step.order}
              </span>
              <span className="font-semibold">{step.name}</span>
              <span className="text-sm text-muted-foreground">
                {CAPTIONS[step.key]}
              </span>
              {index < stanceSteps.length - 1 ? (
                <ChevronRight
                  aria-hidden
                  className="absolute top-1.5 -right-1.5 hidden size-4 translate-x-1/2 text-muted-foreground md:block"
                />
              ) : null}
              {step.key === USUAL_START_KEY ? (
                <p className="flex basis-full items-center gap-1 pl-10 text-sm font-medium md:mt-2 md:basis-auto md:flex-col md:pl-0 md:whitespace-nowrap">
                  <ArrowUp aria-hidden className="size-4 shrink-0" />
                  대부분의 앱은 여기서 시작합니다
                </p>
              ) : null}
            </li>
          ))}
        </ol>
      </section>

      <section
        aria-labelledby="journey-outside"
        className="rounded-lg border border-dashed border-muted-foreground/40 p-5 md:p-6"
      >
        <h2
          id="journey-outside"
          className="text-sm font-semibold text-muted-foreground"
        >
          여기부터는
        </h2>
        <ol
          role="list"
          className="mt-5 flex flex-wrap items-center gap-x-1 gap-y-3"
        >
          {outsideSteps.map((step, index) => (
            <li key={step.key} className="flex items-center gap-1">
              {index > 0 ? (
                <ChevronRight
                  aria-hidden
                  className="size-4 text-muted-foreground"
                />
              ) : null}
              <span className="flex size-6 shrink-0 items-center justify-center rounded-full border text-xs text-muted-foreground">
                {step.order}
              </span>
              <span className="text-sm">{step.name}</span>
            </li>
          ))}
        </ol>
        <p className="mt-5 text-sm text-muted-foreground">
          중개사, 법무사, 세무사
        </p>
        <p className="text-sm text-muted-foreground">기한은 법이 정합니다</p>
      </section>
    </div>
  )
}
