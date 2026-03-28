import os
import asyncio
import threading
from flask import Flask, request, Response
from openai import AzureOpenAI
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity, ActivityTypes

app = Flask(__name__)

# 1. Initialize Azure OpenAI Client
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)

# 2. Setup Bot Framework Adapter with your verified credentials
# Note: Ensure MicrosoftAppTenantId is set in Azure for SingleTenant bots
settings = BotFrameworkAdapterSettings(
    app_id=os.getenv("MicrosoftAppId"),
    app_password=os.getenv("MicrosoftAppPassword")
)
adapter = BotFrameworkAdapter(settings)

# 3. Background Task: AI Logic & Proactive Response
def background_process(activity: Activity):
    """
    Handles the long-running OpenAI call and sends the reply back.
    Runs in a separate thread to beat the 15s Azure timeout.
    """
    async def send_reply():
        try:
            # Only process if the activity is a message
            if activity.type != ActivityTypes.message:
                return

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

            # Send the reply back to Telegram/WebChat via the adapter
            # This requires an async context
            await adapter.send_activity(response_activity)

        except Exception as e:
            print(f"Error in background_process: {e}")

    # Run the async helper in the background thread's loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_reply())
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

    activity = Activity().deserialize(body)
    
    # Check if the activity is a user message
    if activity.type == ActivityTypes.message:
        # Launch the background thread for the AI logic
        # This allows us to return 200 OK to Azure immediately
        thread = threading.Thread(target=background_process, args=(activity,))
        thread.start()

    # Return 200 OK immediately (well within the 15s limit)
    return Response(status=200)

if __name__ == "__main__":
    # Ensure port 8000 matches your WEBSITES_PORT environment variable
    app.run(port=8000)
