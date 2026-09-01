import os
import logging
from datetime import datetime
import pyotp
import requests
import urllib.parse
import base64
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

class FyersAutoLogin:
    # Railway Variables se details aayengi, yahan kuch nahi likhna hai
    CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
    SECRET_KEY = os.getenv("FYERS_SECRET_KEY")
    USER_ID = os.getenv("FYERS_USER_ID")
    PIN = os.getenv("FYERS_PIN")
    TOTP_KEY = os.getenv("FYERS_TOTP_KEY")
    REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://127.0.0.1:8000/")
    
    TOKEN_FILE = "fyers_token.txt"

    @classmethod
    def get_token(cls):
        if os.path.exists(cls.TOKEN_FILE):
            file_date = datetime.fromtimestamp(os.path.getmtime(cls.TOKEN_FILE)).date()
            if file_date == datetime.now().date():
                with open(cls.TOKEN_FILE, 'r') as f:
                    return f.read().strip()
        
        logger.info("Generating New Fyers Token via Zero-Click Auto-Login...")
        try:
            from fyers_apiv3 import fyersModel
            
            if not cls.TOTP_KEY:
                logger.error("❌ Auto-Login Failed: TOTP Key is missing in Variables!")
                return None

            totp = pyotp.TOTP(cls.TOTP_KEY).now()
            app_id_hash = cls.CLIENT_ID.split('-')[0]
            headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
            
            # Step A: Send OTP
            payload1 = {"fy_id": base64.b64encode(f"{cls.USER_ID}".encode()).decode(), "app_id": "2"}
            res1 = requests.post("https://api-t2.fyers.in/vag/public/v2/send_login_otp", json=payload1, headers=headers).json()
            
            # Step B: Verify OTP
            payload2 = {"request_key": res1["request_key"], "otp": totp}
            res2 = requests.post("https://api-t2.fyers.in/vag/public/v2/verify_otp", json=payload2, headers=headers).json()
            
            # Step C: Verify PIN
            payload3 = {"request_key": res2["request_key"], "identity_type": "pin", "identifier": base64.b64encode(f"{cls.PIN}".encode()).decode()}
            res3 = requests.post("https://api-t2.fyers.in/vag/public/v2/verify_pin", json=payload3, headers=headers).json()
            access_token_vag = res3["data"]["access_token"]
            
            # Step D: Get Auth Code
            headers_auth = {"authorization": f"Bearer {access_token_vag}", "content-type": "application/json"}
            payload4 = {"fyers_id": cls.USER_ID, "app_id": app_id_hash, "redirect_uri": cls.REDIRECT_URI, "appType": "100", "code_challenge": "", "state": "None", "scope": "", "nonce": "", "response_type": "code", "create_cookie": True}
            res4 = requests.post("https://api.fyers.in/api/v2/token", json=payload4, headers=headers_auth).json()
            
            parsed = urllib.parse.urlparse(res4["Url"])
            auth_code = urllib.parse.parse_qs(parsed.query)["auth_code"][0]
            
            # Step E: Generate Final Token
            session = fyersModel.SessionModel(client_id=cls.CLIENT_ID, secret_key=cls.SECRET_KEY, redirect_uri=cls.REDIRECT_URI, response_type="code", grant_type="authorization_code")
            session.set_token(auth_code)
            token_response = session.generate_token()
            access_token = token_response["access_token"]
            
            # Save for the day
            with open(cls.TOKEN_FILE, 'w') as f:
                f.write(access_token)
            logger.info("✅ Fyers Auto-Login Successful! Token Saved.")
            return access_token
        except Exception as e:
            logger.error(f"❌ Auto-Login Pipeline Failed: {e}")
            return None

def get_fyers_client():
    try:
        from fyers_apiv3 import fyersModel
        access_token = FyersAutoLogin.get_token()
        if access_token:
            return fyersModel.FyersModel(client_id=FyersAutoLogin.CLIENT_ID, is_async=False, token=access_token, log_path="")
    except Exception as e:
        logger.error(f"Failed to initialize client: {e}")
    return None

fyers = get_fyers_client()
