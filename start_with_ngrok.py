import uvicorn
from pyngrok import ngrok
import logging
import time
from config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def start_with_ngrok():
    settings = get_settings()

    # Kill any existing ngrok processes/tunnels first
    logger.info("Cleaning up existing ngrok sessions...")
    try:
        ngrok.kill()
        time.sleep(2)
        logger.info("✓ Existing ngrok session cleaned up")
    except Exception as e:
        logger.debug(f"No existing ngrok session to clean up: {e}")

    # Start ngrok tunnel
    logger.info("Starting ngrok tunnel...")
    public_url = ngrok.connect(settings.port)
    # Forceer https, ook als ngrok http teruggeeft
    if not str(public_url).startswith("https://"):
        public_url = str(public_url).replace("http://", "https://", 1)
    logger.info(f"✓ Ngrok tunnel established!")
    logger.info(f"✓ Public URL: {public_url}")
    logger.info(f"✓ Webhook URL: {public_url}/api/webhook/mkg")
    logger.info(f"✓ Local URL: http://localhost:{settings.port}")
    
    # Start FastAPI
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )

if __name__ == "__main__":
    start_with_ngrok()
