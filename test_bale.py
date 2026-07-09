import requests

BALE_BOT_TOKEN = "944232679:wSNlQZtETWB64BvhG1k2O4aQKWrTSxS8Vbo"
BALE_ADMIN_ID  = 1932923897
BASE_URL = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"

# Download a proper JPEG
url = "https://www.gstatic.com/webp/gallery/1.jpg"
print("Downloading...")
r = requests.get(url, timeout=15)
img = r.content
print(f"Size: {len(img)} bytes, content-type: {r.headers.get('content-type')}")

with open("/tmp/real.jpg", "wb") as f:
    f.write(img)

# sendPhoto
print("\n--- sendPhoto ---")
with open("/tmp/real.jpg", "rb") as f:
    r = requests.post(f"{BASE_URL}/sendPhoto",
        files={"photo": ("real.jpg", f, "image/jpeg")},
        data={"chat_id": str(BALE_ADMIN_ID)},
        timeout=60)
j = r.json()
print(j.get("ok"), j.get("description",""), j.get("error_code",""))

# sendDocument  
print("\n--- sendDocument ---")
with open("/tmp/real.jpg", "rb") as f:
    r = requests.post(f"{BASE_URL}/sendDocument",
        files={"document": ("real.jpg", f, "image/jpeg")},
        data={"chat_id": str(BALE_ADMIN_ID)},
        timeout=60)
j = r.json()
print(j.get("ok"), j.get("description",""), j.get("error_code",""))
