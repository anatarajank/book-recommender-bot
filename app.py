import os
from flask import Flask, request
from openai import AzureOpenAI  # Updated import for v1.x

app = Flask(__name__)

# Initialize the Azure OpenAI client
# It will automatically look for AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT 
# if you name them exactly like that in your environment variables.
client = AzureOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),  
    api_version=os.getenv("OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("OPENAI_ENDPOINT")
)

@app.route("/api/messages", methods=["POST"])
def messages():
    data = request.json
    user_query = data.get("text", "")

    # Updated method call: client.chat.completions.create
    response = client.chat.completions.create(
        model=os.getenv("DEPLOYMENT_NAME"),  # Your GPT deployment name
        messages=[
            {"role": "system", "content": "You are a helpful book recommendation assistant."},
            {"role": "user", "content": user_query}
        ]
    )

    # Note the change from dictionary-style ['choices'] to object-style .choices
    return {"reply": response.choices[0].message.content}

if __name__ == "__main__":
    app.run()