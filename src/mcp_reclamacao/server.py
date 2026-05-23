import json
from pathlib import Path
from contextvars import ContextVar
from fastmcp import FastMCP
try:
    from .core.dataset_registry import (
        get_active_dataset_id,
        list_session_datasets,
        load_dataset,
        resolve_dataset,
        save_dataset,
    )
except ImportError:
    from core.dataset_registry import (  # type: ignore
        get_active_dataset_id,
        list_session_datasets,
        load_dataset,
        resolve_dataset,
        save_dataset,
    )

mcp = FastMCP(
    "mcp_reclamacao",
    instructions="Servidor MCP para analise de reclamacoes. Fornece tools para carregar, filtrar e exportar dados de CSV ou Excel.",
)

# Cache por sessao: 1 chat = 1 contexto de dados isolado
_SESSION_ID: ContextVar[str] = ContextVar("mcp_session_id", default="default")
_CACHE_BY_SESSION: dict[str, dict[str, list[dict]]] = {}
_CACHE_FILTRADO_BY_SESSION: dict[str, dict[str, list[dict]]] = {}
_CACHE_META_BY_SESSION: dict[str, dict[str, dict]] = {}
_CACHE_FILTRADO_META_BY_SESSION: dict[str, dict[str, dict]] = {}


def set_active_session(session_id: str | None) -> None:
    _SESSION_ID.set((session_id or "default").strip() or "default")


def get_active_session_id() -> str:
    return _SESSION_ID.get()


def _cache() -> dict[str, list[dict]]:
    sid = get_active_session_id()
    return _CACHE_BY_SESSION.setdefault(sid, {})


def _cache_filtrado() -> dict[str, list[dict]]:
    sid = get_active_session_id()
    return _CACHE_FILTRADO_BY_SESSION.setdefault(sid, {})


def _cache_meta() -> dict[str, dict]:
    sid = get_active_session_id()
    return _CACHE_META_BY_SESSION.setdefault(sid, {})


def _cache_filtrado_meta() -> dict[str, dict]:
    sid = get_active_session_id()
    return _CACHE_FILTRADO_META_BY_SESSION.setdefault(sid, {})


def get_cache_snapshot() -> dict[str, list[dict]]:
    # Compatibilidade: snapshot do "cache" da sessao, agora vindo do dataset registry.
    sid = get_active_session_id()
    out: dict[str, list[dict]] = {}
    for ds in list_session_datasets(sid):
        df = load_dataset(ds["dataset_id"])
        if df is not None:
            out[ds["source_ref"]] = df.to_dict(orient="records")
    return out


def _registrar_meta(caminho: str, tool: str, colunas: list, filtrado: bool = False) -> None:
    """Registra metadados de qual tool modificou o cache e quando."""
    from datetime import datetime
    entry = {"tool": tool, "colunas": colunas, "ts": datetime.now().strftime("%H:%M:%S")}
    if filtrado:
        _cache_filtrado_meta()[caminho] = entry
    else:
        _cache_meta()[caminho] = entry


def _slug_coluna(nome: str) -> str:
    """Normaliza nome de coluna para comparação tolerante a acento/variações."""
    import re
    import unicodedata

    txt = str(nome or "").strip().lower()
    txt = "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn")
    txt = txt.replace("limpio", "limpo")
    txt = re.sub(r"[^a-z0-9_]+", "_", txt)
    txt = re.sub(r"_+", "_", txt).strip("_")
    return txt


def _resolver_coluna(df, coluna: str) -> tuple[str | None, list[str]]:
    """Resolve nome de coluna com tolerância a acento/typo e devolve sugestões."""
    import difflib

    colunas = [str(c) for c in df.columns]
    if coluna in colunas:
        return coluna, []

    alvo = _slug_coluna(coluna)
    mapa_slug: dict[str, str] = {}
    for c in colunas:
        s = _slug_coluna(c)
        if s not in mapa_slug:
            mapa_slug[s] = c

    if alvo in mapa_slug:
        return mapa_slug[alvo], []

    # Tenta variantes comuns
    variantes = {
        alvo,
        alvo.replace("_limpio", "_limpo"),
        alvo.replace("limpio", "limpo"),
        alvo.replace("transcricao", "transcricao"),
        alvo.replace("transcri", "transcri"),
    }
    for v in variantes:
        if v in mapa_slug:
            return mapa_slug[v], []

    sug_slugs = difflib.get_close_matches(alvo, list(mapa_slug.keys()), n=3, cutoff=0.6)
    sugestoes = [mapa_slug[s] for s in sug_slugs]
    return None, sugestoes


@mcp.tool(
    description=(
        "Carrega um arquivo CSV ou Excel. "
        "Armazena todos os registros internamente para processamento em lote. "
        "Retorna ao modelo apenas o schema (colunas) e 3 linhas de preview. "
        "Suporta .csv, .xlsx e .xls."
    )
)
def carregar_arquivo(caminho: str) -> str:
    """Le CSV ou Excel, guarda no cache e retorna apenas schema + preview ao LLM."""
    import pandas as pd

    path = Path(caminho)
    if not path.exists():
        return json.dumps({"erro": f"Arquivo nao encontrado: {caminho}"})

    if path.suffix.lower() in [".xlsx", ".xls"]:
        df = pd.read_excel(caminho)
    else:
        df = pd.read_csv(caminho, encoding="utf-8", on_bad_lines="skip")

    sid = get_active_session_id()
    dataset_id = save_dataset(sid, df, caminho, is_filtered=False)
    _registrar_meta(caminho, "carregar_arquivo", list(df.columns))

    return json.dumps(
        {
            "dataset_id": dataset_id,
            "total_registros": len(df),
            "colunas": list(df.columns),
            "preview_3_linhas": df.head(3).to_dict(orient="records"),
        },
        ensure_ascii=False,
        default=str,
    )


def _get_from_cache(caminho: str, usar_filtrado: bool = False) -> tuple[list[dict] | None, str]:
    """Recupera dados da sessao via Dataset Registry por dataset_id/caminho/nome."""
    sid = get_active_session_id()
    dataset_id, source_ref = resolve_dataset(sid, caminho or "", prefer_filtered=usar_filtrado)
    if not dataset_id:
        return None, ""
    df = load_dataset(dataset_id)
    if df is None:
        return None, ""
    return df.to_dict(orient="records"), source_ref


def _get_dataset_id_from_ref(caminho: str, usar_filtrado: bool = False) -> str | None:
    sid = get_active_session_id()
    dataset_id, _source_ref = resolve_dataset(sid, caminho or "", prefer_filtered=usar_filtrado)
    return dataset_id
    return None, ""


@mcp.tool(
    description="Normaliza texto (lowercase, remove pontuação e acentos) para uma coluna do dataframe em cache."
)
def normalizar_nlp(caminho: str, coluna: str) -> str:
    import pandas as pd
    import unicodedata
    import re

    dados, path_real = _get_from_cache(caminho)
    if dados is None:
        return json.dumps({"erro": f"Arquivo '{caminho}' nao esta no cache."})

    df = pd.DataFrame(dados)
    coluna_real, sugestoes = _resolver_coluna(df, coluna)
    if not coluna_real:
        return json.dumps(
            {
                "erro": f"Coluna '{coluna}' nao existe para normalizacao.",
                "sugestoes": sugestoes,
                "colunas_disponiveis": list(df.columns),
            },
            ensure_ascii=False,
        )
    
    def clean(text):
        if not isinstance(text, str): return text
        text = text.lower()
        text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
        text = re.sub(r"[^\w\s]", "", text)
        return text.strip()

    nova_coluna = f"{coluna_real}_limpo"
    df[nova_coluna] = df[coluna_real].apply(clean)
    sid = get_active_session_id()
    parent_id = _get_dataset_id_from_ref(path_real, usar_filtrado=False)
    save_dataset(sid, df, path_real, is_filtered=False, parent_dataset_id=parent_id)
    _registrar_meta(caminho, "normalizar_nlp", list(df.columns))
    return json.dumps({"sucesso": True, "coluna_origem": coluna_real, "nova_coluna": nova_coluna}, ensure_ascii=False)


@mcp.tool(
    description="Filtra registros que mencionam palavras-chave específicas em uma coluna."
)
def filtrar_por_palavras(caminho: str, coluna: str, palavras: list[str]) -> str:
    import pandas as pd
    dados, path_real = _get_from_cache(caminho)
    if dados is None: return json.dumps({"erro": "Arquivo nao em cache"})

    df = pd.DataFrame(dados)
    coluna_real, sugestoes = _resolver_coluna(df, coluna)
    if not coluna_real:
        return json.dumps(
            {
                "erro": f"Coluna '{coluna}' nao existe para filtro por palavras.",
                "sugestoes": sugestoes,
                "colunas_disponiveis": list(df.columns),
            },
            ensure_ascii=False,
        )

    padrao = "|".join(palavras)
    df_filtrado = df[df[coluna_real].astype(str).str.contains(padrao, case=False, na=False)]
    
    sid = get_active_session_id()
    parent_id = _get_dataset_id_from_ref(path_real, usar_filtrado=False)
    save_dataset(sid, df_filtrado, path_real, is_filtered=True, parent_dataset_id=parent_id)
    _registrar_meta(path_real, "filtrar_por_palavras", list(df_filtrado.columns), filtrado=True)
    return json.dumps(
        {
            "coluna_usada": coluna_real,
            "total_original": len(df),
            "total_filtrado": len(df_filtrado),
        },
        ensure_ascii=False,
    )


@mcp.tool(
    description="Lematiza uma coluna do dataframe (ex: 'reclamacoes' -> 'reclamacao'). Utiliza o modelo spaCy pt_core_news_sm."
)
def lematizar_nlp(caminho: str, coluna: str) -> str:
    import pandas as pd
    import spacy

    dados, path_real = _get_from_cache(caminho)
    if dados is None:
        return json.dumps({"erro": "Arquivo nao em cache"})

    # Carrega o modelo (o download ja foi feito no passo anterior)
    try:
        nlp = spacy.load("pt_core_news_sm", disable=["parser", "ner"])
    except Exception:
        return json.dumps({"erro": "Modelo spaCy 'pt_core_news_sm' nao encontrado. Execute 'python -m spacy download pt_core_news_sm'"})

    df = pd.DataFrame(dados)

    coluna_informada = str(coluna or "").strip()
    coluna_base_pedida = coluna_informada
    for sufixo in ("_limpio", "_limpo", "_lemma"):
        if coluna_base_pedida.endswith(sufixo):
            coluna_base_pedida = coluna_base_pedida[: -len(sufixo)]
            break

    coluna_base_real, sugestoes_base = _resolver_coluna(df, coluna_base_pedida)
    if not coluna_base_real:
        return json.dumps(
            {
                "erro": f"Coluna base '{coluna}' nao encontrada para lematizacao.",
                "sugestoes": sugestoes_base,
                "colunas_disponiveis": list(df.columns),
            },
            ensure_ascii=False,
        )
    
    def lemmatize(text):
        if not isinstance(text, str) or not text.strip(): return text
        doc = nlp(text)
        return " ".join([token.lemma_ for token in doc])

    # Se a coluna limpa existir (pos-normalizacao), lematizamos ela.
    # Tambem aceita quando o usuario informa '_limpio' por engano.
    coluna_alvo, _ = _resolver_coluna(df, f"{coluna_base_real}_limpo")
    if not coluna_alvo:
        coluna_alvo = coluna_base_real

    nova_coluna = f"{coluna_base_real}_lemma"
    df[nova_coluna] = df[coluna_alvo].apply(lemmatize)
    
    sid = get_active_session_id()
    parent_id = _get_dataset_id_from_ref(path_real, usar_filtrado=False)
    save_dataset(sid, df, path_real, is_filtered=False, parent_dataset_id=parent_id)
    _registrar_meta(path_real, "lematizar_nlp", list(df.columns))
    return json.dumps(
        {
            "sucesso": True,
            "coluna_base": coluna_base_real,
            "coluna_lematizada_a_partir_de": coluna_alvo,
            "nova_coluna": nova_coluna,
        },
        ensure_ascii=False,
    )


def get_registros_cache(caminho: str) -> list[dict]:
    """Funcao Python pura (nao e tool). Retorna os registros completos do cache.
    Usada pelo agente para iterar em lotes sem passar tudo pelo contexto do LLM."""
    dados, _path = _get_from_cache(caminho, usar_filtrado=False)
    return dados or []


def get_registros_filtrado(caminho: str) -> list[dict]:
    """Funcao Python pura (nao e tool). Retorna os registros do cache filtrado."""
    dados, _path = _get_from_cache(caminho, usar_filtrado=True)
    return dados or []


@mcp.tool(
    description=(
        "Lista todos os arquivos carregados na sessão atual, suas colunas disponíveis, "
        "quantos registros têm e qual tool os processou por último. "
        "Use SEMPRE que não souber qual arquivo usar ou antes de chamar qualquer tool de processamento."
    )
)
def listar_contexto_sessao() -> str:
    """Retorna o estado atual do cache: arquivos carregados, colunas e qual tool gerou cada estado."""
    sid = get_active_session_id()
    datasets = list_session_datasets(sid)
    if not datasets:
        return json.dumps(
            {"status": "vazio", "mensagem": "Nenhum dataset na sessao. Use carregar_arquivo primeiro."},
            ensure_ascii=False,
        )

    itens = []
    for ds in datasets:
        df = load_dataset(ds["dataset_id"])
        colunas = list(df.columns) if df is not None else []
        itens.append(
            {
                "dataset_id": ds["dataset_id"],
                "arquivo": ds["source_name"],
                "caminho": ds["source_ref"],
                "total_registros": ds["row_count"],
                "colunas": colunas,
                "is_filtrado": bool(ds["is_filtered"]),
                "parent_dataset_id": ds["parent_dataset_id"],
                "criado_em": ds["created_at"],
            }
        )

    return json.dumps(
        {
            "session_id": sid,
            "datasets": itens,
            "total_datasets": len(itens),
            "dataset_ativo": get_active_dataset_id(sid, filtered=False),
            "dataset_filtrado_ativo": get_active_dataset_id(sid, filtered=True),
        },
        ensure_ascii=False,
        default=str,
    )


@mcp.tool(
    description=(
        "Filtra o dataframe já carregado em memória com base em condições informadas. "
        "Recebe uma lista JSON de filtros, cada um com 'coluna', 'operador' e 'valor'. "
        "Operadores suportados: ==, !=, >, <, >=, <=, contains, startswith, endswith, isnull, notnull. "
        "O resultado filtrado fica em cache e pode ser exportado com exportar_dataframe. "
        "Retorna total original, total filtrado e preview de 3 linhas."
    )
)
def filtrar_registros(caminho: str, filtros_json: str) -> str:
    """Aplica filtros ao dataframe em cache e armazena o resultado filtrado."""
    import pandas as pd

    dados_cache, path_real = _get_from_cache(caminho, usar_filtrado=False)
    if dados_cache is None:
        return json.dumps(
            {
                "erro": f"Arquivo nao carregado no cache: {caminho}. Use carregar_arquivo primeiro.",
                "dica": "Use o caminho completo retornado por carregar_arquivo ou chame listar_contexto_sessao.",
            }
        )

    try:
        filtros = json.loads(filtros_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"erro": f"filtros_json invalido: {exc}"})

    df = pd.DataFrame(dados_cache)
    total_original = len(df)

    for f in filtros:
        coluna = (
            f.get("coluna")
            or f.get("nome")
            or f.get("column")
            or f.get("campo")
        )
        operador = (
            f.get("operador")
            or f.get("comparacao")
            or f.get("operator")
            or f.get("op")
        )
        valor = f.get("valor")
        if valor is None:
            valor = f.get("value")

        # Normaliza variações comuns enviadas pelo modelo/UI
        op_norm = str(operador or "").strip().lower()
        op_map = {
            "contém": "contains",
            "contem": "contains",
            "igual": "==",
            "diferente": "!=",
            "maior": ">",
            "menor": "<",
            "maior_ou_igual": ">=",
            "menor_ou_igual": "<=",
            "inicia_com": "startswith",
            "termina_com": "endswith",
            "nulo": "isnull",
            "nao_nulo": "notnull",
            "não_nulo": "notnull",
        }
        operador = op_map.get(op_norm, operador)

        # Modelos menores às vezes inventam prefixo "_" em colunas textuais
        coluna_informada = str(coluna or "").strip()
        if coluna_informada.startswith("_"):
            coluna_informada = coluna_informada.lstrip("_")

        coluna_real, sugestoes = _resolver_coluna(df, coluna_informada)
        if not coluna_real:
            return json.dumps(
                {
                    "erro": f"Coluna '{coluna}' nao existe.",
                    "sugestoes": sugestoes,
                    "colunas_disponiveis": list(df.columns),
                },
                ensure_ascii=False,
            )

        if operador == "==":
            df = df[df[coluna_real] == valor]
        elif operador == "!=":
            df = df[df[coluna_real] != valor]
        elif operador == ">":
            df = df[df[coluna_real] > valor]
        elif operador == "<":
            df = df[df[coluna_real] < valor]
        elif operador == ">=":
            df = df[df[coluna_real] >= valor]
        elif operador == "<=":
            df = df[df[coluna_real] <= valor]
        elif operador == "contains":
            df = df[df[coluna_real].astype(str).str.contains(str(valor), case=False, na=False)]
        elif operador == "startswith":
            df = df[df[coluna_real].astype(str).str.startswith(str(valor), na=False)]
        elif operador == "endswith":
            df = df[df[coluna_real].astype(str).str.endswith(str(valor), na=False)]
        elif operador == "isnull":
            df = df[df[coluna_real].isnull()]
        elif operador == "notnull":
            df = df[df[coluna_real].notnull()]
        else:
            return json.dumps({"erro": f"Operador '{operador}' nao suportado."})

    sid = get_active_session_id()
    parent_id = _get_dataset_id_from_ref(path_real, usar_filtrado=False)
    save_dataset(sid, df, path_real, is_filtered=True, parent_dataset_id=parent_id)
    _registrar_meta(path_real, "filtrar_registros", list(df.columns), filtrado=True)
    return json.dumps(
        {
            "total_original": total_original,
            "total_filtrado": len(df),
            "colunas": list(df.columns),
        },
        ensure_ascii=False,
    )


@mcp.tool(
    description=(
        "Realiza análise de série temporal em uma coluna de data. "
        "Permite agrupar por Dia (D), Mês (MS) ou Ano (YS). "
        "Calcula métricas como contagem (count), soma (sum) ou média (mean) de uma coluna de valor. "
        "Se 'metrica' for 'sum' ou 'mean', a 'coluna_valor' é OBRIGATÓRIA. "
        "Se 'coluna_valor' não for informada, realize apenas a contagem (count)."
    )
)
def analisar_serie_temporal(
    caminho: str,
    coluna_data: str,
    frequencia: str = "MS",
    metrica: str = "count",
    coluna_valor: str | None = None,
    usar_cache_filtrado: bool = False,
) -> str:
    """Gera dados de série temporal para análise de tendências."""
    import pandas as pd

    dados, path_real = _get_from_cache(caminho, usar_cache_filtrado)
    if dados is None:
        return json.dumps({
            "erro": f"Dados não encontrados para o caminho: {caminho}",
            "dica": "Certifique-se de carregar o arquivo primeiro."
        })

    df = pd.DataFrame(dados)

    # Resolve coluna de data
    col_dt, sug_dt = _resolver_coluna(df, coluna_data)
    if not col_dt:
        return json.dumps({"erro": f"Coluna de data '{coluna_data}' não encontrada.", "sugestoes": sug_dt})

    try:
        # Converte para datetime garantindo que seja data (suporta dia/mes/ano ou ano-mes-dia)
        df[col_dt] = pd.to_datetime(df[col_dt], errors="coerce", dayfirst=True)
        df = df.dropna(subset=[col_dt])
    except Exception as e:
        return json.dumps({"erro": f"Erro ao converter coluna '{col_dt}' para data: {str(e)}"})

    # Se metrica for soma ou media, precisamos de uma coluna de valor
    if metrica in ["sum", "mean"]:
        col_val, sug_val = _resolver_coluna(df, coluna_valor) if coluna_valor else (None, [])
        if not col_val:
            return json.dumps({"erro": f"Métrica '{metrica}' requer uma 'coluna_valor' numérica válida."})
        
        # Garante que a coluna de valor é numérica
        df[col_val] = pd.to_numeric(df[col_val], errors="coerce")
        res = df.resample(frequencia, on=col_dt)[col_val].agg(metrica).reset_index()
    else:
        # Default: contagem de registros
        res = df.resample(frequencia, on=col_dt).size().reset_index(name="quantidade")

    # Formata a data para leitura fácil no JSON
    res[col_dt] = res[col_dt].dt.strftime("%Y-%m-%d")

    return json.dumps(
        {
            "frequencia": frequencia,
            "metrica": metrica,
            "dados": res.to_dict(orient="records"),
            "resumo": f"Análise temporal de {len(res)} períodos concluída.",
        },
        ensure_ascii=False,
    )


@mcp.tool(
    description=(
        "Agrupa registros por uma ou mais colunas e calcula estatísticas. "
        "Suporta: count, sum, mean, median, std, min, max, p25, p75, p90, p95, p99. "
        "Resultado fica em cache para posterior exportação."
    )
)
def agrupar_registros(
    caminho: str,
    colunas_agrupamento: list[str] | str,
    metricas: list[str] = None,
    coluna_valor: str | None = None,
    lidar_nulos: str = "drop",
    usar_cache_filtrado: bool = False,
) -> str:
    """Agrupa dados por dimensões categóricas com múltiplas métricas."""
    import pandas as pd

    # Normalizar input
    if isinstance(colunas_agrupamento, str):
        colunas_agrupamento = [colunas_agrupamento]

    if metricas is None:
        metricas = ["count", "mean"] if coluna_valor else ["count"]

    # Recupera dados
    dados, path_real = _get_from_cache(caminho, usar_cache_filtrado)
    if dados is None:
        return json.dumps(
            {
                "erro": f"Dados não encontrados para: {caminho}",
                "dica": (
                    "Use o caminho completo retornado por carregar_arquivo "
                    "ou chame listar_contexto_sessao para ver os caminhos válidos em cache."
                ),
            },
            ensure_ascii=False,
        )

    df = pd.DataFrame(dados)

    # Validações
    if len(colunas_agrupamento) > 3:
        return json.dumps({"erro": "Máximo 3 colunas para agrupamento"})

    # Resolver colunas de agrupamento
    cols_reais = []
    for col in colunas_agrupamento:
        col_real, sugestoes = _resolver_coluna(df, col)
        if not col_real:
            return json.dumps({
                "erro": f"Coluna '{col}' não encontrada",
                "sugestoes": sugestoes,
                "colunas_disponiveis": list(df.columns)
            }, ensure_ascii=False)
        cols_reais.append(col_real)

    # Resolver coluna de valor se necessário
    col_valor_real = None
    if coluna_valor:
        col_valor_real, sugestoes = _resolver_coluna(df, coluna_valor)
        if not col_valor_real:
            return json.dumps({
                "erro": f"Coluna de valor '{coluna_valor}' não encontrada",
                "sugestoes": sugestoes
            }, ensure_ascii=False)
        # Validar que é numérica
        df[col_valor_real] = pd.to_numeric(df[col_valor_real], errors="coerce")

    # Validar que métricas com valor têm coluna_valor
    metricas_com_valor = {"sum", "mean", "median", "std", "min", "max"} | {m for m in metricas if m.startswith("p")}
    if any(m in metricas for m in metricas_com_valor) and not col_valor_real:
        return json.dumps({
            "erro": f"Métricas {list(metricas_com_valor)} requerem 'coluna_valor' numérica"
        }, ensure_ascii=False)

    # Tratar NaN
    df_work = df.copy()
    removidas = 0

    if lidar_nulos == "drop":
        colunas_check = cols_reais + ([col_valor_real] if col_valor_real else [])
        removidas = len(df_work) - len(df_work.dropna(subset=colunas_check))
        df_work = df_work.dropna(subset=colunas_check)
    elif lidar_nulos == "fill":
        for col in cols_reais + ([col_valor_real] if col_valor_real else []):
            if df_work[col].dtype == 'object':
                df_work[col] = df_work[col].fillna("Vazio")
            else:
                df_work[col] = df_work[col].fillna(0)
    elif lidar_nulos == "group":
        for col in cols_reais:
            if df_work[col].dtype == 'object':
                df_work[col] = df_work[col].fillna("(Vazio)")

    # Executar agrupamento
    try:
        # Preparar dicionário de agregação
        agg_dict = {}

        for metrica in metricas:
            if metrica == "count":
                pass
            elif metrica.startswith("p") and metrica[1:].isdigit():
                percentil = int(metrica[1:]) / 100.0
                agg_dict[metrica] = (col_valor_real, lambda x, p=percentil: x.quantile(p))
            elif metrica in ["mean", "sum", "std", "min", "max", "median"]:
                agg_dict[metrica] = (col_valor_real, metrica)

        # Executar agrupamento
        if agg_dict:
            # Há agregações além/ou incluindo count
            resultado = df_work.groupby(cols_reais, as_index=False).agg(**agg_dict)
            if "count" in metricas:
                resultado["count"] = df_work.groupby(cols_reais).size().values
        else:
            # Apenas count
            resultado = df_work.groupby(cols_reais).size().reset_index(name="count")

        # Formatar resultado
        resultado = resultado.fillna(0)

        # Arredondar apenas colunas numéricas
        for col in resultado.columns:
            if resultado[col].dtype in ['float64', 'float32']:
                resultado[col] = resultado[col].round(4)

    except Exception as e:
        return json.dumps({"erro": f"Erro ao agrupar: {str(e)}"}, ensure_ascii=False)

    # Armazenar em cache
    sid = get_active_session_id()
    parent_id = _get_dataset_id_from_ref(path_real, usar_filtrado=usar_cache_filtrado)
    save_dataset(sid, resultado, path_real, is_filtered=True, parent_dataset_id=parent_id)
    _registrar_meta(path_real, "agrupar_registros", list(resultado.columns), filtrado=True)

    # Preparar resumo legível para apresentação
    resumo_itens = []
    for idx, row in resultado.iterrows():
        if idx >= 10:  # Mostrar apenas top 10
            break
        item_str = " | ".join([f"{col}: {row[col]}" for col in resultado.columns])
        resumo_itens.append(item_str)

    resumo_legivel = "\n".join(resumo_itens)
    if len(resultado) > 10:
        resumo_legivel += f"\n... e mais {len(resultado) - 10} grupos"

    # Montar resposta
    resposta = {
        "sucesso": True,
        "total_grupos": len(resultado),
        "metricas_calculadas": metricas,
        "dados": resultado.to_dict(orient="records")[:20],
        "resumo": f"Agrupamento concluído: {len(resultado)} grupos encontrados",
        "resumo_legivel": resumo_legivel,
    }

    if len(colunas_agrupamento) == 1:
        resposta["coluna_agrupamento"] = cols_reais[0]
    else:
        resposta["colunas_agrupamento"] = cols_reais

    if removidas > 0:
        resposta["aviso"] = f"{removidas} linhas com NaN foram removidas"

    return json.dumps(resposta, ensure_ascii=False, default=str)


@mcp.tool(
    description=(
        "Exporta o dataframe para um arquivo CSV. "
        "Por padrão exporta o dataframe filtrado (se houver filtro aplicado). "
        "Passe usar_filtrado=false para exportar o dataframe completo original. "
        "Cria o diretório de destino automaticamente se não existir."
    )
)
def exportar_dataframe(caminho: str, caminho_saida: str, usar_filtrado: bool = True) -> str:
    """Salva o dataframe (filtrado ou completo) em CSV."""
    import pandas as pd

    # Usa _get_from_cache para normalizar barras/aspas igual às outras tools
    dados_filtrado, path_real = _get_from_cache(caminho, usar_filtrado=True)
    dados_completo, path_real_c = _get_from_cache(caminho, usar_filtrado=False)

    if usar_filtrado and dados_filtrado is not None:
        registros = dados_filtrado
        caminho = path_real
        origem = "filtrado"
    elif dados_completo is not None:
        registros = dados_completo
        caminho = path_real_c
        origem = "completo"
    else:
        return json.dumps({"erro": f"Nenhum dado em cache para: {caminho}. Use carregar_arquivo primeiro."})

    if not registros:
        return json.dumps({"erro": "Nenhum registro para exportar (resultado vazio)."})

    df = pd.DataFrame(registros)
    Path(caminho_saida).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(caminho_saida, index=False, encoding="utf-8-sig")

    return json.dumps(
        {
            "sucesso": True,
            "origem": origem,
            "registros_exportados": len(df),
            "arquivo": caminho_saida,
        },
        ensure_ascii=False,
    )


@mcp.tool(
    description=("OCR para extrair texto de imagens. Recebe o caminho de uma imagem e retorna o texto extraído. Suporta formatos comuns como .jpg, .png, .pdf (primeira página).")
)
def ocr_extrair_texto(caminho_imagem: str) -> str:
    """Extrai texto de uma imagem usando OCR."""
    import pytesseract
    from PIL import Image
    import pdfplumber
    import os

    # Pasta tessdata local do projeto (contém por.traineddata)
    local_tessdata = str(Path(__file__).parents[2] / "suporte" / "tessdata")

    # Executável: preferir instalação padrão Windows, depois PATH
    tesseract_exe = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    tesseract_dir = r"C:\Program Files\Tesseract-OCR"
    if not Path(tesseract_exe).exists():
        import shutil
        found = shutil.which("tesseract")
        if found:
            tesseract_exe = found
            tesseract_dir = str(Path(found).parent)

    # Adiciona a pasta ao PATH para que as DLLs dependentes sejam encontradas
    if tesseract_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = tesseract_dir + os.pathsep + os.environ.get("PATH", "")

    # Usa o tessdata local do projeto (garante por.traineddata disponível)
    os.environ["TESSDATA_PREFIX"] = local_tessdata

    pytesseract.pytesseract.tesseract_cmd = tesseract_exe

    try:
        if caminho_imagem.lower().endswith(".pdf"):
            with pdfplumber.open(caminho_imagem) as pdf:
                primeira_pagina = pdf.pages[0]
                img = primeira_pagina.to_image()
                texto = pytesseract.image_to_string(img.original, lang="por")
        else:
            img = Image.open(caminho_imagem)
            texto = pytesseract.image_to_string(img, lang="por")
        return json.dumps({"sucesso": True, "texto_extraido": texto.strip()})
    except Exception as e:
        return json.dumps({"erro": f"Falha ao processar imagem: {str(e)}"})


if __name__ == "__main__":
    mcp.run()
