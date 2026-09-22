# Eevee Agent

**Eevee Agent** is an original, general-purpose Agent Runtime in Python, bringing together model inference, tool execution, permission management, long-term memory, Skills, autonomous Skill evolution, MCP integrations, sub-agents, and session recovery. It is originally designed to be my personalized agent, and thus the instructions and prompts are in Chinese. Codex is used to aid the implementation of specific functions.

## Core Capabilities

- **General-purpose Agent Runtime:** I built a unified runtime to coordinate models, tools, permissions, context, Memory, Skills, and external integrations.
- **Complete Agent Loop:** Model invocation, tool-call parsing, permission checks, tool execution, tool-result feedback, continued reasoning, and session persistence form a complete execution loop.
- **OpenAI / Anthropic Protocol Support:** The runtime supports both OpenAI-compatible and Anthropic-compatible model endpoints.
- **Tools and Permission Management:** It supports file reading and writing, precise edits, search, shell commands, Skills, sub-agents, and MCP tools. Plan Mode blocks ordinary write operations and shell execution.
- **Skills:** Project-level and user-level `SKILL.md` files capture reusable methods. The runtime supports Skill discovery, retrieval, invocation, inline or forked execution, and versioned evolution.
- **Long-term Memory:** Memories are isolated by a hash of the project path and can store user preferences, project context, previous decisions, and reference material.
- **Self-evolving Skills:** I implemented a feedback-driven pipeline that extracts reusable rules and creates or merges `SKILL.md` files.
- **MCP Integration:** A custom stdio JSON-RPC MCP client exposes external server tools to the agent under the `mcp__server__tool` naming convention.
- **Sub-agents:** The runtime supports built-in `explore`, `plan`, and `general` agents, as well as custom sub-agents.
- **Session Recovery and Context Compaction:** Sessions are saved automatically and can be resumed with `--resume`. The `/compact` command condenses conversation context, while large tool results can be truncated or persisted separately.

## Architecture

I organized the runtime around the following execution flow:

```text
User request
  -> agents/main.py
  -> Agent.chat()
  -> Build prompt / retrieve Skills / prefetch Memory / initialize MCP
  -> Call an OpenAI-compatible or Anthropic-compatible model
  -> Receive text or tool calls
  -> Check permissions in the runtime
  -> Execute tools / Skills / MCP tools / sub-agents
  -> Return tool results to the model
  -> Save the session
  -> Run Skill usage tracking and online Skill evolution in the background
```

## Project Structure

```text
EeveeAgent/
├── agents/
│   ├── main.py                    # CLI entry point, REPL, argument parsing
│   ├── agent.py                   # Runtime, model calls, tool dispatch, compaction
│   ├── tools.py                   # Built-in tools and permission system
│   ├── prompt.py                  # Dynamic system prompt construction
│   ├── skills.py                  # Skill discovery, retrieval, execution, management
│   ├── online_skill_evolution.py  # Skill extraction and add/merge/discard decisions
│   ├── skill_evolution.py         # Persistence, version history, audit records
│   ├── memory.py                  # Long-term memory
│   ├── mcp_client.py              # stdio JSON-RPC MCP client
│   ├── subagent.py                # Sub-agent configuration
│   ├── session.py                 # Session persistence and recovery
│   └── ui.py                      # Terminal UI
├── .eevee/
│   ├── skills/                    # Project-level Skills
│   └── skill-evolution/           # Skill evolution audit artifacts
├── wiki/                          # Project documentation
├── Dockerfile
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Set up the environment

**Recommended prerequisites:** Python 3.11+, macOS or Linux, Git, an OpenAI-compatible or Anthropic-compatible model endpoint, and optionally `ripgrep`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`

The project automatically loads a `.env` file from the current directory or a parent directory.

**Anthropic-compatible endpoint:**

```dotenv
APIKEY=sk-your-api-key
API=https://your-host/anthropic
MODEL=claude-sonnet-4-6
```

**OpenAI-compatible endpoint:**

```dotenv
OPENAI_API_KEY=sk-your-api-key
OPENAI_BASE_URL=https://your-host/v1
MODEL=gpt-4o
```

**Alternative generic variables:**

```dotenv
APIKEY=sk-your-api-key
API=https://your-host/v1
MODEL=deepseek-chat
```

The runtime selects its protocol using the following rules:

- If `API` or `--api-base` contains `/anthropic` in its path, it uses the Anthropic-compatible protocol.
- Otherwise, when an OpenAI base URL is provided, it uses the OpenAI-compatible protocol.
- `--model` overrides `MODEL` in `.env`.

**Optional settings for OpenAI-compatible endpoints, including Ollama:**

```dotenv
# Used by Eevee Agent to determine when to compact a conversation.
# Keep this aligned with Ollama's actual context window.
EEVEE_CONTEXT_WINDOW=32768

# Ollama/OpenAI-compatible reasoning effort: none, low, medium, or high.
OLLAMA_REASONING_EFFORT=low
```

These can also be overridden at launch with `--context-window 32768` and `--thinking-level low`. `EEVEE_CONTEXT_WINDOW` controls the runtime's compaction threshold; it does **not** resize Ollama's KV cache. Configure Ollama's actual context window through `OLLAMA_CONTEXT_LENGTH` when starting `ollama serve`, or through `PARAMETER num_ctx` in a Modelfile.

### 3. Start the interactive REPL

```bash
python3 -m agents.main
```

Example request:

```text
Read this project and explain how the agent loop works.
```

### 4. Run a one-shot task

```bash
python3 -m agents.main "Summarize the project structure and core modules."
```

### 5. Use Plan Mode

Plan Mode lets me inspect and plan complex changes before approving execution. It blocks ordinary write operations and shell commands while planning.

```bash
python3 -m agents.main --plan "Analyze how to improve Skill retrieval."
```

Inside the REPL, enter:

```text
/plan
```

### 6. Resume a session

```bash
python3 -m agents.main --resume
```

## Self-evolving Skills

One of the main mechanisms I implemented in Eevee Agent is **feedback-driven Skill evolution**. Rather than changing its instructions after every interaction, the runtime waits for user feedback and extracts rules that can be reused in future tasks. It can then create a new project-level or user-level `SKILL.md`, merge the rule into an existing Skill, or discard it.

### 1. Enable automatic Skill evolution

Automatic Skill evolution is enabled by default. To make the configuration explicit, add the following to `.env`:

```dotenv
EEVEE_AUTO_SKILL_EVOLUTION=1
EEVEE_AUTO_SKILL_TARGET=project
```

- `EEVEE_AUTO_SKILL_EVOLUTION=1` enables online Skill evolution.
- `EEVEE_AUTO_SKILL_TARGET=project` stores automatically created Skills in `.eevee/skills/` for the current project.

To make Skills available across projects, use:

```dotenv
EEVEE_AUTO_SKILL_TARGET=user
```

Skill locations:

```text
Project: <project>/.eevee/skills/<skill_name>/SKILL.md
User:    ~/.eevee/skills/<skill_name>/SKILL.md
```

### 2. Run with write permissions

Background Skill updates require a permission mode that allows file modifications. I generally use:

```bash
EEVEE_AUTO_SKILL_EVOLUTION=1 \
EEVEE_AUTO_SKILL_TARGET=project \
python3 -m agents.main --accept-edits
```

A more permissive alternative is:

```bash
python3 -m agents.main --yolo
```

I do not recommend using `--yolo` as the default, because it skips permission confirmations. `--accept-edits` is the more controlled option for routine Skill evolution.

### 3. Provide reusable feedback

The system is designed to capture stable, explicit rules that can improve future tasks—not every individual request.

**One-off request; not a good Skill candidate:**

```text
Write a 500-word government report.
```

**Reusable feedback; suitable for a Skill:**

```text
For future government reports, work summaries, and research reports, provide a
usable first draft instead of asking several questions first. Structure it
around the title, background, key findings, problem analysis, proposed actions,
and next steps. Keep the language formal and measured.
```

**Feedback suitable for evolving an existing Skill:**

```text
For these reports, include key quantitative indicators rather than relying
only on conceptual descriptions.
```

### 4. How the evolution pipeline works

I separated task execution from experience extraction so that the agent does not immediately turn its own output into a permanent rule:

```text
Turn N: User request
  -> Agent completes the task
  -> Save a pending extraction window

Turn N+1: User feedback
  -> Combine feedback with the pending window
  -> online_ingest()
  -> Extractor proposes a reusable Skill candidate
  -> Maintainer decides: add / merge / discard
  -> create_skill_file() or evolve_skill_file(), if applicable
  -> Write SKILL.md
  -> Record provenance, usage statistics, and version history
```

For traceability, the runtime stores audit artifacts under:

```text
.eevee/skill-evolution/usage.jsonl
.eevee/skill-evolution/online_provenance.jsonl
.eevee/skill-evolution/online_skill_provenance.json
.eevee/skill-evolution/skill_usage_stats.json
.eevee/skill-evolution/history/
```

These records make Skill changes inspectable, whether a rule was added or merged, and how Skills are used over time.
