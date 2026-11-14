# Model Arena — AWS 배포 (Terraform + GitHub Actions)

공유 run 저장소와 로그인이 필요한 팀 대시보드. **2026-11-14 `ap-northeast-2`에 배포됨.** `main`에 push하면 GitHub Actions가 테스트 후 자동 배포한다.

## 구성

```mermaid
flowchart LR
  U[팀원 브라우저] --> CF[CloudFront]
  CF -->|viewer-request| LE[Lambda@Edge 로그인 검사]
  LE -->|쿠키 없음| HUI[Cognito Hosted UI]
  HUI -->|code + PKCE| LE
  LE -->|JWT 검증 통과| CF
  CF -->|OAC, site/ 만| S3[(S3 runs/ · site/)]
  CLI[arena CLI / CI] -->|submit · pull · publish| S3
  GH[GitHub Actions main] -->|OIDC 역할| TF[terraform apply]
```

| 리소스 | 역할 |
|---|---|
| S3 `s-trader-arena-<계정>-<리전>` | `runs/`(제출 run), `site/`(대시보드). 비공개·버전관리·AES256·TLS 전용 |
| CloudFront + OAC | `site/`만 읽는 HTTPS 대시보드. 캐시 없음 |
| Cognito 사용자 풀 + Hosted UI | 관리자가 만든 사용자만 로그인(자가 가입 없음), 비밀번호 12자 이상, TOTP MFA 선택 |
| Lambda@Edge (`us-east-1`) | 로그인 쿠키(ID 토큰) RS256 서명·발급자·대상·만료 검증, `/_auth/callback`·`/_auth/logout` 처리. 설정은 SSM `/s-trader-arena/auth-config` |
| IAM 그룹 `s-trader-arena-team` | 팀원 `runs/*`·`site/*` 읽기/쓰기(삭제 없음) |
| state | S3 `s-trader-tfstate-…` 의 `arena/terraform.tfstate`, S3 네이티브 잠금 (`infra/bootstrap`) |

로그인 세션은 8시간(ID 토큰). 만료 후 접속하면 Cognito 세션으로 다시 로그인된다.

## 팀원 초대

```sh
infra/arena/add-user.sh name@example.com     # 임시 비밀번호 출력, 첫 로그인 때 변경
```

run 제출 권한이 필요한 팀원은 IAM 사용자 이름을 `team_users`에 추가한다(`terraform.tfvars`는 Git 제외이므로 CI 배포 시에는 변수 기본값을 바꾸거나 GitHub 변수로 넘기는 방식으로 확장한다).

## 배포

- 자동: `main` push → `.github/workflows/deploy.yml` → 테스트 → `terraform apply` → `arena pull/build/publish` → 로그인 강제 확인(302/401)
- 수동(로컬):

```sh
cp infra/arena/backend.hcl.example infra/arena/backend.hcl   # 계정 ID 입력
terraform -chdir=infra/arena init -backend-config=backend.hcl
terraform -chdir=infra/arena plan
terraform -chdir=infra/arena apply
```

CI 역할 `s-trader-arena-github-deploy`는 이 저장소의 `main` 브랜치에서만 사용할 수 있고, 권한은 `s-trader-arena*` 리소스와 state로 제한된다. 저장소 변수 `AWS_DEPLOY_ROLE_ARN`, `TF_STATE_BUCKET`을 사용한다.

주의: CloudFront 함수/Lambda 연결을 바꿀 때 Terraform이 배포 갱신 전에 이전 함수를 지우려다 409가 날 수 있다. 그때는 `-target=aws_cloudfront_distribution.site`를 먼저 적용한다. Lambda@Edge 삭제는 복제본 정리 때문에 수 시간 걸릴 수 있다.
