import json
import os
from groq import Groq
from .state import AgentState

def orquestrador_node(state: AgentState):
    """Nó 1: Analisa o pedido e decide o plano de execução ou apenas conversa."""
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    
    # Busca as instruções do arquivo de prompt
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "orquestrador_reclamacao.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        sistema_instrucoes = f.read()

    # Adicionamos uma instrução extra para lidar com conversa casual
    sistema_instrucoes += """
    DIFERENCIAÇÃO DE INTENÇÃO:
    - Se o usuário estiver apenas saudando (oi, olá, etc) ou fazendo perguntas gerais sem pedir análise de dados, responda de forma amigável e defina "etapas": ["conversa"].
    - Se houver intenção de analisar dados, siga o formato JSON do plano.
    """

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": sistema_instrucoes},
            {"role": "user", "content": state['user_request']}
        ],
        response_format={"type": "json_object"}
    )
    
    plano_json = json.loads(response.choices[0].message.content)
    
    # Decide o próximo nó
    if "conversa" in plano_json.get("etapas", []):
        return {
            "plan": [],
            "next_node": "END",
            "messages": [response.choices[0].message.content]
        }

    return {
        "plan": plano_json.get("etapas", []),
        "next_node": "data_agent",
        "messages": ["Orquestrador definiu o plano: " + str(plano_json)]
    }
