import os
import asyncio
import threading
import requests as http_requests
from flask import Flask, request, Response
from openai import AzureOpenAI
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ConversationReference
from botframework.connector.auth import MicrosoftAppCredentials

app = Flask(__name__)

# 1. Initialize Azure OpenAI Client
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)

# 2. Setup Bot Framework Adapter for Single-Tenant
settings = BotFrameworkAdapterSettings(
    app_id=os.getenv("MicrosoftAppId"),
    app_password=os.getenv("MicrosoftAppPassword"),
    channel_auth_tenant=os.getenv("MicrosoftAppTenantId")
)
adapter = BotFrameworkAdapter(settings)

# Audio content types we recognise as voice messages
AUDIO_CONTENT_TYPES = {"audio/ogg", "audio/mpeg", "audio/wav", "audio/mp4", "audio/webm", "audio/ogg; codecs=opus"}

# Image content types we recognise for OCR
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/bmp", "image/gif", "image/webp"}


# ---------- helpers ----------

def _get_bot_auth_token() -> str:
    """
    Obtain a Bearer token from Azure AD so we can download attachments
    that are hosted on the Bot Framework service URL.
    """
    app_id = os.getenv("MicrosoftAppId")
    app_pw = os.getenv("MicrosoftAppPassword")
    tenant = os.getenv("MicrosoftAppTenantId", "botframework.com")
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    resp = http_requests.post(token_url, data={
        "grant_type": "client_credentials",
        "client_id": app_id,
        "client_secret": app_pw,
        "scope": "https://api.botframework.com/.default"
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def _download_attachment(url: str, service_url: str) -> bytes:
    """
    Downloads an attachment. If the file is hosted on the Bot Connector
    (service_url domain), we attach a Bearer token; otherwise we do a
    plain GET (e.g. Telegram CDN links come pre-authenticated).
    """
    headers = {}
    # Bot-connector-hosted attachments need auth
    if service_url and url.startswith(service_url):
        token = _get_bot_auth_token()
        headers["Authorization"] = f"Bearer {token}"

    resp = http_requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.content


def _transcribe_audio(audio_bytes: bytes, content_type: str = "audio/ogg") -> str:
    """
    Transcribes audio using Azure Speech Service REST API.
    Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in your App Service env vars.
    Supports: audio/ogg (Telegram voice), audio/wav, audio/webm.
    """
    speech_key = os.getenv("AZURE_SPEECH_KEY")
    speech_region = os.getenv("AZURE_SPEECH_REGION")

    if not speech_key or not speech_region:
        raise ValueError("AZURE_SPEECH_KEY and AZURE_SPEECH_REGION must be set")

    # Map common content types to what the Speech REST API expects
    content_type_map = {
        "audio/ogg": "audio/ogg; codecs=opus",
        "audio/webm": "audio/webm; codecs=opus",
        "audio/wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "audio/wave": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "audio/x-wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
    }
    api_content_type = content_type_map.get(content_type, "audio/ogg; codecs=opus")

    url = (
        f"https://{speech_region}.stt.speech.microsoft.com"
        f"/speech/recognition/conversation/cognitiveservices/v1"
    )
    params = {"language": os.getenv("SPEECH_LANGUAGE", "en-US")}
    headers = {
        "Ocp-Apim-Subscription-Key": speech_key,
        "Content-Type": api_content_type,
        "Accept": "application/json"
    }

    resp = http_requests.post(url, params=params, headers=headers,
                              data=audio_bytes, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    if result.get("RecognitionStatus") == "Success":
        return result["DisplayText"]
    else:
        print(f"DEBUG: Speech recognition status: {result.get('RecognitionStatus')}")
        return None


def _extract_text_from_image(image_bytes: bytes) -> str | None:
    """
    Extracts text from an image using Azure Computer Vision OCR REST API.
    Set AZURE_CV_KEY and AZURE_CV_ENDPOINT in your App Service env vars.
    """
    cv_key = os.getenv("AZURE_CV_KEY")
    cv_endpoint = os.getenv("AZURE_CV_ENDPOINT")  # e.g. https://cv-new-final.cognitiveservices.azure.com/

    if not cv_key or not cv_endpoint:
        raise ValueError("AZURE_CV_KEY and AZURE_CV_ENDPOINT must be set")

    # Use the OCR API (synchronous, ideal for book covers / short text)
    url = f"{cv_endpoint.rstrip('/')}/vision/v3.2/ocr"
    params = {"language": "en", "detectOrientation": "true"}
    headers = {
        "Ocp-Apim-Subscription-Key": cv_key,
        "Content-Type": "application/octet-stream"
    }

    resp = http_requests.post(url, params=params, headers=headers,
                              data=image_bytes, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    # Parse OCR response: regions -> lines -> words
    lines = []
    for region in result.get("regions", []):
        for line in region.get("lines", []):
            words = [w["text"] for w in line.get("words", [])]
            if words:
                lines.append(" ".join(words))

    extracted = "\n".join(lines)
    return extracted if extracted.strip() else None


def _extract_user_text(activity: Activity) -> str | None:
    """
    Returns the user's message as text.
    - If the user typed a text message, return activity.text.
    - If the user sent a voice/audio attachment, download & transcribe it.
    - If the user sent an image, extract text via OCR.
    - Returns None if we can't extract anything useful.
    """
    if activity.attachments:
        for attachment in activity.attachments:
            content_type = (attachment.content_type or "").lower().split(";")[0].strip()
            full_content_type = (attachment.content_type or "").lower()

            # --- Audio / Voice ---
            if content_type.startswith("audio/") or full_content_type in AUDIO_CONTENT_TYPES:
                print(f"DEBUG: Voice attachment detected — type={attachment.content_type}")
                audio_bytes = _download_attachment(
                    attachment.content_url,
                    activity.service_url
                )
                print(f"DEBUG: Downloaded {len(audio_bytes)} bytes of audio")
                transcribed = _transcribe_audio(audio_bytes, content_type)
                print(f"DEBUG: Transcription result: {transcribed}")
                return transcribed

            # --- Image / OCR ---
            if content_type in IMAGE_CONTENT_TYPES:
                print(f"DEBUG: Image attachment detected — type={attachment.content_type}")
                image_bytes = _download_attachment(
                    attachment.content_url,
                    activity.service_url
                )
                print(f"DEBUG: Downloaded {len(image_bytes)} bytes of image")
                extracted = _extract_text_from_image(image_bytes)
                print(f"DEBUG: OCR extracted text: {extracted}")
                if extracted:
                    # Provide context so GPT knows the text came from an image
                    return (
                        f"The user sent an image containing the following text:\n"
                        f"{extracted}\n\n"
                        f"Based on this text, provide relevant book recommendations."
                    )
                else:
                    return "The user sent an image but no text could be extracted from it. Ask them to send a clearer image or type their request."

    # Fall back to plain text
    if activity.text:
        return activity.text

    return None


# ---------- core ----------

# 3. Background Task: AI Logic & Proactive Response
def background_process(activity: Activity):
    """
    Handles OpenAI call and sends the reply back proactively.
    Runs in a separate thread to beat the 15s Azure timeout.
    """
    async def send_reply():
        try:
            if activity.type != ActivityTypes.message:
                return

            # Trust the service URL for proactive messaging
            MicrosoftAppCredentials.trust_service_url(activity.service_url)

            # Extract text — either typed or voice-transcribed
            user_text = _extract_user_text(activity)
            if not user_text:
                print("DEBUG: No text or audio found in activity, skipping.")
                return

            print(f"DEBUG: User query: {user_text}")

            # Call Azure OpenAI chat completion
            system_prompt = (
                "You are a helpful book recommendation assistant. "
                "Format your responses to be visually appealing in a chat app.\n\n"
                "Formatting rules:\n"
                "- Use emojis to make responses engaging (📚 for books, ⭐ for ratings, "
                "✍️ for authors, 📖 for genres, 💡 for tips)\n"
                "- Use numbered lists with emojis for book recommendations\n"
                "- Structure each recommendation as:\n"
                "  📚 Book Title by ✍️ Author Name\n"
                "  ⭐ Brief reason why it's recommended\n"
                "- Add a short intro and a friendly closing line\n"
                "- Do NOT use markdown formatting like **, __, `, or # — "
                "only use plain text with emojis and line breaks\n"
                "- Keep responses concise but informative"
            )
            completion = client.chat.completions.create(
                model=os.getenv("DEPLOYMENT_NAME"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ]
            )
            reply_text = completion.choices[0].message.content
            print(f"DEBUG: Got OpenAI response: {reply_text[:100]}...")

            # Build a ConversationReference from the incoming activity
            conversation_reference = ConversationReference(
                activity_id=activity.id,
                bot=activity.recipient,
                channel_id=activity.channel_id,
                conversation=activity.conversation,
                service_url=activity.service_url,
                user=activity.from_property
            )

            # Define the callback that sends the reply via turn_context
            # Send as plain text to avoid Telegram MarkdownV2 parsing errors
            # (characters like . ! - ( ) are reserved in MarkdownV2)
            async def reply_callback(turn_context: TurnContext):
                reply_activity = Activity(
                    type=ActivityTypes.message,
                    text=reply_text,
                    text_format="plain"
                )
                await turn_context.send_activity(reply_activity)

            # Use continue_conversation to proactively send the reply
            await adapter.continue_conversation(
                conversation_reference,
                reply_callback,
                os.getenv("MicrosoftAppId")
            )
            print("DEBUG: Reply sent successfully.")

        except Exception as e:
            print(f"ERROR in background_process: {str(e)}")
            import traceback
            traceback.print_exc()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(send_reply())
    finally:
        loop.close()


# 4. Main Webhook Endpoint
@app.route("/api/messages", methods=["POST"])
def messages():
    """
    Main entry point for Azure Bot Service.
    """
    if "application/json" in request.headers.get("Content-Type", ""):
        body = request.json
    else:
        return Response(status=415)

    activity = Activity().deserialize(body)

    if activity.type == ActivityTypes.message:
        thread = threading.Thread(target=background_process, args=(activity,))
        thread.daemon = True
        thread.start()

    return Response(status=200)


if __name__ == "__main__":
    app.run(port=8000)
