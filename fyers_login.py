import os
import json
import webbrowser
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel

CONFIG_PATH = "/sdcard/fyers/fyers_config.json"
TOKEN_PATH = "/sdcard/fyers/access_token.txt"
REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"

# 1. Credentials Verification
cfg = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except Exception:
        pass

app_id = cfg.get("client_id", "").strip()
secret_id = cfg.get("secret_key", "").strip()

if not app_id or "YOUR" in app_id:
    app_id = input("1. FYERS Dashboard se App ID paste karein: ").strip()

# Handle -100 suffix properly
if not app_id.endswith("-100"):
    app_id = f"{app_id}-100"

if not secret_id or "YOUR" in secret_id:
    secret_id = input("2. FYERS Dashboard se Secret ID paste karein: ").strip()

# Update config
cfg["client_id"] = app_id
cfg["secret_key"] = secret_id
cfg["redirect_uri"] = REDIRECT_URI

with open(CONFIG_PATH, "w") as f:
    json.dump(cfg, f, indent=4)

# 2. Start Session & Generate Login Link
session = fyersModel.SessionModel(
    client_id=app_id,
    secret_key=secret_id,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

auth_link = session.generate_authcode()
print("\n" + "=" * 55)
print("🔑 BROWSER MEIN LOGIN KAREIN")
print("=" * 55)
print(auth_link)
print("=" * 55 + "\n")

try:
    os.system(f"termux-open-url '{auth_link}'")
except Exception:
    webbrowser.open(auth_link)

# 3. Read Redirect URL / Auth Code
user_input = input("📋 Page load hone ke baad browser ka pura URL yahan paste karein: ").strip()

auth_code = user_input
if "auth_code=" in user_input:
    parsed = urlparse(user_input)
    auth_code = parse_qs(parsed.query).get("auth_code", [user_input])[0]

try:
    session.set_token(auth_code)
    response = session.generate_token()

    if "access_token" in response:
        with open(TOKEN_PATH, "w") as f:
            f.write(response["access_token"])
        print("\n🎉 SUCCESS! access_token.txt generate ho gaya hai!")
    else:
        print(f"\n❌ FYERS Error Response: {response}")
except Exception as e:
    print(f"\n❌ Error: {e}")
