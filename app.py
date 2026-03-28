import os
import asyncio
import threading
from flask import Flask, request, Response
from openai import AzureOpenAI
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity, ActivityTypes
from botframework.connector.auth import MicrosoftAppCredentials

app = Flask(__name__)

# 1. Initialize Azure OpenAI Client
# Note: Ensure these match your Azure Environment Variable Names exactly
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)

# 2. Setup Bot Framework Adapter for Single-Tenant
# We MUST include channel_auth_tenant for the proactive reply to work
settings = BotFrameworkAdapterSettings(
    app_id=os.getenv("MicrosoftAppId"),
    app_password=os.getenv("MicrosoftAppPassword"),
    channel_auth_tenant=os.getenv("MicrosoftAppTenantId") # Crucial for Single-Tenant
)
adapter = BotFrameworkAdapter(settings)

# 3. Background Task: AI Logic & Proactive Response
def background_process(activity: Activity):
    """
    Handles OpenAI call and sends the reply back.
    Runs in a separate thread to beat the 15s Azure timeout.
    """
    async def send_reply():
        try:
            # Acknowledge receipt in logs
            print(f"DEBUG: Processing message: {activity.text}")

            # Verify activity type
            if activity.type != ActivityTypes.message:
                return

            # REQUIRED for Proactive Messaging: Trust the service URL (Telegram/WebChat)
            # This allows the bot to send a message back to the user's specific channel
            MicrosoftAppCredentials.trust_service_url(activity.service_url)

            # Call Azure OpenAI
            completion = client.chat.completions.create(
                model=os.getenv("DEPLOYMENT_NAME"),
                messages=[
                    {"role": "system", "content": "You are a helpful book recommendation assistant."},
                    {"role": "user", "content": activity.text}
                ]
            )
            reply_text = completion.choices[0].message.content

            # Create the response activity
            response_activity = Activity(
                type=ActivityTypes.message,
                text=reply_text,
                recipient=activity.from_property,
                from_property=activity.recipient,
                conversation=activity.conversation,
                reply_to_id=activity.id,
                service_url=activity.service_url
            )

            # Send the reply back
            await adapter.send_activity(response_activity)
            print("DEBUG: Reply sent successfully.")

        except Exception as e:
            # This will now appear in your Azure Log Stream
            print(f"ERROR in background_process: {str(e)}")

    # Standard Asyncio loop boilerplate for a thread
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
    if "application/json" in request.headers["Content-Type"]:
        body = request.json
    else:
        return Response(status=415)

    # Deserialize the incoming request into an Activity object
    activity = Activity().deserialize(body)
    
    if activity.type == ActivityTypes.message:
        # Launch the thread so we can return 200 OK immediately
        thread = threading.Thread(target=background_process, args=(activity,))
        thread.daemon = True # Ensures thread dies if main process exits
        thread.start()

    # Return 200 OK immediately to satisfy the 15s Azure timeout
    return Response(status=200)

if __name__ == "__main__":
    # Ensure port 8000 matches your WEBSITES_PORT environment variable
    app.run(port=8000)
