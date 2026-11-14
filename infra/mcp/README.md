# Model Arena MCP — Bedrock AgentCore 배포 (Terraform)

| 리소스 | 역할 |
|---|---|
| ECR `s-trader-arena-mcp` | MCP 서버 이미지(linux/arm64, 불변 태그 = 커밋 SHA, 최근 20개 보관) |
| Cognito 앱 클라이언트 `s-trader-arena-mcp` | 기존 사용자 풀에서 AI 도구용 로그인(code + PKCE, `http://localhost:8976/callback`, ID 토큰 1시간, 갱신 30일) |
| IAM 역할 `s-trader-arena-mcp-runtime` | 런타임 실행 역할: ECR pull, 로그·추적, S3 `site/`·`conditions/` 읽기, `runs/`·`studies/`·`drafts/` 읽기·쓰기(삭제 없음) |
| AgentCore Runtime `s_trader_arena_mcp` | 서버 프로토콜 MCP, Cognito JWT 인증(`allowed_audience` = MCP 클라이언트), `Authorization` 헤더 전달(호출자 신원), PUBLIC 네트워크 |

기존 `infra/arena` state(사용자 풀, 로그인 도메인, 공유 저장소)를 `terraform_remote_state`로 읽는다. state는 `arena/mcp.tfstate`다.

```sh
cp infra/mcp/backend.hcl.example infra/mcp/backend.hcl   # 계정 ID 입력
terraform -chdir=infra/mcp init -backend-config=backend.hcl
terraform -chdir=infra/mcp apply -var state_bucket=<state 버킷> -var image_tag=<ECR 태그>   # image_tag 비우면 레지스트리·역할·클라이언트만
terraform -chdir=infra/mcp output -raw setup_string                                        # 팀원에게 전달
```

이미지 빌드(ARM64): `docker buildx build --platform linux/arm64 -f infra/mcp/Dockerfile -t <ecr>/s-trader-arena-mcp:<tag> --push .`. 보통은 CI(`deploy.yml`의 `mcp` 작업, `ubuntu-24.04-arm`)가 한다.

검증(2026-11-14):
- 로그인 없는 호출은 거부된다.
- 실제 Cognito 로그인(임시 사용자)으로 확인했다: `arena_whoami` 이메일, 순위 조회, 조건 조합, 로컬 parquet으로 run 초안 작성·검증, 제출(제출자 = 로그인 이메일), 실험 폴더 미리보기(컨테이너에서 parquet 해석).
- 시험 데이터와 사용자는 삭제했다.
