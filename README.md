# Auto S Trader

주식 후보 스캐너, 리서치 도구와 로컬 모의매매 프로젝트입니다.

- 루트의 `*_scan.mjs`: 종가·야간 후보 스캔
- `closing_research.mjs`, `performance_tracker.mjs`: 리서치와 성과 추적
- `notify_slack.mjs`: Slack 알림
- [`autotrader/`](autotrader/README.md): 모의매매 엔진, 데이터 연결, 모델 연구 코드와 실행 문서

## 설치와 테스트

Node.js 24.14 이상이 필요합니다. 프로젝트 루트에서 실행합니다.

```sh
npm --prefix autotrader ci --ignore-scripts
node --test *.test.mjs
npm --prefix autotrader test
npm --prefix autotrader run demo
```

Python 모델 연구 의존성은 `autotrader/modeling/requirements.lock`에 있습니다.

## 로컬 파일 설정

토스 인증정보의 빈 예제는 `autotrader/credentials.example.env`에 있습니다. 필요한 경우 이를 `autotrader/.env.toss.local`로 복사한 뒤 로컬에서 값을 입력합니다. 모의매매 설정은 `autotrader/config.template.json`과 실행 문서를 참고합니다.

Slack 알림은 환경변수 `SLACK_WEBHOOK_URL` 또는 로컬 `slack_webhook.txt`를 사용합니다. 개인 입력인 `holdings.json`, `watchlist.json`, `toss_daily_review.json`은 필요한 기능을 사용할 때 로컬에서 준비합니다. 루틴 문서의 절대 경로는 실행 환경에 맞게 변경합니다.

강의 원본·영상·압축파일, 수집 데이터·스프레드시트, `outputs/` 결과물, 개인 보유·관심종목, 인증정보, 가상환경·의존성·캐시·빌드 결과는 Git에서 제외합니다. 강의 이미지와 데이터가 필요한 연구 작업은 해당 자료를 로컬에 별도로 준비해야 합니다.
