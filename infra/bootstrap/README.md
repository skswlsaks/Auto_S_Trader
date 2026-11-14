# Bootstrap — 원격 state + GitHub 배포 역할

계정 관리자가 **한 번만** 수동으로 적용한다. CI는 이 스택을 바꾸지 않는다.

- S3 `s-trader-tfstate-<계정>-<리전>`: Terraform state(버전관리·암호화·TLS 전용). 잠금은 S3 네이티브(`use_lockfile`)
- GitHub OIDC 공급자 + 역할 `s-trader-arena-github-deploy`: `skswlsaks/Auto_S_Trader`의 `main` 브랜치 워크플로만 사용할 수 있다. 장기 액세스 키는 없다. 이 저장소는 GitHub **불변 subject**(`repo:owner@ID/repo@ID:ref:…`)를 사용하므로 신뢰 조건도 ID 기반이다(저장소 이름 변경·재생성으로 탈취 불가). 확인: `gh api repos/OWNER/REPO/actions/oidc/customization/sub`. 권한은 `infra/arena` 리소스(이름 접두사 `s-trader-arena`)와 그 state로 제한한다.

```sh
# 최초 1회: state 버킷이 없으므로 로컬 state로 생성 후 버킷으로 이전
terraform -chdir=infra/bootstrap init -backend=false
terraform -chdir=infra/bootstrap apply
terraform -chdir=infra/bootstrap init -migrate-state -backend-config=backend.hcl
```

`backend.hcl`의 버킷 이름은 계정 ID를 포함하므로 로컬에서 만든다(`backend.hcl.example` 참고). 배포 역할 ARN은 GitHub 저장소 변수 `AWS_DEPLOY_ROLE_ARN`, state 버킷은 `TF_STATE_BUCKET`에 넣는다.
