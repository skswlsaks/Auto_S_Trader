# Model Arena 웹 (Next.js)

Next.js 16 정적 export 앱. S3 `site/`에 올라가고 CloudFront + Cognito(Lambda@Edge)가 보호한다. 데이터는 런타임에 `/data/*.json`을 읽으므로 새 run·실험이 올라와도 다시 빌드할 필요가 없다.

| 경로 | 화면 |
|---|---|
| `/` | 개요: 현재 구간 판정, 조건 실험 요약, 재현성 |
| `/runs/` | 모델 순위: forest plot 열, 지표 묶음(핵심·수익의 질·위험·안정성·노출·유의성) 전환, 비교 선택 |
| `/run/?id=` | run 상세: 누적·낙폭(확대), 지표 탭, 분포, 월별, 재현 정보 |
| `/compare/?ids=` | 비교: 지표 소형 다중 차트, 곡선, 모델 지도, 월별 손익 |
| `/studies/`, `/studies/view/?id=` | 조건 실험 격자(히트맵), 셀 상세, 셀 비교, 다중 시험 보정 |
| `/lab/` | 조합 실험실: 모델·조건을 골라 브라우저에서 바로 계산, 무작위 선택 대비 백분위 |
| `/lab/?mode=search` | 최적 조합 찾기: 모델 × 조건(최대 2개) 전체 탐색. 앞 기간에서 순위·보정 q, 뒤 기간과 잠금 시험에서 같은 조합 확인 |

- 차트: Apache ECharts(필요한 모듈만 등록, `src/components/charts`), 표: TanStack Table, 기본 요소: Radix(`src/components/ui`).
- 지표 정의는 `src/lib/metric-defs.ts` 한 곳에 있다(이름·좋은 방향·설명).
- 실험실 계산 엔진 `src/lib/engine.ts`는 Python 셀 집계와 같은 정의이며 `tests/engine.test.ts`가 일치를 확인한다.
- 조합 탐색 `src/lib/search.ts`는 `arena/search.py`와 같은 알고리즘이며 `tests/search.test.ts`가 공유 고정 데이터(`tests/fixtures/search.json`)로 일치를 확인한다. Python 쪽을 바꾸면 `uv run python -m arena.tests.test_search --write`로 고정 데이터를 다시 만든다.

```sh
cd web && npm ci
uv run --directory .. python -m arena build   # ../outputs/arena/site/data 생성 (public/data가 그곳을 가리킴)
npm run dev                                    # http://localhost:3000
npx tsc --noEmit && node --test tests/engine.test.ts tests/search.test.ts && npm run build   # web/out
```

로컬 `public/data`는 `../../outputs/arena/site/data`로 가는 심볼릭 링크이며 Git에 넣지 않는다.
