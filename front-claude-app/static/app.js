// ─── State ────────────────────────────────────────────────────────────────────

let currentConversation = null;   // selected conversation object
let conversationMessages = [];    // messages from Front for this conversation
let chatHistory = [];             // chat history with Claude
let messagesContext = "";         // formatted email context for Claude

// ─── Navigation ──────────────────────────────────────────────────────────────

function goToStep(step) {
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    document.querySelectorAll(".step").forEach(s => s.classList.remove("active"));

    document.getElementById("step" + step).classList.add("active");
    document.querySelectorAll(".step").forEach(s => {
        if (parseInt(s.dataset.step) <= step) s.classList.add("active");
    });
}

// ─── Step 1: Load conversations from Front ───────────────────────────────────

async function loadInboxes() {
    try {
        const resp = await fetch("/api/inboxes");
        const data = await resp.json();
        const select = document.getElementById("inbox-select");
        if (data._results) {
            data._results.forEach(inbox => {
                const opt = document.createElement("option");
                opt.value = inbox.id;
                opt.textContent = inbox.name;
                select.appendChild(opt);
            });
        }
    } catch (e) {
        console.error("Failed to load inboxes:", e);
    }
}

async function loadConversations() {
    const inboxId = document.getElementById("inbox-select").value;
    const list = document.getElementById("conversations-list");
    list.innerHTML = '<p class="placeholder"><span class="loading"></span> Chargement...</p>';

    try {
        let url = "/api/conversations";
        if (inboxId) url += "?inbox_id=" + encodeURIComponent(inboxId);

        const resp = await fetch(url);
        const data = await resp.json();

        if (data.error) {
            list.innerHTML = `<p class="placeholder">Erreur : ${escapeHtml(data.error)}</p>`;
            return;
        }

        const conversations = data._results || [];
        if (conversations.length === 0) {
            list.innerHTML = '<p class="placeholder">Aucune conversation trouvée.</p>';
            return;
        }

        list.innerHTML = "";
        conversations.forEach(conv => {
            const item = document.createElement("div");
            item.className = "conversation-item";
            item.onclick = () => selectConversation(conv);

            const subject = conv.subject || "(sans objet)";
            const preview = conv.last_message ? (conv.last_message.body || "").substring(0, 150) : "";
            const status = conv.status || "";

            item.innerHTML = `
                <div>
                    <div class="subject">${escapeHtml(subject)}</div>
                    <div class="preview">${escapeHtml(stripHtml(preview))}</div>
                    <div class="meta">${escapeHtml(status)} · ${conv.last_message ? formatDate(conv.last_message.created_at) : ""}</div>
                </div>
            `;
            list.appendChild(item);
        });
    } catch (e) {
        list.innerHTML = `<p class="placeholder">Erreur de connexion. Vérifiez votre token Front.</p>`;
        console.error(e);
    }
}

async function selectConversation(conv) {
    currentConversation = conv;
    chatHistory = [];

    // Load messages for this conversation
    const msgResp = await fetch(`/api/conversations/${conv.id}/messages`);
    const msgData = await msgResp.json();
    conversationMessages = msgData._results || [];

    // Build context string for Claude
    messagesContext = conversationMessages.map(m => {
        const from = m.author ? (m.author.first_name || "") + " " + (m.author.last_name || "") : "Inconnu";
        const body = stripHtml(m.body || "");
        const date = formatDate(m.created_at);
        return `[${date}] ${from}:\n${body}`;
    }).join("\n---\n");

    // Show context preview
    const preview = document.getElementById("context-preview");
    preview.innerHTML = `<strong>Conversation : ${escapeHtml(conv.subject || "(sans objet)")}</strong>`;
    conversationMessages.forEach(m => {
        const from = m.author ? (m.author.first_name || "") + " " + (m.author.last_name || "") : "Inconnu";
        const body = stripHtml(m.body || "").substring(0, 300);
        preview.innerHTML += `
            <div class="msg-item">
                <div class="msg-from">${escapeHtml(from)}</div>
                <div class="msg-body">${escapeHtml(body)}</div>
            </div>
        `;
    });

    // Clear chat and go to step 2
    document.getElementById("chat-messages").innerHTML = "";
    goToStep(2);

    // Auto-send first message to Claude
    addChatMessage("assistant", "J'ai bien reçu le contexte de cette conversation. Que souhaitez-vous que je rédige comme réponse ? Donnez-moi vos instructions (ton, contenu, points à aborder...).");
}

// ─── Step 2: Chat with Claude ────────────────────────────────────────────────

function addChatMessage(role, content) {
    chatHistory.push({ role, content });

    const container = document.getElementById("chat-messages");
    const div = document.createElement("div");
    div.className = "chat-msg " + role;

    let html = escapeHtml(content);

    // Add "Use as draft" button to assistant messages
    if (role === "assistant") {
        html += `<br><button class="use-draft-btn" onclick="useDraft(this)">Utiliser comme brouillon</button>`;
    }

    div.innerHTML = html;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

async function sendToClaud() {
    const input = document.getElementById("user-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    addChatMessage("user", message);

    const sendBtn = document.getElementById("send-btn");
    sendBtn.disabled = true;
    sendBtn.innerHTML = '<span class="loading"></span> Claude réfléchit...';

    try {
        const resp = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                messages_context: messagesContext,
                chat_history: chatHistory.filter(m => m.role !== "system"),
                user_message: message,
            }),
        });

        const data = await resp.json();
        if (data.error) {
            addChatMessage("assistant", "Erreur : " + data.error);
        } else {
            addChatMessage("assistant", data.response);
        }
    } catch (e) {
        addChatMessage("assistant", "Erreur de connexion avec Claude. Vérifiez votre clé API.");
        console.error(e);
    } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = "Envoyer";
    }
}

function useDraft(btn) {
    // Get the text content of the parent message (excluding the button text)
    const msgDiv = btn.closest(".chat-msg");
    const fullText = msgDiv.textContent.replace("Utiliser comme brouillon", "").trim();

    document.getElementById("draft-content").value = fullText;
    goToStep(3);
}

// ─── Step 3: Send draft to Front ─────────────────────────────────────────────

async function sendDraftToFront() {
    const body = document.getElementById("draft-content").value.trim();
    if (!body) return;

    const statusDiv = document.getElementById("draft-status");
    const btn = document.getElementById("send-draft-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="loading"></span> Envoi en cours...';
    statusDiv.className = "status-message";
    statusDiv.textContent = "";

    try {
        // Get channels to find the right one
        const channelsResp = await fetch("/api/channels");
        const channelsData = await channelsResp.json();
        const channels = channelsData._results || [];

        // Pick the first email channel, or first available
        const emailChannel = channels.find(c => c.type === "smtp" || c.type === "email") || channels[0];

        const resp = await fetch("/api/drafts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                conversation_id: currentConversation.id,
                body: body.replace(/\n/g, "<br>"),
                channel_id: emailChannel ? emailChannel.id : undefined,
            }),
        });

        const data = await resp.json();
        if (data.error) {
            statusDiv.className = "status-message error";
            statusDiv.textContent = "Erreur : " + data.error;
        } else {
            statusDiv.className = "status-message success";
            statusDiv.textContent = "Brouillon créé avec succès dans Front !";
        }
    } catch (e) {
        statusDiv.className = "status-message error";
        statusDiv.textContent = "Erreur de connexion avec Front.";
        console.error(e);
    } finally {
        btn.disabled = false;
        btn.textContent = "Envoyer comme brouillon dans Front";
    }
}

// ─── Agent Config ────────────────────────────────────────────────────────────

function toggleConfig() {
    const panel = document.getElementById("config-panel");
    panel.classList.toggle("active");
}

async function saveInstructions() {
    const instructions = document.getElementById("agent-instructions").value;
    try {
        await fetch("/api/config/instructions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ instructions }),
        });
        updateConfigStatus();
    } catch (e) {
        console.error("Failed to save instructions:", e);
    }
}

async function uploadFile() {
    const input = document.getElementById("file-upload");
    const file = input.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("file", file);

    try {
        const resp = await fetch("/api/config/upload", { method: "POST", body: formData });
        const data = await resp.json();
        if (data.error) {
            alert("Erreur: " + data.error);
        } else {
            loadFilesList();
            updateConfigStatus();
        }
    } catch (e) {
        console.error("Failed to upload file:", e);
    }
    input.value = "";
}

async function removeFile(filename) {
    try {
        await fetch("/api/config/files/" + encodeURIComponent(filename), { method: "DELETE" });
        loadFilesList();
        updateConfigStatus();
    } catch (e) {
        console.error("Failed to remove file:", e);
    }
}

async function loadFilesList() {
    try {
        const resp = await fetch("/api/config");
        const data = await resp.json();
        const list = document.getElementById("files-list");
        list.innerHTML = "";
        data.files.forEach(f => {
            const tag = document.createElement("span");
            tag.className = "file-tag";
            tag.innerHTML = escapeHtml(f) + ' <span class="remove-file" onclick="removeFile(\'' + escapeHtml(f) + '\')">&times;</span>';
            list.appendChild(tag);
        });
        if (data.instructions) {
            document.getElementById("agent-instructions").value = data.instructions;
        }
    } catch (e) {
        console.error("Failed to load config:", e);
    }
}

function updateConfigStatus() {
    const statusEl = document.getElementById("config-status");
    const instructions = document.getElementById("agent-instructions").value.trim();
    const filesList = document.getElementById("files-list").children.length;
    const parts = [];
    if (instructions) parts.push("instructions ok");
    if (filesList > 0) parts.push(filesList + " fichier(s)");
    statusEl.textContent = parts.length > 0 ? "Agent: " + parts.join(" + ") : "";
}

// ─── Utilities ───────────────────────────────────────────────────────────────

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function stripHtml(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return tmp.textContent || tmp.innerText || "";
}

function formatDate(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString("fr-FR") + " " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

// Handle Enter key in chat input
document.getElementById("user-input").addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendToClaud();
    }
});

// ─── Init ────────────────────────────────────────────────────────────────────

loadInboxes();
loadFilesList();
