"""
TradeSync cTrader Executor
===========================
Places, modifies, and closes trades via the cTrader Open API.
Calculates lot size dynamically from account balance and SL distance.

PAPER_MODE = True  → logs trades, sends Telegram alerts, no real execution
PAPER_MODE = False → live execution via cTrader Open API

Switch to False once your cTrader app status changes from Submitted → Active.
"""

import os
import json
import asyncio
from dotenv import load_dotenv

load_dotenv()

from src.state.database import (
    save_position, update_signal_status,
    log_event, get_setting
)

# ─── MODE CONFIG (change this one line to go live) ───────────────────────────
PAPER_MODE = True
# ─────────────────────────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv('CTRADER_CLIENT_ID')
CLIENT_SECRET = os.getenv('CTRADER_CLIENT_SECRET')
ACCOUNT_ID    = int(os.getenv('CTRADER_ACCOUNT_ID'))
HOST          = os.getenv('CTRADER_HOST', 'demo.ctraderapi.com')
PORT          = int(os.getenv('CTRADER_PORT', 5035))

# Pip values per lot for common pairs (USD account)
PIP_VALUES = {
    'EURUSD': 10.0, 'GBPUSD': 10.0, 'AUDUSD': 10.0,
    'NZDUSD': 10.0, 'USDCAD': 10.0, 'USDCHF': 10.0,
    'USDJPY': 10.0, 'GBPJPY': 10.0, 'EURJPY': 10.0,
    'XAUUSD': 10.0, 'NAS100': 10.0, 'US30':   10.0,
    'SPX500': 10.0, 'USOIL':  10.0, 'BTCUSD': 10.0,
}

# Pip size per pair (how much price moves per pip)
PIP_SIZES = {
    'USDJPY': 0.01, 'GBPJPY': 0.01, 'EURJPY': 0.01,
    'XAUUSD': 0.1,  'BTCUSD': 1.0,  'NAS100': 1.0,
    'US30':   1.0,
}


def get_pip_size(pair: str) -> float:
    return PIP_SIZES.get(pair, 0.0001)


def calculate_lot_size(account_balance: float, risk_percent: float,
                       sl_price: float, entry_price: float, pair: str) -> float:
    """
    lot_size = (balance × risk%) / (sl_pips × pip_value_per_lot)
    Minimum lot size: 0.01
    """
    pip_size    = get_pip_size(pair)
    sl_pips     = abs(entry_price - sl_price) / pip_size
    pip_value   = PIP_VALUES.get(pair, 10.0)

    if sl_pips == 0:
        return 0.01

    risk_amount = account_balance * risk_percent
    lot_size    = risk_amount / (sl_pips * pip_value)
    return max(round(lot_size, 2), 0.01)


# ── Paper mode execution ─────────────────────────────────────────────────────

async def execute_paper_trade(signal_id: str, signal_data: dict,
                               channel_id: str, lot_size: float,
                               account_balance: float, risk_percent: float):
    """Simulate trade execution — no real orders placed."""

    pair      = signal_data.get('pair')
    direction = signal_data.get('direction')
    sl        = signal_data.get('sl')
    tp_raw    = signal_data.get('tp', [])
    tp        = tp_raw if isinstance(tp_raw, list) else json.loads(tp_raw or '[]')
    tp_display= ', '.join(str(t) for t in tp) if tp else 'N/A'
    risk_usd  = round(account_balance * risk_percent, 2)
    fake_pos  = f"PAPER-{signal_id}"

    print(f"   📝 PAPER MODE — Simulating: {direction} {pair}")
    print(f"   📝 Lot size: {lot_size} | SL: {sl} | TP: {tp_display}")
    print(f"   📝 Risk: ${risk_usd} ({int(risk_percent*100)}%)")

    # Save to state store as paper position
    save_position(
        signal_id=signal_id,
        channel_id=channel_id,
        pair=pair,
        direction=direction,
        lot_size=lot_size,
        entry_price=0.0,
        sl=float(sl),
        tp=json.dumps(tp),
        ctrader_position_id=fake_pos
    )
    update_signal_status(signal_id, 'paper_executed')
    log_event('paper_trade', f"{direction} {pair} {lot_size} lots — paper mode",
              signal_id=signal_id, channel_id=channel_id)

    # Notify Francis via bot
    from src.bot.notification_bot import send_message
    await send_message(
        f"📝 <b>PAPER TRADE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair:      {pair}\n"
        f"📈 Direction: {direction}\n"
        f"📦 Lots:      {lot_size}\n"
        f"🛑 SL:        {sl}\n"
        f"🎯 TP:        {tp_display}\n"
        f"💰 Risk:      ${risk_usd} ({int(risk_percent*100)}%)\n"
        f"🔖 Position:  #{fake_pos}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>⏳ Live execution pending cTrader app approval</i>"
    )


# ── Live cTrader execution ───────────────────────────────────────────────────

async def execute_live_trade(signal_id: str, signal_data: dict,
                              channel_id: str, lot_size: float,
                              account_balance: float, risk_percent: float):
    """Place a real order via cTrader Open API."""
    from ctrader_open_api import Client, Protobuf, TcpProtocol
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiMessages_pb2 import *
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

    pair      = signal_data.get('pair')
    direction = signal_data.get('direction')
    sl        = float(signal_data.get('sl'))
    tp_raw    = signal_data.get('tp', [])
    tp        = tp_raw if isinstance(tp_raw, list) else json.loads(tp_raw or '[]')

    loop              = asyncio.get_event_loop()
    authorized_future = loop.create_future()
    position_future   = loop.create_future()
    client_obj        = Client(HOST, PORT, TcpProtocol)

    def on_connected(client):
        req = ProtoOAApplicationAuthReq()
        req.clientId     = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        client.send(req)

    def on_message(client, message):
        msg_type = message.payloadType

        if msg_type == ProtoOAApplicationAuthRes().payloadType:
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = ACCOUNT_ID
            req.accessToken         = os.getenv('CTRADER_ACCESS_TOKEN', '')
            client.send(req)

        elif msg_type == ProtoOAAccountAuthRes().payloadType:
            if not authorized_future.done():
                authorized_future.set_result(True)

            # Place order immediately after auth
            order = ProtoOANewOrderReq()
            order.ctidTraderAccountId = ACCOUNT_ID
            order.symbolName          = pair
            order.orderType           = ProtoOAOrderType.Value('MARKET')
            order.tradeSide           = (
                ProtoOATradeSide.Value('BUY') if direction == 'BUY'
                else ProtoOATradeSide.Value('SELL')
            )
            order.volume   = int(lot_size * 100000)
            order.stopLoss = sl
            if tp:
                order.takeProfit = float(tp[0])
            client.send(order)

        elif msg_type == ProtoOAExecutionEvent().payloadType:
            event       = Protobuf.extract(message, ProtoOAExecutionEvent)
            position_id = str(event.position.positionId) if event.position else None
            if position_id and not position_future.done():
                position_future.set_result(position_id)

        elif msg_type == ProtoOAOrderErrorEvent().payloadType:
            err = Protobuf.extract(message, ProtoOAOrderErrorEvent)
            print(f"   ❌ Order error: {err.description}")
            if not position_future.done():
                position_future.set_exception(RuntimeError(err.description))

    client_obj.setConnectedCallback(on_connected)
    client_obj.setMessageReceivedCallback(on_message)
    client_obj.setDisconnectedCallback(lambda c: print("   ⚠️  cTrader disconnected"))
    client_obj.startService()

    try:
        await asyncio.wait_for(authorized_future, timeout=10.0)
        position_id = await asyncio.wait_for(position_future, timeout=10.0)

        save_position(
            signal_id=signal_id,
            channel_id=channel_id,
            pair=pair,
            direction=direction,
            lot_size=lot_size,
            entry_price=0.0,
            sl=sl,
            tp=json.dumps(tp),
            ctrader_position_id=position_id
        )
        update_signal_status(signal_id, 'executed')
        log_event('trade_executed', f"{direction} {pair} {lot_size} lots — pos #{position_id}",
                  signal_id=signal_id, channel_id=channel_id)

        from src.bot.notification_bot import send_execution_confirmation
        await send_execution_confirmation(
            signal_id=signal_id,
            signal_data=signal_data,
            lot_size=lot_size,
            position_id=position_id,
            account_balance=account_balance,
            risk_percent=risk_percent
        )
        print(f"   ✅ Live trade executed — Position #{position_id}")

    finally:
        client_obj.stopService()


# ── Main entry point ─────────────────────────────────────────────────────────

async def execute_trade(signal_id: str, signal_data: dict, channel_id: str):
    """
    Route to paper or live execution based on PAPER_MODE flag.
    Calculates lot size before routing.
    """
    pair         = signal_data.get('pair')
    direction    = signal_data.get('direction')
    sl           = signal_data.get('sl')
    entry        = signal_data.get('entry')
    risk_percent = float(get_setting('risk_percent') or 0.02)

    # Estimate entry if not provided
    pip_size    = get_pip_size(pair)
    entry_price = float(entry) if entry else (
        float(sl) + (20 * pip_size) if direction == 'BUY'
        else float(sl) - (20 * pip_size)
    )

    # Use fixed balance in paper mode
    account_balance = 10000.0

    lot_size = calculate_lot_size(
        account_balance=account_balance,
        risk_percent=risk_percent,
        sl_price=float(sl),
        entry_price=entry_price,
        pair=pair
    )

    print(f"\n   {'📝' if PAPER_MODE else '📈'} {'Paper' if PAPER_MODE else 'Live'} execution: {direction} {pair}")
    print(f"   Lot size: {lot_size} | Risk: {risk_percent*100}%")

    try:
        if PAPER_MODE:
            await execute_paper_trade(
                signal_id=signal_id,
                signal_data=signal_data,
                channel_id=channel_id,
                lot_size=lot_size,
                account_balance=account_balance,
                risk_percent=risk_percent
            )
        else:
            await execute_live_trade(
                signal_id=signal_id,
                signal_data=signal_data,
                channel_id=channel_id,
                lot_size=lot_size,
                account_balance=account_balance,
                risk_percent=risk_percent
            )

    except Exception as e:
        print(f"   ❌ Executor error: {e}")
        update_signal_status(signal_id, 'failed')
        log_event('trade_failed', str(e), signal_id=signal_id, channel_id=channel_id)

        from src.bot.notification_bot import send_system_alert
        await send_system_alert(f"Trade execution failed for signal {signal_id}:\n{e}")
        