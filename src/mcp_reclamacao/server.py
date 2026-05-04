import json
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP(
    "mcp_reclamacao",
    instructions="Servidor MCP para analise de reclamacoes. Fornece tools para carregar, filtrar e exportar dados de CSV ou Excel.",
)

# Cache interno: armazena os registros completos por caminho de arquivo
# O LLM nao tem acesso direto a este cache - so o agente acessa via get_registros_cache()
_CACHE: dict[str, list[dict]] = {}

# Cache do dataframe filtrado (sobrescrito a cada filtro aplicado)
_CACHE_FILTRADO: dict[str, list[dict]] = {}


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

    # Guarda tudo no cache para processamento posterior
    _CACHE[caminho] = df.to_dict(orient="records")

    return json.dumps(
        {
            "total_registros": len(df),
            "colunas": list(df.columns),
            "preview_3_linhas": df.head(3).to_dict(orient="records"),
        },
        ensure_ascii=False,
        default=str,
    )


@mcp.tool(
    description="Normaliza texto (lowercase, remove pontuação e acentos) para uma coluna do dataframe em cache."
)
def normalizar_nlp(caminho: str, coluna: str) -> str:
    import pandas as pd
    import unicodedata
    import re

    if caminho not in _CACHE:
        return json.dumps({"erro": "Arquivo nao em cache"})

    df = pd.DataFrame(_CACHE[caminho])
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
    _CACHE[caminho] = df.to_dict(orient="records")
    return json.dumps({"sucesso": True, "coluna_origem": coluna_real, "nova_coluna": nova_coluna}, ensure_ascii=False)


@mcp.tool(
    description="Filtra registros que mencionam palavras-chave específicas em uma coluna."
)
def filtrar_por_palavras(caminho: str, coluna: str, palavras: list[str]) -> str:
    import pandas as pd
    if caminho not in _CACHE: return json.dumps({"erro": "Cache vazio"})

    df = pd.DataFrame(_CACHE[caminho])
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
    
    _CACHE_FILTRADO[caminho] = df_filtrado.to_dict(orient="records")
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

    if caminho not in _CACHE:
        return json.dumps({"erro": "Arquivo nao em cache"})

    # Carrega o modelo (o download ja foi feito no passo anterior)
    try:
        nlp = spacy.load("pt_core_news_sm", disable=["parser", "ner"])
    except Exception:
        return json.dumps({"erro": "Modelo spaCy 'pt_core_news_sm' nao encontrado. Execute 'python -m spacy download pt_core_news_sm'"})

    df = pd.DataFrame(_CACHE[caminho])

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
    
    _CACHE[caminho] = df.to_dict(orient="records")
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
    return _CACHE.get(caminho, [])


def get_registros_filtrado(caminho: str) -> list[dict]:
    """Funcao Python pura (nao e tool). Retorna os registros do cache filtrado."""
    return _CACHE_FILTRADO.get(caminho, [])


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

    if caminho not in _CACHE:
        return json.dumps({"erro": f"Arquivo nao carregado no cache: {caminho}. Use carregar_arquivo primeiro."})

    try:
        filtros = json.loads(filtros_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"erro": f"filtros_json invalido: {exc}"})

    df = pd.DataFrame(_CACHE[caminho])
    total_original = len(df)

    for f in filtros:
        coluna = f.get("coluna")
        operador = f.get("operador")
        valor = f.get("valor")

        if coluna not in df.columns:
            return json.dumps({"erro": f"Coluna '{coluna}' nao existe. Colunas disponíveis: {list(df.columns)}"})

        if operador == "==":
            df = df[df[coluna] == valor]
        elif operador == "!=":
            df = df[df[coluna] != valor]
        elif operador == ">":
            df = df[df[coluna] > valor]
        elif operador == "<":
            df = df[df[coluna] < valor]
        elif operador == ">=":
            df = df[df[coluna] >= valor]
        elif operador == "<=":
            df = df[df[coluna] <= valor]
        elif operador == "contains":
            df = df[df[coluna].astype(str).str.contains(str(valor), case=False, na=False)]
        elif operador == "startswith":
            df = df[df[coluna].astype(str).str.startswith(str(valor), na=False)]
        elif operador == "endswith":
            df = df[df[coluna].astype(str).str.endswith(str(valor), na=False)]
        elif operador == "isnull":
            df = df[df[coluna].isnull()]
        elif operador == "notnull":
            df = df[df[coluna].notnull()]
        else:
            return json.dumps({"erro": f"Operador '{operador}' nao suportado."})

    _CACHE_FILTRADO[caminho] = df.to_dict(orient="records")

    return json.dumps(
        {
            "total_original": total_original,
            "total_filtrado": len(df),
            "colunas": list(df.columns),
            "preview_3_linhas": df.head(3).to_dict(orient="records"),
        },
        ensure_ascii=False,
        default=str,
    )


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

    if usar_filtrado and caminho in _CACHE_FILTRADO:
        registros = _CACHE_FILTRADO[caminho]
        origem = "filtrado"
    elif caminho in _CACHE:
        registros = _CACHE[caminho]
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


if __name__ == "__main__":
    mcp.run()