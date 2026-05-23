import json
import os
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
PROMPTS_DIR = BASE_DIR / "prompts"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def get_models_config() -> dict:
    return _load_json(CONFIG_DIR / "models.json")


@lru_cache(maxsize=1)
def get_agents_config() -> dict:
    return _load_json(CONFIG_DIR / "agents.json")


@lru_cache(maxsize=1)
def get_tools_config() -> dict:
    return _load_json(CONFIG_DIR / "tools.json")


def get_agent(agent_id: str) -> dict:
    agents_cfg = get_agents_config()
    agent = agents_cfg["agents"].get(agent_id)
    if not agent:
        raise ValueError(f"Agente nao configurado: {agent_id}")
    return agent


def load_prompt(prompt_file: str) -> str:
    return (PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")


def get_prompt_for_agent(agent_id: str) -> str:
    return load_prompt(get_agent(agent_id)["prompt_file"])


def get_provider_and_model(agent_id: str) -> tuple[str, str]:
    models_cfg = get_models_config()
    agent_cfg = get_agent(agent_id)
    provider = agent_cfg.get("provider") or models_cfg["default_provider"]
    provider_cfg = models_cfg["providers"][provider]
    model = agent_cfg.get("model") or provider_cfg["default_model"]
    return provider, model


def get_model_options() -> list[str]:
    cfg = get_agents_config()
    models = set()
    for agent_cfg in cfg["agents"].values():
        if agent_cfg.get("model"):
            models.add(agent_cfg["model"])
    return sorted(models)


def build_tool_schemas(tool_names: list[str]) -> list[dict]:
    all_tools = {t["name"]: t for t in get_tools_config()["tools"]}
    schemas = []
    for name in tool_names:
        tool = all_tools.get(name)
        if not tool:
            raise ValueError(f"Tool nao configurada em tools.json: {name}")
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
        )
    return schemas


def create_client(provider: str):
    if provider == "groq":
        from groq import Groq

        return Groq(api_key=os.environ["GROQ_API_KEY"])
    if provider == "openai":
        from openai import OpenAI

        return OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    raise ValueError(f"Provider nao suportado: {provider}")
