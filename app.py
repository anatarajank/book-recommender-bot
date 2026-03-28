import os
import asyncio
import threading
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


# 3. Background Task: AI Logic & Proactive Response
def background_process(activity: Activity):
    """
    Handles OpenAI call and sends the reply back proactively.
    Runs in a separate thread to beat the 15s Azure timeout.
    """
    async def send_reply():
        try:
            print(f"DEBUG: Processing message: {activity.text}")

            if activity.type != ActivityTypes.message:
                return

            # Trust the service URL for proactive messaging
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
            async def reply_callback(turn_context: TurnContext):
                await turn_context.send_activity(reply_text)

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
