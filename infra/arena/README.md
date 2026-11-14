# Model Arena — AWS 배포 (Terraform)

공유 run 저장소와 팀 대시보드를 만든다. **2026-11-14 기본 프로필 계정 `ap-northeast-2`에 배포됨.** 비용은 사용량 기준이며 소규모 팀이면 월 수 달러 이하를 예상한다(S3 저장·요청, CloudFront 전송·Function 호출).

| 리소스 | 역할 |
|---|---|
| S3 `s-trader-arena-<계정>-<리전>` | `runs/`(제출 run, 불변 규칙은 CLI가 강제), `site/`(대시보드). 비공개·버전관리·AES256·TLS 전용 |
| CloudFront + OAC | `site/`만 읽는 HTTPS 대시보드. 캐시 없음(항상 최신 data.json) |
| CloudFront Function | Basic 인증(사용자 `arena`, 비밀번호는 Terraform이 생성) |
| IAM 그룹 `s-trader-arena-team` | 팀원 `runs/*`·`site/*` 읽기/쓰기(삭제 없음) |

```sh
terraform -chdir=infra/arena init
terraform -chdir=infra/arena plan
terraform -chdir=infra/arena apply
terraform -chdir=infra/arena output -raw basic_auth_password

export ARENA_REMOTE=$(terraform -chdir=infra/arena output -raw arena_remote)
uv run python -m arena submit <run_id>
uv run python -m arena pull && uv run python -m arena build && uv run python -m arena publish
```

팀원 추가: `team_users = ["iam-user-name"]`를 `terraform.tfvars`에 넣고 `apply`. 팀원은 자기 AWS 자격증명으로 같은 명령을 사용한다.

주의: state는 로컬 `terraform.tfstate`(Git 제외)이며 생성된 비밀번호를 포함한다. 여러 명이 인프라를 바꾸게 되면 S3 backend + 잠금으로 옮긴다. Basic 인증은 가벼운 접근 제한이며 SSO 수준 인증이 아니다.
