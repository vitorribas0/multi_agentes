"""Agente de auditoria orientado a configuracao (modelos, prompts e tools)."""

import json
from pathlib import Path

from dotenv import load_dotenv

from .core.registry import build_tool_schemas, create_client, get_agent, get_prompt_for_agent, get_provider_and_model
from .core.tool_runtime import TOOL_MAP
from .server import get_registros_cache

load_dotenv(Path(__file__).parents[2] / ".env")

_MAX_ITERATIONS = 20
_TAMANHO_LOTE = 10


class AgenteAuditoria:
    def __init__(self, verbose: bool = True) -> None:
        provider, model = get_provider_and_model("auditoria_lotes")
        self._model = model
        self._client = create_client(provider)
        self._system_prompt = get_prompt_for_agent("auditoria_lotes")
        self._tools = build_tool_schemas(get_agent("auditoria_lotes").get("tools", []))
        self._verbose = verbose

    def _log(self, mensagem: str) -> None:
        if self._verbose:
            print(mensagem)

    def _executar_tool(self, nome: str, args: dict) -> str:
        if nome == "filtrar_registros" and isinstance(args.get("filtros_json"), list):
            args["filtros_json"] = json.dumps(args["filtros_json"], ensure_ascii=False)
        resultado = TOOL_MAP[nome](**args)
        return str(resultado)

    def _analisar_lote(self, lote: list[dict], colunas_validacao: list[str]) -> list[dict]:
        prompt_lote = (
            "Analise cada reclamacao e preencha as colunas de validacao.\n"
            f"Colunas: {', '.join(colunas_validacao)}\n"
            "Retorne apenas JSON valido com todos os campos.\n\n"
            f"Registros:\n{json.dumps(lote, ensure_ascii=False, default=str)}"
        )
        resposta = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt_lote},
            ],
        )
        conteudo = resposta.choices[0].message.content.strip()
        if conteudo.startswith("```"):
            conteudo = conteudo.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(conteudo)

    def _processar_em_lotes(self, caminho: str, colunas_validacao: list[str], caminho_saida: str) -> str:
        import pandas as pd

        registros = get_registros_cache(caminho)
        resultados: list[dict] = []
        total = len(registros)
        for inicio in range(0, total, _TAMANHO_LOTE):
            lote = registros[inicio : inicio + _TAMANHO_LOTE]
            resultados.extend(self._analisar_lote(lote, colunas_validacao))

        Path(caminho_saida).parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(resultados)
        df.to_csv(caminho_saida, index=False, encoding="utf-8-sig")
        return f"Analise concluida. {total} registros processados. Arquivo salvo em: {caminho_saida}"

    def executar(
        self,
        instrucao: str,
        caminho_arquivo: str | None = None,
        colunas_validacao: list[str] | None = None,
        caminho_saida: str | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": instrucao},
        ]

        for _ in range(1, _MAX_ITERATIONS + 1):
            resposta = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=self._tools,
                tool_choice="auto",
            )
            msg = resposta.choices[0].message
            if not msg.tool_calls:
                break

            messages.append(msg)
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                resultado = self._executar_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": resultado})

                if tc.function.name == "carregar_arquivo" and caminho_arquivo and colunas_validacao and caminho_saida:
                    return self._processar_em_lotes(caminho_arquivo, colunas_validacao, caminho_saida)
        return "Fluxo concluido."
