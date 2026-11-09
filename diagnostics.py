"""Readable local diagnostics. Raw broker payloads and credentials are never exported."""
import json

LABELS = {
    'gap':'수익률 갭(%p)','gap_percent':'갭 기준(%p)','a_return':'A 보유 수익률(%)','b_return':'B 보유 수익률(%)',
    'armed':'갭 재진입 허용','daily_lots':'당일 기준수량과 고정주수','reference_price':'시장가 수량산정 참고가','order_type':'주문 방식',
    'account_cash':'100% 현금·D+2 기준 계좌 현금','target_100_percent_capacity':'매수 대상 종목별 100% 주문가능 한도',
    'query_status':'체결조회 상태',
    'quote_snapshots':'시세 조회값/수신 시각','received_at':'시세 수신 시각',
    'post_holdings':'거래 후 보유 상태','average_cost':'매도 종목 평균 매입가',
    'estimated_profit_before_costs':'원가비례 예상 매도손익(수수료·세금 차감 전)',
    'orders':'첫 매도 / 후속 매수 주문 상태','config_snapshot':'신호 시점 설정 사본','strategy_revision':'전략 로직 식별자',
    'cycle_id':'사이클 번호','state':'수익률 갭/재진입/당일 고정주수','reference':'기준가','mode':'대기 방향',
    'basis_source':'기준 출처','source':'매도 종목/기준 출처','target':'매수 종목',
    'b_current':'판단 당시 B 현재가','b_average':'B 평균 매입가','threshold':'신호 목표가',
    'comparison':'비교 조건','trigger_percent':'신호 비율(%)','action':'종목 간 매매 방향',
    'cap':'합산 원금 한도','total_cost':'A+B 합산 매입원금','daily_max':'일일 한도',
    'daily_count':'당일 예약 횟수','held':'보유수량','available':'매도가능수량',
    'percent':'매도수량 비율(%)','raw_qty':'버림 전 수량','qty':'주문수량',
    'budget':'매수금액 계산','price':'매수 지정가','proceeds':'매도 체결금/예상금',
    'cash':'매수 계산에 사용한 현금','headroom':'한도 여유','reserve_percent':'비용 예비율(%)',
    'one_share_with_reserve':'비용 여유 포함 1주 필요금액','limiting_factors':'최소금액을 제한한 항목',
    'quantity':'최종 수량','cash_query':'현금 API 조회값','cash_before':'매도 전 현금',
    'sell_price':'매도 지정가','cash_100_percent':'100% 현금 주문가능금액',
    'order_allowance':'주문가능금액','d2_deposit':'D+2 추정예수금','cash_unsettled':'현금미수금',
    'queried_at':'조회 시각','side':'매수/매도','symbol':'종목코드','order':'주문번호',
    'limit_price':'지정가','order_type':'주문 방식','requested_qty':'요청 수량',
    'filled_qty':'확인된 체결수량','gross':'실제 체결금액','average':'체결 평균가',
    'attempt':'재조회 차수','buy_order_sent':'후속 매수 전송 여부','failed_stage':'중단 직전 단계',
    'order_outcome':'주문 상태','completed_legs':'전량 체결 확인한 단계',
    'next_state':'다음 기준/방향','previous_state':'이전 기준/방향','incomplete_cycle':'미완료 기록 존재',
    'actions':'중단 후 조치','cash_note':'자금 상태 설명','api':'키움 API','broker_code':'증권사 응답 코드',
    'auto_retry':'자동 재전송','feasibility':'양방향 예상 가능 여부',
    'expected_source':'매도 종목 예상 잔고','actual_source':'매도 종목 조회 잔고',
    'expected_target':'매수 종목 예상 잔고','actual_target':'매수 종목 조회 잔고',
    'remaining_sale_cash_before_costs':'매도금-매수금 차액(수수료·세금 차감 전)',
    'settings':'적용 설정','detail':'사이클 계획','at':'시각','note':'참고',
    'sell_qty':'매도수량','estimated_proceeds':'예상 매도금','estimated_buy_qty':'예상 매수수량','possible':'예상 가능 여부'}


def readable(record):
    def translate(value):
        if isinstance(value,dict):
            return {LABELS.get(k,k):translate(v) for k,v in value.items()}
        if isinstance(value,list):
            return [translate(v) for v in value]
        return value
    return (f"[{record['trace']}] {record['at']}\n단계: {record['stage']}\n"
            f"설명: {record['explanation']}\n" +
            json.dumps(translate(record['data']),ensure_ascii=False,indent=2) + '\n')


def trade_history(records):
    """One row per trace/leg; cumulative fills replace rather than double count."""
    names = {}
    orders = {}
    for record in records:
        data = record['data']
        if record['stage'] == 'SIGNAL':
            for item in data.get('config_snapshot', {}).values():
                if isinstance(item,dict) and 'code' in item:
                    names[item['code']] = item.get('name',item['code'])
        if record['stage'] not in ('ORDER_ACCEPTED','PARTIAL_FILL','FILL_CONFIRMED','ORDER_REJECTED','ORDER_UNKNOWN'):
            continue
        side, symbol = data.get('side',''), data.get('symbol','')
        key = (record['trace'], side)
        previous = orders.get(key,{})
        previous.update(data)
        previous.update(trace=record['trace'], at=record['at'], stage=record['stage'], explanation=record['explanation'])
        previous['name'] = names.get(symbol,symbol)
        orders[key] = previous
    return sorted(orders.values(),key=lambda x:x['at'], reverse=True)
