import json
import logging
import mimetypes
import os
import re
import inspect
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

# Adiciona o caminho do server.py ao sys.path para importar as tools
_mcp_path = str(settings.MCP_SERVER_PATH)
if _mcp_path not in sys.path:
    sys.path.insert(0, _mcp_path)

from groq import Groq
# from openai import OpenAI  # COMENTADO - trocar de volta se necessário
from server import analisar_serie_temporal, carregar_arquivo, exportar_dataframe, filtrar_por_palavras, filtrar_registros, lematizar_nlp, listar_contexto_sessao, normalizar_nlp, ocr_extrair_texto, agrupar_registros
from .agent_config import AGENTS, DEFAULT_MODEL, MODEL_OPTIONS, ORCHESTRATOR_PROMPT_PATH, TOOLS
from .models import ChatSession, Message, Skill

logger = logging.getLogger(__name__)

_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
# _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))  # COMENTADO - trocar de volta se necessário

TOOL_MAP = {
    "carregar_arquivo": carregar_arquivo,
    "filtrar_registros": filtrar_registros,
    "exportar_dataframe": exportar_dataframe,
    "normalizar_nlp": normalizar_nlp,
    "lematizar_nlp": lematizar_nlp,
    "filtrar_por_palavras": filtrar_por_palavras,
    "analisar_serie_temporal": analisar_serie_temporal,
    "ocr_extrair_texto": ocr_extrair_texto,
    "listar_contexto_sessao": listar_contexto_sessao,
    "agrupar_registros": agrupar_registros,
}


def _formatar_resultado_tool(nome_tool: str, resultado: str) -> dict:
    """Formata resultados de tools para exibição mais amigável."""
    try:
        if not isinstance(resultado, str):
            return {"resultado": resultado, "formatado": False}

        dados = json.loads(resultado)

        # Formatar especialmente agrupamentos
        if nome_tool == "agrupar_registros" and dados.get("sucesso"):
            return {
                "resultado": dados,
                "formatado": True,
                "resumo_apresentacao": dados.get("resumo_legivel", "")
            }

        return {"resultado": dados, "formatado": False}
    except (json.JSONDecodeError, Exception):
        return {"resultado": resultado, "formatado": False}


def _tool_flow_summary(tool_name: str) -> list[str]:
    """Resumo didático de funcionamento para exibição na UI."""
    summaries = {
        "carregar_arquivo": [
            "Valida existência e extensão do arquivo (.csv/.xlsx/.xls)",
            "Lê os dados com pandas",
            "Armazena registros completos em cache interno",
            "Retorna colunas e preview de 3 linhas",
        ],
        "filtrar_registros": [
            "Lê o dataframe do cache",
            "Converte filtros_json para lista de filtros",
            "Aplica filtros por coluna e operador",
            "Salva resultado no cache filtrado e retorna resumo",
        ],
        "normalizar_nlp": [
            "Lê dataframe do cache",
            "Normaliza texto (lowercase, sem acento, sem pontuação)",
            "Cria coluna com sufixo _limpo",
            "Atualiza cache e retorna confirmação",
        ],
        "lematizar_nlp": [
            "Lê dataframe do cache",
            "Carrega modelo spaCy pt_core_news_sm",
            "Lematiza a coluna original ou coluna _limpo",
            "Cria coluna com sufixo _lemma e atualiza cache",
        ],
        "filtrar_por_palavras": [
            "Lê dataframe do cache",
            "Monta padrão com palavras-chave",
            "Filtra linhas da coluna alvo por correspondência",
            "Salva resultado no cache filtrado",
        ],
        "exportar_dataframe": [
            "Escolhe origem: filtrado (padrão) ou completo",
            "Converte registros em DataFrame",
            "Cria diretório de destino se necessário",
            "Exporta CSV com UTF-8-SIG e retorna metadados",
        ],
        "analisar_serie_temporal": [
            "Lê dados do cache (normal ou filtrado)",
            "Converte coluna de data para datetime",
            "Agrupa por frequência (dia, mês ou ano)",
            "Calcula métrica (count, sum ou mean)",
            "Retorna série para gráfico inline no chat",
        ],
        "listar_contexto_sessao": [
            "Lê _CACHE e _CACHE_FILTRADO do servidor",
            "Para cada arquivo: retorna nome, caminho, total de registros e colunas",
            "Indica qual tool processou o dado por último e quando",
            "Se houver cache filtrado, mostra quantos registros restaram após filtro",
        ],
    }
    return summaries.get(tool_name, ["Tool sem resumo detalhado cadastrado."])


@require_GET
def tool_detail_api(request):
    """Retorna explicação e código-fonte de uma tool específica."""
    tool_name = (request.GET.get("name") or "").strip()
    if not tool_name:
        return JsonResponse({"error": "Parâmetro 'name' é obrigatório."}, status=400)
    if tool_name not in TOOL_MAP:
        return JsonResponse({"error": f"Tool não encontrada: {tool_name}"}, status=404)

    fn = TOOL_MAP[tool_name]
    try:
        code = inspect.getsource(fn)
    except OSError:
        code = "# Código-fonte indisponível para esta tool."

    return JsonResponse(
        {
            "name": tool_name,
            "flow_summary": _tool_flow_summary(tool_name),
            "source_code": code,
        },
        json_dumps_params={"ensure_ascii": False},
    )


def _run_tool_call(tool_name: str, args: dict) -> tuple[dict, str, dict | None]:
    """Executa uma tool localmente e retorna args normalizados, resultado e metadado de download."""
    if not isinstance(args, dict):
        args = {}
    exported_file = None

    # Intercepta exportar_dataframe: redireciona saída para uploads/
    if tool_name == "exportar_dataframe":
        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # Deriva nome a partir do arquivo de origem se caminho_saida não foi passado
        origem_path = args.get("caminho") or args.get("caminho_saida") or "resultado"
        base_name = Path(args.get("caminho_saida", "")).name or f"{Path(origem_path).stem}_exportado.csv"
        if not base_name.endswith(".csv"):
            base_name += ".csv"
        original_name = base_name
        safe_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
        args["caminho_saida"] = str(_UPLOAD_DIR / safe_name)

    # filtros_json pode vir como lista (schema array) -> converte para string
    if tool_name == "filtrar_registros" and isinstance(args.get("filtros_json"), list):
        args["filtros_json"] = json.dumps(args["filtros_json"], ensure_ascii=False)

    resultado = TOOL_MAP[tool_name](**args)

    # Captura info do arquivo exportado
    if tool_name == "exportar_dataframe":
        try:
            r = json.loads(resultado)
            if r.get("sucesso"):
                exported_file = {
                    "name": original_name,
                    "url": f"/api/download/?file={safe_name}",
                }
            # Se houve erro, loga no resultado para o LLM explicar ao usuário
        except Exception:
            pass

    return args, resultado, exported_file


def _repair_json(raw: str) -> dict | None:
    """Tenta parsear JSON possivelmente malformado gerado pelo LLM.

    Lida com: chaves sem aspas, prefixo $, aspas simples, trailing comma.
    """
    # Tentativa 1: JSON válido direto
    try:
        return json.loads(raw)
    except Exception:
        pass

    # Tentativa 2: normaliza chaves sem aspas e com $ (ex: {$pedido: "valor"})
    fixed = re.sub(r'\$?([a-zA-Z_]\w*)\s*:', r'"\1":', raw)
    # troca aspas simples por duplas (apenas em valores simples)
    fixed = re.sub(r":\s*'([^']*)'", r': "\1"', fixed)
    # remove trailing commas antes de } ou ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    try:
        return json.loads(fixed)
    except Exception:
        pass

    # Tentativa 3: extrai pares chave:valor manualmente
    pairs = re.findall(r'\$?([a-zA-Z_]\w*)\s*:\s*"([^"]*)"', raw)
    if pairs:
        return {k: v for k, v in pairs}

    pairs_sq = re.findall(r"\$?([a-zA-Z_]\w*)\s*:\s*'([^']*)'", raw)
    if pairs_sq:
        return {k: v for k, v in pairs_sq}

    return None


def _extract_failed_tool_from_error(error_text: str) -> tuple[str, dict] | None:
    """Extrai (nome_da_tool, args) de mensagens tool_use_failed do Groq."""
    fn_prefix = r"(?<!\\w)(?:<|\\.)?function="

    # Formato 1: <function=nome={...}>
    m = re.search(rf"{fn_prefix}([a-zA-Z_][\\w]*)=(\{{.*\}})>", error_text)
    if m:
        parsed = _repair_json(m.group(2))
        if parsed is not None:
            return m.group(1), parsed

    # Formato 2: <function=nome({...})>
    m = re.search(rf"{fn_prefix}([a-zA-Z_][\\w]*)\((\{{.*\}})\)>", error_text)
    if m:
        parsed = _repair_json(m.group(2))
        if parsed is not None:
            return m.group(1), parsed

    # Formato 3: <function=nome>{...}  (inclui JSON malformado como {$chave: valor})
    m = re.search(rf"{fn_prefix}([a-zA-Z_][\\w]*)>\s*(\{{.*\}})", error_text, re.DOTALL)
    if m:
        parsed = _repair_json(m.group(2))
        if parsed is not None:
            return m.group(1), parsed

    return None


def _extract_ocr_text_from_result(tool_name: str, resultado: str) -> str:
    """Extrai o texto extraído do JSON retornado pela tool OCR.
    
    Se for ocr_extrair_texto com JSON contendo 'texto_extraido', retorna apenas o texto.
    Caso contrário, retorna o resultado como-is.
    """
    try:
        if tool_name != "ocr_extrair_texto":
            return str(resultado)
        
        resultado_str = str(resultado)
        data = json.loads(resultado_str)
        if data.get("sucesso") and "texto_extraido" in data:
            texto = data["texto_extraido"].strip()
            return texto
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        pass
    except Exception:
        pass
    
    return str(resultado)


def _extract_tool_calls_from_text(error_text: str) -> list[tuple[str, dict]]:
    """Extrai múltiplas chamadas no formato <function=nome>{...} ou .function=nome>{...}."""
    calls: list[tuple[str, dict]] = []

    matches = list(re.finditer(r"(?<!\w)(?:<|\.)?function=([a-zA-Z_][\w]*)>", error_text))
    if not matches:
        single = _extract_failed_tool_from_error(error_text)
        return [single] if single else []

    decoder = json.JSONDecoder()
    for idx, m in enumerate(matches):
        tool_name = m.group(1)
        seg_start = m.end()
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(error_text)
        segment = error_text[seg_start:seg_end]

        pos = 0
        while pos < len(segment):
            while pos < len(segment) and segment[pos].isspace():
                pos += 1
            if pos >= len(segment) or segment[pos] != "{":
                break
            try:
                obj, end_pos = decoder.raw_decode(segment, pos)
            except Exception:
                # Tenta reparar JSON malformado no segmento
                raw_seg = segment[pos:].strip()
                obj = _repair_json(raw_seg)
                if obj is None:
                    break
                end_pos = pos + len(raw_seg)

            if isinstance(obj, dict):
                calls.append((tool_name, obj))
            pos = end_pos

    if not calls:
        single = _extract_failed_tool_from_error(error_text)
        return [single] if single else []
    return calls


def _extract_history_style_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Extrai chamadas no formato textual: [tool:nome] args={...}."""
    calls: list[tuple[str, dict]] = []
    decoder = json.JSONDecoder()

    for m in re.finditer(r"\[tool:([a-zA-Z_][\w]*)\]\s*args\s*=", text or ""):
        tool_name = m.group(1)
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except Exception:
            continue
        if isinstance(obj, dict):
            calls.append((tool_name, obj))

    return calls


def _find_last_loaded_path(history: list[dict]) -> str | None:
    """Recupera o último caminho de arquivo usado nas tools da conversa."""
    for msg in reversed(history or []):
        if msg.get("role") != "assistant":
            continue
        for t in reversed(msg.get("toolsCalled", [])):
            args = t.get("args", {}) if isinstance(t, dict) else {}
            for key in ("caminho", "caminho_imagem"):
                caminho = args.get(key)
                if isinstance(caminho, str) and caminho.strip():
                    return caminho.strip()
    return None


_TOOLS_WITH_CAMINHO = {
    "filtrar_registros",
    "exportar_dataframe",
    "normalizar_nlp",
    "lematizar_nlp",
    "filtrar_por_palavras",
    "analisar_serie_temporal",
    "ocr_extrair_texto",
}


def _sanitize_tool_args(tool_name: str, args: dict, fallback_caminho: str | None) -> dict:
    """Normaliza args vindos de function-like text para melhorar robustez."""
    if not isinstance(args, dict):
        return {}

    # Remove espaços acidentais nas chaves e normaliza strings
    clean: dict = {}
    for k, v in args.items():
        key = str(k).strip()
        clean[key] = v

    # Alguns modelos inventam payload dataframe; ignoramos e usamos o caminho carregado
    clean.pop("dataframe", None)

    # Injeta automaticamente o caminho do último arquivo carregado para qualquer
    # tool que precise de "caminho" quando o LLM não passou o argumento
    if tool_name in _TOOLS_WITH_CAMINHO:
        caminho_key = "caminho_imagem" if tool_name == "ocr_extrair_texto" else "caminho"
        if not clean.get(caminho_key) and fallback_caminho:
            clean[caminho_key] = fallback_caminho

    if tool_name == "analisar_serie_temporal":
        usar_filtrado = clean.get("usar_cache_filtrado")
        if isinstance(usar_filtrado, str):
            val = usar_filtrado.strip().lower()
            if val in {"true", "1", "sim", "yes"}:
                clean["usar_cache_filtrado"] = True
            elif val in {"false", "0", "nao", "não", "no"}:
                clean["usar_cache_filtrado"] = False

        metrica = clean.get("metrica")
        if isinstance(metrica, str):
            clean["metrica"] = metrica.strip().lower()

    return clean


def chat_view(request):
    """Renderiza a interface principal do chat."""
    return render(request, "chat/index.html")


# Extensões permitidas para upload de planilhas e imagens
_ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".pdf"}
_IMAGE_EXTENSIONS   = {".jpg", ".jpeg", ".png", ".pdf"}
_UPLOAD_DIR = settings.BASE_DIR / "uploads"


# ── Helpers de Skills ─────────────────────────────────────────────────────────

def _skills_list() -> list[dict]:
    """Retorna todas as skills salvas no banco de dados."""
    return [s.to_dict() for s in Skill.objects.all()]


def _match_skills(user_message: str) -> tuple[list[dict], str]:
    """Retorna (skills_ativas, mensagem_limpa).

    Ativação por /nome: prefixo /id ou /nome (slug) da skill.
    Ativação automática: chama o LLM leve para decidir se a mensagem bate com 'when_to_use'.
    Retorna também a mensagem sem o prefixo /comando.
    """
    all_skills = [s for s in _skills_list() if s.get("active", True)]
    if not all_skills:
        return [], user_message

    cleaned = user_message
    explicit: list[dict] = []

    # ── Modo 1: /nome_skill explícito ────────────────────────
    if user_message.startswith("/"):
        parts = user_message.split(None, 1)
        cmd = parts[0][1:].lower().strip()  # tira a barra
        cleaned = parts[1].strip() if len(parts) > 1 else ""
        for s in all_skills:
            slug = re.sub(r'[^a-z0-9]+', '_', s["name"].lower()).strip('_')
            if cmd in (s["id"].lower(), slug):
                explicit.append(s)
        if explicit:
            # Se não há texto após o comando, pede ao LLM para executar a skill com o contexto atual
            skill_name = explicit[0]["name"]
            default_msg = f"Execute a skill '{skill_name}' com os dados disponíveis na sessão."
            return explicit, cleaned if cleaned else default_msg
        # Comando não reconhecido — trata mensagem inteira normalmente
        cleaned = user_message

    # ── Modo 2: classificação pelo LLM leve ──────────────────
    matched: list[dict] = []
    for skill in all_skills:
        when_to_use = (skill.get("when_to_use") or "").strip()
        if not when_to_use:
            continue
        try:
            resp = _client.chat.completions.create(
                model="llama-3.1-8b-instant",
                # model="gpt-4o-mini",  # COMENTADO - trocar de volta se necessário
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é um classificador binário estrito. "
                            "Ative a skill APENAS se o pedido do usuário for EXPLICITAMENTE sobre o cenário descrito. "
                            "Se a mensagem falar em carregar arquivo, abrir dados, fazer upload ou qualquer operação "
                            "que precede a análise, responda NAO. "
                            "Só responda SIM se o pedido principal do usuário for exatamente o que o cenário descreve. "
                            "Responda SOMENTE a palavra SIM ou a palavra NAO, sem pontuação, sem explicação."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Cenário em que a skill deve ser ativada: {when_to_use}\n"
                            f"Mensagem do usuário: {cleaned}\n"
                            "A mensagem é ESPECIFICAMENTE sobre o cenário acima? Resposta (SIM ou NAO):"
                        ),
                    },
                ],
                max_tokens=5,
                temperature=0,
            )
            answer = (resp.choices[0].message.content or "").strip().upper()
            # Aceita apenas SIM exato (com ou sem ponto final)
            if answer.rstrip(".") == "SIM":
                matched.append(skill)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Skill match error (%s): %s", skill.get("id"), exc)

    return matched, cleaned


# ── Skills como tools reais ───────────────────────────────────────────────────

_SKILL_TOOL_PREFIX = "skill__"


def _build_dynamic_system_prompt() -> str:
    """Carrega o prompt do orquestrador do .md e injeta tools MCP e skills ativas dinamicamente."""
    try:
        template = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        template = "{MCP_TOOLS}\n{SKILLS_SECTION}"

    # Tools MCP derivadas de TOOLS (sem hardcode)
    mcp_names = [t["function"]["name"] for t in TOOLS if t.get("type") == "function"]
    mcp_section = "\n".join(f"  • {name}" for name in mcp_names)

    # Skills ativas cadastradas pelo usuário
    skills = [s for s in _skills_list() if s.get("active", True)]
    if skills:
        skills_section = "\n".join(
            f"  • **{s['name']}**: {(s.get('when_to_use') or s.get('instructions', ''))[:150]}"
            for s in skills
        )
    else:
        skills_section = "  (nenhuma skill cadastrada ainda)"

    return (
        template
        .replace("{MCP_TOOLS}", mcp_section)
        .replace("{SKILLS_SECTION}", skills_section)
    )


def _build_skill_tools() -> list[dict]:
    """Converte skills ativas em definições de tool no formato Groq/OpenAI function calling."""
    skill_tools = []
    for s in _skills_list():
        if not s.get("active", True):
            continue
        slug = s.get("slug") or re.sub(r'[^a-z0-9]+', '_', s["name"].lower()).strip('_')
        tool_name = f"{_SKILL_TOOL_PREFIX}{slug}"
        skill_tools.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": (
                    f"{s.get('when_to_use') or s.get('instructions', '')}\n\n"
                    f"[Skill: {s['name']}]"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pedido": {
                            "type": "string",
                            "description": "Descreva detalhadamente o que deve ser feito com os dados.",
                        },
                    },
                    "required": ["pedido"],
                },
            },
        })
    return skill_tools


def _resolve_skill_by_tool_name(tool_name: str) -> dict | None:
    """Retorna a skill correspondente a um nome de tool `skill__<slug>`, ou None."""
    if not tool_name.startswith(_SKILL_TOOL_PREFIX):
        return None
    slug = tool_name[len(_SKILL_TOOL_PREFIX):]
    for s in _skills_list():
        s_slug = s.get("slug") or re.sub(r'[^a-z0-9]+', '_', s["name"].lower()).strip('_')
        if s_slug == slug:
            return s
    return None


def _run_skill_tool_call(skill: dict, pedido: str) -> tuple[str, str | None, dict]:
    """Gera código para a skill, executa e retorna (resultado_texto, img_b64, execution_record)."""
    import logging  # noqa: PLC0415
    logger = logging.getLogger(__name__)

    generated_code = _generate_skill_code(skill, pedido)
    if not generated_code or generated_code.startswith("__ERROR__:"):
        reason = generated_code.replace("__ERROR__:", "") if generated_code else "resposta vazia do gerador"
        msg = f"Erro ao gerar código da skill: {reason}"
        logger.warning("_run_skill_tool_call: falha na skill '%s': %s", skill["name"], reason)
        return msg, None, {
            "name": skill["name"], "slug": skill.get("slug", ""),
            "code": "", "output": msg,
        }

    # Garante que o código atribui `resultado` — se não, injeta como fallback
    if "resultado" not in generated_code:
        logger.warning("_run_skill_tool_call: código gerado sem 'resultado', adicionando wrap")
        generated_code = generated_code + "\nresultado = str(locals().get('resultado', 'Executado com sucesso.'))"

    output_text, output_img = _execute_skill_code(generated_code)
    record = {
        "name": skill["name"],
        "slug": skill.get("slug", ""),
        "code": generated_code,
        "output": output_text or "",
    }
    return output_text or "Skill executada.", output_img, record


def _get_data_context() -> dict:
    """Retorna schema completo dos DataFrames no cache para uso no prompt de geração de código."""
    try:
        from server import _CACHE  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        info = {}
        for caminho, registros in (_CACHE or {}).items():
            if registros:
                nome = Path(caminho).stem
                df = pd.DataFrame(registros)
                info[nome] = {
                    "shape": list(df.shape),
                    "columns": list(df.columns),
                    "dtypes": {c: str(t) for c, t in df.dtypes.items()},
                    "sample": df.head(3).to_dict(orient="records"),
                }
        return info
    except Exception:
        return {}


# Caminho do prompt do gerador de código de skills
_SKILL_CODE_GENERATOR_PROMPT = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "mcp_reclamacao" / "prompts" / "skill_code_generator.md"
)


def _generate_skill_code(skill: dict, user_message: str) -> str:
    """Usa o LLM para gerar código Python dinamicamente para atender ao pedido do usuário.

    O prompt é carregado de src/mcp_reclamacao/prompts/skill_code_generator.md.
    """
    import logging  # noqa: PLC0415
    logger = logging.getLogger(__name__)

    data_ctx = _get_data_context()

    # Monta descrição detalhada de cada DataFrame
    if data_ctx:
        data_blocks = []
        for nome, info in data_ctx.items():
            col_types = ", ".join(f"{c} ({t})" for c, t in info["dtypes"].items())
            sample_lines = "\n".join("  " + str(row) for row in info["sample"])
            data_blocks.append(
                f'dados["{nome}"]  →  {info["shape"][0]} linhas × {info["shape"][1]} colunas\n'
                f'  Colunas e tipos: {col_types}\n'
                f'  Amostra:\n{sample_lines}'
            )
        data_desc = "\n\n".join(data_blocks)
    else:
        data_desc = "(nenhum dado carregado na sessão)"

    example_block = ""
    example_code = (skill.get("code") or "").strip()
    if example_code:
        example_block = (
            f"**Código de referência / template** (adapte ao pedido do usuário):\n"
            f"```python\n{example_code}\n```"
        )

    # Carrega o prompt do arquivo .md e substitui os placeholders
    try:
        prompt_template = _SKILL_CODE_GENERATOR_PROMPT.read_text(encoding="utf-8")
    except Exception:
        prompt_template = "{DATA_CONTEXT}\n{SKILL_NAME}\n{SKILL_INSTRUCTIONS}\n{EXAMPLE_BLOCK}\n{USER_MESSAGE}"

    full_prompt = (
        prompt_template
        .replace("{DATA_CONTEXT}", data_desc)
        .replace("{SKILL_NAME}", skill.get("name", ""))
        .replace("{SKILL_INSTRUCTIONS}", skill.get("instructions", ""))
        .replace("{EXAMPLE_BLOCK}", example_block)
        .replace("{USER_MESSAGE}", user_message)
    )

    try:
        resp = _client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            # model="gpt-4o",  # COMENTADO - trocar de volta se necessário
            messages=[{"role": "user", "content": full_prompt}],
            temperature=0.1,
            max_tokens=2000,
        )
        code = resp.choices[0].message.content.strip()
        # Remove fences markdown caso o modelo inclua mesmo com instrução contrária
        if "```" in code:
            code = "\n".join(
                line for line in code.splitlines()
                if not line.strip().startswith("```")
            ).strip()
        return code
    except Exception as exc:
        exc_str = str(exc)
        # Fallback para modelo menor em caso de rate limit
        if "rate_limit" in exc_str or "429" in exc_str:
            logger.warning("_generate_skill_code: rate limit no modelo principal, tentando fallback")
            try:
                resp = _client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    # model="gpt-4o-mini",  # COMENTADO - trocar de volta se necessário
                    messages=[{"role": "user", "content": full_prompt}],
                    temperature=0.1,
                    max_tokens=2000,
                )
                code = resp.choices[0].message.content.strip()
                if "```" in code:
                    code = "\n".join(
                        line for line in code.splitlines()
                        if not line.strip().startswith("```")
                    ).strip()
                return code
            except Exception as exc2:
                logger.warning("_generate_skill_code fallback error: %s", exc2)
                return f"__ERROR__:{exc2}"
        logger.warning("_generate_skill_code error: %s", exc)
        return f"__ERROR__:{exc}"


def _execute_skill_code(code: str) -> tuple[str, str | None]:
    """Executa o código Python da skill com acesso ao cache de DataFrames.

    Retorna (texto_output, base64_png_ou_None).
    O código pode:
      - Usar `dados` (dict nome_arquivo → DataFrame) ou variáveis diretas pelo nome do arquivo
      - Usar `pd`, `np`, `plt`
      - Definir a variável `resultado` para forçar o texto de saída
      - Plotar com matplotlib — a figura é capturada e retornada como imagem
    """
    import base64
    import contextlib
    import io
    import importlib
    import logging

    logger = logging.getLogger(__name__)

    # Monta contexto com os DataFrames do cache MCP
    try:
        from server import _CACHE
        import pandas as pd
        dados: dict = {}
        for caminho, registros in (_CACHE or {}).items():
            if registros:
                nome = Path(caminho).stem
                df = pd.DataFrame(registros)
                dados[nome] = df
    except Exception as exc:
        logger.warning("_execute_skill_code: erro ao acessar _CACHE: %s", exc)
        dados = {}

    ctx: dict = {"__builtins__": __builtins__, "dados": dados}

    # Libs comuns disponíveis no contexto
    for alias, mod in [("pd", "pandas"), ("np", "numpy"), ("plt", "matplotlib.pyplot"),
                       ("json", "json"), ("datetime", "datetime"), ("re", "re"),
                       ("requests", "requests")]:
        try:
            ctx[alias] = importlib.import_module(mod)
        except ImportError:
            pass

    # Atalhos para datetime: dt.now() funciona diretamente (datetime.datetime class)
    import datetime as _dt_mod
    ctx["dt"] = _dt_mod.datetime          # dt.now(), dt.strptime(), etc.
    ctx["date"] = _dt_mod.date            # date.today()
    ctx["timedelta"] = _dt_mod.timedelta  # timedelta(days=1)

    # DataFrames acessíveis diretamente pelo nome do arquivo
    ctx.update(dados)

    # Configura matplotlib para não abrir janela
    try:
        import matplotlib
        matplotlib.use("Agg")
    except Exception:
        pass

    stdout_buf = io.StringIO()
    img_b64: str | None = None

    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(compile(code, "<skill>", "exec"), ctx)  # noqa: S102

        # Captura figura matplotlib se foi criada
        try:
            import matplotlib.pyplot as plt
            if plt.get_fignums():
                buf = io.BytesIO()
                plt.savefig(buf, format="png", bbox_inches="tight", dpi=130)
                buf.seek(0)
                img_b64 = base64.b64encode(buf.read()).decode()
                plt.close("all")
        except Exception:
            pass

        text = stdout_buf.getvalue().strip()
        if "resultado" in ctx and ctx["resultado"] is not None:
            extra = str(ctx["resultado"])
            text = extra + ("\n" + text if text else "")

        return text or "Código executado com sucesso.", img_b64

    except Exception as exc:
        logger.warning("_execute_skill_code error: %s", exc)
        return f"Erro na execução do código da skill: {exc}", None


@csrf_exempt
@require_POST
def upload_file(request):
    """Recebe um arquivo Excel/CSV, salva em /uploads/ e retorna o caminho."""
    file = request.FILES.get("file")
    if not file:
        return JsonResponse({"error": "Nenhum arquivo enviado."}, status=400)

    ext = Path(file.name).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return JsonResponse(
            {"error": f"Tipo não permitido. Use: {', '.join(_ALLOWED_EXTENSIONS)}"},
            status=400,
        )

    # Limita 20 MB
    if file.size > 20 * 1024 * 1024:
        return JsonResponse({"error": "Arquivo muito grande. Limite: 20 MB."}, status=400)

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Nome único para evitar colisões
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.name).stem}{ext}"
    dest = _UPLOAD_DIR / safe_name

    with open(dest, "wb") as f:
        for chunk in file.chunks():
            f.write(chunk)

    return JsonResponse({"path": str(dest), "name": file.name, "is_image": ext in _IMAGE_EXTENSIONS})


@csrf_exempt
@require_POST
def chat_api(request):
    """Endpoint da API que processa a mensagem e retorna a resposta do assistente."""
    try:
        data = json.loads(request.body)
        user_message = data.get("message", "").strip()
        history = data.get("history", [])
        requested_model = (data.get("model") or "").strip()
        selected_model = requested_model if requested_model in MODEL_OPTIONS else DEFAULT_MODEL
        session_id = (data.get("session_id") or "").strip()
        session_title = (data.get("session_title") or user_message[:40] or "Chat").strip()
        last_loaded_path = _find_last_loaded_path(history)

        if not user_message:
            return JsonResponse({"error": "Mensagem vazia."}, status=400)

        # System prompt dinâmico: inclui tools MCP + skills cadastradas pelo usuário
        system_prompt = _build_dynamic_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]

        # Injeta automaticamente o contexto do cache na primeira mensagem do sistema.
        # Assim o LLM sempre sabe quais arquivos estão disponíveis sem precisar chamar tool.
        try:
            ctx_raw = listar_contexto_sessao()
            ctx = json.loads(ctx_raw)
            if ctx.get("total_arquivos", 0) > 0:
                linhas = []
                for arq in ctx["arquivos_em_cache"]:
                    linha = (
                        f"- '{arq['arquivo']}' | caminho: {arq['caminho']} | "
                        f"{arq['total_registros']} registros | colunas: {arq['colunas']} | "
                        f"última tool: {arq['ultima_tool_principal']}"
                    )
                    if "cache_filtrado" in arq:
                        cf = arq["cache_filtrado"]
                        linha += (
                            f" | filtrado: {cf['total_registros_filtrados']} registros "
                            f"(por {cf['ultima_tool_filtro']})"
                        )
                    linhas.append(linha)
                ctx_text = "CONTEXTO DA SESSÃO - Arquivos carregados em memória:\n" + "\n".join(linhas)
                messages[0]["content"] = system_prompt + "\n\n" + ctx_text
        except Exception:
            pass  # Se o cache estiver vazio ou ocorrer erro, segue sem contexto

        # Skills são expostas como tools reais — o LLM decide quando chamá-las
        skill_tools = _build_skill_tools()
        all_tools = TOOLS + skill_tools

        skill_output_image: str | None = None
        skill_executions: list[dict] = []

        for msg in history:
            role = msg.get("role")
            if role == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                tools_called_hist = msg.get("toolsCalled", [])
                if tools_called_hist:
                    # Inclui resumo das tools no conteúdo para o modelo manter contexto
                    tool_summary = "\n".join(
                        f"[tool:{t['tool']}] args={json.dumps(t['args'], ensure_ascii=False)} "
                        f"→ {str(t['result'])[:300]}"
                        for t in tools_called_hist
                    )
                    content = f"[Ferramentas chamadas nesta etapa]\n{tool_summary}\n\n{msg['content']}"
                    messages.append({"role": "assistant", "content": content})
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        tools_called = []
        exported_file = None  # preenchido se exportar_dataframe for chamado
        ocr_text_extracted = None  # armazena texto extraído por OCR
        msg = None

        # 1ª chamada: o modelo decide se usa tool/skill ou responde direto
        try:
            response = _client.chat.completions.create(
                model=selected_model,
                messages=messages,
                tools=all_tools,
                tool_choice="auto",
            )
            msg = response.choices[0].message
        except Exception as exc:
            # Fallback para casos de tool_use_failed com failed_generation
            parsed_calls = _extract_tool_calls_from_text(str(exc))
            if not parsed_calls:
                raise

            executed = []
            for nome, args in parsed_calls:
                # Verifica se é uma skill tool
                skill_fb = _resolve_skill_by_tool_name(nome)
                if skill_fb:
                    pedido_fb = args.get("pedido") or user_message
                    resultado_fb, img_fb, record_fb = _run_skill_tool_call(skill_fb, pedido_fb)
                    if img_fb:
                        skill_output_image = img_fb
                    skill_executions.append(record_fb)
                    tools_called.append({"tool": nome, "args": args, "result": resultado_fb})
                    executed.append((nome, resultado_fb))
                    continue
                if nome not in TOOL_MAP:
                    continue
                args = _sanitize_tool_args(nome, args, last_loaded_path)
                args, resultado, exported_file_fallback = _run_tool_call(nome, args)
                if exported_file_fallback:
                    exported_file = exported_file_fallback
                tools_called.append({"tool": nome, "args": args, "result": resultado})
                executed.append((nome, resultado))

            if not executed:
                raise

            nome = executed[0][0]
            resultado = "\n".join(f"[{n}] {r}" for n, r in executed)

            # Resposta final amigável mesmo sem tool_call_id válido
            messages_fallback = messages + [
                {
                    "role": "assistant",
                    "content": (
                        f"Ferramenta executada automaticamente devido a falha de formatação do modelo: {nome}."
                    ),
                },
                {
                    "role": "assistant",
                    "content": f"Resultado da ferramenta: {resultado}",
                },
            ]
            response_final = _client.chat.completions.create(
                model=selected_model,
                messages=messages_fallback,
            )
            answer = response_final.choices[0].message.content
            return _chat_save_and_respond(
                session_id, session_title, selected_model,
                user_message, answer, tools_called, exported_file,
                skill_executions, skill_output_image,
            )

        if msg.tool_calls:
            logger.info(f"[api_chat] Detectados {len(msg.tool_calls)} tool_calls")
            # Constrói dict limpo do assistant — model_dump() inclui campos extras do SDK
            # que o Groq rejeita ou usa de forma errada na 2ª chamada.
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                nome = tc.function.name
                args = json.loads(tc.function.arguments)

                # ── Skill tool call ──────────────────────────────────────────
                skill = _resolve_skill_by_tool_name(nome)
                if skill:
                    pedido = args.get("pedido") or user_message
                    resultado_sk, img_sk, record = _run_skill_tool_call(skill, pedido)
                    if img_sk:
                        skill_output_image = img_sk
                    skill_executions.append(record)
                    tools_called.append({"tool": nome, "args": args, "result": resultado_sk})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": resultado_sk or "(skill executada sem saída)",
                    })
                    continue

                # ── Tool MCP normal ──────────────────────────────────────────
                args = _sanitize_tool_args(nome, args, last_loaded_path)
                args, resultado, exported_file_delta = _run_tool_call(nome, args)
                if exported_file_delta:
                    exported_file = exported_file_delta
                tools_called.append({"tool": nome, "args": args, "result": resultado})
                # Extrai texto extraído do JSON de OCR para exibição clara ao modelo
                content_para_modelo = _extract_ocr_text_from_result(nome, resultado)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content_para_modelo,
                })
                
                # Armazena texto extraído de OCR
                if nome == "ocr_extrair_texto" and content_para_modelo:
                    ocr_text_extracted = content_para_modelo

            # 2ª chamada: formula a resposta com base nos resultados das tools/skills.
            # Quando apenas skills foram executadas, usa prompt simplificado com resultados diretos
            # para evitar que modelos menores falhem ao interpretar o formato role:tool.
            only_skills = bool(skill_executions) and len(skill_executions) == len(tools_called)
            if only_skills:
                skill_results_text = "\n".join(
                    f"- {se['name']}: {se.get('output', '(sem saída)')}"
                    for se in skill_executions
                )
                messages_final = [
                    {
                        "role": "system",
                        "content": (
                            "Você é um assistente útil. Responda sempre em português.\n\n"
                            f"As seguintes skills foram executadas e retornaram:\n{skill_results_text}\n\n"
                            "Use esses resultados para responder diretamente ao usuário de forma clara e objetiva. "
                            "Não mencione nomes técnicos de tools ou funções internas."
                        ),
                    },
                    {"role": "user", "content": user_message},
                ]
            else:
                system_content = (
                    "Você é um assistente útil. Com base nos resultados das ferramentas já executadas, "
                    "formule uma resposta direta, clara e em português para o usuário. "
                    "NÃO mencione nomes de tools, funções internas ou restrições do sistema."
                )
                
                # Se OCR foi executada, adiciona instrução para mostrar o texto
                if ocr_text_extracted:
                    system_content += (
                        f"\n\n[IMPORTANTE] Você extraiu o seguinte texto de uma imagem:\n"
                        f"{ocr_text_extracted}\n\n"
                        f"RESPONDA MOSTRANDO ESTE TEXTO CLARAMENTE para o usuário."
                    )
                
                messages_final = [
                    {"role": "system", "content": system_content}
                ] + [m for m in messages if m["role"] != "system"]
            logger.info(f"[api_chat] Chamando modelo com {len(messages_final)} mensagens. Último role: {messages_final[-1]['role'] if messages_final else 'N/A'}")
            response_final = _client.chat.completions.create(
                model=selected_model,
                messages=messages_final,
            )
            answer = response_final.choices[0].message.content
            logger.info(f"[2ª chamada após tools] Resposta gerada com sucesso. Length: {len(answer) if answer else 0}")
        else:
            # Fallback: alguns modelos devolvem chamada de tool em texto em vez de tool_call.
            parsed_inline_calls = _extract_tool_calls_from_text(msg.content or "")
            if parsed_inline_calls:
                executed = []
                for nome, args in parsed_inline_calls:
                    if nome not in TOOL_MAP:
                        continue
                    args = _sanitize_tool_args(nome, args, last_loaded_path)
                    args, resultado, exported_file_delta = _run_tool_call(nome, args)
                    if exported_file_delta:
                        exported_file = exported_file_delta
                    tools_called.append({"tool": nome, "args": args, "result": resultado})
                    executed.append((nome, resultado))

                if not executed:
                    answer = msg.content
                    return _chat_save_and_respond(
                        session_id, session_title, selected_model,
                        user_message, answer, tools_called, exported_file,
                        skill_executions, skill_output_image,
                    )

                nome = executed[0][0]
                # Extrai texto extraído de OCR para cada resultado
                resultado_parts = []
                for n, r in executed:
                    content = _extract_ocr_text_from_result(n, r)
                    resultado_parts.append(f"[{n}] {content}")
                resultado = "\n".join(resultado_parts)

                messages_inline = messages + [
                    {
                        "role": "assistant",
                        "content": f"Ferramenta executada automaticamente: {nome} ({len(executed)} chamada(s)).",
                    },
                    {
                        "role": "assistant",
                        "content": f"Resultado da ferramenta: {resultado}",
                    },
                ]
                response_final = _client.chat.completions.create(
                    model=selected_model,
                    messages=messages_inline,
                )
                answer = response_final.choices[0].message.content
                logger.info(f"[api_chat] Fallback inline: resposta gerada. Length: {len(answer) if answer else 0}")
            else:
                # Fallback adicional: modelos às vezes "imprimem" o histórico [tool:...] args={...}
                parsed_hist_calls = _extract_history_style_tool_calls(msg.content or "")
                if parsed_hist_calls:
                    executed = []
                    for nome, args in parsed_hist_calls:
                        if nome not in TOOL_MAP:
                            continue
                        args = _sanitize_tool_args(nome, args, last_loaded_path)
                        args, resultado, exported_file_delta = _run_tool_call(nome, args)
                        if exported_file_delta:
                            exported_file = exported_file_delta
                        tools_called.append({"tool": nome, "args": args, "result": resultado})
                        executed.append((nome, resultado))

                    if executed:
                        nome = executed[0][0]
                        # Extrai texto extraído de OCR para cada resultado
                        resultado_parts = []
                        for n, r in executed:
                            content = _extract_ocr_text_from_result(n, r)
                            resultado_parts.append(f"[{n}] {content}")
                        resultado = "\n".join(resultado_parts)
                        messages_hist = messages + [
                            {
                                "role": "assistant",
                                "content": f"Ferramenta executada automaticamente: {nome} ({len(executed)} chamada(s)).",
                            },
                            {
                                "role": "assistant",
                                "content": f"Resultado da ferramenta: {resultado}",
                            },
                        ]
                        response_final = _client.chat.completions.create(
                            model=selected_model,
                            messages=messages_hist,
                        )
                        answer = response_final.choices[0].message.content
                        logger.info(f"[api_chat] Fallback history: resposta gerada. Length: {len(answer) if answer else 0}")
                    else:
                        answer = msg.content
                else:
                    answer = msg.content

        logger.info(f"[api_chat] Retornando resposta. tools_called={len(tools_called)}, answer_len={len(answer) if answer else 0}")
        return _chat_save_and_respond(
            session_id, session_title, selected_model,
            user_message, answer, tools_called, exported_file,
            skill_executions, skill_output_image,
        )

    except Exception as exc:
        import traceback
        logger.error(f"[api_chat] Erro não tratado: {exc}\n{traceback.format_exc()}")
        return JsonResponse({"error": str(exc)}, status=500)


def _chat_save_and_respond(
    session_id: str,
    session_title: str,
    model: str,
    user_message: str,
    answer: str,
    tools_called: list,
    exported_file,
    skill_executions: list,
    skill_output_image: str | None,
) -> JsonResponse:
    """Salva a interação no banco e retorna o JsonResponse padrão do chat_api."""
    # Cria ou recupera a sessão
    if session_id:
        session, _ = ChatSession.objects.get_or_create(
            id=session_id,
            defaults={"title": session_title, "model": model},
        )
        # Atualiza título/modelo se mudou
        if session.title != session_title or session.model != model:
            session.title = session_title
            session.model = model
            session.save(update_fields=["title", "model", "updated_at"])
    else:
        session = ChatSession.objects.create(
            id=uuid.uuid4().hex,
            title=session_title,
            model=model,
        )

    # Salva a mensagem do usuário e a resposta do assistente
    Message.objects.create(session=session, role="user", content=user_message)
    Message.objects.create(
        session=session,
        role="assistant",
        content=answer or "",
        tools_called=tools_called or [],
    )

    return JsonResponse({
        "answer": answer,
        "session_id": session.id,
        "tools_called": tools_called,
        "exported_file": exported_file,
        "activated_skills": [r["name"] for r in skill_executions],
        "skill_output_image": skill_output_image,
        "skill_executions": skill_executions,
    })


def settings_view(request):
    """Renderiza a tela de configurações."""
    return render(request, "chat/settings.html")


@require_GET
def download_file(request):
    """Serve arquivos exportados de /uploads/ para download seguro."""
    filename = request.GET.get("file", "")
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise Http404

    file_path = _UPLOAD_DIR / filename
    # Só serve arquivos dentro de uploads/ que existam
    if not file_path.exists() or not file_path.is_file():
        raise Http404
    try:
        file_path.resolve().relative_to(_UPLOAD_DIR.resolve())
    except ValueError:
        raise Http404

    content_type, _ = mimetypes.guess_type(str(file_path))
    content_type = content_type or "application/octet-stream"
    response = FileResponse(open(file_path, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response



def settings_api(request):
    """Retorna JSON com tools, agentes e configurações atuais."""
    tools_data = [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": t["function"]["parameters"].get("properties", {}),
            "required": t["function"]["parameters"].get("required", []),
        }
        for t in TOOLS
    ]
    return JsonResponse({
        "tools": tools_data,
        "agents": AGENTS,
        "default_model": DEFAULT_MODEL,
        "model_options": MODEL_OPTIONS,
    })


# ── Skills API ────────────────────────────────────────────────────────────────

@require_GET
def skill_list_api(request):
    """Lista todas as skills salvas."""
    return JsonResponse({"skills": _skills_list()}, json_dumps_params={"ensure_ascii": False})


@csrf_exempt
@require_POST
def skill_save_api(request):
    """Cria ou atualiza uma skill. Body JSON: {name, when_to_use, instructions, active?, id?, code?}."""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "JSON inválido."}, status=400)

    name = (data.get("name") or "").strip()
    when_to_use = (data.get("when_to_use") or "").strip()
    instructions = (data.get("instructions") or "").strip()

    if not name:
        return JsonResponse({"error": "Campo 'name' obrigatório."}, status=400)
    if not instructions:
        return JsonResponse({"error": "Campo 'instructions' obrigatório."}, status=400)
    if not when_to_use:
        return JsonResponse({"error": "Campo 'when_to_use' obrigatório."}, status=400)

    skill_id = (data.get("id") or "").strip() or uuid.uuid4().hex[:12]
    if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
        return JsonResponse({"error": "id inválido."}, status=400)

    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

    skill, _ = Skill.objects.update_or_create(
        id=skill_id,
        defaults={
            "name": name,
            "slug": slug,
            "when_to_use": when_to_use,
            "instructions": instructions,
            "code": (data.get("code") or "").strip(),
            "active": bool(data.get("active", True)),
        },
    )
    return JsonResponse({"skill": skill.to_dict()}, json_dumps_params={"ensure_ascii": False})


@csrf_exempt
@require_POST
def skill_delete_api(request):
    """Remove uma skill pelo id. Body JSON: {id}."""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "JSON inválido."}, status=400)

    skill_id = (data.get("id") or "").strip()
    if not skill_id or not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
        return JsonResponse({"error": "id inválido."}, status=400)

    deleted, _ = Skill.objects.filter(id=skill_id).delete()
    if not deleted:
        return JsonResponse({"error": "Skill não encontrada."}, status=404)

    return JsonResponse({"ok": True})


# ── Sessions API ──────────────────────────────────────────────────────────────

@require_GET
def session_list_api(request):
    """Lista todas as sessões salvas (sem mensagens)."""
    sessions = [s.to_dict() for s in ChatSession.objects.all()]
    return JsonResponse({"sessions": sessions}, json_dumps_params={"ensure_ascii": False})


@require_GET
def session_detail_api(request):
    """Retorna uma sessão com todas as mensagens. Query param: ?id=<session_id>"""
    session_id = request.GET.get("id", "").strip()
    if not session_id:
        return JsonResponse({"error": "Parâmetro 'id' obrigatório."}, status=400)
    try:
        session = ChatSession.objects.get(id=session_id)
    except ChatSession.DoesNotExist:
        return JsonResponse({"error": "Sessão não encontrada."}, status=404)
    return JsonResponse(session.to_dict(include_messages=True), json_dumps_params={"ensure_ascii": False})


@csrf_exempt
@require_POST
def session_delete_api(request):
    """Remove uma sessão e todas as suas mensagens. Body JSON: {id}."""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "JSON inválido."}, status=400)

    session_id = (data.get("id") or "").strip()
    if not session_id:
        return JsonResponse({"error": "Campo 'id' obrigatório."}, status=400)

    deleted, _ = ChatSession.objects.filter(id=session_id).delete()
    if not deleted:
        return JsonResponse({"error": "Sessão não encontrada."}, status=404)

    return JsonResponse({"ok": True})
