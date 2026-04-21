import os
import asyncio
import base64
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from dotenv import load_dotenv

load_dotenv()

from src.state.database import (
    save_raw_message, get_last_message_id,
    get_recent_messages, upsert_channel,
    is_channel_paused, log_event
)

API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')

# --- Channels to monitor ---
CHANNELS = [
    'kelvin_talent',
]

IMAGE_DIR = 'downloads'
os.makedirs(IMAGE_DIR, exist_ok=True)

# Deduplication cache to prevent double-processing
_processed_messages = set()


async def download_image(client, message) -> str | None:
    """Download image from message, return local file path."""
    try:
        path = await client.download_media(message.media, file=IMAGE_DIR + '/')
        return path
    except Exception as e:
        print(f"⚠️  Could not download image: {e}")
        return None


async def encode_image_base64(path: str) -> str | None:
    """Read image file and return base64 string for Claude vision."""
    try:
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        print(f"⚠️  Could not encode image: {e}")
        return None


async def process_message(client, message, channel_id: str, channel_name: str):
    """
    Preprocess a single message before sending to classifier.
    Handles: text, images, captions, replies.
    """

    # Skip empty messages
    if not message.text and not message.media:
        return

    content = message.text or ''
    has_image = False
    image_path = None
    image_b64 = None

    # Handle image/media
    if message.media and isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument)):
        has_image = True
        image_path = await download_image(client, message)
        if image_path:
            image_b64 = await encode_image_base64(image_path)

    # If message is a reply, prepend the original message for context
    if message.reply_to_msg_id:
        try:
            original = await client.get_messages(channel_id, ids=message.reply_to_msg_id)
            if original and original.text:
                content = f"[Replying to: {original.text}]\n{content}"
        except Exception:
            pass

    # Save to state store
    message_id = str(message.id)
    save_raw_message(
        message_id=message_id,
        channel_id=channel_id,
        content=content,
        has_image=has_image,
        image_path=image_path
    )

    # Get rolling context for this channel
    context_messages = get_recent_messages(channel_id, limit=20)

    print(f"\n📨 [{channel_name}] New message")
    print(f"   ID: {message_id}")
    print(f"   Text: {content[:80]}{'...' if len(content) > 80 else ''}")
    print(f"   Has image: {has_image}")

    # --- Hand off to classifier ---
    from src.classifier.groq_classifier import classify_message
    await classify_message(
        message_id=message_id,
        channel_id=channel_id,
        channel_name=channel_name,
        content=content,
        image_b64=image_b64,
        context_messages=context_messages
    )


async def run_listener():
    """Start the Telegram listener."""
    print("🚀 Starting TradeSync Telegram Listener...")

    client = TelegramClient('tradesync_session', API_ID, API_HASH)
    await client.start()

    print("✅ Telegram client connected.")

    # Register channels in state store and resolve entity IDs
    channel_entities = {}
    for username in CHANNELS:
        try:
            entity = await client.get_entity(username)
            channel_id = str(abs(entity.id))
            channel_name = getattr(entity, 'title', username)
            upsert_channel(channel_id, channel_name)
            channel_entities[channel_id] = channel_name
            print(f"✅ Monitoring: {channel_name} (ID: {channel_id})")
            log_event('system', f"Started monitoring channel: {channel_name}", channel_id=channel_id)
        except Exception as e:
            print(f"❌ Could not resolve channel '{username}': {e}")

    # Handle new messages
    @client.on(events.NewMessage(chats=CHANNELS))
    async def on_new_message(event):
        message = event.message
        channel_id = str(abs(event.chat_id))
        channel_name = channel_entities.get(channel_id, channel_id)

        # Deduplicate
        dedup_key = f"{channel_id}:{message.id}:new"
        if dedup_key in _processed_messages:
            return
        _processed_messages.add(dedup_key)

        # Skip if channel is paused
        if is_channel_paused(channel_id):
            print(f"⏸  Channel {channel_name} is paused. Skipping.")
            return

        await process_message(client, message, channel_id, channel_name)

    # Handle edited messages
    @client.on(events.MessageEdited(chats=CHANNELS))
    async def on_message_edited(event):
        message = event.message
        channel_id = str(abs(event.chat_id))
        channel_name = channel_entities.get(channel_id, channel_id)

        # Deduplicate edit events — only process each edit once per 5-second window
        dedup_key = f"{channel_id}:{message.id}:edit:{int(datetime.utcnow().timestamp() // 5)}"
        if dedup_key in _processed_messages:
            return
        _processed_messages.add(dedup_key)

        print(f"\n✏️  [{channel_name}] Message edited — re-evaluating...")
        log_event('message_edited', f"Message {message.id} was edited", channel_id=channel_id)

        if is_channel_paused(channel_id):
            return

        await process_message(client, message, channel_id, channel_name)

    print("\n👂 Listening for messages... (Press Ctrl+C to stop)\n")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(run_listener())