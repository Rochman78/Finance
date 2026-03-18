import os
import json
import anthropic
import requests
from functools import wraps
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max upload
CORS(app)

FRONT_API_BASE = "https://api2.frontapp.com"
FRONT_API_TOKEN = os.getenv("FRONT_API_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CONFIG_FILE = os.path.join(DATA_DIR, "inbox_configs.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"txt", "pdf", "md", "csv", "json", "docx"}

DEFAULT_PROMPT = """Tu es un assistant professionnel specialise dans la redaction de reponses emails.
On te fournit le contexte d'une conversation email (messages precedents).
Tu dois proposer un brouillon de reponse professionnel, clair et adapte au ton de la conversation.
Reponds en francais sauf si la conversation est dans une autre langue.
Sois concis et professionnel."""


# ─── Config persistence ──────────────────────────────────────────────────────


def load_all_configs():
    """Load per-inbox configs from disk."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_all_configs(configs):
    """Save per-inbox configs to disk."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


def get_inbox_config(inbox_id):
    """Get config for a specific inbox."""
    configs = load_all_configs()
    return configs.get(inbox_id, {"instructions": "", "files": {}})


def set_inbox_config(inbox_id, config):
    """Set config for a specific inbox."""
    configs = load_all_configs()
    configs[inbox_id] = config
    save_all_configs(configs)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def front_headers():
    return {
        "Authorization": f"Bearer {FRONT_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def front_headers_no_content_type():
    return {
        "Authorization": f"Bearer {FRONT_API_TOKEN}",
        "Accept": "application/json",
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


def inbox_upload_dir(inbox_id):
    """Get upload directory for a specific inbox."""
    safe_id = secure_filename(inbox_id)
    d = os.path.join(UPLOAD_DIR, safe_id)
    os.makedirs(d, exist_ok=True)
    return d


def require_admin(f):
    """Decorator to require admin authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("X-Admin-Password", "")
        if auth != ADMIN_PASSWORD:
            return jsonify({"error": "Acces refuse. Mot de passe admin incorrect."}), 403
        return f(*args, **kwargs)
    return decorated


# ─── Pages ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ─── Admin auth ──────────────────────────────────────────────────────────────


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    """Verify admin password."""
    data = request.get_json()
    if data.get("password") == ADMIN_PASSWORD:
        return jsonify({"status": "ok"})
    return jsonify({"error": "Mot de passe incorrect"}), 403


# ─── Per-inbox agent config (admin only) ─────────────────────────────────────


@app.route("/api/config/<inbox_id>", methods=["GET"])
@require_admin
def get_config(inbox_id):
    """Get config for a specific inbox."""
    config = get_inbox_config(inbox_id)
    return jsonify({
        "instructions": config.get("instructions", ""),
        "files": list(config.get("files", {}).keys()),
    })


@app.route("/api/config/<inbox_id>/instructions", methods=["POST"])
@require_admin
def save_instructions(inbox_id):
    """Save custom instructions for an inbox."""
    data = request.get_json()
    config = get_inbox_config(inbox_id)
    config["instructions"] = data.get("instructions", "")
    set_inbox_config(inbox_id, config)
    return jsonify({"status": "ok"})


@app.route("/api/config/<inbox_id>/upload", methods=["POST"])
@require_admin
def upload_file(inbox_id):
    """Upload a reference file for an inbox."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": f"Type non supporte. Types acceptes: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    filename = secure_filename(f.filename)
    upload_path = inbox_upload_dir(inbox_id)
    filepath = os.path.join(upload_path, filename)
    f.save(filepath)

    content = extract_file_text(filepath)

    config = get_inbox_config(inbox_id)
    if "files" not in config:
        config["files"] = {}
    config["files"][filename] = content
    set_inbox_config(inbox_id, config)

    return jsonify({"status": "ok", "filename": filename})


@app.route("/api/config/<inbox_id>/files/<filename>", methods=["DELETE"])
@require_admin
def delete_file(inbox_id, filename):
    """Remove an uploaded file for an inbox."""
    filename = secure_filename(filename)
    config = get_inbox_config(inbox_id)
    if "files" in config:
        config["files"].pop(filename, None)
    set_inbox_config(inbox_id, config)

    filepath = os.path.join(inbox_upload_dir(inbox_id), filename)
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


@app.route("/api/inboxes/<inbox_id>/conversations")
def get_inbox_conversations(inbox_id):
    """List non-archived conversations for a specific inbox."""
    page_token = request.args.get("page_token", "")

    # Use Front search to get only unarchived conversations in this inbox
    url = f"{FRONT_API_BASE}/conversations/search/{inbox_id}"
    params = {
        "q": "[statuses:unassigned,assigned]",
    }
    if page_token:
        params["page_token"] = page_token

    resp = requests.get(url, headers=front_headers(), params=params)

    # Fallback: if search endpoint fails, use inbox conversations and filter
    if resp.status_code != 200:
        url = f"{FRONT_API_BASE}/inboxes/{inbox_id}/conversations"
        params = {}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=front_headers(), params=params)
        if resp.status_code != 200:
            return jsonify({"error": resp.text}), resp.status_code
        data = resp.json()
        # Filter out archived conversations client-side
        if "_results" in data:
            data["_results"] = [
                c for c in data["_results"]
                if c.get("status") not in ("archived", "trashed", "deleted")
            ]
        return jsonify(data)

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
    inbox_id = data.get("inbox_id", "")
    messages_context = data.get("messages_context", "")
    chat_history = data.get("chat_history", [])
    user_message = data.get("user_message", "")

    if not user_message and not messages_context:
        return jsonify({"error": "No message provided"}), 400

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load per-inbox config
    config = get_inbox_config(inbox_id) if inbox_id else {}
    instructions = config.get("instructions", "")
    files = config.get("files", {})

    # Build system prompt
    system = instructions if instructions else DEFAULT_PROMPT

    if files:
        system += "\n\n--- DOCUMENTS DE REFERENCE ---"
        for fname, fcontent in files.items():
            system += f"\n\n[{fname}]:\n{fcontent}"

    if messages_context:
        system += f"\n\n--- CONVERSATION EMAIL ---\n{messages_context}"

    # IMPORTANT: remind Claude to only draft, never send
    system += "\n\nIMPORTANT: Tu rediges uniquement des BROUILLONS. Ne propose jamais d'envoyer directement."

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


# ─── Draft to Front (DRAFT ONLY — never sends) ──────────────────────────────


@app.route("/api/drafts", methods=["POST"])
def create_draft():
    """Create a draft reply in Front. This ONLY creates a draft, never sends."""
    data = request.get_json()
    conversation_id = data.get("conversation_id")
    body = data.get("body", "")
    channel_id = data.get("channel_id")

    if not conversation_id or not body:
        return jsonify({"error": "conversation_id and body are required"}), 400

    # ONLY use the draft creation endpoint — never the send/reply endpoint
    payload = {"body": body}
    if channel_id:
        payload["channel_id"] = channel_id

    resp = requests.post(
        f"{FRONT_API_BASE}/conversations/{conversation_id}/drafts",
        headers=front_headers(),
        json=payload,
    )

    if resp.status_code not in (200, 201, 202):
        return jsonify({"error": resp.text}), resp.status_code

    # Return success even if Front returns empty body (202)
    try:
        return jsonify(resp.json())
    except Exception:
        return jsonify({"status": "draft_created"})


@app.route("/api/channels")
def get_channels():
    """List channels."""
    resp = requests.get(f"{FRONT_API_BASE}/channels", headers=front_headers())
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
