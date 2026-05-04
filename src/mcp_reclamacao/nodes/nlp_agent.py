import json
from .state import AgentState
from ..server import normalizar_nlp, lematizar_nlp, filtrar_por_palavras

def nlp_agent_node(state: AgentState):
    """
    Nó 3: Agente NLP / ETL.
    Limpa, lematiza o texto e reduz o volume de dados.
    """
    caminho = state.get("file_path")
    if not caminho: return {"messages": ["NLP: Caminho não encontrado"]}
    
    coluna_texto = "texto_reclamacao" # Pode vir do state futuramente

    print(f"  [Agente NLP] Normalizando coluna: {coluna_texto}")
    normalizar_nlp(caminho, coluna_texto)
    
    print(f"  [Agente NLP] Lematizando coluna: {coluna_texto}_limpo")
    lematizar_nlp(caminho, coluna_texto)
    
    print("  [Agente NLP] Filtrando relevância...")
    # O filtro agora pode olhar para a coluna lematizada para ser mais preciso
    resultado_filtro = filtrar_por_palavras(caminho, f"{coluna_texto}_lemma", ["negativa", "prazo", "indevida"])
    res = json.loads(resultado_filtro)
    
    return {
        "messages": [f"NLP concluído (Norm + Lemma). Redução: {res['total_original']} -> {res['total_filtrado']} registros."]
    }
