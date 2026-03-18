import os
import json
import anthropic
import requests
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
CORS(app)

FRONT_API_BASE = "https://api2.frontapp.com"
FRONT_API_TOKEN = os.getenv("FRONT_API_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SYSTEM_PROMPT = """Tu es un assistant professionnel spécialisé dans la rédaction de réponses emails.
On te fournit le contexte d'une conversation email (messages précédents).
Tu dois proposer un brouillon de réponse professionnel, clair et adapté au ton de la conversation.
Réponds en français sauf si la conversation est dans une autre langue.
Sois concis et professionnel."""


def front_headers():
    return {
        "Authorization": f"Bearer {FRONT_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ─── Pages ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ─── Front API endpoints ─────────────────────────────────────────────────────


@app.route("/api/inboxes")
def get_inboxes():
    """List all inboxes from Front."""
    resp = requests.get(f"{FRONT_API_BASE}/inboxes", headers=front_headers())
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


@app.route("/api/conversations")
def get_conversations():
    """List conversations, optionally filtered by inbox."""
    inbox_id = request.args.get("inbox_id")
    query = request.args.get("q", "")
    page_token = request.args.get("page_token", "")

    if inbox_id:
        url = f"{FRONT_API_BASE}/inboxes/{inbox_id}/conversations"
    else:
        url = f"{FRONT_API_BASE}/conversations"

    params = {}
    if query:
        params["q"] = query
    if page_token:
        params["page_token"] = page_token

    resp = requests.get(url, headers=front_headers(), params=params)
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


@app.route("/api/conversations/<path:conversation_id>/messages")
def get_messages(conversation_id):
    """Get all messages for a conversation."""
    resp = requests.get(
        f"{FRONT_API_BASE}/conversations/{conversation_id}/messages",
        headers=front_headers(),
    )
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


# ─── Claude API endpoint ─────────────────────────────────────────────────────


@app.route("/api/chat", methods=["POST"])
def chat_with_claude():
    """Send conversation context + user instructions to Claude and get a draft."""
    data = request.get_json()
    messages_context = data.get("messages_context", "")
    chat_history = data.get("chat_history", [])
    user_message = data.get("user_message", "")

    if not user_message and not messages_context:
        return jsonify({"error": "No message provided"}), 400

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = SYSTEM_PROMPT
    if messages_context:
        system += f"\n\nVoici le contexte de la conversation email:\n{messages_context}"

    api_messages = []
    for msg in chat_history:
        api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=system,
        messages=api_messages,
    )

    assistant_text = response.content[0].text
    return jsonify({"response": assistant_text})


# ─── Draft to Front ──────────────────────────────────────────────────────────


@app.route("/api/drafts", methods=["POST"])
def create_draft():
    """Create a draft reply in Front for a given conversation."""
    data = request.get_json()
    conversation_id = data.get("conversation_id")
    body = data.get("body", "")
    author_id = data.get("author_id")

    if not conversation_id or not body:
        return jsonify({"error": "conversation_id and body are required"}), 400

    # Create a reply draft on the conversation
    payload = {
        "body": body,
        "channel_id": data.get("channel_id"),
    }
    if author_id:
        payload["author_id"] = author_id

    # Front API: create a draft for the conversation
    resp = requests.post(
        f"{FRONT_API_BASE}/conversations/{conversation_id}/drafts",
        headers=front_headers(),
        json=payload,
    )

    if resp.status_code not in (200, 201):
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


@app.route("/api/channels")
def get_channels():
    """List channels (needed to identify which channel to draft from)."""
    resp = requests.get(f"{FRONT_API_BASE}/channels", headers=front_headers())
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


@app.route("/api/teammates")
def get_teammates():
    """List teammates (needed for author_id when creating drafts)."""
    resp = requests.get(f"{FRONT_API_BASE}/teammates", headers=front_headers())
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
