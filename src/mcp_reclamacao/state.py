from typing import TypedDict, Annotated, List, Optional
import operator

class AgentState(TypedDict):
    # Pedido original do usuário
    user_request: str
    
    # Plano de execução (etapas)
    plan: List[str]
    
    # SQL do Athena (se necessário)
    sql_query: Optional[str]
    
    # Caminho do arquivo carregado (CSV/Excel) no cache
    file_path: Optional[str]
    
    # Conteúdo da normativa (texto limpo)
    normativa_text: Optional[str]
    
    # Prompt de sistema construído para avaliação
    compliance_system_prompt: Optional[str]
    
    # Resultado parcial/final da análise
    analysis_results: Optional[str]
    
    # Controle de fluxo
    next_node: str
    
    # Histórico de mensagens/logs (opcional)
    messages: Annotated[List[str], operator.add]
