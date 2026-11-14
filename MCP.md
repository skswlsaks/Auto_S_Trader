# Model Arena MCP — 팀원 AI로 결과 조회·조합·제출

팀원이 쓰는 AI(Claude Code, Claude Desktop, Codex, Cursor 등)가 Model Arena를 직접 다루게 하는 MCP 서버다. 서버는 **Amazon Bedrock AgentCore**에서 실행되고, 팀원은 **Cognito 계정**(대시보드와 같은 계정)으로 로그인한다. AWS 키도, 저장소 전체도 필요 없다.

```
AI 도구 ── stdio ── arena_mcp_proxy.py ── HTTPS + Cognito ID 토큰 ──▶ AgentCore Runtime (MCP)
            (내 컴퓨터, 표준 라이브러리만)                          JWT 검증 → 서버 → S3 공유 저장소
```

- 제출자는 로그인한 사람의 이메일로 기록된다. 서버가 토큰에서 읽으므로 다른 사람 이름으로 낼 수 없다.
- 서버는 실행 역할 권한으로만 S3에 접근한다(`runs/`, `studies/`, `drafts/` 쓰기, 삭제 없음).

## 팀원: 연결하기 (5분)

1. 관리자에게 두 가지를 받는다: **Cognito 초대**(이메일, 임시 비밀번호)와 **설정 문자열**.
2. 프록시를 받는다. Python 3.9 이상만 있으면 된다.
   ```sh
   curl -fsSLo ~/arena_mcp_proxy.py https://raw.githubusercontent.com/skswlsaks/Auto_S_Trader/main/tools/arena_mcp_proxy.py
   ```
   저장소를 clone했다면 `tools/arena_mcp_proxy.py`를 그대로 써도 된다.
3. 설정, 로그인, 연결 확인을 한다.
   ```sh
   python3 ~/arena_mcp_proxy.py setup <설정 문자열>
   python3 ~/arena_mcp_proxy.py login     # 브라우저가 열림. 첫 로그인 때 비밀번호 변경
   python3 ~/arena_mcp_proxy.py test      # 서버 정보, 도구 수, 내 이메일
   ```
   토큰은 `~/.config/model-arena/`(권한 600)에 저장되고 30일 동안 자동 갱신된다. 만료되면 `login`을 다시 한다.
4. AI 도구에 등록한다(아래 표).

| 도구 | 등록 방법 |
|---|---|
| **Claude Code** | `claude mcp add model-arena -s user -- python3 ~/arena_mcp_proxy.py`. 저장소 안에서는 `.mcp.json`이 자동으로 잡힌다 |
| **Claude Desktop** | 설정 → 개발자 → 설정 편집에 아래 JSON 추가 후 재시작 |
| **Codex CLI** | `~/.codex/config.toml`에 아래 TOML 추가 |
| **Cursor** | `~/.cursor/mcp.json`에 아래 JSON |

```json
{ "mcpServers": { "model-arena": { "command": "python3", "args": ["/Users/<나>/arena_mcp_proxy.py"] } } }
```

```toml
[mcp_servers.model-arena]
command = "python3"
args = ["/Users/<나>/arena_mcp_proxy.py"]
```

확인: AI에게 "Model Arena에서 내가 누구로 로그인돼 있어?"라고 물으면 `arena_whoami`가 이메일을 돌려준다.

## 도구 (18개)

| 분류 | 도구 | 하는 일 |
|---|---|---|
| 조회 | `arena_overview`, `arena_list_runs`, `arena_get_run`, `arena_compare_runs` | 트랙·구간, 순위(보정 q, 무작위 대비 백분위, 재현 상태), run 상세, 비교 |
| | `arena_list_studies`, `arena_study_grid` | 조건 실험 목록, 행×열 격자(n, 평균, 95% 구간, q, 유의 여부) |
| 조합 | `arena_list_lab_sources`, `arena_lab_query` | 모델 × 조건을 골라 즉시 계산. 조건 없는 같은 모델, 같은 수 무작위 선택과 비교. 그룹별 결과 |
| 기준 | `arena_schema`, `arena_metric_definitions` | 제출 형식과 지표 정의 |
| 작성 | `arena_create_run`, `arena_validate_run` | 거래 목록이나 내 컴퓨터의 거래 파일(JSON/CSV/parquet)로 run 초안 작성·검증. 모델 파일·입력 해시·git 커밋 자동 기록 |
| 제출 | `arena_submit_run`, `arena_preview_study`, `arena_submit_study` | run 제출(불변), 실험 폴더 미리보기·제출 |
| 기타 | `arena_whoami`, `arena_request_refresh`, `arena_reproduce_run` | 로그인 확인, 대시보드 갱신(10분마다 자동), 재현 확인(로컬 서버 전용) |

내 컴퓨터의 파일 경로(`trades_path`, `artifacts`, 실험 `folder`)는 프록시가 읽어서 내용으로 보낸다. 작성 중인 run은 서버의 `drafts/<내 이메일>/`에 보관되고, 제출하면 공유 순위에 올라간다.

## 이렇게 요청하면 된다

- "dev-late 순위를 보여주고, 무작위 대비 95% 이상인 모델이 있는지 알려줘"
- "entry-gate 실험에서 강사 추천 종목만, 진입 방식 × 기간 격자를 보여줘. 보정 후 유의한 칸이 있어?"
- "뒤틀림 분류기 진입에 전날 코스닥 20일선 위 조건을 걸면 어떻게 바뀌어? 요일별로도 나눠 봐"
- "~/research/new-exp/20261120-v1 실험을 미리보기하고 이상 없으면 제출해줘"
- "results/trades.parquet으로 dev-late 구간 run을 만들고, 검증 결과를 보여준 뒤 제출해줘"

## 규칙

- 지표는 플랫폼이 계산한다. 같은 트랙·구간 안에서만 비교한다.
- 수익률은 비용 후 소수(0.01 = 1%)이고, 미청산 거래도 분모에 남는다.
- 잠금 시험(holdout) 데이터로 조건이나 임계값을 고르지 않는다.
- 조합 실험실 결과는 탐색용이다(보정 전). 결정은 실험 격자의 q값과 새 기간 시험으로 한다.

## 관리자

- **팀원 초대**: `infra/arena/add-user.sh name@example.com`. 임시 비밀번호가 나오며 대시보드와 MCP에 공통으로 쓴다.
- **설정 문자열**: `terraform -chdir=infra/mcp output -raw setup_string`. 서버 주소, 로그인 도메인, 클라이언트 ID가 들어 있다. 비밀값은 아니지만 계정 정보가 담겨 있어 공개 저장소에는 넣지 않는다.
- **배포**: `main`에 push하면 CI가 ARM64 이미지를 빌드해 ECR에 올리고 Runtime을 갱신한다(`infra/mcp/README.md`).
- **로컬 서버**(AWS 자격증명이 있는 사람용, 재현 확인 포함): `ARENA_REMOTE=s3://… uv run python -m arena.mcp_server`
