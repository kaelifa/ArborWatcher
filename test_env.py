from dotenv import load_dotenv
import os, requests

load_dotenv()

token = os.getenv("TELEGRAM_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

print("TOKEN starts with:", token[:12])
print("CHAT_ID:", chat_id)

if token and chat_id:
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": "✅ .env test from Kristina"})
    print("Telegram response:", r.json())
else:
    print("Missing token or chat ID.")