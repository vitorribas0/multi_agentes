import json
import os
from groq import Groq
from .state import AgentState
from ..server import carregar_arquivo, ocr_extrair_texto

def data_agent_node(state: AgentState):
    """
    Nó 2: Agente de Dados. 
    Responsável por carregar arquivos (CSV/Excel) ou extrair texto via OCR (Imagens/PDF).
    """
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # Carrega as instruções do especialista em dados
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "data_agent_reclamacao.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        sistema_instrucoes = f.read()

    # Se o arquivo já foi passado no estado ou pedido
    caminho = state.get("file_path")
    
    if not caminho:
        return {"messages": ["Erro: Caminho do arquivo não fornecido para o Data Agent."]}
        
    extensao = os.path.splitext(caminho)[1].lower()
    
    if extensao in ['.png', '.jpg', '.jpeg', '.pdf']:
        print(f"  [Agente de Dados] Executando OCR: {caminho}")
        resultado_raw = ocr_extrair_texto(caminho)
        tipo_processamento = "OCR (Imagem/PDF Scan)"
    else:
        print(f"  [Agente de Dados] Carregando Tabela: {caminho}")
        resultado_raw = carregar_arquivo(caminho)
        tipo_processamento = "Tabela (CSV/Excel)"

    res_dados = json.loads(resultado_raw)
    
    if "erro" in res_dados:
        return {"messages": [f"Erro no {tipo_processamento}: {res_dados['erro']}"]}

    # Decide o que enviar para a análise da IA baseado no tipo de processamento
    conteudo_para_ia = res_dados.get('texto_extraido') if "OCR" in tipo_processamento else res_dados.get('preview_3_linhas')

    # Usa a IA para confirmar se os dados carregados são o que o orquestrador pediu
    res_ia = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": sistema_instrucoes},
            {"role": "user", "content": f"Tipo: {tipo_processamento}. Conteúdo: {conteudo_para_ia}. Pedido original: {state['user_request']}"}
        ]
    )
        
    return {
        "messages": [
            f"Processamento concluído via {tipo_processamento}.",
            f"Análise do Especialista: {res_ia.choices[0].message.content}"
        ]
    }
