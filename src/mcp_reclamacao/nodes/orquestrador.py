import json

from ..core.registry import create_client, get_prompt_for_agent, get_provider_and_model
from ..state import AgentState


def orquestrador_node(state: AgentState):
    """No 1: analisa o pedido e decide o plano de execucao."""
    provider, model = get_provider_and_model("orquestrador")
    client = create_client(provider)
    sistema_instrucoes = get_prompt_for_agent("orquestrador")

    sistema_instrucoes += """
    DIFERENCIACAO DE INTENCAO:
    - Se o usuario estiver apenas saudando ou conversando, responda de forma amigavel e use etapas ["conversa"].
    - Se houver intencao de analisar dados, siga o formato JSON do plano.
    """

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sistema_instrucoes},
            {"role": "user", "content": state["user_request"]},
        ],
        response_format={"type": "json_object"},
    )

    plano_json = json.loads(response.choices[0].message.content)
    if "conversa" in plano_json.get("etapas", []):
        return {"plan": [], "next_node": "END", "messages": [response.choices[0].message.content]}

    return {
        "plan": plano_json.get("etapas", []),
        "next_node": "data_agent",
        "messages": ["Orquestrador definiu o plano: " + str(plano_json)],
    }
