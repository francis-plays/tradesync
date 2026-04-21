"""
TradeSync Signal Classifier
============================
Current LLM: Groq (llama-3.2-11b-vision-preview) — FREE
To switch LLM: update the CLIENT and MODEL sections below only.

Swap options:
- Claude:  use anthropic library, model = "claude-sonnet-4-5"
- OpenAI:  use openai library,   model = "gpt-4o"
- Groq:    use groq library,     model = "llama-3.2-11b-vision-preview" (current)
"""

import os
import json
import uuid
import base64
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# ─── LLM CONFIG (change only this section to swap models) ───────────────────
LLM_PROVIDER = "groq"
LLM_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"
client       = Groq(api_key=os.getenv('GROQ_API_KEY'))
# ─────────────────────────────────────────────────────────────────────────────

from src.state.database import (
    save_signal, log_event,
    is_kill_switch_active, is_channel_paused
)

SYMBOL_MAP = {
    "GOLD": "XAUUSD",
    "XAU":  "XAUUSD",
    "GU":   "GBPUSD",
    "EU":   "EURUSD",
    "GJ":   "GBPJPY",
    "EJ":   "EURJPY",
    "UJ":   "USDJPY",
    "AU":   "AUDUSD",
    "NU":   "NZDUSD",
    "UC":   "USDCAD",
    "US":   "USDCHF",
    "NAS":  "NAS100",
    "NAZ":  "NAS100",
    "DJ":   "US30",
    "DOW":  "US30",
    "SP":   "SPX500",
    "OIL":  "USOIL",
    "BTC":  "BTCUSD",
    "ETH":  "ETHUSD",
}

CLASSIFIER_PROMPT = """You are a trading signal classifier for a Forex/CFD trade copier system.

Your job is to analyse a Telegram message (and image if provided) and classify it.

CATEGORIES:
1. signal     — A clear trade entry instruction
2. management — A trade management command (close, move SL, breakeven, partial close)
3. uncertain  — Possibly a signal but missing key info or ambiguous
4. noise      — General chat, news, analysis, or anything not actionable

CONTEXT (last 20 messages from this channel):
{context}

CURRENT MESSAGE:
{message}

RULES:
- signal requires: direction (BUY/SELL) + instrument/pair + ideally SL or TP
- If direction or pair is missing → uncertain
- If message says close/exit/take profit/move sl/breakeven → management
- Market commentary, news, motivation, unrelated chat → noise
- For images: look for entry arrows, SL/TP levels, chart annotations

Respond ONLY with a valid JSON object. No text before or after. No markdown fences.

Examples:

SIGNAL:
{{"classification":"signal","confidence":"high","pair":"EURUSD","direction":"BUY","entry":1.0850,"sl":1.0820,"tp":["1.0890","1.0920"],"signal_type":"market","reasoning":"Clear buy signal with entry SL and two TPs"}}

MANAGEMENT:
{{"classification":"management","action":"close","pair":"EURUSD","reasoning":"Trader says close EURUSD now"}}

UNCERTAIN:
{{"classification":"uncertain","confidence":"low","pair":"XAUUSD","direction":"BUY","entry":null,"sl":null,"tp":[],"signal_type":"market","reasoning":"Possible buy on Gold but no SL or TP"}}

NOISE:
{{"classification":"noise","reasoning":"General market commentary no trade instruction"}}
"""


def resolve_symbol(raw: str) -> str | None:
    """Resolve trader shorthand to broker symbol."""
    if not raw:
        return None
    upper = raw.upper().strip()
    return SYMBOL_MAP.get(upper, upper)


def validate_signal(data: dict) -> tuple[bool, str]:
    """Post-LLM rule validation. Returns (is_valid, reason)."""
    pair      = data.get('pair')
    direction = data.get('direction', '').upper()
    sl        = data.get('sl')
    entry     = data.get('entry')

    if not pair:
        return False, "No pair identified"

    if direction not in ('BUY', 'SELL'):
        return False, f"Invalid direction: {direction}"

    if sl is not None:
        try:
            sl = float(sl)
            if entry:
                entry = float(entry)
                if direction == 'BUY' and sl >= entry:
                    return False, f"BUY signal but SL ({sl}) is above entry ({entry})"
                if direction == 'SELL' and sl <= entry:
                    return False, f"SELL signal but SL ({sl}) is below entry ({entry})"
            if sl <= 0:
                return False, f"SL value nonsensical: {sl}"
        except (ValueError, TypeError):
            return False, f"SL is not a valid number: {sl}"

    return True, "ok"


async def classify_message(
    message_id: str,
    channel_id: str,
    channel_name: str,
    content: str,
    image_b64: str | None,
    context_messages: list
):
    """Main classification function called by the listener."""

    if is_kill_switch_active():
        print("   🔴 Kill switch active — skipping classification")
        return

    if is_channel_paused(channel_id):
        print("   ⏸  Channel paused — skipping")
        return

    signal_id    = str(uuid.uuid4())[:8]
    context_text = "\n".join(context_messages[-20:]) if context_messages else "No prior context."
    prompt_text  = CLASSIFIER_PROMPT.format(
        context=context_text,
        message=content or "(no text — image only)"
    )

    # Build messages payload
    messages_payload = []

    if image_b64 and LLM_PROVIDER == "groq":
        # Groq vision — image must come before text
        messages_payload.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}"
                    }
                },
                {
                    "type": "text",
                    "text": prompt_text
                }
            ]
        })
    else:
        messages_payload.append({
            "role": "user",
            "content": prompt_text
        })

    try:
        print(f"   🤖 Classifying via {LLM_PROVIDER} ({LLM_MODEL})...")

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages_payload,
            max_tokens=1000,
            temperature=0.1  # Low temperature for consistent JSON output
        )

        raw_output = response.choices[0].message.content.strip()

        # Strip markdown fences if model wraps in ```json
        if raw_output.startswith("```"):
            raw_output = raw_output.split("```")[1]
            if raw_output.startswith("json"):
                raw_output = raw_output[4:]
            raw_output = raw_output.strip()

        result         = json.loads(raw_output)
        classification = result.get('classification', 'noise').lower()

        print(f"   📊 Classification: {classification.upper()}")
        print(f"   💬 Reasoning: {result.get('reasoning', '')}")

        # ── SIGNAL ──────────────────────────────────────────────────────────
        if classification == 'signal':
            result['pair'] = resolve_symbol(result.get('pair', ''))
            is_valid, reason = validate_signal(result)

            if not is_valid:
                print(f"   ⚠️  Validation failed: {reason} → routing to UNCERTAIN")
                classification        = 'uncertain'
                result['classification'] = 'uncertain'
                result['reasoning']   = f"Failed validation: {reason}. {result.get('reasoning','')}"

        if classification == 'signal':
            save_signal(
                signal_id=signal_id,
                message_id=message_id,
                channel_id=channel_id,
                classification='signal',
                confidence=result.get('confidence'),
                pair=result.get('pair'),
                direction=result.get('direction'),
                entry=result.get('entry'),
                sl=result.get('sl'),
                tp=json.dumps(result.get('tp', [])),
                signal_type=result.get('signal_type', 'market'),
                llm_reasoning=result.get('reasoning')
            )
            log_event(
                'signal_detected',
                f"{result.get('direction')} {result.get('pair')} — confidence: {result.get('confidence')}",
                signal_id=signal_id,
                channel_id=channel_id
            )
            print(f"   ✅ Valid signal: {result.get('direction')} {result.get('pair')} | SL: {result.get('sl')} | TP: {result.get('tp')}")

            from src.executor.safety_gate import process_signal
            await process_signal(signal_id=signal_id, signal_data=result, channel_id=channel_id)

        # ── UNCERTAIN ────────────────────────────────────────────────────────
        elif classification == 'uncertain':
            save_signal(
                signal_id=signal_id,
                message_id=message_id,
                channel_id=channel_id,
                classification='uncertain',
                confidence=result.get('confidence', 'low'),
                pair=result.get('pair'),
                direction=result.get('direction'),
                sl=result.get('sl'),
                tp=json.dumps(result.get('tp', [])),
                llm_reasoning=result.get('reasoning')
            )
            log_event('uncertain_signal', result.get('reasoning'), signal_id=signal_id, channel_id=channel_id)

            from src.bot.notification_bot import send_approval_request
            await send_approval_request(
                signal_id=signal_id,
                channel_name=channel_name,
                signal_data=result
            )

        # ── MANAGEMENT ───────────────────────────────────────────────────────
        elif classification == 'management':
            print(f"   🔧 Management: {result.get('action')} {result.get('pair', '')}")
            log_event('management_command', f"{result.get('action')} {result.get('pair','')}", channel_id=channel_id)

            from src.executor.trade_manager import handle_management
            await handle_management(result, channel_id)

        # ── NOISE ────────────────────────────────────────────────────────────
        else:
            print(f"   🔇 Noise — discarded")
            log_event('noise', result.get('reasoning', ''), channel_id=channel_id)

    except json.JSONDecodeError as e:
        print(f"   ❌ JSON parse error: {e}")
        print(f"   Raw output was: {raw_output[:300]}")
        log_event('classifier_error', f"JSON parse error: {e}", channel_id=channel_id)

    except Exception as e:
        print(f"   ❌ Classifier error: {e}")
        log_event('classifier_error', str(e), channel_id=channel_id)