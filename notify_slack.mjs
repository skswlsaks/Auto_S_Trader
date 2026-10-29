// Slack 알림 전송 (Incoming Webhook)
// 사용법:
//   node notify_slack.mjs <메시지 파일 경로>      # 파일 내용을 전송
//   echo "텍스트" | node notify_slack.mjs           # 표준입력 전송
//   node notify_slack.mjs --test                    # 연결 테스트 메시지
// 웹훅 URL은 같은 폴더의 slack_webhook.txt(한 줄) 또는 환경변수 SLACK_WEBHOOK_URL에서 읽는다.
// 메시지는 Slack mrkdwn 형식(*굵게*, `코드`, ```블록```)으로 작성. 긴 메시지는 3,500자 단위로 나눠 보낸다.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));

async function webhook() {
  if (process.env.SLACK_WEBHOOK_URL) return process.env.SLACK_WEBHOOK_URL.trim();
  try { return (await fs.readFile(path.join(root, 'slack_webhook.txt'), 'utf8')).trim(); }
  catch { throw new Error('slack_webhook.txt 없음 — Slack Incoming Webhook URL을 이 파일에 한 줄로 저장하세요'); }
}

export function chunks(text, size = 3500) {
  // 줄 단위로 나누고, 코드블록 중간에서 잘리면 닫았다가 다음 조각에서 다시 연다
  const out = []; let cur = '', inFence = false;
  for (const line of text.split('\n')) {
    if (cur && cur.length + line.length + 5 > size) {
      out.push(inFence ? cur + '\n```' : cur);
      cur = inFence ? '```' : '';
    }
    cur += (cur ? '\n' : '') + line;
    if ((line.match(/```/g) || []).length % 2 === 1) inFence = !inFence;
  }
  if (cur) out.push(cur);
  return out;
}

async function main() {
  const url = await webhook();
  if (!/^https:\/\/hooks\.slack\.com\//.test(url)) throw new Error('웹훅 URL 형식이 아님 (https://hooks.slack.com/... 이어야 함)');
  let text;
  if (process.argv[2] === '--test') text = `:white_check_mark: s_trader 종가베팅 알림 연결 테스트 (${new Date().toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })})`;
  else if (process.argv[2]) text = await fs.readFile(process.argv[2], 'utf8');
  else { const buf = []; for await (const c of process.stdin) buf.push(c); text = Buffer.concat(buf).toString('utf8'); }
  if (!text.trim()) throw new Error('보낼 내용이 없음');
  const parts = chunks(text.trim());
  for (const [i, part] of parts.entries()) {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: part, unfurl_links: false }) });
    const body = await res.text();
    if (!res.ok || body.trim() !== 'ok') throw new Error(`Slack 전송 실패 (${i + 1}/${parts.length}): HTTP ${res.status} ${body.slice(0, 200)}`);
  }
  console.log(`Slack 전송 완료: ${parts.length}개 메시지`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(e => { console.error(e.message); process.exitCode = 1; });
