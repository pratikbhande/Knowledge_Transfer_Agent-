const API_URL = "http://localhost:8010/api";

const repoUrlInput = document.getElementById("repoUrl");
const githubTokenInput = document.getElementById("githubToken");
const anthropicKeyInput = document.getElementById("anthropicKey");
const launchBtn = document.getElementById("launchBtn");
const logContainer = document.getElementById("logContainer");
const missionProgress = document.getElementById("missionProgress");
const novaStatusDot = document.getElementById("novaStatusDot");
const debriefPanel = document.getElementById("debriefPanel");
const toggleDebriefBtn = document.getElementById("toggleDebriefBtn");
const debriefContent = document.getElementById("debriefContent");
const chatInput = document.getElementById("chatInput");
const chatSendBtn = document.getElementById("chatSendBtn");
const chatMessages = document.getElementById("chatMessages");
const orionStatusDot = document.getElementById("orionStatusDot");

let currentMissionId = null;
let currentSSE = null;

// Tree Data 
let treeData = null; // root node
let nodesMap = new Map(); // id -> node for quick lookup

// --- D3 SETUP ---
const width = document.getElementById('treeContainer').clientWidth;
const height = document.getElementById('treeContainer').clientHeight;

const svg = d3.select("#treeContainer").append("svg")
    .attr("width", width)
    .attr("height", height)
    .call(d3.zoom().on("zoom", function (event) {
        svgGroup.attr("transform", event.transform);
    }));

const svgGroup = svg.append("g");
// Base simulation (or we use hierarchy)
const treemap = d3.tree().size([width - 100, height - 100]);

function appendLog(logData) {
    const p = document.createElement("p");
    p.className = `log-line log-msg ${logData.level}`;
    const timeStr = new Date().toLocaleTimeString();
    p.innerHTML = `<span class="log-time">[${timeStr}]</span> <span class="log-agent">[${logData.agent}]</span> ${logData.message}`;
    logContainer.appendChild(p);
    logContainer.scrollTop = logContainer.scrollHeight;

    if (logData.progress !== undefined) {
        missionProgress.style.width = `${logData.progress}%`;
    }
}

function launchMission() {
    const repo_url = repoUrlInput.value;
    const github_token = githubTokenInput.value;
    const anthropic_key = anthropicKeyInput.value;

    if (!repo_url) {
        alert("Repo URL is required.");
        return;
    }

    logContainer.innerHTML = "";
    missionProgress.style.width = "0%";
    novaStatusDot.classList.add("active");

    fetch(`${API_URL}/mission/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            repo_url, github_token, anthropic_key
        })
    })
        .then(res => res.json())
        .then(data => {
            currentMissionId = data.mission_id;
            startSSEStream(currentMissionId);
            // Clear old graph
            treeData = null;
            nodesMap.clear();
            svgGroup.selectAll("*").remove();
        })
        .catch(err => {
            appendLog({ level: "error", agent: "SYS", message: `Failed to launch: ${err}` });
            novaStatusDot.classList.remove("active");
        });
}

launchBtn.addEventListener("click", launchMission);

function startSSEStream(mission_id) {
    if (currentSSE) currentSSE.close();

    currentSSE = new EventSource(`${API_URL}/mission/${mission_id}/stream`);

    currentSSE.addEventListener("log", (e) => {
        const data = JSON.parse(e.data);
        appendLog(data);
    });

    currentSSE.addEventListener("node_added", (e) => {
        const node = JSON.parse(e.data);
        addNodeToTree(node);
    });

    currentSSE.addEventListener("mission_complete", (e) => {
        novaStatusDot.classList.remove("active");
        currentSSE.close();
        fetchDebrief();
    });

    currentSSE.onerror = (e) => {
        console.error("SSE Error:", e);
        novaStatusDot.classList.remove("active");
        currentSSE.close();
    };
}

function addNodeToTree(node) {
    if (node.type === "root") {
        treeData = node;
        nodesMap.set(node.id, treeData);
        updateTree();
        return;
    }

    // Since we stream chronologically, we attach to previous node to form a chain
    // (mocking the tree structure)
    let parentNode = null;
    let fallbackParent = treeData;

    // Simplification: Attach to latest node in map to form a timeline
    const allIds = Array.from(nodesMap.keys());
    if (allIds.length > 0) {
        parentNode = nodesMap.get(allIds[allIds.length - 1]);
    }

    if (!parentNode) parentNode = fallbackParent;

    if (!parentNode.children) parentNode.children = [];
    parentNode.children.push(node);
    nodesMap.set(node.id, node);

    updateTree();
}

function updateTree() {
    if (!treeData) return;

    const root = d3.hierarchy(treeData, d => d.children);
    root.x0 = width / 2;
    root.y0 = height - 50; // Majestic node starts bottom

    const treeMapData = treemap(root);
    const nodes = treeMapData.descendants();
    const links = treeMapData.descendants().slice(1);

    // Normalize y to grow visually upwards
    nodes.forEach(d => { d.y = height - 50 - (d.depth * 100) });

    // LINKS
    let i = 0;
    const link = svgGroup.selectAll('path.link')
        .data(links, d => d.id || (d.id = ++i));

    const linkEnter = link.enter().insert('path', "g")
        .attr("class", "link")
        .attr('d', d => {
            const o = { x: d.parent ? d.parent.x0 : d.x, y: d.parent ? d.parent.y0 : d.y };
            return diagonal(o, o);
        })
        .attr("fill", "none")
        .attr("stroke", "var(--gold)")
        .attr("stroke-width", "3px")
        .style("filter", "drop-shadow(0 0 5px rgba(252, 163, 17, 0.6))");

    const linkUpdate = linkEnter.merge(link);
    linkUpdate.transition()
        .duration(600)
        .attr('d', d => diagonal(d, d.parent));

    // NODES
    const node = svgGroup.selectAll('g.node')
        .data(nodes, d => d.id || (d.id = ++i));

    const nodeEnter = node.enter().append('g')
        .attr('class', 'node')
        .attr("transform", d => `translate(${d.parent ? d.parent.x0 : root.x0},${d.parent ? d.parent.y0 : root.y0})`)
        .style("opacity", 0)
        .on('click', clickNode);

    nodeEnter.append('circle')
        .attr('class', 'node-circle')
        .attr('r', 1e-6)
        .style("fill", d => getNodeColor(d.data.type))
        .style("stroke", "#ffffff")
        .style("stroke-width", 2)
        .style("filter", d => d.data.type === 'supernova' ? "drop-shadow(0 0 12px var(--gold))" : "drop-shadow(0 0 6px var(--cyan))");

    const nodeUpdate = nodeEnter.merge(node);

    nodeUpdate.transition()
        .duration(600)
        .style("opacity", 1)
        .attr("transform", d => `translate(${d.x},${d.y})`);

    nodeUpdate.select('circle.node-circle')
        .transition()
        .duration(600)
        .attr('r', d => d.data.type === 'root' ? 14 : (d.data.type === 'supernova' ? 12 : 8));

    nodes.forEach(d => {
        d.x0 = d.x;
        d.y0 = d.y;
    });
}

function diagonal(s, d) {
    if (!s || !d) return "";
    return `M ${s.x} ${s.y}
            C ${s.x} ${(s.y + d.y) / 2},
              ${d.x} ${(s.y + d.y) / 2},
              ${d.x} ${d.y}`;
}

function getNodeColor(type) {
    switch (type) {
        case 'root': return 'var(--cyan)';
        case 'supernova': return 'var(--surface)';
        default: return 'var(--surface-2)';
    }
}

function clickNode(event, d) {
    const data = d.data;
    const tt = document.getElementById("nodeTooltip");

    document.getElementById("ttTitle").innerText = data.label || "Root Node";
    document.getElementById("ttBadge").innerText = data.original_type || data.type;
    document.getElementById("ttDate").innerText = data.date || "";
    document.getElementById("ttAuthor").innerText = data.author || "";
    document.getElementById("ttWhy").innerText = data.why || "The origin of the repository.";
    document.getElementById("ttProblem").innerText = data.problem || "";
    document.getElementById("ttModules").innerText = (data.modules && data.modules.join(", ")) || "None";
    document.getElementById("ttConfidenceBar").style.width = `${(data.confidence || 0) * 100}%`;

    tt.classList.remove("hidden");
    tt.classList.add("visible");
}

document.getElementById("ttCloseBtn").addEventListener("click", () => {
    document.getElementById("nodeTooltip").classList.add("hidden");
    document.getElementById("nodeTooltip").classList.remove("visible");
});

function fetchDebrief() {
    fetch(`${API_URL}/mission/${currentMissionId}/report`)
        .then(r => r.json())
        .then(data => {
            const d = data.report;
            if (!d) return;
            debriefContent.innerHTML = `
                <div class="report-card">
                    <h3>🚀 Mission Overview</h3>
                    <p>Repository: ${d.overview.repo} | Signals Analysed: ${d.overview.total_signals}</p>
                </div>
                <div class="report-card">
                    <h3>📖 Origin Narrative</h3>
                    <p>${d.narrative.replace(/\\n/g, '<br>')}</p>
                </div>
                <div class="report-card">
                    <h3>❤️ Codebase Health</h3>
                    <p>${d.health_assessment}</p>
                </div>
            `;
            debriefPanel.classList.remove("collapsed");
        });
}

toggleDebriefBtn.addEventListener("click", () => {
    debriefPanel.classList.toggle("collapsed");
});

// Chat logic
let chatHistory = [];
chatSendBtn.addEventListener("click", sendOrionMessage);
chatInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter") sendOrionMessage();
});

document.querySelectorAll(".chip").forEach(chip => {
    chip.addEventListener("click", (e) => {
        chatInput.value = e.target.innerText;
        document.getElementById("suggestedChips").style.display = 'none';
        sendOrionMessage();
    });
});

function sendOrionMessage() {
    const q = chatInput.value.trim();
    if (!q || !currentMissionId) return;

    chatInput.value = "";
    document.getElementById("suggestedChips").style.display = 'none';

    const userMsg = document.createElement("div");
    userMsg.className = "chat-msg msg-user";
    userMsg.innerText = q;
    chatMessages.appendChild(userMsg);

    chatHistory.push({ role: "user", content: q });
    orionStatusDot.classList.add("active");

    const orionMsg = document.createElement("div");
    orionMsg.className = "chat-msg msg-orion";
    chatMessages.appendChild(orionMsg);

    fetch(`${API_URL}/mission/${currentMissionId}/orion`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            question: q,
            history: chatHistory.slice(-5) // Send last 5 msgs
        })
    }).then(response => {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let assistantReply = "";

        function readStream() {
            reader.read().then(({ done, value }) => {
                if (done) {
                    orionStatusDot.classList.remove("active");
                    chatHistory.push({ role: "assistant", content: assistantReply });
                    return;
                }
                const chunk = decoder.decode(value);
                // Simple SSE decode assuming generic fast emit format
                const lines = chunk.split('\n');
                lines.forEach(l => {
                    if (l.startsWith("data:")) {
                        const jsonStr = l.replace("data: ", "").trim();
                        if (jsonStr) {
                            try {
                                const parsed = JSON.parse(jsonStr);
                                if (parsed.event === "message") {
                                    assistantReply += parsed.data;
                                    orionMsg.innerHTML = assistantReply.replace(/\n/g, '<br>');
                                    chatMessages.scrollTop = chatMessages.scrollHeight;
                                }
                            } catch (e) { } // skip partial SSE chunks easily
                        }
                    }
                });
                readStream();
            });
        }
        readStream();
    });
}
