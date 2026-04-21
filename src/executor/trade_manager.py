"""
Handles management commands: close, breakeven, SL moves.
"""
from src.state.database import get_open_position_by_pair, close_position, log_event

async def handle_management(command: dict, channel_id: str):
    action = command.get('action', '').lower()
    pair   = command.get('pair', '')

    position = get_open_position_by_pair(channel_id, pair)

    if not position:
        print(f"   ⚠️  No open position found for {pair} on this channel")
        return

    if action == 'close':
        from src.executor.ctrader_executor import CTraderExecutor
        executor = CTraderExecutor()
        await executor.connect()
        await executor.close_position_by_id(
            ctrader_position_id=position['ctrader_position_id'],
            pair=pair,
            lot_size=position['lot_size']
        )
        close_position(position['signal_id'])
        log_event('trade_closed', f"Closed {pair} via management command", channel_id=channel_id)

        from src.bot.notification_bot import send_trade_closed
        await send_trade_closed(pair, position['ctrader_position_id'], reason="signal")
        executor.disconnect()

    else:
        print(f"   ⚠️  Management action '{action}' not yet implemented")
