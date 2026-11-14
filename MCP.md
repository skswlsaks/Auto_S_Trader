# Model Arena MCP — 팀원 AI로 결과 조회·제출

팀원이 쓰는 AI(Claude Code, Claude Desktop, Codex, Cursor 등)가 Model Arena를 직접 다룰 수 있게 하는 MCP 서버다. 순위·실험 격자 조회, 모델 × 조건 조합 계산, 표준 run 작성, 제출, 재현 확인을 도구로 제공한다. 서버는 각자 컴퓨터에서 실행되고(stdio), 공유 저장소(S3)에는 **각자의 AWS 자격증명**으로 접근한다.

## 1. 준비 (한 번)

1. 저장소 받기: `git clone https://github.com/skswlsaks/Auto_S_Trader && cd Auto_S_Trader`
2. [uv](https://docs.astral.sh/uv/) 설치 후 `uv sync`
3. AWS CLI v2와 자격증명. 관리자가 IAM 사용자를 `s-trader-arena-team` 그룹에 넣어야 한다(`infra/arena`의 `team_users`). 권한은 `runs/`·`studies/`·`site/`·`conditions/` 읽기·쓰기이고 삭제는 없다.
4. 공유 저장소 주소를 환경변수로 둔다. 값은 관리자에게 받는다(`terraform -chdir=infra/arena output -raw arena_remote`).
   ```sh
   echo 'export ARENA_REMOTE=s3://<arena 버킷>' >> ~/.zshrc
   ```
5. (선택) `gh auth login`. 제출 직후 대시보드를 즉시 갱신할 수 있다. 없으면 30분 안에 자동 갱신된다.

## 2. AI 도구에 연결

| 도구 | 방법 |
|---|---|
| **Claude Code** | 저장소 폴더에서 `claude`를 실행하면 루트의 `.mcp.json`을 읽고 `model-arena` 서버 사용을 물어본다. 승인하면 끝. 다른 폴더에서도 쓰려면: `claude mcp add model-arena -s user -e ARENA_REMOTE=$ARENA_REMOTE -- uv run --directory /절대/경로/Auto_S_Trader --quiet python -m arena.mcp_server` |
| **Claude Desktop** | 설정 → 개발자 → 설정 편집(`claude_desktop_config.json`)에 아래 JSON 추가 후 재시작 |
| **Codex CLI** | `~/.codex/config.toml`에 아래 TOML 추가 |
| **Cursor** | `.cursor/mcp.json`에 Claude Desktop과 같은 JSON |

```json
{
  "mcpServers": {
    "model-arena": {
      "command": "/절대/경로/uv",
      "args": ["run", "--directory", "/절대/경로/Auto_S_Trader", "--quiet", "python", "-m", "arena.mcp_server"],
      "env": { "ARENA_REMOTE": "s3://<arena 버킷>", "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin" }
    }
  }
}
```

```toml
[mcp_servers.model-arena]
command = "uv"
args = ["run", "--directory", "/절대/경로/Auto_S_Trader", "--quiet", "python", "-m", "arena.mcp_server"]
env = { ARENA_REMOTE = "s3://<arena 버킷>" }
```

데스크톱 앱은 셸 환경을 물려받지 않으므로 `uv`는 절대 경로(`which uv`)로, `PATH`에는 `aws`가 있는 폴더를 넣는다.

연결 확인: AI에게 "Model Arena 개요 보여줘"라고 하면 `arena_overview`가 호출된다. 직접 확인하려면 `uv run python -m unittest arena.tests.test_mcp`를 실행한다.

## 3. 도구

| 분류 | 도구 | 하는 일 |
|---|---|---|
| 조회 | `arena_overview` | 트랙·구간별 run 수, 실험 목록, 데이터 생성 시각 |
| | `arena_list_runs` | 순위(구간·종류·정렬 기준), 핵심 지표·보정 q·무작위 대비 백분위·재현 상태 |
| | `arena_get_run` / `arena_compare_runs` | run 상세(지표 전체·재현 정보·모델 파일) / 여러 run 나란히 |
| | `arena_list_studies` / `arena_study_grid` | 조건 실험 목록 / 행×열 격자(n, 평균, 95% 구간, q, 유의 여부) |
| 조합 | `arena_list_lab_sources` / `arena_lab_query` | 모델 × 조건을 골라 즉시 계산: 조건 없는 같은 모델·같은 수 무작위 선택과 비교, 그룹별 결과 |
| 기준 | `arena_schema` / `arena_metric_definitions` | 제출 형식(run.json·trades.json·study.json)과 지표 정의 |
| 작성 | `arena_create_run` / `arena_validate_run` | 거래 목록(리스트·JSON·CSV·parquet)으로 표준 run 생성, 재현 정보 자동 기록, 검증·지표 미리보기 |
| 제출 | `arena_submit_run` | 공유 저장소에 업로드(run_id 불변, 같은 결과에 증거 추가는 enrich) |
| | `arena_preview_study` / `arena_submit_study` | 실험 폴더 해석·격자 미리보기 / 업로드(폴더는 수정하지 않음) |
| | `arena_reproduce_run` / `arena_request_refresh` | 재현 확인(입력 해시·모델 재생·커밋 재실행) / 대시보드 즉시 갱신 |

프롬프트 `submit_results`(run 또는 study)는 제출 절차를 단계별로 안내한다. 리소스는 `arena://schema`, `arena://metrics`다.

## 4. 이렇게 요청하면 된다

- "dev-late 구간 순위를 평균 순수익 기준으로 보여주고, 무작위 대비 95% 이상인 모델이 있는지 알려줘"
- "entry-gate 실험에서 강사 추천 종목만, 진입 방식 × 기간 격자를 보여줘. 보정 후 유의한 칸이 있어?"
- "뒤틀림 분류기 진입에 전날 코스닥 20일선 위 조건을 걸면 결과가 어떻게 바뀌어? 요일별로도 나눠 봐"
- "outputs/model-research/new-exp/20261120-v1 실험을 미리보기하고 이상 없으면 제출해줘"
- "내 모델 거래 목록 results/trades.parquet으로 dev-late 구간 run을 만들고 검증 후 제출해줘"

## 5. 규칙 (서버 설명에도 들어 있음)

- 지표는 플랫폼이 계산한다. 같은 트랙·구간 안에서만 비교한다.
- 수익률은 비용 후 소수(0.01 = 1%)이고, 미청산 거래도 분모에 남는다.
- 잠금 시험(holdout) 데이터로 조건이나 임계값을 고르지 않는다.
- 조합 실험실 결과는 탐색용이다(보정 전). 결정은 실험 격자의 q값과 새 기간 시험으로 한다.
