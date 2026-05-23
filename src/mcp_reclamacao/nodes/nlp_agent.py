import json

from ..core.registry import create_client, get_prompt_for_agent, get_provider_and_model
from ..server import filtrar_por_palavras, lematizar_nlp, normalizar_nlp
from ..state import AgentState


def nlp_agent_node(state: AgentState):
    """No 3: normaliza, lematiza e filtra texto."""
    provider, model = get_provider_and_model("nlp_agent")
    client = create_client(provider)
    sistema_instrucoes = get_prompt_for_agent("nlp_agent")

    caminho = state.get("file_path")
    if not caminho:
        return {"messages": ["Erro: caminho do arquivo nao encontrado para o NLP Agent."]}

    coluna_texto = "texto_reclamacao"
    normalizar_nlp(caminho, coluna_texto)
    lematizar_nlp(caminho, coluna_texto)

    res_ia = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sistema_instrucoes},
            {
                "role": "user",
                "content": (
                    f"Pedido do usuario: {state['user_request']}. "
                    "Quais termos (maximo 5) devo usar para filtrar relevancia?"
                ),
            },
        ],
    )
    sugestao_filtro = res_ia.choices[0].message.content
    filtros = [f.strip() for f in sugestao_filtro.split(",")][:5]

    resultado_filtro = filtrar_por_palavras(caminho, f"{coluna_texto}_lemma", filtros)
    res = json.loads(resultado_filtro)
    return {
        "messages": [
            "NLP Agent aplicou inteligencia linguistica.",
            f"Filtros IA: {sugestao_filtro}",
            f"Reducao de volume: {res.get('total_original', 0)} -> {res.get('total_filtrado', 0)} registros.",
        ]
    }
