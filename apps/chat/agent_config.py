"""Configuracao central do chat baseada em arquivos JSON no modulo MCP."""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
MCP_DIR = BASE_DIR / "src" / "mcp_reclamacao"
CONFIG_DIR = MCP_DIR / "config"
PROMPTS_DIR = MCP_DIR / "prompts"

_models_cfg = json.loads((CONFIG_DIR / "models.json").read_text(encoding="utf-8"))
_agents_cfg = json.loads((CONFIG_DIR / "agents.json").read_text(encoding="utf-8"))
_tools_cfg = json.loads((CONFIG_DIR / "tools.json").read_text(encoding="utf-8"))

DEFAULT_PROVIDER = _models_cfg["default_provider"]
DEFAULT_MODEL = _models_cfg["providers"][DEFAULT_PROVIDER]["default_model"]
MODEL_OPTIONS = sorted({a.get("model") for a in _agents_cfg["agents"].values() if a.get("model")})

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        },
    }
    for tool in _tools_cfg["tools"]
]

ORCHESTRATOR_PROMPT_PATH = PROMPTS_DIR / _agents_cfg["agents"]["orquestrador"]["prompt_file"]

AGENTS = [
    {
        "id": agent_id,
        "name": cfg.get("name", agent_id),
        "description": f"Agente {cfg.get('name', agent_id)} configurado via JSON.",
        "model": cfg.get("model", DEFAULT_MODEL),
        "provider": cfg.get("provider", DEFAULT_PROVIDER),
        "tools": cfg.get("tools", []),
        "system_prompt": str(PROMPTS_DIR / cfg.get("prompt_file", "")),
        "status": "ativo",
    }
    for agent_id, cfg in _agents_cfg["agents"].items()
]
