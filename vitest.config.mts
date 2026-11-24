import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // 대상은 src/lib/domain 의 순수 함수
    //   - DOM 을 쓰지 않아 기본 환경(node)으로 실행
    //   - include 가 없으면 node_modules 와 .git 을 뺀 전체를 탐색
    include: ['src/lib/domain/**/*.test.ts'],
  },
})
