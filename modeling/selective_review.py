"""Human review of pre-entry boundary cases; causal top-three study report."""
from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import atomic_json
from .daily_trend_study import POLICIES,OUTPUT as TREND
from .nextday_eval import daily_frame
from .pattern_eval import sha
from .selective_daytrade import MODES,OUTPUT,SOURCE


def choose_cases(frame):
    """Strata use information available at the signal, never subsequent returns."""
    specs=[('structure_pending','눌림·재상승 확인 전 알림',~frame.candidate,6),
           ('flow_fading','직전 구간보다 거래량 약함',frame.volume_expansion5.lt(1),6),
           ('extension','장중 상승·VWAP 이격이 큼',frame.open_return.gt(.01)&frame.vwap_extension_atr.ge(2),6),
           ('confirmed_control','프로그램상 구조 확인 대조',frame.candidate,4),
           ('top3_control','전일 추세 상위3 대조',frame.top3,2)]
    chosen=[];used=set()
    for i,(kind,label,mask,count) in enumerate(specs):
        group=frame.loc[mask & ~frame.index.isin(used)]
        if group.empty:continue
        sample=group.sample(min(count,len(group)),random_state=20261101+i)
        used.update(sample.index)
        sample=sample.copy();sample['review_group']=kind;sample['review_group_label']=label;chosen.append(sample)
    return pd.concat(chosen,ignore_index=True)


def draw_chart(frame,path,title,daily=False,levels=None,signal=None):
    fig,(ax,volume_ax)=plt.subplots(2,1,sharex=True,figsize=(12,5.4),height_ratios=[3,1],layout='constrained');x=np.arange(len(frame))
    colors=np.where(frame.close>=frame.open,'#138d75','#bf4938')
    ax.vlines(x,frame.low,frame.high,colors=colors,linewidth=.8)
    ax.bar(x,np.maximum(np.abs(frame.close-frame.open),.00001),bottom=np.minimum(frame.open,frame.close),width=.65,color=colors)
    volume_ax.bar(x,frame.volume,color=colors,width=.65);volume_ax.set_ylabel('Volume');volume_ax.grid(alpha=.15)
    for name,label,color in [('ma20','MA20','#c0841a'),('ma60','MA60','#64748b'),('ema20_price','EMA20','#c0841a'),('vwap_price','Session VWAP','#64748b')]:
        if name in frame:ax.plot(x,frame[name],label=label,color=color,linewidth=1)
    for (label,value),color in zip((levels or {}).items(),['#2563eb','#7c3aed','#b45309','#4d7c0f']):
        if value is not None and np.isfinite(value):ax.axhline(value,label=label,color=color,linestyle='--',linewidth=.8)
    if signal is not None:
        positions=np.flatnonzero(frame.timestamp.eq(pd.Timestamp(signal)))
        if len(positions):ax.axvline(positions[0],color='#dc2626',linestyle=':',label='Model signal')
    ax.set_title(title);ax.set_ylabel('Adjusted daily price' if daily else 'Raw minute price');ax.grid(alpha=.15)
    ticks=np.unique(np.r_[np.arange(0,len(frame),max(1,len(frame)//7)),len(frame)-1])
    labels=frame.date.iloc[ticks] if daily else frame.timestamp.dt.tz_convert('Asia/Seoul').dt.strftime('%H:%M').iloc[ticks]
    volume_ax.set_xticks(ticks,labels,rotation=20,ha='right')
    if ax.get_legend_handles_labels()[0]:ax.legend(fontsize=8,loc='best')
    path.parent.mkdir(parents=True,exist_ok=True);fig.savefig(path,dpi=105);plt.close(fig)


def make_review(output):
    output=Path(output);folder=output/'review';folder.mkdir(exist_ok=True)
    diagnostics=pd.read_parquet(output/'diagnostics.parquet')
    picked=choose_cases(diagnostics)
    watch=pd.read_parquet(SOURCE/'watchlist.parquet').set_index(['symbol','day'])
    trades=pd.read_parquet(TREND/'trades.parquet')
    trades=trades.loc[trades.policy.eq('half_percent')&trades.method.eq('system_bearish_transfer_model')].set_index(['symbol','day'])
    names={r['symbol']:r['name'] for r in json.loads((SOURCE/'universe.json').read_text())['symbols']}
    cards=[];manifest=[]
    for n,r in enumerate(picked.itertuples(index=False),1):
        identity=f'R{n:02d}';symbol,day=r.symbol,r.day;daily=watch.loc[(symbol,day)]
        a=daily_frame(SOURCE/'daily'/f'{symbol}-1d-adjusted.parquet');raw=daily_frame(SOURCE/'daily'/f'{symbol}-1d-raw.parquet')
        a['ma20']=a.close.rolling(20).mean();a['ma60']=a.close.rolling(60).mean()
        a=a.loc[a.date.le(r.selection_day)].tail(90).copy()
        ratio=float(raw.loc[raw.date.eq(r.selection_day),'close'].iloc[0]/a.close.iloc[-1])
        daily_levels={label:float(daily[col]/ratio) if pd.notna(daily[col]) else None
                      for label,col in [('Reference','reference_price'),('Secret','secret_price'),('Point','point_price')]}
        full=pd.read_parquet(output/'frames'/f'{symbol}-{day}.parquet')
        prefix=full.loc[full.timestamp.le(pd.Timestamp(r.signal_at))].tail(100).copy()
        assert prefix.timestamp.max()==pd.Timestamp(r.signal_at) and a.date.max()==r.selection_day
        lev={label:getattr(r,col) for label,col in [('Resistance','resistance_price'),('Structure low','structural_low'),('Pullback low','pullback_low')]}
        draw_chart(a,folder/f'charts/{identity}-daily.png',f'{identity} {symbol} | D close {r.selection_day} only',True,daily_levels)
        local_signal=pd.Timestamp(r.signal_at).tz_convert('Asia/Seoul').strftime('%Y-%m-%d %H:%M KST')
        draw_chart(prefix,folder/f'charts/{identity}-prefix.png',f'{identity} {symbol} | signal {local_signal} | NO FUTURE BARS',levels=lev,signal=r.signal_at)
        result=trades.loc[(symbol,day)]
        future_levels={'Entry':result.entry_price,'Initial stop':result.stop_price,'TP3':result.entry_price*1.03,'TP5':result.entry_price*1.05}
        draw_chart(full,folder/f'charts/{identity}-result.png',f'{identity} {symbol} | FULL SESSION / OUTCOME DISCLOSED',levels=future_levels,signal=r.signal_at)
        pct=lambda v:'알 수 없음' if pd.isna(v) else f'{v:.2%}'
        number=lambda v:'알 수 없음' if pd.isna(v) else f'{v:.2f}'
        outcome=f"비용 후 순손익 {pct(result.realized_net_return) if result.complete else '미청산'} · {result.outcome_category} · 청산 사유 {result.exit_reason}"
        manifest.append(dict(id=identity,symbol=symbol,name=names[symbol],day=day,selection_day=r.selection_day,signal_at=r.signal_at,
            group=r.review_group,program_rule_stage=r.rule_stage,program_candidate=bool(r.candidate),
            daily_strength_rank=None if pd.isna(r.daily_strength_rank) else int(r.daily_strength_rank),
            up_score=float(r.up_score),volume_expansion5=None if pd.isna(r.volume_expansion5) else float(r.volume_expansion5),
            vwap_extension_atr=None if pd.isna(r.vwap_extension_atr) else float(r.vwap_extension_atr),
            source_frame_sha256=sha(output/'frames'/f'{symbol}-{day}.parquet'),
            daily_last_date=a.date.max(),prefix_last_timestamp=prefix.timestamp.max().isoformat(),
            selection_basis='Pre-signal heuristic stratum, no PnL selection',annotation_status='pending'))
        shape='<option value="">형태 판단 선택</option><option value="confirmed">뒤틀림 완성</option><option value="forming">형성 중</option><option value="absent">뒤틀림 아님</option><option value="unclear">보류</option>'
        entry='<option value="">진입 판단 선택</option><option value="enter">진입 가능</option><option value="wait">기다림</option><option value="avoid">제외</option><option value="unclear">보류</option>'
        cards.append(f'''<section id="{identity}" class="case" data-id="{identity}"><h2>{identity} · {html.escape(names[symbol])}({symbol}) · {day} {pd.Timestamp(r.signal_at).tz_convert('Asia/Seoul').strftime('%H:%M')}</h2>
        <p><b>검토 구분:</b> {r.review_group_label}. 프로그램 단계={r.rule_stage}, 확인 신호={r.candidate}. 이 구분은 정답이 아닙니다.</p>
        <p>전일 추세 순위 {number(r.daily_strength_rank)} · 모델 점수 {r.up_score:.3f}(보정된 확률 아님) · 최근 거래량 {number(r.volume_expansion5)}배 · VWAP 이격 {number(r.vwap_extension_atr)}ATR · 시초가 대비 {pct(r.open_return)}</p>
        <p>전일 선정: {html.escape(str(daily.reason))}. 일봉 포인트 단계={html.escape(str(daily.stage))}. 이 목록 안에서 장중 매수를 판단하는 검수이며 포인트·시크릿의 장중 재확인을 새 강제 조건으로 넣지 않았습니다.</p>
        <img loading="lazy" src="charts/{identity}-daily.png"><img loading="lazy" src="charts/{identity}-prefix.png">
        <div class="inputs"><select aria-label="{identity} 형태 판단" class="shape">{shape}</select><select aria-label="{identity} 진입 판단" class="entry">{entry}</select><input aria-label="{identity} 판단 근거" class="notes" placeholder="판단 근거·추가로 보고 싶은 정보"></div>
        <details class="outcome"><summary>판단을 적은 뒤 이후 가격·모의 결과 보기</summary><p>{html.escape(outcome)}</p><img loading="lazy" src="charts/{identity}-result.png"></details></section>''')
    atomic_json(folder/'manifest.json',dict(samples=manifest,heuristic_strata_not_gold=True,labels_pending=True))
    atomic_json(folder/'annotations-template.json',dict(samples=[dict(id=r['id'],shape='',entry='',notes='',outcome_seen=False,status='pending') for r in manifest]))
    intro='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>장중 진입 경계 사례 검토</title>
    <style>body{font:16px/1.7 system-ui;color:#183044;max-width:1250px;margin:25px auto;padding:15px}section{border-top:3px solid #ccd;margin-top:35px;padding-top:15px}img{width:100%;border:1px solid #eee}.inputs{display:flex;gap:10px;flex-wrap:wrap;margin:15px 0}input{flex:1;min-width:250px}select,input,button{font:inherit;padding:8px}details{background:#f4f6f8;padding:12px}header{position:sticky;top:0;background:#fff;padding:10px;z-index:2;border-bottom:1px solid #ccc}</style>
    <header><button id="export">판단 JSON 내보내기</button> <span id="progress"></span> · <a href="../REPORT.html">상위3 재생 결과</a></header>
    <h1>전일 선정은 유지하고, 장중에 기다렸어야 할 자리를 검토</h1>
    <p>전일 규칙을 통과한 연구 관찰목록에서 나온 모델 진입 사례다. 구조 확인 대기·거래량 둔화·가격 이격 등의 수치로 골랐으며 이후 손익으로 골라내지 않았다. 대조 사례도 포함하고 있으므로 전부 잘못된 매수라는 뜻은 아니다. 형태와 진입 적합성을 따로 판단해 달라. 미래 결과는 접힌 영역에 있으며 먼저 열면 그 사실도 내보내기에 기록된다. 이 검수 표본으로 정밀도·재현율을 추정하거나 자동 정답을 만들지 않는다.</p>'''
    script='''<script>
    const cases=[...document.querySelectorAll('.case')],key='selective-review-20261101-v2';
    let previous={};try{previous=JSON.parse(localStorage.getItem(key)||'{}');}catch(e){}
    const read=c=>({id:c.dataset.id,shape:c.querySelector('.shape').value,entry:c.querySelector('.entry').value,notes:c.querySelector('.notes').value,outcome_seen:c.dataset.seen==='true',status:c.querySelector('.shape').value||c.querySelector('.entry').value?'reviewed':'pending'});
    function save(){const rows=cases.map(read);try{localStorage.setItem(key,JSON.stringify(Object.fromEntries(rows.map(r=>[r.id,r]))));}catch(e){}document.querySelector('#progress').textContent=rows.filter(r=>r.status==='reviewed').length+'/'+rows.length+'건 작성';}
    for(const c of cases){let r=previous[c.dataset.id];if(r){c.querySelector('.shape').value=r.shape||'';c.querySelector('.entry').value=r.entry||'';c.querySelector('.notes').value=r.notes||'';c.dataset.seen=String(!!r.outcome_seen);}c.addEventListener('input',save);c.querySelector('details').addEventListener('toggle',e=>{if(e.target.open){c.dataset.seen='true';save();}});}
    document.querySelector('#export').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify({created_at:new Date().toISOString(),label_type:'human_review_separate_shape_and_entry',samples:cases.map(read)},null,2)],{type:'application/json'}));a.download='selective-review-annotations.json';a.click();URL.revokeObjectURL(a.href);};save();
    </script>'''
    (folder/'REVIEW.html').write_text(intro+''.join(cards)+script)
    return manifest


def render_report(output):
    output=Path(output);r=json.loads((output/'statistics.json').read_text())
    opportunity=json.loads((output/'later-price-opportunity.json').read_text())
    oc=opportunity['counts']
    rows=[]
    for mode,label in MODES.items():
        for policy,pname in POLICIES.items():
            key=f'{mode}/{policy}/system_bearish_transfer_model'
            v=r['statistics']['post_training'].get(key)
            if not v:
                rows.append(f'<tr><td>{label}</td><td>{pname}</td><td colspan="9">실제 진입 없음</td></tr>');continue
            s,c,a,e=v['accuracy'],v['categories'],v['account'],v['execution']
            acc='—' if s['accuracy'] is None else f"{s['accuracy']:.1%}"
            rows.append(f"<tr><td>{label}</td><td>{pname}</td><td>{s['successes']}/{s['entries']}</td><td>{acc}</td><td>{c['rise_then_breakeven_success']}</td><td>{e['mean_net_return']:.3%}</td><td>{e['median_capital']:,.0f}원</td><td>{a['net_pnl_krw']:,.0f}원</td><td>{a['max_drawdown_pct']:.2f}%</td><td>{e.get('tp3_any',0)}/{e.get('tp5_any',0)}</td><td>{v['proposed_entries']-v['accepted_entries']}</td></tr>")
    diag=pd.read_parquet(output/'diagnostics.parquet')
    annual=[]
    for mode,label in MODES.items():
        v=r['statistics']['full_year'][f'{mode}/half_percent/system_bearish_transfer_model']
        s,e,a=v['accuracy'],v['execution'],v['account']
        annual.append(f"<tr><td>{label}</td><td>{s['successes']}/{s['entries']}</td><td>{s['accuracy']:.1%}</td><td>{e['mean_net_return']:.3%}</td><td>{a['net_pnl_krw']:,.0f}원</td><td>{e['tp3_any']}/{e['tp5_any']}</td><td>{s['unclosed']}</td></tr>")
    body=f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>전일 강한3종목과 장중 확인 비교</title>
    <style>body{{font:15px/1.7 system-ui;max-width:1450px;margin:30px auto;padding:15px;color:#183044}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd;padding:8px}}th{{background:#eef3f5}}</style>
    <h1>전일 관찰목록 → 강한3종목 → 장중 뒤틀림</h1>
    <p><a href="review/REVIEW.html">사람이 판단할 경계·대조 사례 열기</a></p>
    <p>사용자가 설명한 역할을 구분했다. 전일 종가의 규칙 확인은 관찰목록 단계이고, 당일 매수는 그 안에서 뒤틀림과 강도를 판단하는 단계다. 기존 저장 관찰목록을 유지했으며 포인트·시크릿의 장중 재확인을 새 강제 필터로 추가하지 않았다. 이 관찰목록은 연구용 대리 조건을 포함해 실제 사용자 명단과의1:1 일치는 별도 검증이다.</p>
    <p>하루3건은 최대3건이다. D 종가 기준 큰 상승 추세 안에서20일 수익률50%, MA60의20일 상승률25%, ATR20 변동성15%,20일 중앙 거래대금10%의 당일 후보 내 백분위 점수로3종목을 고정했다. 다음날 신호가 없는 종목도 순위 계산에 포함했고 다른 종목으로 대체하지 않는다. 숫자·가중치는 결과를 보기 전에 고정한 실험값이며 최적화된 기준은 아니다. 다음날 종가로 가장 많이 오른 종목을 골라낸 결과가 아니다.</p>
    <p>장중 확인 추가는 기존 모델 점수와 유동성 조건에 더해 프로그램의 돌파→눌림→재상승 확인을 기다린다. 거래량 회복 추가는 최근5봉 평균 거래량이 앞선 기준 구간 이상인 조건이다. 이 자동 단계는 사람이 검증한 정답이 아니다. 초기 손절,3%/35%,5%/35%, 잔량30%, 진입 시간은 유지하며 사용자 확정 본절은+0.5% 정책이다. 첫3% 이후 발동은 비교용, 한 봉 지연은 체결 민감도다.</p>
    <p>초기자금1,000만원, 기존 제안 수량 종목당 최대300만원, 최대3종목, 하루최대3건, 일일2% 손실 신규진입 제한을 기존 계좌 엔진으로 재생했다. 자금 부족 시 제안 수량을 임의 축소하지 않고 건너뛴다. 부분 매도 대금은 전량 청산 때 현금에 반영하는 보수적 가정이다. 미청산은 자금·슬롯을 계속 차지하며 마지막 관측 가격으로 평가한다. 후속 가격을 복원하지 못한 잔량의 진짜 손익은 알 수 없다.</p>
    <h2>학습 기준일 이후2026-08-27~10-28: 하락 전이 모델, 초기1,000만원으로 별도 시작</h2>
    <table><tr><th>장중 조건</th><th>본절 정책</th><th>성공/진입</th><th>정확도</th><th>본절 성공</th><th>거래당 평균 순손익률</th><th>매수금 중앙값</th><th>계좌 평가손익</th><th>최대 낙폭</th><th>3%/5% 일부 체결</th><th>자금·슬롯·일일제한 제외</th></tr>{''.join(rows)}</table>
    <p><b>사용자 정책+0.5%에서 세 조건 모두 순손실이며 확인 조건을 더한 정확도 개선도 확인되지 않았다.</b> 조건 추가 시 총손실이 줄어든 정도에는 거래 수와 투입 금액 감소가 포함된다. 매수금은300만원 고정이 아니라 직전·진입 봉 거래량1% 용량으로 제한된 제안 수량이다. 따라서 절대 손실 감소만으로 우수한 진입법이라고 판단하지 않는다.</p>
    <p>정확도는 실제 진입 전체 중 비용 포함 총손익이 본절 이상이고 전량 청산된 비율이다. 수익과 실제 본절 청산을 모두 성공으로 세고 미청산은 성공이 아니다. 이후 기간도 이미 본 연구 구간이며 수치 차이를 독립 성능 개선이나 실체결 확률로 주장하지 않는다. 현재 상장 목록·일봉 대리 규칙·380봉 세션 필터·분봉 순서·가정 비용 한계를 유지한다.</p>
    <h2>과거1년2025-10-29~2026-10-28: 사용자+0.5% 본절·하락 전이 모델</h2>
    <p>148종목·726개 상위3 선정일을 포함해 같은 조건을 재생했다. 초기1,000만원으로 시작한 별도 계좌다. 학습에 사용한 시기까지 포함한 회고적 결과이며 독립 성능은 아니다.</p>
    <table><tr><th>장중 조건</th><th>성공/진입</th><th>정확도</th><th>거래당 평균 순손익률</th><th>계좌 평가손익</th><th>3%/5% 일부 체결</th><th>미청산</th></tr>{''.join(annual)}</table>
    <h2>기존 장중 진입의 검토 근거</h2><p>이후 구간 {len(diag)}건의 프로그램 단계 분포: {html.escape(str(diag.rule_stage.value_counts().to_dict()))}. 확인 단계가 아니라는 이유만으로 잘못된 매수라고 확정하지 않고 사람이 차트를 판단하도록 했다.</p>
    <h2>수익을 길게 가져갈 여지가 있었나</h2>
    <p>현재 신호·사용자+0.5%·하락 전이 모델의 실제 채택{opportunity['entries']}건 중, 청산한 뒤 관측 고가가 매수 체결가 대비+3% 가격대를 넘은 사례는{oc['after_exit_price_tp3']}건,+5%는{oc['after_exit_price_tp5']}건이다. 고가에 매도 충격 가정은 반영했지만 체결 용량·호가·봉내 순서는 반영하지 않은 사후 가격 관찰이다. 이 가격에 팔 수 있었다거나 손절 없이 기다릴 수 있었다는 뜻이 아니다. 실제 이 정책의3%/5% 부분 익절 체결은0/0건이었다.</p>
    <p>모델 병행 보유 봉 수 중앙값은{opportunity['model_median_holding_bars']:.0f}봉, 같은 진입에서 하락 모델을 끈 고정 주문만도{opportunity['same_entry_system_only_median_holding_bars']:.0f}봉이다. 단순히 하락 모델을 끄거나 오래 보유하면 해결된다는 근거는 없다. 같은53건 기준 거래당 평균 순손익은 모델 병행{opportunity['same_entry_model_net_return_mean']:.3%}, 고정 주문만{opportunity['same_entry_system_only_net_return_mean']:.3%}다.</p>
    <p>다음 검증 후보는 본절 청산 이후 새 눌림·재상승이 확인되는 재진입, 진입 시 지지선까지의 거리와 평상시 분봉 변동폭의 비율, 당일 섹터·지수 대비 상대강도다. 현재 가중치나 본절·익절 규칙을 사후 수익에 맞춰 변경하지 않는다. 재진입도 하루 총3건 한도에 포함해야 하며 아직 이 보고서의 검증된 정책은 아니다.</p>
    <p><a href="later-price-opportunity.json">청산 사유별 이후 가격 관찰</a> · <a href="later-price-opportunity.parquet">거래별 이후 가격·동일 진입 고정주문 대조</a> · <a href="verification.json">수량·현금·정확도 검산</a></p>
    <p><a href="statistics.json">전체·이후 구간 및 고정주문만 비교</a> · <a href="accounts.json">계좌별 일별 손익·실제 채택 로그</a> · <a href="selection.parquet">전일 순위·전체 후보</a> · <a href="trades.parquet">제안 거래별 체결</a> · <a href="protocol.json">고정 정의·원본 해시</a></p>'''
    (output/'REPORT.html').write_text(body)


if __name__=='__main__':
    render_report(OUTPUT);samples=make_review(OUTPUT)
    print(json.dumps(dict(review_cases=len(samples),groups=pd.Series([r['group'] for r in samples]).value_counts().to_dict()),ensure_ascii=False))
