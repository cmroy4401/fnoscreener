import os
import logging
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

load_dotenv()
logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN")

def get_fyers_client():
    try:
        if CLIENT_ID and ACCESS_TOKEN:
            return fyersModel.FyersModel(client_id=CLIENT_ID, is_async=False, token=ACCESS_TOKEN, log_path="")
        else:
            logger.error("❌ Missing FYERS_CLIENT_ID or FYERS_ACCESS_TOKEN in environment variables!")
            return None
    except Exception as e:
        logger.error(f"Failed to initialize client: {e}")
        return None

fyers = get_fyers_client()
