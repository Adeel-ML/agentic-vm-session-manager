const state = {
    sessions: [],
    activeSessionId: null,
    activeEventSource: null,
    activeRunId: null,
};

const MAX_VISIBLE_MESSAGES = 220;
const MAX_TEXT_LENGTH = 420;

const ui = {
    sessionList: document.querySelector("#session-list"),
    newSessionBtn: document.querySelector("#new-session-btn"),
    clearSessionsBtn: document.querySelector("#clear-sessions-btn"),
    messages: document.querySelector("#messages"),
    vmMeta: document.querySelector("#vm-meta"),
    runStatus: document.querySelector("#run-status"),
    vncFrame: document.querySelector("#vnc-frame"),
    composer: document.querySelector("#composer"),
    promptInput: document.querySelector("#prompt-input"),
};

const CREATE_BUTTON_IDLE_LABEL = "Start New Task";

async function api(path, options = {}) {
    const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!response.ok) {
        const body = await response.text();
        throw new Error(body || `HTTP ${response.status}`);
    }
    if (response.status === 204) {
        return null;
    }
    return await response.json();
}

function closeStream() {
    if (state.activeEventSource) {
        state.activeEventSource.close();
        state.activeEventSource = null;
    }
    state.activeRunId = null;
}

function setStatus(message) {
    ui.runStatus.textContent = message;
}

function setCreateButtonBusy(isBusy) {
    ui.newSessionBtn.disabled = isBusy;
    ui.newSessionBtn.textContent = isBusy ? "Creating..." : CREATE_BUTTON_IDLE_LABEL;
}

function addMessage(role, text) {
    if (!text) {
        return;
    }

    const node = document.createElement("div");
    node.className = `msg ${role}`;
    node.textContent = text;
    ui.messages.appendChild(node);

    while (ui.messages.childElementCount > MAX_VISIBLE_MESSAGES) {
        ui.messages.firstElementChild?.remove();
    }

    ui.messages.scrollTop = ui.messages.scrollHeight;
}

function renderSessions() {
    ui.sessionList.innerHTML = "";

    for (const session of state.sessions) {
        const item = document.createElement("li");
        item.className = "session-item";
        if (session._pending) {
            item.classList.add("pending");
        }
        if (session.id === state.activeSessionId) {
            item.classList.add("active");
        }

        const row = document.createElement("div");
        row.className = "session-row";

        const info = document.createElement("div");
        const title = document.createElement("div");
        title.className = "session-title";
        title.textContent = session.title;

        const meta = document.createElement("div");
        meta.className = "session-meta";
        meta.textContent = session._pending
            ? "Starting VM..."
            : `${session.id.slice(0, 8)} | ${session.status}`;
        info.append(title, meta);

        if (session._pending) {
            const pendingBadge = document.createElement("span");
            pendingBadge.className = "session-pending-badge";
            pendingBadge.textContent = "Starting";
            row.append(info, pendingBadge);
        } else {
            const deleteBtn = document.createElement("button");
            deleteBtn.type = "button";
            deleteBtn.className = "session-delete-btn";
            deleteBtn.textContent = "Delete";
            deleteBtn.addEventListener("click", (event) => {
                event.stopPropagation();
                deleteSession(session.id).catch((error) => {
                    setStatus("Delete failed");
                    addMessage("system", String(error));
                });
            });
            row.append(info, deleteBtn);
        }

        item.appendChild(row);

        if (!session._pending) {
            item.addEventListener("click", () => selectSession(session.id));
        }
        ui.sessionList.appendChild(item);
    }
}

async function loadSessions() {
    const response = await api("/api/sessions");
    state.sessions = response.sessions;
    renderSessions();

    if (!state.activeSessionId && state.sessions.length > 0) {
        await selectSession(state.sessions[0].id);
    }
}

function renderHistoryMessages(messages) {
    ui.messages.innerHTML = "";
    for (const message of messages) {
        const normalized = normalizeMessageContent(message);
        if (!normalized) {
            continue;
        }
        addMessage(message.role, normalized);
    }
}

function truncateText(value, maxLength = MAX_TEXT_LENGTH) {
    const raw = String(value ?? "").replace(/\s+/g, " ").trim();
    if (!raw) {
        return "";
    }

    const scrubbed = raw.replace(/[A-Za-z0-9+/=]{120,}/g, "[payload omitted]");
    if (scrubbed.length <= maxLength) {
        return scrubbed;
    }
    return `${scrubbed.slice(0, maxLength)}...`;
}

function safeStringify(value, maxLength = 220) {
    try {
        return truncateText(JSON.stringify(value), maxLength);
    } catch {
        return "[unserializable payload]";
    }
}

function summarizeToolResult(content, isError = false) {
    if (typeof content === "string") {
        const message = truncateText(content, 240);
        return message ? `Tool result${isError ? " (error)" : ""}: ${message}` : null;
    }

    if (Array.isArray(content)) {
        let imageCount = 0;
        const textParts = [];
        for (const part of content) {
            if (part?.type === "image") {
                imageCount += 1;
                continue;
            }
            if (part?.type === "text") {
                const text = truncateText(part.text, 180);
                if (text) {
                    textParts.push(text);
                }
            }
        }

        const pieces = [];
        if (textParts.length > 0) {
            pieces.push(textParts.join(" "));
        }
        if (imageCount > 0) {
            pieces.push(`[${imageCount} image${imageCount > 1 ? "s" : ""} omitted]`);
        }
        if (pieces.length === 0) {
            pieces.push("[structured payload omitted]");
        }

        return `Tool result${isError ? " (error)" : ""}: ${pieces.join(" ")}`;
    }

    if (content && typeof content === "object") {
        const output = truncateText(content.output ?? "", 180);
        const error = truncateText(content.error ?? "", 180);
        const hasImage = Boolean(content.base64_image);
        const parts = [];
        if (output) {
            parts.push(output);
        }
        if (error) {
            parts.push(`ERROR: ${error}`);
        }
        if (hasImage) {
            parts.push("[image omitted]");
        }
        if (parts.length === 0) {
            parts.push(safeStringify(content, 180));
        }
        return `Tool result${isError ? " (error)" : ""}: ${parts.join(" ")}`;
    }

    return null;
}

function normalizeMessageContent(message) {
    if (typeof message.content === "string") {
        return truncateText(message.content);
    }

    if (Array.isArray(message.content)) {
        const textChunks = [];
        const metaChunks = [];

        for (const block of message.content) {
            if (typeof block === "string") {
                const text = truncateText(block);
                if (text) {
                    textChunks.push(text);
                }
                continue;
            }
            if (block.type === "text") {
                const text = truncateText(block.text);
                if (text) {
                    textChunks.push(text);
                }
                continue;
            }
            if (block.type === "tool_use") {
                metaChunks.push(`Tool use: ${block.name || "unknown"}`);
                continue;
            }
            if (block.type === "tool_result") {
                const summary = summarizeToolResult(block.content, Boolean(block.is_error));
                if (summary) {
                    metaChunks.push(summary);
                }
                continue;
            }
            if (block.type === "thinking") {
                continue;
            }
            metaChunks.push(`[${block.type || "content"}]`);
        }

        // User messages mostly contain tool_result payloads in history; hide those by default.
        if (message.role === "user") {
            if (textChunks.length > 0) {
                return textChunks.join("\n");
            }
            return null;
        }

        if (textChunks.length > 0) {
            return textChunks.join("\n");
        }
        if (metaChunks.length > 0) {
            return metaChunks.join(" | ");
        }
        return null;
    }

    return safeStringify(message.content, MAX_TEXT_LENGTH);
}

function resetSessionView() {
    closeStream();
    state.activeSessionId = null;
    ui.vmMeta.textContent = "No session selected";
    ui.vncFrame.src = "about:blank";
    ui.messages.innerHTML = "";
    setStatus("Idle");
    renderSessions();
}

function getLatestRunId(messages) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const runId = messages[i]?.run_id;
        if (runId) {
            return runId;
        }
    }
    return null;
}

async function syncRunStatusForSession(sessionId, messages) {
    const latestRunId = getLatestRunId(messages);
    if (!latestRunId) {
        setStatus("Ready");
        return;
    }

    try {
        const run = await api(`/api/sessions/${sessionId}/runs/${latestRunId}`);
        if (run.status === "queued") {
            setStatus("Queued...");
            streamRun(sessionId, latestRunId);
            return;
        }
        if (run.status === "running") {
            setStatus("Running...");
            streamRun(sessionId, latestRunId);
            return;
        }
        if (run.status === "failed") {
            setStatus("Failed");
            return;
        }
        if (run.status === "completed") {
            setStatus("Completed");
            return;
        }
    } catch {
        // If run lookup fails (stale message/run reference), keep session usable.
    }

    setStatus("Ready");
}

async function selectSession(sessionId) {
    closeStream();
    state.activeSessionId = sessionId;
    renderSessions();

    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session || session._pending) {
        return;
    }

    ui.vmMeta.textContent = `Session ${session.id.slice(0, 8)} | model ${session.model}`;
    ui.vncFrame.src = session.novnc_url;

    const history = await api(`/api/sessions/${sessionId}/messages`);
    renderHistoryMessages(history.messages);
    await syncRunStatusForSession(sessionId, history.messages);
}

async function createSession() {
    if (ui.newSessionBtn.disabled) {
        return;
    }

    setCreateButtonBusy(true);

    const pendingId = `pending-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
    state.sessions = [
        {
            id: pendingId,
            title: "New Agent Task",
            status: "creating",
            _pending: true,
        },
        ...state.sessions,
    ];
    renderSessions();

    setStatus("Creating session...");
    try {
        const session = await api("/api/sessions", {
            method: "POST",
            body: JSON.stringify({}),
        });

        state.sessions = state.sessions.filter((item) => item.id !== pendingId);

        // Re-sync with backend to avoid stale client list when session creation is slow.
        await loadSessions();
        if (!state.sessions.some((item) => item.id === session.id)) {
            state.sessions = [session, ...state.sessions];
            renderSessions();
        }

        await selectSession(session.id);
    } catch (error) {
        state.sessions = state.sessions.filter((item) => item.id !== pendingId);
        renderSessions();
        throw error;
    } finally {
        setCreateButtonBusy(false);
    }
}

async function deleteSession(sessionId) {
    const target = state.sessions.find((session) => session.id === sessionId);
    if (!target || target._pending) {
        return;
    }

    const ok = window.confirm(`Delete task ${target.id.slice(0, 8)}?`);
    if (!ok) {
        return;
    }

    await api(`/api/sessions/${sessionId}`, { method: "DELETE" });
    state.sessions = state.sessions.filter((session) => session.id !== sessionId);

    if (state.activeSessionId === sessionId) {
        if (state.sessions.length === 0) {
            resetSessionView();
            return;
        }
        renderSessions();
        await selectSession(state.sessions[0].id);
        return;
    }

    renderSessions();
}

async function clearAllSessions() {
    const deletableSessions = state.sessions.filter((session) => !session._pending);
    if (deletableSessions.length === 0) {
        return;
    }

    const ok = window.confirm("Delete all tasks from the list?");
    if (!ok) {
        return;
    }

    setStatus("Clearing tasks...");
    const ids = deletableSessions.map((session) => session.id);
    for (const sessionId of ids) {
        try {
            await api(`/api/sessions/${sessionId}`, { method: "DELETE" });
        } catch {
            // Continue cleanup even if one deletion fails.
        }
    }

    state.sessions = [];
    resetSessionView();
}

function streamRun(sessionId, runId) {
    closeStream();
    state.activeRunId = runId;

    const source = new EventSource(
        `/api/sessions/${sessionId}/runs/${runId}/events`
    );
    state.activeEventSource = source;

    source.onmessage = async (event) => {
        const payload = JSON.parse(event.data);

        if (payload.type === "assistant.block") {
            const block = payload.block;
            if (block.type === "text") {
                addMessage("assistant", truncateText(block.text || ""));
            } else if (block.type === "tool_use") {
                addMessage("assistant", `Tool use -> ${block.name || "unknown"}`);
            } else if (block.type === "thinking") {
                // Keep the log concise by omitting raw thinking blocks.
            } else {
                addMessage("assistant", safeStringify(block));
            }
            return;
        }

        if (payload.type === "tool.result") {
            const result = payload.result || {};
            const line = summarizeToolResult(result, Boolean(result.error));
            addMessage("tool", line || "Tool executed");
            return;
        }

        if (payload.type === "run.started") {
            setStatus("Running...");
            return;
        }

        if (payload.type === "run.completed") {
            setStatus("Completed");
            source.close();
            state.activeEventSource = null;
            const history = await api(`/api/sessions/${sessionId}/messages`);
            renderHistoryMessages(history.messages);
            return;
        }

        if (payload.type === "run.failed") {
            setStatus("Failed");
            addMessage("system", payload.error || "Run failed");
            source.close();
            state.activeEventSource = null;
            return;
        }

        if (payload.type === "stream.closed") {
            source.close();
            state.activeEventSource = null;
            return;
        }
    };

    source.onerror = () => {
        setStatus("Stream disconnected");
    };
}

async function sendPrompt(event) {
    event.preventDefault();

    const sessionId = state.activeSessionId;
    if (!sessionId) {
        setStatus("Create a session first");
        return;
    }

    const text = ui.promptInput.value.trim();
    if (!text) {
        return;
    }

    addMessage("user", text);
    ui.promptInput.value = "";

    const response = await api(`/api/sessions/${sessionId}/messages`, {
        method: "POST",
        body: JSON.stringify({ text }),
    });

    streamRun(sessionId, response.run.id);
}

ui.newSessionBtn.addEventListener("click", () => {
    createSession().catch((error) => {
        setStatus("Create session failed");
        addMessage("system", String(error));
    });
});

ui.clearSessionsBtn.addEventListener("click", () => {
    clearAllSessions().catch((error) => {
        setStatus("Clear failed");
        addMessage("system", String(error));
    });
});

ui.composer.addEventListener("submit", (event) => {
    sendPrompt(event).catch((error) => {
        setStatus("Send failed");
        addMessage("system", String(error));
    });
});

loadSessions().catch((error) => {
    setStatus("Failed to load sessions");
    addMessage("system", String(error));
});
