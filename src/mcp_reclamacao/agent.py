"""
Orquestrador de auditoria de reclamações.

Responsabilidades:
- Carregar o system prompt a partir de arquivo
- Registrar as tools do server MCP
- Executar o loop de raciocínio (LLM + tools) até a conclusão
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

from server import carregar_arquivo, filtrar_registros, exportar_dataframe, get_registros_cache

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parents[2] / ".env")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_MODEL = "llama-3.3-70b-versatile"
_MAX_ITERATIONS = 20

# ---------------------------------------------------------------------------
# Definição das tools (function calling)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "carregar_arquivo",
            "description": (
                "Carrega reclamações de um arquivo CSV ou Excel. "
                "Retorna preview, lista de colunas e total de registros. "
                "Suporta .csv, .xlsx e .xls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {
                        "type": "string",
                        "description": "Caminho absoluto ou relativo do arquivo CSV/Excel",
                    },
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
                "Filtra o dataframe em cache com base em condições. "
                "filtros_json é uma lista JSON de objetos com 'coluna', 'operador' e 'valor'. "
                "Operadores: ==, !=, >, <, >=, <=, contains, startswith, endswith, isnull, notnull. "
                "Exemplo: [{\"coluna\": \"status\", \"operador\": \"==\", \"valor\": \"aberto\"}]"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {
                        "type": "string",
                        "description": "Caminho do arquivo já carregado com carregar_arquivo",
                    },
                    "filtros_json": {
                        "type": "array",
                        "description": "Lista de filtros com coluna, operador e valor",
                        "items": {
                            "type": "object",
                            "properties": {
                                "coluna": {"type": "string"},
                                "operador": {"type": "string"},
                                "valor": {},
                            },
                            "required": ["coluna", "operador"],
                        },
                    },
                },
                "required": ["caminho", "filtros_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exportar_dataframe",
            "description": (
                "Exporta o dataframe para CSV. "
                "Por padrão exporta o filtrado; passe usar_filtrado=false para exportar o completo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caminho": {
                        "type": "string",
                        "description": "Caminho do arquivo original (chave do cache)",
                    },
                    "caminho_saida": {
                        "type": "string",
                        "description": "Caminho do arquivo CSV de saída",
                    },
                    "usar_filtrado": {
                        "type": "boolean",
                        "description": "true para exportar o filtrado, false para o completo",
                    },
                },
                "required": ["caminho", "caminho_saida"],
            },
        },
    },
]

_TOOL_MAP: dict = {
    "carregar_arquivo": carregar_arquivo,
    "filtrar_registros": filtrar_registros,
    "exportar_dataframe": exportar_dataframe,
}

_TAMANHO_LOTE = 10  # registros por chamada ao LLM

# ---------------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------------


class AgenteAuditoria:
    """Orquestrador que usa Groq + tools MCP para analisar reclamações."""

    def __init__(self, verbose: bool = True) -> None:
        self._client = Groq(api_key=os.environ["GROQ_API_KEY"])
        self._system_prompt = self._carregar_prompt("auditoria.md")
        self._verbose = verbose

    # ------------------------------------------------------------------
    # Métodos privados
    # ------------------------------------------------------------------

    def _carregar_prompt(self, nome_arquivo: str) -> str:
        caminho = _PROMPTS_DIR / nome_arquivo
        return caminho.read_text(encoding="utf-8")

    def _executar_tool(self, nome: str, args: dict) -> str:
        if nome == "filtrar_registros" and isinstance(args.get("filtros_json"), list):
            args["filtros_json"] = json.dumps(args["filtros_json"], ensure_ascii=False)
        resultado = _TOOL_MAP[nome](**args)
        if self._verbose:
            print(f"  [tool: {nome}] → {str(resultado)[:150]}...")
        return str(resultado)

    def _log(self, mensagem: str) -> None:
        if self._verbose:
            print(mensagem)

    # ------------------------------------------------------------------
    # Métodos de processamento em lote
    # ------------------------------------------------------------------

    def _analisar_lote(self, lote: list[dict], colunas_validacao: list[str]) -> list[dict]:
        """Envia um lote de registros ao LLM para preenchimento das colunas de validação."""
        prompt_lote = (
            f"Analise cada reclamação abaixo e preencha as colunas de validação solicitadas.\n"
            f"Colunas a preencher: {', '.join(colunas_validacao)}\n\n"
            f"Retorne APENAS um JSON válido: lista de objetos com TODOS os campos originais "
            f"mais as colunas de validação preenchidas. Sem texto adicional.\n\n"
            f"Registros:\n{json.dumps(lote, ensure_ascii=False, default=str)}"
        )

        resposta = self._client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt_lote},
            ],
        )

        conteudo = resposta.choices[0].message.content.strip()
        # Remove markdown code block se presente
        if conteudo.startswith("```"):
            conteudo = conteudo.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        return json.loads(conteudo)

    def _processar_em_lotes(
        self, caminho: str, colunas_validacao: list[str], caminho_saida: str
    ) -> str:
        """Itera pelos registros em lotes e acumula os resultados analisados."""
        import pandas as pd

        registros = get_registros_cache(caminho)
        total = len(registros)
        resultados: list[dict] = []

        self._log(f"\n  Processando {total} registros em lotes de {_TAMANHO_LOTE}...")

        for inicio in range(0, total, _TAMANHO_LOTE):
            lote = registros[inicio : inicio + _TAMANHO_LOTE]
            fim = min(inicio + _TAMANHO_LOTE, total)
            self._log(f"  Lote {inicio + 1}–{fim} de {total}")
            analisados = self._analisar_lote(lote, colunas_validacao)
            resultados.extend(analisados)

        # Salva resultado final
        Path(caminho_saida).parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(resultados)
        df.to_csv(caminho_saida, index=False, encoding="utf-8-sig")

        return (
            f"Análise concluída. {total} registros processados.\n"
            f"Arquivo salvo em: {caminho_saida}\n\n"
            + "\n".join(
                f"- {col}: {df[col].value_counts().to_dict()}"
                for col in colunas_validacao
                if col in df.columns
            )
        )

    # ------------------------------------------------------------------
    # Interface pública
    # ------------------------------------------------------------------

    def executar(
        self,
        instrucao: str,
        caminho_arquivo: str | None = None,
        colunas_validacao: list[str] | None = None,
        caminho_saida: str | None = None,
    ) -> str:
        """
        Executa a análise de reclamações.

        Fase 1: LLM chama `carregar_arquivo` para entender o schema (3 linhas).
        Fase 2: Agente itera os registros completos em lotes sem expor ao LLM.

        Args:
            instrucao: Instrução do auditor em linguagem natural.
            caminho_arquivo: Caminho do arquivo de reclamações.
            colunas_validacao: Colunas que o auditor quer criar.
            caminho_saida: Caminho do CSV de saída.
        """
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": instrucao},
        ]

        self._log(f"\n{'='*60}")
        self._log(f"INSTRUÇÃO:\n{instrucao.strip()}")
        self._log(f"{'='*60}")

        # Fase 1: LLM inspeciona schema via tool (recebe só 3 linhas)
        for iteracao in range(1, _MAX_ITERATIONS + 1):
            self._log(f"\n[Fase 1 — Iteração {iteracao}] Consultando o modelo...")

            resposta = self._client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )

            msg = resposta.choices[0].message

            # LLM decidiu não chamar mais tools → schema entendido
            if not msg.tool_calls:
                self._log("\n✓ Schema entendido pelo agente.")
                break

            messages.append(msg)
            for tc in msg.tool_calls:
                nome = tc.function.name
                args = json.loads(tc.function.arguments)
                resultado = self._executar_tool(nome, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": resultado,
                    }
                )

                # Após carregar_arquivo, dispara Fase 2 se os parâmetros foram fornecidos
                if nome == "carregar_arquivo" and caminho_arquivo and colunas_validacao and caminho_saida:
                    self._log("\n[Fase 2] Processando registros em lotes (fora do contexto do LLM)...")
                    return self._processar_em_lotes(caminho_arquivo, colunas_validacao, caminho_saida)

        return "Limite de iterações atingido sem conclusão."
