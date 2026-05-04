import os
from langgraph.graph import StateGraph, END
from .state import AgentState
from .nodes.orquestrador import orquestrador_node
from .nodes.data_agent import data_agent_node
from .nodes.nlp_agent import nlp_agent_node

def create_audit_graph():
    # Inicializa o Grafo de Estados
    workflow = StateGraph(AgentState)

    # Adiciona os Nós
    workflow.add_node("orquestrador", orquestrador_node)
    workflow.add_node("data_agent", data_agent_node)
    workflow.add_node("nlp_agent", nlp_agent_node)

    # Define as Bordas (Fluxo)
    workflow.set_entry_point("orquestrador")
    
    # Roteamento dinâmico baseado no next_node definido pelo Orquestrador
    workflow.add_conditional_edges(
        "orquestrador",
        lambda state: state["next_node"],
        {
            "data_agent": "data_agent",
            "END": END
        }
    )
    
    workflow.add_edge("data_agent", "nlp_agent")
    workflow.add_edge("nlp_agent", END)

    return workflow.compile()

# Exemplo de uso (mental)
# graph = create_audit_graph()
# graph.invoke({"user_request": "Analise reclamações de jan/2025", "messages": []})
