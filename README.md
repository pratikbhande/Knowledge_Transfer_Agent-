# COSMOBASE 🚀

**COSMOBASE** is a codebase knowledge intelligence platform with a deep space, pixel-art aesthetic. It traverses a GitHub repository's git history to autonomously build a living, interactive **Knowledge Tree** (the **Stellar Map**). This map tells the exhaustive story of how a codebase was born and evolved. You can interact with this tree in real-time, or chat with **ORION**, a specialized knowledge-Retrieval-Augmented Generation (RAG) assistant, to ask questions about the architectural decisions found.

---

## 📸 Screenshots
*(Coming soon — drop your screenshots here!)*

---

## 🛠 Features
- **NOVA Agent Pipeline**: Autonomously scans commits, branches, and PRs from absolute 0. Leverages Anthropic Claude to determine `decision` logic for every chunk of commits.
- **Stellar Map**: A D3.js tree representation mapping out your git repository's growth, starring normal Signals and bright Supernova features.
- **Mission Control**: SSE-streamed interactive logging feed.
- **Deep Space Comms (ORION)**: Integrated chat allowing you to ask queries regarding the indexed repo knowledge.
- **Dockerized architecture**: Ready to scale out with zero-configuration live-reloads on your mounted data.

## 🚀 Installation & Standard Usage

This project operates natively via **Docker Compose**:

1. Clone the repository and navigate into the `cosmobase` directory:
   ```bash
   git clone https://github.com/yourusername/cosmobase.git
   cd cosmobase
   ```

2. Initialize your Environment Variables
   ```bash
   cp .env.example .env
   # Edit .env and supply your Anthropic API Key (Claude) + optional GitHub token for private repo bypass
   ```

3. Launch into Orbit
   ```bash
   docker-compose up --build
   ```

4. Once running:
   - Navigate to **`http://localhost:8080`** to access the Front End Interface.
   - The backend runs on `http://localhost:8000`.

## 🏗 Architecture
- **Backend (Python 3.11, FastAPI)**: Orchestrates endpoints, SSE (`EventSource`) streams for UI loading, and hooks into `PyGithub`.
- **Agents**:
   - `NOVA`: The core agent determining PR semantics and diffs.
   - `ORION`: The query agent attached to ChromaDB and sentence-transformers.
- **Vector DB**: ChromaDB runs locally, persisting your `.missions` into local volumes.
- **Frontend (Vanilla HTML/CSS/JS + D3.js)**: A no-build-step static UI offering native DOM interactions and space styling.

## ⚠️ Notes for POC
- The project limits Deep Analysis of large repos implicitly based on chunk configurations to adhere to LLM limits.
- If using public repos *without* a GitHub token, ensure you are abiding by GitHub's 60 requests/hr rate limits. Applying your GitHub Token is highly recommended.
- "Stars" and other artifacts are rendered functionally but optimized for Desktop 1440px displays.

## 🧰 Built With
- **FastAPI**
- **PyGithub**
- **Anthropic / OpenAI Python SDKs**
- **ChromaDB / sentence-transformers**
- **d3.js v7**
- **Vanilla JS & CSS**
