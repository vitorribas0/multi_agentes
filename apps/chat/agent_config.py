# ─────────────────────────────────────────────────────────────────────────────
# Configuração central dos agentes e tools do sistema MCP Reclamações.
# Edite apenas este arquivo para mudar modelos, system prompts ou tools.
# ─────────────────────────────────────────────────────────────────────────────

# Modelo padrão usado pelo agente principal
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_PROVIDER = "Groq"

# Modelos disponíveis para seleção na UI de configurações
MODEL_OPTIONS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

# System prompt do agente
SYSTEM_PROMPT = (
    "Você é um assistente especializado em análise de anomalias em reclamações (MCP).\n"
    "\n"
    "REGRAS OBRIGATÓRIAS:\n"
    "1. NUNCA simule ou invente chamadas de ferramentas. NUNCA escreva texto como "
    "`carregar_arquivo(...)`, `filtrar_registros(...)` ou qualquer outro nome de função. "
    "Ferramentas só podem ser chamadas pelo mecanismo real de function calling.\n"
    "2. Use EXCLUSIVAMENTE as ferramentas disponíveis: carregar_arquivo, filtrar_registros, exportar_dataframe, normalizar_nlp, lematizar_nlp, filtrar_por_palavras, analisar_serie_temporal. "
    "NÃO invente outras funções como filtrar_data, salvar_arquivo, exportar_csv, etc.\n"
    "3. Para filtrar dados, use SEMPRE filtrar_registros com a lista de filtros correta.\n"
    "4. Para exportar dados em CSV, use SEMPRE exportar_dataframe.\n"
    "5. Se o usuário pedir para 'extrair', 'exportar', 'salvar' ou 'baixar' dados em CSV, "
    "chame exportar_dataframe imediatamente — não peça confirmação.\n"
    "6. Lembre-se do contexto: se um arquivo já foi carregado nessa conversa, use o mesmo caminho.\n"
    "7. Para pipeline de NLP, prefira: normalizar_nlp -> lematizar_nlp -> filtrar_por_palavras.\n"
    "8. Se o usuário pedir gráfico de série temporal, use analisar_serie_temporal e apresente o gráfico inline na resposta; não exporte/baixe arquivo a menos que solicitado explicitamente.\n"
    "\n"
    "Seja conciso, claro e amigável. Responda sempre em português."
)

# Definição das tools no formato OpenAI/Groq function calling
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "carregar_arquivo",
            "description": (
                "Carrega um arquivo CSV ou Excel. "
                "Armazena todos os registros internamente para processamento em lote. "
                "Retorna ao modelo apenas o schema (colunas) e 3 linhas de preview. "
                "Suporta .csv, .xlsx e .xls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho absoluto do arquivo CSV ou Excel"},
                },
                "required": ["caminho"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filtrar_registros",
            "description": (
                "Filtra o dataframe já carregado em memória com base em condições informadas. "
                "Recebe uma lista JSON de filtros, cada um com 'coluna', 'operador' e 'valor'. "
                "Operadores suportados: ==, !=, >, <, >=, <=, contains, startswith, endswith, isnull, notnull. "
                "O resultado filtrado fica em cache e pode ser exportado com exportar_dataframe."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do arquivo já carregado"},
                    "filtros_json": {
                        "type": "array",
                        "description": "Lista de filtros a aplicar",
                        "items": {
                            "type": "object",
                            "properties": {
                                "coluna":   {"type": "string", "description": "Nome da coluna"},
                                "operador": {"type": "string", "description": "Operador: ==, !=, >, <, >=, <=, contains, startswith, endswith, isnull, notnull"},
                                "valor":    {"description": "Valor para comparar (omitir em isnull/notnull)"}
                            },
                            "required": ["coluna", "operador"]
                        }
                    },
                },
                "required": ["caminho", "filtros_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "normalizar_nlp",
            "description": (
                "Normaliza texto de uma coluna do dataframe em cache: converte para minúsculas, "
                "remove acentos e pontuação. Cria uma nova coluna com sufixo '_limpo'. "
                "Use antes de filtrar por palavras para melhorar a busca."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do arquivo já carregado"},
                    "coluna": {"type": "string", "description": "Nome da coluna de texto a normalizar"},
                },
                "required": ["caminho", "coluna"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lematizar_nlp",
            "description": (
                "Lematiza uma coluna do dataframe (ex: 'reclamacoes' -> 'reclamacao'). "
                "Utiliza o modelo spaCy pt_core_news_sm. "
                "Use após normalizar_nlp para melhorar a busca semântica."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do arquivo já carregado"},
                    "coluna": {"type": "string", "description": "Nome da coluna de texto para lematização"},
                },
                "required": ["caminho", "coluna"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filtrar_por_palavras",
            "description": (
                "Filtra registros que mencionam uma ou mais palavras-chave em uma coluna. "
                "Para melhores resultados, normalize a coluna primeiro com normalizar_nlp e use a coluna '_limpo'. "
                "O resultado fica em cache e pode ser exportado com exportar_dataframe."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do arquivo já carregado"},
                    "coluna": {"type": "string", "description": "Nome da coluna onde buscar as palavras"},
                    "palavras": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de palavras-chave a buscar (case-insensitive)",
                    },
                },
                "required": ["caminho", "coluna", "palavras"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analisar_serie_temporal",
            "description": (
                "Realiza análise de série temporal em uma coluna de data. "
                "Permite agrupar por Dia (D), Mês (MS) ou Ano (YS). "
                "Calcula métricas como contagem (count), soma (sum) ou média (mean). "
                "Retorna os pontos da série para exibição de gráfico inline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do arquivo já carregado"},
                    "coluna_data": {"type": "string", "description": "Nome da coluna de data"},
                    "frequencia": {"type": "string", "description": "Frequência: D (dia), MS (mês), YS (ano)", "default": "MS"},
                    "metrica": {"type": "string", "description": "Métrica: count, sum, mean", "default": "count"},
                    "coluna_valor": {"type": "string", "description": "Coluna numérica para sum/mean (opcional)"},
                    "usar_cache_filtrado": {"type": "boolean", "description": "Se true, usa cache filtrado", "default": False},
                },
                "required": ["caminho", "coluna_data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exportar_dataframe",
            "description": (
                "Exporta o dataframe para um arquivo CSV. "
                "Por padrão exporta o dataframe filtrado (se houver filtro aplicado). "
                "Passe usar_filtrado=false para exportar o dataframe completo original."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {"type": "string", "description": "Caminho do arquivo de origem (já carregado)"},
                    "caminho_saida": {"type": "string", "description": "Caminho do arquivo CSV de saída"},
                    "usar_filtrado": {"type": "boolean", "description": "Se true (padrão), exporta o filtrado; se false, exporta o original completo"},
                },
                "required": ["caminho", "caminho_saida"],
            },
        },
    },
]

# Metadados dos agentes (usado na tela de configurações)
AGENTS = [
    {
        "id": "analista_reclamacoes",
        "name": "Analista de Reclamações",
        "description": (
            "Agente principal de análise. Processa arquivos de reclamações, "
            "identifica anomalias e gera relatórios de auditoria."
        ),
        "model": DEFAULT_MODEL,
        "provider": DEFAULT_PROVIDER,
        "tools": [t["function"]["name"] for t in TOOLS],
        "system_prompt": SYSTEM_PROMPT,
        "status": "ativo",
    },
]
