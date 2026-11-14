"""D-close point breakout purchase, D+1 immediately active BE and fixed 35/35 targets."""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .config import atomic_json, load_config
from .daytrade import bearish_structure, session_bars
from .execution import sell_tax_bps
from .liquid_trend import alarms
from .market_data import OUTPUT as SOURCE
from .market_research import fetch_day, known_prefix
from .nextday_eval import daily_frame
from .pattern_eval import sha
from .shape_experiment import DEVELOP, ROOT
from .shape_v2 import SHAPE_FEATURES
from .toss import TossAPI

OUTPUT = ROOT / 'outputs/model-research/closing-bet/20261030-krx-v2'
POLICIES = {'cost_be': '비용 포함 본절·고정 익절',
            'cost_be_bearish': '비용 포함 본절·고정 익절·기존 하락 모델',
            'price_be': '매수가 본절·고정 익절'}


def overnight_exit(future, entry, execution, bearish=None, cost_be=True):
    """No same-day entry debit: the entire minute capacity belongs to overnight exits.

    Resting stop wins OHLC conflicts. Gaps execute at the worse open, and an
    unfilled stop stays a market exit. A completed-bar bearish alarm sells from
    the next minute. The residual is scheduled out at 15:19 for 15:20 execution.
    """
    if entry['quantity'] < 3 or entry['capital'] <= 0:
        raise ValueError('Need a filled overnight position with three tranches')
    if not future.empty:
        if future.day.nunique() != 1 or future.minute.iloc[0] != 541:
            raise ValueError('Need D+1 regular open at 09:01 bar end')
        if not future.timestamp.diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all():
            raise ValueError('Unknown minute gap; pass only the known prefix')
        if pd.Timestamp(entry['entry_at']) >= future.timestamp.iloc[0] - pd.Timedelta(minutes=1):
            raise ValueError('Purchase must precede next-day open')
    if bearish is None:
        bearish = np.zeros(len(future), dtype=bool)
    if len(bearish) != len(future):
        raise ValueError('Unaligned bearish alarms')
    q, capital = entry['quantity'], entry['capital']
    impact = (execution['slippage_bps'] + execution['half_spread_bps']) / 10000
    desired = [math.floor(q * .35), math.floor(q * .70) - math.floor(q * .35)]
    sold_targets = [0, 0]
    left, proceeds, forced = q, 0., None
    fills, ambiguous, stop_triggered = [], False, False
    for i, bar in enumerate(future.itertuples(index=False)):
        fee = (execution['sell_fee_bps'] + sell_tax_bps(execution, bar.timestamp)) / 10000
        capacity = math.floor(bar.volume * execution['participation'])

        def sell(wanted, price, reason):
            nonlocal left, proceeds, capacity
            amount = min(left, wanted, capacity)
            if amount:
                proceeds += amount * price * (1 - fee)
                left -= amount
                capacity -= amount
                fills.append(dict(bar_end=bar.timestamp.isoformat(), quantity=amount,
                                  price=float(price), reason=reason))
            return amount

        if forced:
            sell(left, bar.open * (1 - impact), forced)
        if left and forced is None and bar.volume > 0:
            stop = capital / q / ((1 - impact) * (1 - fee)) if cost_be else entry['entry_price']
            if bar.low <= stop:
                ambiguous |= any(bar.high * (1 - impact) >= entry['entry_price'] * (1 + gain)
                                 and sold_targets[k] < desired[k] for k, gain in enumerate((.03, .05)))
                forced, stop_triggered = 'breakeven_stop', True
                sell(left, min(bar.open, stop) * (1 - impact), forced)
            else:
                for k, gain in enumerate((.03, .05)):
                    limit = entry['entry_price'] * (1 + gain)
                    if bar.high * (1 - impact) >= limit:
                        sold_targets[k] += sell(desired[k] - sold_targets[k],
                                                max(bar.open * (1 - impact), limit), f'tp{round(gain * 100)}')
        if left and forced is None and bearish[i]:
            forced = 'bearish_structure'
        if left and forced is None and bar.minute >= 919:
            forced = 'session_end'
        if not left:
            break
    net = proceeds / capital - 1 if not left else None
    marked = None
    if len(future):
        fee = (execution['sell_fee_bps'] + sell_tax_bps(execution, bar.timestamp)) / 10000
        marked = (proceeds + left * bar.close * (1 - impact) * (1 - fee)) / capital - 1
    return dict(complete=not bool(left), remaining_quantity=left, realized_net_return=net,
                marked_net_return=marked, proceeds=proceeds, quantity=q, capital=capital,
                tp3_quantity=sold_targets[0], tp5_quantity=sold_targets[1],
                tp3_complete=sold_targets[0] == desired[0], tp5_complete=sold_targets[1] == desired[1],
                stop_triggered=stop_triggered, ambiguous=bool(ambiguous),
                exit_reason=forced or (fills[-1]['reason'] if fills else 'unobserved'),
                exit_bar_end=fills[-1]['bar_end'] if not left else None, fills=fills)


def collect_daily(output):
    """Use the live scanner's KRX daily source rather than integrated daily OHLC."""
    folder = output / 'krx-daily'
    folder.mkdir(exist_ok=True)
    paths = sorted((SOURCE / 'daily').glob('*-1d-adjusted.parquet'))

    def one(source):
        symbol = source.name.split('-')[0]
        target = folder / f'{symbol}.json'
        if target.exists():
            return str(target), sha(target)
        url = f'https://api.finance.naver.com/siseJson.naver?symbol={symbol}&requestType=1&startTime=20250501&endTime=20261028&timeframe=day'
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com/'})
                with urllib.request.urlopen(req, timeout=20) as response:
                    raw = response.read().decode('utf-8')
                bars = []
                for m in re.finditer(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)', raw):
                    bars.append(dict(date=f'{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}',
                        **{k: float(m[i]) for i, k in enumerate(['open', 'high', 'low', 'close', 'volume'], 2)}))
                if not bars:
                    raise ValueError('Empty KRX history: ' + symbol)
                atomic_json(target, dict(symbol=symbol, url=url, fetched_at=time.time(), bars=bars, raw=raw))
                return str(target), sha(target)
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)

    hashes = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(one, path) for path in paths]
        for n, task in enumerate(as_completed(tasks), 1):
            path, expected = task.result()
            hashes[path] = expected
            if n % 100 == 0 or n == len(tasks):
                print(f'KRX daily {n}/{len(tasks)}', flush=True)
    return hashes


def screen(output):
    target = output / 'signals.parquet'
    if target.exists():
        saved = json.loads((output / 'screening.json').read_text())
        for path, expected in saved['source_hashes'].items():
            if sha(path) != expected:
                raise ValueError(f'Frozen signal source changed: {path}')
        if sha(target) != saved['signals_sha256']:
            raise ValueError('Frozen signals changed')
        return pd.read_parquet(target), saved
    universe = {v['symbol']: v for v in json.loads((SOURCE / 'universe.json').read_text())['symbols']}
    inputs, prices, hashes, calendar = [], [], collect_daily(output), set()
    for path in sorted((SOURCE / 'daily').glob('*-1d-adjusted.parquet')):
        symbol = path.name.split('-')[0]
        rawpath = path.with_name(f'{symbol}-1d-raw.parquet')
        adjusted, raw = daily_frame(path), daily_frame(rawpath)
        if adjusted.date.tolist() != raw.date.tolist():
            raise ValueError('Raw/adjusted calendar mismatch')
        krx = pd.DataFrame(json.loads((output / 'krx-daily' / f'{symbol}.json').read_text())['bars'])
        if krx.date.duplicated().any() or not krx.date.is_monotonic_increasing:
            raise ValueError('Invalid KRX daily dates')
        calendar.update(krx.date)
        inputs.append(dict(symbol=symbol, bars=krx.to_dict('records')))
        factors = pd.Series(raw.close.to_numpy() / adjusted.close.to_numpy(), index=raw.date)
        prices.append(krx[['date', 'close', 'volume']].assign(symbol=symbol, market=universe[symbol]['market'],
            name=universe[symbol]['name'], adjusted_close=krx.close,
            raw_close=krx.close * krx.date.map(factors)).rename(columns={
                'date': 'selection_day', 'close': 'krx_close', 'volume': 'daily_volume'}))
        hashes.update({str(path): sha(path), str(rawpath): sha(rawpath)})
    replay = ROOT / 'trading/closing_bet_replay.mjs'
    hashes.update({str(replay): sha(replay), str(ROOT / 'trading/jongga_scan.mjs'): sha(ROOT / 'trading/jongga_scan.mjs'),
                   str(SOURCE / 'universe.json'): sha(SOURCE / 'universe.json')})
    result = subprocess.run(['node', str(replay)], input=json.dumps(dict(symbols=inputs,
        start='2025-10-29', end='2026-10-28'), allow_nan=False), text=True, capture_output=True, check=True)
    frame = pd.DataFrame(json.loads(result.stdout)['rows']).merge(pd.concat(prices, ignore_index=True),
        on=['symbol', 'selection_day'], validate='many_to_one')
    factor = frame.raw_close / frame.adjusted_close
    for col in ('point_price', 'secret_price', 'ma20_price'):
        frame[col] *= factor
    days = sorted(calendar)
    following = dict(zip(days[:-1], days[1:]))
    frame['day'] = frame.selection_day.map(following)
    frame['turnover'] = frame.raw_close * frame.daily_volume
    frame['liquid'] = frame.turnover.ge(1e10)
    frame.to_parquet(target, index=False)
    summary = dict(scanned_symbols=len(inputs), universe_symbols=len(universe), signals=len(frame),
        liquid_signals=int(frame.liquid.sum()), no_next_session=int(frame.day.isna().sum()),
        first_selection=frame.selection_day.min(), last_selection=frame.selection_day.max(),
        source_hashes=hashes, signals_sha256=sha(target), rules=json.loads(result.stdout)['rules'])
    atomic_json(output / 'screening.json', summary)
    return frame, summary


def collect_one(output, row, api):
    path = output / 'minutes' / f"{row['symbol']}-{row['day']}.parquet"
    for folder in (SOURCE / 'minutes', ROOT / 'outputs/model-research/closing-bet/20261030-v1/minutes'):
        old = folder / path.name
        if not path.exists() and old.exists():
            path.symlink_to(old.resolve())
    return fetch_day(output, row, api)


def statistics(frame):
    done = frame.complete
    net = pd.to_numeric(frame.realized_net_return)
    success = done & net.ge(-1e-12)
    tp = frame.tp3_quantity.gt(0)
    befill = frame.fills.map(lambda values: any(v['reason'] == 'breakeven_stop' for v in values))
    return dict(entries=len(frame), completed=int(done.sum()), unclosed=int((~done).sum()),
        successes=int(success.sum()), success_rate=float(success.mean()),
        positive=int((done & net.gt(1e-12)).sum()), exact_flat=int((done & net.abs().le(1e-12)).sum()),
        losses=int((done & net.lt(-1e-12)).sum()), tp3_any=int(tp.sum()), tp5_any=int(frame.tp5_quantity.gt(0).sum()),
        tp3_complete=int(frame.tp3_complete.sum()), tp5_complete=int(frame.tp5_complete.sum()),
        be_filled=int(befill.sum()), be_success=int((befill & success).sum()),
        be_losses=int((befill & done & ~success).sum()), ambiguous=int(frame.ambiguous.sum()),
        mean_net_return_completed=float(net.loc[done].mean()) if done.any() else None,
        median_net_return_completed=float(net.loc[done].median()) if done.any() else None,
        mean_marked_return=float(pd.to_numeric(frame.marked_net_return).mean()),
        best=float(net.loc[done].max()) if done.any() else None,
        worst=float(net.loc[done].min()) if done.any() else None,
        profit_factor=float(net.loc[done & net.gt(0)].sum() / -net.loc[done & net.lt(0)].sum())
            if (done & net.lt(0)).any() else None,
        net_pnl_krw_completed=float((frame.proceeds - frame.capital).loc[done].sum()))


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    execution = load_config()['execution']
    signals, screening = screen(output)
    price = signals.raw_close * (1 + (execution['slippage_bps'] + execution['half_spread_bps']) / 10000)
    quantity = np.minimum(np.floor(execution['max_position_krw'] / (price * (1 + execution['buy_fee_bps'] / 10000))),
                          np.floor(signals.daily_volume * execution['participation']))
    selected = signals.loc[signals.day.notna() & quantity.ge(3)].sort_values(
        ['selection_day', 'turnover', 'symbol'], ascending=[True, False, True]).groupby('selection_day', sort=True).head(3)
    if not selected.selection_day.lt(selected.day).all():
        raise ValueError('Noncausal next-day assignment')
    selected.to_parquet(output / 'selection.parquet', index=False)
    protocol = dict(version='close-bet-nextday-immediate-be-v1', execution=execution, policies=POLICIES,
        entry='Final D KRX daily close, rescaled by same-date raw/adjusted corporate-action factor, plus7bps buy impact and fee; idealized same-close fill',
        selection='Existing jongga evaluate entry on Naver KRX daily, no learned entry; top3 by D close*volume then symbol; 100억원 is warning only; need at least3 shares within3m budget',
        exits='D+1 09:00 regular open onward: immediately active own-cost BE; +3%35%, +5%35%, residual bearish alarm next minute or 15:19->15:20 session exit',
        scope='1119 cached raw+adjusted histories, new matching KRX daily histories; excludes raw-only histories, IPO branch, historical risk flags and sector sorting',
        capacity='Buy <=1% D total volume, <=KRW3m each; sell <=1% each minute; D auction participation unavailable',
        comparison='Separate symbol episodes, <=3 new entries per D; not a cash-constrained compounded account if residuals remain',
        same_bar='Stop first, including conflicts with profit targets; incomplete exposure remains in denominator',
        source_screening_sha256=sha(output / 'screening.json'), selection_sha256=sha(output / 'selection.parquet'),
        modules={name: sha(Path(__file__).with_name(name)) for name in (
            'closing_bet_study.py', 'daytrade.py', 'execution.py', 'shape_v2.py', 'liquid_trend.py',
            'market_research.py', 'market_data.py', 'toss.py')},
        bearish_model_sha256=sha(DEVELOP / 'confirmation-model.joblib'),
        limits=['KRX daily source matches original scanner; D end-of-day features and same-close fill remain idealized rather than an executable pre-auction signal.',
                'KRX+NXT integrated minute data, 09:01-15:20 bar ends; overnight/pre-market path excluded, stop starts at regular open.',
                'Current membership/survivorship and point-in-time corporate action adjustment limitations remain.',
                '100억원 warning is retained, not an exclusion; ranking by close*volume substitutes for unavailable historical sector ranking.',
                'Existing bearish transfer model has no independent bearish-label validation; full-year replay is retrospective.',
                '2026-08-27 onward was previously inspected; it is a chronological slice, not an independent holdout.',
                'Cost and OHLC capacity assumptions do not verify ticks, queue, latency or auction fills.'],
        model_retrained=False, live_orders=False)
    frozen = output / 'protocol.json'
    if frozen.exists() and json.loads(frozen.read_text()) != protocol:
        raise ValueError('Experiment changed; use another output directory')
    atomic_json(frozen, protocol)
    (output / 'minutes').mkdir(exist_ok=True)
    config = load_config()
    api = TossAPI(config['credentials_file'], config['api_min_interval_seconds'])
    records = selected.to_dict('records')
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(collect_one, output, row, api) for row in records]
        for n, task in enumerate(as_completed(tasks), 1):
            task.result()
            if n % 50 == 0 or n == len(tasks):
                atomic_json(output / 'progress.json', dict(stage='collect', completed=n, total=len(tasks)))
                print(f'Close bet minutes {n}/{len(tasks)}', flush=True)
    model = joblib.load(DEVELOP / 'confirmation-model.joblib')
    trades, hashes, coverage = [], {}, []
    for n, row in enumerate(records, 1):
        path = output / 'minutes' / f"{row['symbol']}-{row['day']}.parquet"
        hashes[str(path)] = sha(path)
        allbars = session_bars(pd.read_parquet(path))
        regular = allbars.loc[allbars.minute.between(541, 920)].reset_index(drop=True)
        future = known_prefix(regular) if len(regular) and regular.minute.iloc[0] == 541 else regular.iloc[:0]
        coverage.append(dict(symbol=row['symbol'], day=row['day'], available=len(regular), known_prefix=len(future)))
        bear_by_time = {}
        if len(future):
            down = bearish_structure(allbars, row['symbol'])
            mask = alarms(model.predict_proba(down[SHAPE_FEATURES])[:, 1], down, False)
            bear_by_time = dict(zip(allbars.timestamp, mask))
        bearish = np.array([bear_by_time.get(t, False) for t in future.timestamp], dtype=bool)
        price = row['raw_close'] * (1 + (execution['slippage_bps'] + execution['half_spread_bps']) / 10000)
        q = min(math.floor(execution['max_position_krw'] / (price * (1 + execution['buy_fee_bps'] / 10000))),
                math.floor(row['daily_volume'] * execution['participation']))
        if q < 3:
            raise ValueError('Selected purchase cannot support fixed tranches')
        entry = dict(entry_at=row['selection_day'] + 'T15:30:00+09:00', entry_price=price,
                     quantity=q, capital=q * price * (1 + execution['buy_fee_bps'] / 10000))
        for policy in POLICIES:
            result = overnight_exit(future, entry, execution,
                bearish=bearish if policy == 'cost_be_bearish' else None, cost_be=policy != 'price_be')
            assert sum(v['quantity'] for v in result['fills']) + result['remaining_quantity'] == q
            used = {}
            for fill in result['fills']:
                at = pd.Timestamp(fill['bar_end'])
                used[at] = used.get(at, 0) + fill['quantity']
            indexed = future.set_index('timestamp')
            assert all(qty <= math.floor(indexed.loc[t, 'volume'] * execution['participation']) for t, qty in used.items())
            trades.append({**row, **entry, **result, 'policy': policy,
                'period': 'later_slice' if row['selection_day'] >= '2026-08-27' else 'earlier_slice',
                'data_censored': len(future) < 380, 'opening_gap': float(future.open.iloc[0] / price - 1) if len(future) else None})
        if n % 50 == 0 or n == len(records):
            print(f'Close bet replay {n}/{len(records)}', flush=True)
            atomic_json(output / 'progress.json', dict(stage='replay', completed=n, total=len(records)))
    frame = pd.DataFrame(trades)
    frame.to_parquet(output / 'trades.parquet', index=False)
    frame.drop(columns='fills').to_csv(output / 'trades.csv', index=False)
    pd.DataFrame(coverage).to_csv(output / 'coverage.csv', index=False)
    atomic_json(output / 'minute-source-hashes.json', hashes)
    stats = {period: {policy: statistics(g) for policy, g in group.groupby('policy')}
        for period, group in [('full_year', frame), ('later_slice', frame.loc[frame.period.eq('later_slice')])]}
    summary = dict(screening={k: v for k, v in screening.items() if k not in ('source_hashes', 'rules')},
        selected_episodes=len(selected), selected_symbols=int(selected.symbol.nunique()),
        selected_under_100eok=int((~selected.liquid).sum()), rejected_quantity_or_price=int((~quantity.ge(3)).sum()),
        selection_days=int(selected.selection_day.nunique()), first_selection=selected.selection_day.min(),
        last_selection=selected.selection_day.max(), first_exit=selected.day.min(), last_exit=selected.day.max(),
        statistics=stats, verification=dict(status='passed', share_and_minute_capacity_checked=len(frame),
            future_prices_not_used_in_selection=True, same_close_is_idealized=True,
            full_380_minute_sessions=sum(v['known_prefix'] == 380 for v in coverage)))
    atomic_json(output / 'statistics.json', summary)
    render(output, summary)
    atomic_json(output / 'progress.json', dict(stage='finished', episodes=len(selected)))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def render(output, summary):
    lines = ['# 종가베팅 → 다음 날 즉시 본절 보호', '',
        f"매수 {summary['first_selection']}~{summary['last_selection']}, 매도 관측 {summary['first_exit']}~{summary['last_exit']}. "
        f"수정·원시 일봉 모두 있는 1,119종목 중 {summary['selected_symbols']}종목·{summary['selected_episodes']}건.", '',
        '기존 스캐너와 동일한 네이버 KRX 일봉과 포인트 종가 돌파/재돌파 진입 함수를 그대로 사용했다. '
        '당일 거래대금(종가×거래량 근사) 상위 최대3개를 선정했다. 100억원 미만은 제외하지 않았으며 '
        f"{summary['selected_under_100eok']}건이다. 300만원 한도에서 최소3주를 살 수 없는 경우는 사전에 제외했다. "
        '일봉 완성 후 같은 종가에 매수됐다고 가정한 연구다. 실제 동시호가 전에 확정할 수 있는 신호·체결을 검증한 결과가 아니다.', '',
        '+3% 원수량35%, +5%35%, 잔량30%. 다음 날 정규장 개장부터 본절 스탑을 켠다. '
        '비용 포함 본절은 매수·매도 수수료와 날짜별 세금, 스프레드·슬리피지를 회수하는 가격이다. '
        '본절 아래 시가 갭은 손실이며, 분봉 거래량1%를 넘게 매도하지 않는다. 하락 모델은 기존 동결 모델을 사용하며 잔량은 늦어도15:19관측→15:20매도한다.', '',
        '매수·매도 수수료 각0.015%, 2025년 매도세금0.15%/2025-12-29부터0.20%, 편도 가격영향0.07% 가정. '
        '[토스 표준 수수료](https://home.tossinvest.com/ko/open-api), [세금 변경 근거](https://p.tossinvest.com/ko/post?category=52&id=18410&type=notice).', '']
    for period, title in [('full_year', '전체 회고 기간'), ('later_slice', '후반 구간: 2026-08-27 이후, 이미 살펴본 기간')]:
        lines += [f'## {title}', '', '| 방식 | 진입 | 비용 후 본절 이상 | 양의 수익 | 정확한 본절 | 손실 | 미청산 | 평균 순손익(완료) | 3% 일부 체결 | 5% 일부 체결 |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for policy, label in POLICIES.items():
            s = summary['statistics'][period][policy]
            lines.append(f"| {label} | {s['entries']} | {s['successes']} ({s['success_rate']:.1%}) | {s['positive']} | {s['exact_flat']} | "
                f"{s['losses']} | {s['unclosed']} | {s['mean_net_return_completed']:.3%} | {s['tp3_any']} | {s['tp5_any']} |")
        lines += ['']
    lines += ['## 해석 범위', '',
        '- 본절 이상 비율은 전량 청산되고 부분 익절까지 합한 순손익≥0인 건수/전체 진입이다. 미청산은 성공이 아니다.',
        '- 1,119종목은 기존 20일 중간 거래대금 조건으로 수정 일봉을 수집했던 표본이다. 전체2,741종목·모든 사용자 규칙 재현으로 해석하지 않는다. 신규상장 분기·과거 위험종목·섹터 정렬은 제외했다.',
        '- KRX 일봉은 기존 스캐너 원천으로 새로 수집했다. 과거 분할·병합의 가격 배율은 토스 원시/수정 일봉 비율로 환산했다. 과거 수정 이력 vintage는 없으며, 확정 일봉과 같은 종가 체결은 이상화 가정이다. 09:00 이전 가격 경로는 본절 보호 대상이 아니다.',
        '- 봉내 순서를 모르므로 손절·익절 동시 접촉은 손절 우선이다. 거래량·호가·틱·주문 지연과 실제 종가 체결은 별도 검증이 필요하다.',
        '- 하루 최대3개 신규 진입의 독립 사례 비교다. 미청산 잔량의 다음날 점유·현금 제약을 반영한 계좌 복리수익률은 산출하지 않았다.',
        '- 후반 구간도 이미 공개된 연구 기간이다. 하락 전용 형태 정답 독립 검증이나 새 모델 학습은 수행하지 않았다.', '',
        '[거래별 CSV](trades.csv) · [전체 통계](statistics.json) · [동결 조건](protocol.json) · [분봉 범위](coverage.csv)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    run(parser.parse_args().output.resolve())
