# s_trader paper v0.1

실행 가능한 초기 모의매매 버전입니다. **실거래 주문 코드는 없으며, 학습된 지도학습·강화학습 모델도 아직 없습니다.** 토스 실시간 데이터를 연결해도 체결은 로컬 모의 체결입니다. 에이전트 리서치는 입력한 근거 자료를 Bedrock이 검토하는 단계까지 제공하며, 뉴스 자동 수집·사실 검증은 구현하지 않았습니다.

## 문서 안내

- [아키텍처와 기록 범위](ARCHITECTURE.md): 현재 구현, 목표 AWS 구성, 모듈 경계, 기록되는 판단과 누락, 후속 검증.
- [요구사항과 설계 결정](DECISIONS.md): 사용자 확정 사항, 선택 이유, 실험 가정, 미결정 사항, 비용 추정의 전제, Rust 전환 평가.
- [반도체 분봉 모델 개발](MODEL_RESEARCH.md): 데이터 계약, 섹터 국면, 후보·라벨, 순차 검증, 모델 평가와 단계별 완료 조건.
- [모델 학습 방법과 Mac 최적화](MODEL_METHODS.md): 강의 이미지 근거, 뒤틀림 정량화, 라벨링, 비교 실험과 로컬 추론.
- 이 README: 실행·복구·운영 방법과 모의 체결의 제약.

설계 문서의 목표 구성은 배포 완료 상태를 뜻하지 않습니다. 현재 기록은 주요 판단과 행동을 재생하는 수준이며 모든 관망 사유·오류·증권사 원본 메시지의 완전한 감사 기록은 아닙니다.

## 바로 실행

프로젝트 루트에서 Node.js 24.14 이상으로 실행합니다. 테스트한 런타임은 24.14.1입니다. 이 버전의 내장 SQLite는 experimental 경고를 출력합니다.

```sh
npm ci --ignore-scripts
npm test
npm run demo
```

데모는 키 없이 실행되며 `outputs/paper/demo-<실행시각>-<PID>/`에 다음을 만듭니다.

- `config.json`: 합성 데이터용 실험 설정
- `events.jsonl`: 입력 시세 이벤트
- `paper.sqlite`: 상태·정규화 입력 이벤트·매매 원장
- `summary.json`, `ledger.jsonl`: 결과와 판단/체결 내역

합성 시나리오는 돌파→눌림→재상승, 초기 손절 등록, 본절 이동, +3%와 +5%에서 원수량의 35%씩 익절, 추세 하락 시 잔여 청산, 손절가를 뛰어넘는 급락 손실을 포함합니다. **데모 손익은 전략 성과가 아닙니다.**

## 고정된 사용자 규칙

- 종가베팅과 데이트레이딩을 합쳐 최대 3종목. 체결 전 매수 대기도 슬롯을 점유합니다.
- 초기 손절선이 없는 진입은 거부합니다. 매수 체결과 보호 상태 저장은 같은 트랜잭션입니다.
- 진입가에서 설정한 만큼 오르면 본절로 이동합니다. +3%를 기다리지 않습니다. 스탑은 낮추지 않습니다.
- +3%에서 최초 체결 수량의 35%, +5%에서 누적 70%를 매도합니다. 소수 주식은 내림하므로 작은 수량에서는 비율이 달라집니다.
- +3% 전이라도 하락 전환이면 정리합니다. 손절·전량 청산이 미완료 분할 익절보다 우선합니다.
- 계좌 현금·포지션·이벤트 처리·체결 기록은 SQLite 트랜잭션으로 저장합니다. 같은 이벤트를 재생해도 중복 체결하지 않습니다.
- `pause`는 신규 진입만 중지하고 기존 보호는 유지합니다. `flatten`은 다음 거래 가능한 호가에서 잔여 청산을 시도합니다.

## 아직 결정하지 않은 수치

`config.template.json`의 `null`은 의도적입니다. 실행하려면 실험값을 명시해야 합니다. 데모의 본절 이동 +0.5%, 자금·하루 손실 한도·추세선·패턴 허용 오차·비용 가정 등은 **사용자가 채택한 전략 수치가 아닙니다.** `maxInitialRiskPct=0.02`는 진입 시점의 계획상 위험 검사이며 실제 손실 상한을 보장하지 않습니다.

작동하는 설정 형태는 데모가 만든 `config.json`을 참고할 수 있습니다. 실제 종목 실험에는 해당 시점 이전에 알려진 정보로 새 계획을 작성합니다. 날짜와 종목을 바꾸는 것만으로 검증된 설정이 되지는 않습니다.

계획 항목의 예:

```json
{
  "symbol": "005930",
  "strategies": ["day"],
  "reason": "실험용 근거를 작성",
  "informationAsOf": "2026-10-29T08:50:00+09:00",
  "validFrom": "2026-10-29T09:00:00+09:00",
  "validUntil": "2026-10-29T19:50:00+09:00",
  "exitAt": "2026-10-29T19:55:00+09:00",
  "trendline": [
    { "at": "2026-10-28T14:00:00+09:00", "price": 101000 },
    { "at": "2026-10-28T15:00:00+09:00", "price": 100500 }
  ]
}
```

가격과 시각은 스키마 설명용 예시이며 해당 주식의 실제 시세·추천이 아닙니다. 두 anchor를 연결해 시간을 기준으로 연장한 선을 사용합니다. 미래 anchor는 거부합니다. 이 초기 버전의 패턴은 학습된 강사의 판단이 아니라 명시적 선을 이용하는 규칙 가설입니다. 보유 후에는 인과적으로 확인한 눌림 저점의 하향 이탈을 하락 전환 가설로 사용합니다.

## 재생·복구·운영 제어

아래 경로를 실제 데모 출력 경로로 바꿉니다. 같은 설정·DB로 재실행하면 상태를 복구하며, 재생 이벤트 ID가 같으면 건너뜁니다. 다른 내용에 동일 ID를 재사용하면 오류로 중단합니다.

```sh
node cli.mjs replay --config outputs/paper/DEMO/config.json --events outputs/paper/DEMO/events.jsonl --db outputs/paper/replay.sqlite --out outputs/paper/replay-report
node cli.mjs status --config outputs/paper/DEMO/config.json --db outputs/paper/replay.sqlite
node cli.mjs control --config outputs/paper/DEMO/config.json --db outputs/paper/replay.sqlite --action pause
node cli.mjs control --config outputs/paper/DEMO/config.json --db outputs/paper/replay.sqlite --action flatten
node cli.mjs export-events --config outputs/paper/DEMO/config.json --db outputs/paper/replay.sqlite --out outputs/paper/recorded-events.jsonl
```

설정을 변경한 상태로 기존 DB를 열면 거부합니다. 미청산 포지션의 위험 기준을 조용히 바꾸지 않기 위한 제한입니다. 새로운 설정 실험에는 별도 DB를 사용합니다. 이벤트 내보내기는 수집을 중지한 뒤 실행하는 것을 권장합니다. 원장 DB는 계속 증가하므로 장기 수집 전 보관·아카이브 정책을 추가해야 합니다.

## 토스 읽기 전용 실시간 모의매매

실제 키를 코드·채팅·명령행 인수에 넣지 않습니다. 프로세스 환경에 `TOSS_ACCESS_TOKEN` 또는 `TOSS_CLIENT_ID`·`TOSS_CLIENT_SECRET`을 설정합니다. Node의 `--env-file`도 사용할 수 있으나 파일은 프로젝트 밖에 보관하고 접근권한을 제한합니다.

```sh
node --env-file=/secure/path/toss-paper.env cli.mjs toss --config /secure/path/paper-config.json --db /persistent/path/paper.sqlite
```

- 인증을 위한 `POST /oauth2/token` 이외에는 허용된 시세·캘린더 GET과 시세 웹소켓만 사용합니다. 계좌 식별 헤더나 실제 주문 API 호출은 없습니다.
- 토큰은 프로세스 내에서 공유합니다. 새 토큰 발급이 다른 프로세스의 토큰을 무효화할 수 있으므로 가능하면 기존 유효 토큰을 공유합니다.
- 공식 캘린더로 정규장·애프터마켓을 구분하고 동시호가 구간은 연속매매 모의 체결에서 제외합니다. 프리마켓은 이번 버전의 진입 대상이 아닙니다.
- 연결 단절·구독 거부를 감지하면 새 진입 대기와 미완성 패턴을 폐기하고 재연결합니다. 단절 동안의 체결이나 손절을 만들어내지 않습니다.
- 오래된 데이터·역순 데이터·교차 호가는 거부합니다. 토스의 통합 호가는 실제로 교차되어 보일 수 있으므로 이 정책은 기회를 보수적으로 줄일 수 있습니다.
- 실제 키로 연결한 종단 검증은 아직 하지 않았습니다. 테스트는 공식 스키마에 맞춘 응답/웹소켓 대역으로 수행했습니다.

토스는 시세 스트림에 유실 가능성과 순서 번호 부재를 명시합니다. **수신 체결 프레임으로 정확한 45/60틱 차트나 전체 거래량을 재구성했다고 간주하지 않습니다.** 정규장 종가와 통합 일봉 마지막 가격도 다를 수 있습니다.

## 기존 스캐너와 리서치

`jongga_scan.mjs`는 별도 스캐너이며 이 저장소에 포함하지 않습니다. 해당 스캐너에서 만든 일봉 후보 출력이 있을 때 가져올 수 있습니다. 실제 스캔 출력 JSON을 가져올 수 있습니다. replay 출력이나 수집 오류가 있는 출력은 현재 후보로 가져오지 않습니다.

```sh
node cli.mjs import-scan --input outputs/jongga/runs/ACTUAL_SCAN.json --out outputs/paper/candidates.json
```

강세 섹터 밖의 차트 통과 개별주도 보존합니다. 가져온 목록은 관찰 후보이며 자동 실행 계획이 아닙니다. anchor·초기 위험·유효기간 등을 명시한 계획 작성 단계가 남아 있습니다.

Bedrock 근거 검토는 AWS CLI v2와 `bedrock:InvokeModel` 권한, tool use를 지원하는 `BEDROCK_MODEL_ID`, `AWS_REGION`이 필요합니다. 모델이나 리전은 자동 선택하지 않습니다.

```sh
node cli.mjs research --input /path/evidence.json --out outputs/paper/research.json
```

입력은 `asOf`, `candidates`, `sources`입니다. 각 source에 `id`, HTTPS `url`, `title`, `text`, `publishedAt`, `knownAt`을 넣습니다. 후보에는 `symbol`과 KST `scanDate`가 필요합니다. 미래 자료, 누락된 후보 검토, 존재하지 않는 출처 인용을 거부합니다. 모델 결과는 `strong_sector`, `individual_strength`, `hold`이며 주문 권한을 갖지 않습니다. 실제 Bedrock 호출은 아직 실행하지 않았습니다.

## 모의 체결의 범위

- 진입 신호 다음의 유효한 호가에서 ask로 매수하고 bid로 매도하며 불리한 슬리피지를 더합니다.
- 호가 1단 잔량의 설정 비율까지만 체결합니다. 최초 매수는 부분 체결 시 나머지를 취소한 것으로 취급합니다.
- 청산 미체결분은 이후 호가에서 계속 시도합니다. 매도 가격은 손절가로 고정되지 않아 갭 손실이 -2%를 넘을 수 있습니다.
- 거래소별 라우팅·대기열·실제 전송 지연·시장충격·부분체결의 정밀 모델은 없습니다. 가격 이벤트와 호가 이벤트의 시간 차이를 실제 네트워크 지연 모델로 해석하지 않습니다.
- 동일 종목의 보유 중 추가매수는 지원하지 않습니다. 기존 물량 청산 후 새 패턴과 실험상 재진입 대기 조건을 만족하면 재진입할 수 있습니다.
- 종가베팅은 `closing` 전략 계획과 명시적 `signal` 이벤트로 실험할 수 있습니다. 신호에는 `initialStop`이 필수입니다. 정규장 종가 동시호가 배분·체결은 시뮬레이션하지 않으며 자동 종가주문 연결도 아직 없습니다.
- `exitAt` 이후 첫 거래 가능한 호가에서 청산을 시도합니다. 종료 전 호가가 없으면 잔여 보유가 남습니다. 최종 손익은 미청산 평가손익을 포함하며 매매 성공으로 단정하지 않습니다.
- 데모는 매도세금을 0으로 둡니다. 이는 테스트 가정이고 실거래 세율이 아닙니다. 비용을 확정하지 않은 결과로 수익성을 판단하지 않습니다.

## AWS에서 실행할 컨테이너

프로젝트 루트의 `.dockerignore`는 빌드에 필요한 파일만 허용합니다. 강의 영상, 보유내역, Slack webhook 등은 컨텍스트에 포함하지 않습니다.

```sh
docker build -f Dockerfile -t s-trader-paper .
docker volume create s-trader-paper
docker run --rm --read-only --tmpfs /tmp -v s-trader-paper:/state s-trader-paper demo --out /state/demo-1
```

실시간 모의매매 컨테이너에는 설정 파일을 읽기 전용으로, DB 경로를 쓰기 가능한 영속 볼륨으로 연결합니다. 이미지는 기본적으로 도움말만 출력합니다. Bedrock CLI 연구 작업은 AWS CLI가 있는 호스트에서 별도로 실행합니다.

첫 AWS 배포는 서울 리전 EC2/ECS on EC2와 EBS의 단일 호스트로 이 컨테이너를 검증하는 단계입니다. **이 버전은 DynamoDB 기반 실행권·다중 AZ 장애 승계·SageMaker 학습·AgentCore 배포를 구현하거나 배포하지 않았습니다.** SQLite 원장은 로컬 파일시스템/EBS에서 사용하고 S3/EFS 공유 파일을 원장으로 사용하지 않습니다. 후속 단계에서 단일 주문 주체와 증권사 잔고 대조를 포함한 실거래 구조로 확장합니다.

현재 환경에 Docker daemon이 실행 중이지 않아 이미지 빌드는 검증하지 못했습니다. Node 실행·모의 시나리오·자동 테스트는 검증했습니다.

## 공식 참고 자료

- 토스 REST: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
- 토스 시세 웹소켓: https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json
- 토스 FAQ: https://openapi.tossinvest.com/openapi-docs/faq.md
- Bedrock Converse: https://docs.aws.amazon.com/cli/latest/reference/bedrock-runtime/converse.html

국내 토스 조건주문은 현재 명세상 KRX 정규장에서만 발동합니다. 애프터마켓 실거래 보호는 별도 검증해야 합니다. 현재 프로그램은 조건주문을 포함한 실제 주문을 전송하지 않습니다.

## 저장소 범위

이 저장소의 최상위는 원래 `autotrader` 폴더입니다. 강의 원본·영상, 수집 데이터·분석 결과, 로컬 인증정보, 의존성과 가상환경은 포함하지 않습니다. 연구 문서에서 참조하는 강의 자료와 외부 스캐너는 별도로 준비해야 합니다.
