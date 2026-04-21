"""
TradeSync Notification Bot
===========================
Sends trade alerts, approval requests, and system status to Francis via Telegram.
Accepts inline button responses for signal approval/rejection.
"""

import os
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

from src.state.database import (
    save_pending_approval, update_approval_status,
    update_signal_status, get_pending_approval,
    is_kill_switch_active, set_setting,
    set_channel_paused, log_event, get_setting
)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID   = int(os.getenv('TELEGRAM_CHAT_ID'))

# Shared bot instance for sending messages from other modules
_bot = Bot(token=BOT_TOKEN)


# ── Outbound alerts ──────────────────────────────────────────────────────────

async def send_message(text: str):
    """Send a plain text message to Francis."""
    try:
        await _bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='HTML')
    except Exception as e:
        print(f"   ⚠️  Bot send error: {e}")


async def send_approval_request(signal_id: str, channel_name: str, signal_data: dict):
    """Send an uncertain signal to Francis with inline approve/reject buttons."""

    pair       = signal_data.get('pair', 'Unknown')
    direction  = signal_data.get('direction', 'Unknown')
    sl         = signal_data.get('sl', 'N/A')
    tp         = signal_data.get('tp', [])
    confidence = signal_data.get('confidence', 'low')
    reasoning  = signal_data.get('reasoning', '')
    timeout    = int(get_setting('approval_timeout_minutes') or 10)
    expires    = datetime.utcnow() + timedelta(minutes=timeout)
    tp_display = ', '.join(str(t) for t in tp) if isinstance(tp, list) else str(tp)

    text = (
        f"⚠️ <b>UNCERTAIN SIGNAL #{signal_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Channel:    {channel_name}\n"
        f"💱 Pair:       {pair}\n"
        f"📈 Direction:  {direction}\n"
        f"🛑 SL:         {sl}\n"
        f"🎯 TP:         {tp_display}\n"
        f"🔍 Confidence: {confidence}\n"
        f"💬 Reason:     {reasoning}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Expires in {timeout} minutes"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{signal_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{signal_id}"),
        ],
        [
            InlineKeyboardButton("⏸ Pause Channel", callback_data=f"pause:{signal_id}"),
        ]
    ])

    try:
        msg = await _bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode='HTML',
            reply_markup=keyboard
        )
        save_pending_approval(
            signal_id=signal_id,
            expires_at=expires.isoformat(),
            bot_message_id=str(msg.message_id)
        )
        print(f"   📲 Approval request sent for signal {signal_id}")
        log_event('approval_requested', f"Sent approval for {direction} {pair}", signal_id=signal_id)

    except Exception as e:
        print(f"   ❌ Could not send approval request: {e}")


async def send_execution_confirmation(signal_id: str, signal_data: dict,
                                      lot_size: float, position_id: str,
                                      account_balance: float, risk_percent: float):
    """Send trade execution confirmation."""
    pair       = signal_data.get('pair', 'Unknown')
    direction  = signal_data.get('direction', 'Unknown')
    sl         = signal_data.get('sl', 'N/A')
    tp         = signal_data.get('tp', [])
    risk_usd   = round(account_balance * risk_percent, 2)
    tp_display = ', '.join(str(t) for t in tp) if isinstance(tp, list) else str(tp)

    text = (
        f"✅ <b>TRADE EXECUTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair:      {pair}\n"
        f"📈 Direction: {direction}\n"
        f"📦 Lots:      {lot_size}\n"
        f"🛑 SL:        {sl}\n"
        f"🎯 TP:        {tp_display}\n"
        f"💰 Risk:      ${risk_usd} ({int(risk_percent*100)}%)\n"
        f"🔖 Position:  #{position_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await send_message(text)


async def send_trade_closed(pair: str, position_id: str, reason: str = "signal"):
    """Notify Francis a trade was closed."""
    text = (
        f"🔒 <b>TRADE CLOSED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair:     {pair}\n"
        f"🔖 Position: #{position_id}\n"
        f"📋 Reason:   {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await send_message(text)


async def send_system_alert(message: str):
    """Send a system-level alert."""
    await send_message(f"🚨 <b>SYSTEM ALERT</b>\n{message}")


# ── Inbound command handlers ─────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses."""
    query  = update.callback_query
    await query.answer()

    action, signal_id = query.data.split(':', 1)

    if action == 'approve':
        approval = get_pending_approval(signal_id)
        if not approval:
            await query.edit_message_text(f"⚠️ Signal #{signal_id} already processed or expired.")
            return

        if datetime.utcnow() > datetime.fromisoformat(approval['expires_at']):
            update_approval_status(signal_id, 'expired')
            update_signal_status(signal_id, 'expired')
            await query.edit_message_text(f"⏱ Signal #{signal_id} expired.")
            return

        update_approval_status(signal_id, 'approved')
        update_signal_status(signal_id, 'approved')
        log_event('signal_approved', f"Signal {signal_id} approved", signal_id=signal_id)
        await query.edit_message_text(f"✅ Signal #{signal_id} approved — executing...")

        from src.executor.safety_gate import process_approved_signal
        await process_approved_signal(signal_id)

    elif action == 'reject':
        update_approval_status(signal_id, 'rejected')
        update_signal_status(signal_id, 'rejected')
        log_event('signal_rejected', f"Signal {signal_id} rejected", signal_id=signal_id)
        await query.edit_message_text(f"❌ Signal #{signal_id} rejected.")

    elif action == 'pause':
        log_event('channel_paused', f"Channel paused via signal {signal_id}", signal_id=signal_id)
        await query.edit_message_text("⏸ Channel paused. Use /resume to re-enable.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kill = "🔴 ON" if is_kill_switch_active() else "🟢 OFF"
    risk = float(get_setting('risk_percent') or 0.02) * 100
    text = (
        f"📊 <b>TRADESYNC STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Kill Switch: {kill}\n"
        f"Risk/trade:  {risk}%\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting('kill_switch', 'on')
    log_event('kill_switch', 'Kill switch activated')
    await update.message.reply_text("🔴 <b>Kill switch ON.</b> All execution halted.", parse_mode='HTML')


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting('kill_switch', 'off')
    log_event('kill_switch', 'Kill switch deactivated')
    await update.message.reply_text("🟢 <b>Kill switch OFF.</b> Execution resumed.", parse_mode='HTML')


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /risk [percent] e.g. /risk 2")
        return
    try:
        pct = float(context.args[0])
        if pct <= 0 or pct > 100:
            raise ValueError
        set_setting('risk_percent', str(pct / 100))
        log_event('settings_change', f"Risk updated to {pct}%")
        await update.message.reply_text(f"💰 Risk updated to <b>{pct}%</b>", parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("⚠️ Use a number between 1 and 100.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 <b>TRADESYNC COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/status   — System status\n"
        "/kill     — Halt all execution\n"
        "/resume   — Resume execution\n"
        "/risk [%] — Set risk per trade\n"
        "/help     — This message\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text, parse_mode='HTML')


# ── Bot runner ───────────────────────────────────────────────────────────────

def run_bot():
    """Start the bot — uses PTB's own event loop management."""
    print("🤖 Starting TradeSync notification bot...")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("kill",   cmd_kill))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("risk",   cmd_risk))
    app.add_handler(CommandHandler("help",   cmd_help))

    print("✅ Bot running. Send /help to your bot to confirm.")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    run_bot()
    