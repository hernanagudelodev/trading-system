import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    raise SystemExit("TELEGRAM_BOT_TOKEN no está en el .env")

r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
print(r.text)