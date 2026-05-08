import json
import os
from groq import Groq
from .state import AgentState
from ..server import normalizar_nlp, lematizar_nlp, filtrar_por_palavras

def nlp_agent_node(state: AgentState):
    """
    Nó 3: Agente NLP / ETL.
    Limpa, lematiza o texto e reduz o volume de dados com base na especialidade.
    """
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # Carrega as instruções do especialista em NLP
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "nlp_agent_reclamacao.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        sistema_instrucoes = f.read()

    caminho = state.get("file_path")
    if not caminho: 
        return {"messages": ["Erro: Caminho do arquivo não encontrado para o NLP Agent."]}
    
    coluna_texto = "texto_reclamacao" 

    print(f"  [Agente NLP] Normalizando e Lematizando: {coluna_texto}")
    normalizar_nlp(caminho, coluna_texto)
    lematizar_nlp(caminho, coluna_texto)
    
    # Usa a IA para decidir quais palavras-chave de filtro são mais relevantes baseadas no prompt e pedido
    res_ia = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": sistema_instrucoes},
            {"role": "user", "content": f"Pedido do usuário: {state['user_request']}. Quais termos (máximo 5) devo usar para filtrar a relevância?"}
        ]
    )
    
    # Extração simples dos termos sugeridos pela IA (assumindo que o prompt pede termos separados por vírgula)
    sugestao_filtro = res_ia.choices[0].message.content
    filtros = [f.strip() for f in sugestao_filtro.split(",")][:5]

    print(f"  [Agente NLP] Filtrando com termos sugeridos: {filtros}")
    resultado_filtro = filtrar_por_palavras(caminho, f"{coluna_texto}_lemma", filtros)
    res = json.loads(resultado_filtro)
    
    return {
        "messages": [
            f"NLP Agent aplicou inteligência linguística.",
            f"Filtros IA: {sugestao_filtro}",
            f"Redução de volume: {res['total_original']} -> {res['total_filtrado']} registros."
        ]
    }
