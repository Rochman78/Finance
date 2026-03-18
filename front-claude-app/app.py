import os
import json
import anthropic
import requests
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max upload
CORS(app)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"txt", "pdf", "md", "csv", "json", "docx"}

# Store custom instructions and files content in memory
agent_config = {
    "instructions": "",
    "files_content": {},
}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_file_text(filepath):
    """Extract text content from uploaded files."""
    ext = filepath.rsplit(".", 1)[1].lower()
    if ext in ("txt", "md", "csv", "json"):
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    elif ext == "pdf":
        try:
            import PyPDF2
            text = ""
            with open(filepath, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
            return text
        except ImportError:
            return "[PDF support requires PyPDF2: pip install PyPDF2]"
    elif ext == "docx":
        try:
            import docx
            doc = docx.Document(filepath)
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return "[DOCX support requires python-docx: pip install python-docx]"
    return ""

FRONT_API_BASE = "https://api2.frontapp.com"
FRONT_API_TOKEN = os.getenv("FRONT_API_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

DEFAULT_PROMPT = """Tu es un assistant professionnel spécialisé dans la rédaction de réponses emails.
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


# ─── Agent config endpoints ──────────────────────────────────────────────────


@app.route("/api/config", methods=["GET"])
def get_config():
    """Get current agent instructions and uploaded files list."""
    return jsonify({
        "instructions": agent_config["instructions"],
        "files": list(agent_config["files_content"].keys()),
    })


@app.route("/api/config/instructions", methods=["POST"])
def save_instructions():
    """Save custom system instructions."""
    data = request.get_json()
    agent_config["instructions"] = data.get("instructions", "")
    return jsonify({"status": "ok"})


@app.route("/api/config/upload", methods=["POST"])
def upload_file():
    """Upload a reference file for Claude context."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": f"Type non supporté. Types acceptés: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    filename = secure_filename(f.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    f.save(filepath)

    content = extract_file_text(filepath)
    agent_config["files_content"][filename] = content

    return jsonify({"status": "ok", "filename": filename})


@app.route("/api/config/files/<filename>", methods=["DELETE"])
def delete_file(filename):
    """Remove an uploaded file."""
    filename = secure_filename(filename)
    agent_config["files_content"].pop(filename, None)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    return jsonify({"status": "ok"})


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

    # Build system prompt: custom instructions (or default) + files + email context
    if agent_config["instructions"]:
        system = agent_config["instructions"]
    else:
        system = DEFAULT_PROMPT

    if agent_config["files_content"]:
        system += "\n\n--- DOCUMENTS DE REFERENCE ---"
        for fname, fcontent in agent_config["files_content"].items():
            system += f"\n\n[{fname}]:\n{fcontent}"

    if messages_context:
        system += f"\n\n--- CONVERSATION EMAIL ---\n{messages_context}"

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
