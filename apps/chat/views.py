import json
import mimetypes
import os
import re
import inspect
import sys
import uuid
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
from server import analisar_serie_temporal, carregar_arquivo, exportar_dataframe, filtrar_por_palavras, filtrar_registros, lematizar_nlp, listar_contexto_sessao, normalizar_nlp, ocr_extrair_texto
from .agent_config import AGENTS, DEFAULT_MODEL, MODEL_OPTIONS, SYSTEM_PROMPT, TOOLS

_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

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
}


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


def _extract_failed_tool_from_error(error_text: str) -> tuple[str, dict] | None:
    """Extrai (nome_da_tool, args) de mensagens tool_use_failed do Groq."""
    fn_prefix = r"(?<!\\w)(?:<|\\.)?function="

    # Formato 1: <function=nome={...}>
    m = re.search(rf"{fn_prefix}([a-zA-Z_][\\w]*)=(\{{.*\}})>", error_text)
    if m:
        tool_name = m.group(1)
        raw_args = m.group(2)
        try:
            return tool_name, json.loads(raw_args)
        except Exception:
            return None

    # Formato 2: <function=nome({...})>
    m = re.search(rf"{fn_prefix}([a-zA-Z_][\\w]*)\((\{{.*\}})\)>", error_text)
    if m:
        tool_name = m.group(1)
        raw_args = m.group(2)
        try:
            return tool_name, json.loads(raw_args)
        except Exception:
            return None

    # Formato 3: <function=nome>{...}
    m = re.search(rf"{fn_prefix}([a-zA-Z_][\\w]*)>\s*(\{{.*\}})", error_text)
    if m:
        tool_name = m.group(1)
        raw_args = m.group(2)
        try:
            return tool_name, json.loads(raw_args)
        except Exception:
            return None

    return None


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
                break

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
        return args

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
        last_loaded_path = _find_last_loaded_path(history)

        if not user_message:
            return JsonResponse({"error": "Mensagem vazia."}, status=400)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

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
                messages[0]["content"] = SYSTEM_PROMPT + "\n\n" + ctx_text
        except Exception:
            pass  # Se o cache estiver vazio ou ocorrer erro, segue sem contexto

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
        msg = None

        # 1ª chamada: o modelo decide se usa tool ou responde direto
        try:
            response = _client.chat.completions.create(
                model=selected_model,
                messages=messages,
                tools=TOOLS,
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

            return JsonResponse({
                "answer": answer,
                "tools_called": tools_called,
                "exported_file": exported_file,
            })

        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                nome = tc.function.name
                args = json.loads(tc.function.arguments)
                args = _sanitize_tool_args(nome, args, last_loaded_path)

                args, resultado, exported_file_delta = _run_tool_call(nome, args)
                if exported_file_delta:
                    exported_file = exported_file_delta
                tools_called.append({"tool": nome, "args": args, "result": resultado})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(resultado),
                })

            # 2ª chamada: formulação da resposta final
            response_final = _client.chat.completions.create(
                model=selected_model,
                messages=messages,
            )
            answer = response_final.choices[0].message.content
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
                    return JsonResponse({
                        "answer": answer,
                        "tools_called": tools_called,
                        "exported_file": exported_file,
                    })

                nome = executed[0][0]
                resultado = "\n".join(f"[{n}] {r}" for n, r in executed)

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
                        resultado = "\n".join(f"[{n}] {r}" for n, r in executed)
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
                    else:
                        answer = msg.content
                else:
                    answer = msg.content

        return JsonResponse({
            "answer": answer,
            "tools_called": tools_called,
            "exported_file": exported_file,
        })

    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


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
