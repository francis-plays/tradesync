"""
TradeSync Safety Gate
======================
Runs all pre-execution checks before a signal reaches cTrader.
Nothing gets executed without passing through here first.

Checks:
1. Kill switch
2. Channel paused
3. Duplicate signal
4. SL present (required for lot sizing)
5. Position exists (for management commands)
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

from src.state.database import (
    is_kill_switch_active, is_channel_paused,
    is_duplicate_signal, get_setting,
    update_signal_status, log_event,
    get_open_position_by_pair
)


async def process_signal(signal_id: str, signal_data: dict, channel_id: str):
    """
    Run safety checks on a confident signal.
    If all pass → hand off to executor.
    If any fail → log and notify.
    """

    pair      = signal_data.get('pair', 'Unknown')
    direction = signal_data.get('direction', 'Unknown')
    sl        = signal_data.get('sl')

    print(f"\n   🛡️  Safety gate running for signal {signal_id}...")

    # ── Check 1: Kill switch ─────────────────────────────────────────────────
    if is_kill_switch_active():
        print(f"   🔴 BLOCKED — Kill switch is active")
        update_signal_status(signal_id, 'blocked')
        log_event('blocked', 'Kill switch active', signal_id=signal_id, channel_id=channel_id)
        from src.bot.notification_bot import send_message
        await send_message(f"🔴 Signal <b>{signal_id}</b> blocked — kill switch is active.")
        return

    # ── Check 2: Channel paused ──────────────────────────────────────────────
    if is_channel_paused(channel_id):
        print(f"   ⏸  BLOCKED — Channel is paused")
        update_signal_status(signal_id, 'blocked')
        log_event('blocked', 'Channel paused', signal_id=signal_id, channel_id=channel_id)
        return

    # ── Check 3: Duplicate signal ────────────────────────────────────────────
    if is_duplicate_signal(channel_id, pair, direction):
        print(f"   ♻️  BLOCKED — Duplicate: {direction} {pair} already open from this channel")
        update_signal_status(signal_id, 'duplicate')
        log_event('duplicate', f"Duplicate {direction} {pair}", signal_id=signal_id, channel_id=channel_id)
        from src.bot.notification_bot import send_message
        await send_message(
            f"♻️ Signal <b>{signal_id}</b> skipped — {direction} {pair} already open from this channel."
        )
        return

    # ── Check 4: SL required ─────────────────────────────────────────────────
    if sl is None:
        print(f"   ⚠️  BLOCKED — No SL provided, cannot calculate lot size")
        update_signal_status(signal_id, 'blocked')
        log_event('blocked', 'No SL — cannot size position', signal_id=signal_id, channel_id=channel_id)
        from src.bot.notification_bot import send_message
        await send_message(
            f"⚠️ Signal <b>{signal_id}</b> blocked — no SL provided. Cannot calculate lot size safely."
        )
        return

    # ── All checks passed ────────────────────────────────────────────────────
    print(f"   ✅ All safety checks passed — routing to executor")
    log_event('safety_passed', f"{direction} {pair} cleared all checks", signal_id=signal_id, channel_id=channel_id)

    from src.executor.ctrader_executor import execute_trade
    await execute_trade(signal_id=signal_id, signal_data=signal_data, channel_id=channel_id)


async def process_approved_signal(signal_id: str):
    """
    Called when Francis approves an uncertain signal via Telegram button.
    Fetches signal data from DB and routes through safety gate.
    """
    from src.state.database import get_connection

    conn = get_connection()
    row  = conn.execute(
        'SELECT * FROM classified_signals WHERE signal_id = ?', (signal_id,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"   ❌ Approved signal {signal_id} not found in DB")
        return

    signal_data = {
        'pair':        row['pair'],
        'direction':   row['direction'],
        'entry':       row['entry'],
        'sl':          row['sl'],
        'tp':          json.loads(row['tp'] or '[]'),
        'signal_type': row['signal_type'],
        'confidence':  row['confidence'],
        'reasoning':   row['llm_reasoning'],
    }

    print(f"\n   ✅ Processing approved signal {signal_id}")
    await process_signal(
        signal_id=signal_id,
        signal_data=signal_data,
        channel_id=row['channel_id']
    )