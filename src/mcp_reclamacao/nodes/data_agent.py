import json
import os
from .state import AgentState
from ..server import carregar_arquivo

def data_agent_node(state: AgentState):
    """
    Nó 2: Agente de Dados. 
    Responsável por carregar arquivos (CSV/Excel) ou futuramente Athena.
    """
    # Se o arquivo já foi passado no estado ou pedido
    caminho = state.get("file_path")
    
    if not caminho:
        # Se não tem caminho, vamos assumir um padrão ou erro por enquanto
        return {"messages": ["Erro: Caminho do arquivo não fornecido."]}
        
    print(f"  [Agente de Dados] Carregando: {caminho}")
    resultado = carregar_arquivo(caminho)
    res_dados = json.loads(resultado)
    
    if "erro" in res_dados:
        return {"messages": [f"Erro ao carregar dados: {res_dados['erro']}"]}
        
    return {
        "messages": [f"Dados carregados com sucesso. Total de registros: {res_dados['total_registros']}"]
    }
