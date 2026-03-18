// ─── State ────────────────────────────────────────────────────────────────────

let currentInbox = null;          // selected inbox {id, name}
let currentConversation = null;
let conversationMessages = [];
let chatHistory = [];
let messagesContext = "";
let adminPassword = "";           // stored in memory only during session

// ─── Navigation ──────────────────────────────────────────────────────────────

function goToStep(step) {
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    document.getElementById("step" + step).classList.add("active");

    if (step === 0) {
        currentInbox = null;
        loadInboxes();
    }
    if (step === 1 && currentInbox) {
        loadConversations();
    }
}

// ─── Step 0: Choose inbox ────────────────────────────────────────────────────

async function loadInboxes() {
    const grid = document.getElementById("inboxes-grid");
    grid.innerHTML = '<p class="placeholder"><span class="loading"></span> Chargement des boites...</p>';

    try {
        const resp = await fetch("/api/inboxes");
        const data = await resp.json();
        if (data.error) {
            grid.innerHTML = '<p class="placeholder">Erreur : ' + escapeHtml(data.error) + '</p>';
            return;
        }

        const inboxes = data._results || [];
        if (inboxes.length === 0) {
            grid.innerHTML = '<p class="placeholder">Aucune boite trouvee.</p>';
            return;
        }

        grid.innerHTML = "";
        inboxes.forEach(inbox => {
            const card = document.createElement("div");
            card.className = "inbox-card";
            card.onclick = () => selectInbox(inbox);
            card.innerHTML = '<div class="inbox-name">' + escapeHtml(inbox.name) + '</div><div class="inbox-type">' + escapeHtml(inbox.type || "") + '</div>';
            grid.appendChild(card);
        });
    } catch (e) {
        grid.innerHTML = '<p class="placeholder">Erreur de connexion. Verifiez votre token Front.</p>';
        console.error(e);
    }
}

function selectInbox(inbox) {
    currentInbox = inbox;
    document.getElementById("inbox-title").textContent = inbox.name;
    goToStep(1);
}

// ─── Step 1: Conversations (non-archived only) ──────────────────────────────

async function loadConversations() {
    const list = document.getElementById("conversations-list");
    list.innerHTML = '<p class="placeholder"><span class="loading"></span> Chargement...</p>';

    try {
        const resp = await fetch("/api/inboxes/" + encodeURIComponent(currentInbox.id) + "/conversations");
        const data = await resp.json();

        if (data.error) {
            list.innerHTML = '<p class="placeholder">Erreur : ' + escapeHtml(data.error) + '</p>';
            return;
        }

        const conversations = data._results || [];
        if (conversations.length === 0) {
            list.innerHTML = '<p class="placeholder">Aucune conversation non archivee.</p>';
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
            const badgeClass = status === "unassigned" ? "unassigned" : "assigned";

            item.innerHTML = '<div>'
                + '<div class="subject">' + escapeHtml(subject)
                + ' <span class="status-badge ' + badgeClass + '">' + escapeHtml(status) + '</span></div>'
                + '<div class="preview">' + escapeHtml(stripHtml(preview)) + '</div>'
                + '<div class="meta">' + (conv.last_message ? formatDate(conv.last_message.created_at) : "") + '</div>'
                + '</div>';
            list.appendChild(item);
        });
    } catch (e) {
        list.innerHTML = '<p class="placeholder">Erreur de connexion.</p>';
        console.error(e);
    }
}

async function selectConversation(conv) {
    currentConversation = conv;
    chatHistory = [];

    const msgResp = await fetch("/api/conversations/" + conv.id + "/messages");
    const msgData = await msgResp.json();
    conversationMessages = msgData._results || [];

    messagesContext = conversationMessages.map(m => {
        const from = m.author ? (m.author.first_name || "") + " " + (m.author.last_name || "") : "Inconnu";
        const body = stripHtml(m.body || "");
        const date = formatDate(m.created_at);
        return "[" + date + "] " + from + ":\n" + body;
    }).join("\n---\n");

    const preview = document.getElementById("context-preview");
    preview.innerHTML = "<strong>Conversation : " + escapeHtml(conv.subject || "(sans objet)") + "</strong>";
    conversationMessages.forEach(m => {
        const from = m.author ? (m.author.first_name || "") + " " + (m.author.last_name || "") : "Inconnu";
        const body = stripHtml(m.body || "").substring(0, 300);
        preview.innerHTML += '<div class="msg-item"><div class="msg-from">' + escapeHtml(from) + '</div><div class="msg-body">' + escapeHtml(body) + '</div></div>';
    });

    document.getElementById("chat-messages").innerHTML = "";
    goToStep(2);
    addChatMessage("assistant", "J'ai bien recu le contexte de cette conversation. Que souhaitez-vous que je redige comme reponse ?");
}

// ─── Step 2: Chat with Claude ────────────────────────────────────────────────

function addChatMessage(role, content) {
    chatHistory.push({ role, content });
    const container = document.getElementById("chat-messages");
    const div = document.createElement("div");
    div.className = "chat-msg " + role;
    let html = escapeHtml(content);
    if (role === "assistant") {
        html += '<br><button class="use-draft-btn" onclick="useDraft(this)">Utiliser comme brouillon</button>';
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
    sendBtn.innerHTML = '<span class="loading"></span> Claude reflechit...';

    try {
        const resp = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                inbox_id: currentInbox ? currentInbox.id : "",
                messages_context: messagesContext,
                chat_history: chatHistory.filter(m => m.role !== "system"),
                user_message: message,
            }),
        });
        const data = await resp.json();
        if (data.error) { addChatMessage("assistant", "Erreur : " + data.error); }
        else { addChatMessage("assistant", data.response); }
    } catch (e) {
        addChatMessage("assistant", "Erreur de connexion avec Claude.");
        console.error(e);
    } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = "Envoyer";
    }
}

function useDraft(btn) {
    const msgDiv = btn.closest(".chat-msg");
    const fullText = msgDiv.textContent.replace("Utiliser comme brouillon", "").trim();
    document.getElementById("draft-content").value = fullText;
    goToStep(3);
}

// ─── Step 3: Send draft to Front (DRAFT ONLY) ───────────────────────────────

async function sendDraftToFront() {
    const body = document.getElementById("draft-content").value.trim();
    if (!body) return;

    const statusDiv = document.getElementById("draft-status");
    const btn = document.getElementById("send-draft-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="loading"></span> Creation du brouillon...';
    statusDiv.className = "status-message";
    statusDiv.textContent = "";

    try {
        const channelsResp = await fetch("/api/channels");
        const channelsData = await channelsResp.json();
        const channels = channelsData._results || [];
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
            statusDiv.textContent = "Brouillon cree dans Front ! Rien n'a ete envoye au client.";
        }
    } catch (e) {
        statusDiv.className = "status-message error";
        statusDiv.textContent = "Erreur de connexion avec Front.";
        console.error(e);
    } finally {
        btn.disabled = false;
        btn.textContent = "Charger le brouillon dans Front";
    }
}

// ─── Admin ───────────────────────────────────────────────────────────────────

function showAdminLogin() {
    document.getElementById("admin-modal").style.display = "flex";
    document.getElementById("admin-login-form").style.display = "block";
    document.getElementById("admin-panel").style.display = "none";
    document.getElementById("admin-password").value = "";
    document.getElementById("admin-error").textContent = "";
}

function closeAdminModal() {
    document.getElementById("admin-modal").style.display = "none";
}

async function adminLogin() {
    const pwd = document.getElementById("admin-password").value;
    try {
        const resp = await fetch("/api/admin/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: pwd }),
        });
        if (resp.ok) {
            adminPassword = pwd;
            document.getElementById("admin-login-form").style.display = "none";
            document.getElementById("admin-panel").style.display = "block";
            populateAdminInboxSelect();
        } else {
            document.getElementById("admin-error").textContent = "Mot de passe incorrect.";
        }
    } catch (e) {
        document.getElementById("admin-error").textContent = "Erreur de connexion.";
    }
}

async function populateAdminInboxSelect() {
    const select = document.getElementById("admin-inbox-select");
    select.innerHTML = '<option value="">-- Choisir une boite --</option>';
    try {
        const resp = await fetch("/api/inboxes");
        const data = await resp.json();
        (data._results || []).forEach(inbox => {
            const opt = document.createElement("option");
            opt.value = inbox.id;
            opt.textContent = inbox.name;
            select.appendChild(opt);
        });
    } catch (e) { console.error(e); }
}

async function loadInboxConfig() {
    const inboxId = document.getElementById("admin-inbox-select").value;
    const configDiv = document.getElementById("admin-config");
    if (!inboxId) { configDiv.style.display = "none"; return; }

    configDiv.style.display = "block";
    try {
        const resp = await fetch("/api/config/" + encodeURIComponent(inboxId), {
            headers: { "X-Admin-Password": adminPassword },
        });
        const data = await resp.json();
        document.getElementById("admin-instructions").value = data.instructions || "";
        renderAdminFiles(data.files || []);
    } catch (e) { console.error(e); }
}

async function saveAdminInstructions() {
    const inboxId = document.getElementById("admin-inbox-select").value;
    if (!inboxId) return;
    const instructions = document.getElementById("admin-instructions").value;
    try {
        await fetch("/api/config/" + encodeURIComponent(inboxId) + "/instructions", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Admin-Password": adminPassword },
            body: JSON.stringify({ instructions }),
        });
        document.getElementById("save-status").textContent = "Sauvegarde !";
        setTimeout(() => { document.getElementById("save-status").textContent = ""; }, 2000);
    } catch (e) { console.error(e); }
}

async function uploadAdminFile() {
    const inboxId = document.getElementById("admin-inbox-select").value;
    if (!inboxId) return;
    const input = document.getElementById("admin-file-upload");
    const file = input.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("file", file);

    try {
        const resp = await fetch("/api/config/" + encodeURIComponent(inboxId) + "/upload", {
            method: "POST",
            headers: { "X-Admin-Password": adminPassword },
            body: formData,
        });
        const data = await resp.json();
        if (data.error) { alert("Erreur: " + data.error); }
        else { loadInboxConfig(); }
    } catch (e) { console.error(e); }
    input.value = "";
}

async function removeAdminFile(filename) {
    const inboxId = document.getElementById("admin-inbox-select").value;
    if (!inboxId) return;
    try {
        await fetch("/api/config/" + encodeURIComponent(inboxId) + "/files/" + encodeURIComponent(filename), {
            method: "DELETE",
            headers: { "X-Admin-Password": adminPassword },
        });
        loadInboxConfig();
    } catch (e) { console.error(e); }
}

function renderAdminFiles(files) {
    const list = document.getElementById("admin-files-list");
    list.innerHTML = "";
    files.forEach(f => {
        const tag = document.createElement("span");
        tag.className = "file-tag";
        tag.innerHTML = escapeHtml(f) + ' <span class="remove-file" onclick="removeAdminFile(\'' + escapeHtml(f) + '\')">&times;</span>';
        list.appendChild(tag);
    });
}

// ─── Utilities ───────────────────────────────────────────────────────────────

function escapeHtml(str) { const div = document.createElement("div"); div.textContent = str; return div.innerHTML; }
function stripHtml(html) { const tmp = document.createElement("div"); tmp.innerHTML = html; return tmp.textContent || tmp.innerText || ""; }
function formatDate(ts) { if (!ts) return ""; const d = new Date(ts * 1000); return d.toLocaleDateString("fr-FR") + " " + d.toLocaleTimeString("fr-FR", { hour:"2-digit", minute:"2-digit" }); }

document.getElementById("user-input").addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendToClaud(); }
});

// ─── Init ────────────────────────────────────────────────────────────────────

loadInboxes();
