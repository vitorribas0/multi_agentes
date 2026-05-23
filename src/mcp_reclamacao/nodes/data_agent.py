import json
import os

from ..core.registry import create_client, get_prompt_for_agent, get_provider_and_model
from ..server import carregar_arquivo, ocr_extrair_texto
from ..state import AgentState


def data_agent_node(state: AgentState):
    """No 2: processa arquivo tabular ou OCR."""
    provider, model = get_provider_and_model("data_agent")
    client = create_client(provider)
    sistema_instrucoes = get_prompt_for_agent("data_agent")

    caminho = state.get("file_path")
    if not caminho:
        return {"messages": ["Erro: caminho do arquivo nao fornecido para o Data Agent."]}

    extensao = os.path.splitext(caminho)[1].lower()
    if extensao in [".png", ".jpg", ".jpeg", ".pdf"]:
        resultado_raw = ocr_extrair_texto(caminho)
        tipo_processamento = "OCR"
    else:
        resultado_raw = carregar_arquivo(caminho)
        tipo_processamento = "Tabela"

    res_dados = json.loads(resultado_raw)
    if "erro" in res_dados:
        return {"messages": [f"Erro no {tipo_processamento}: {res_dados['erro']}"]}

    conteudo_para_ia = (
        res_dados.get("texto_extraido")
        if tipo_processamento == "OCR"
        else res_dados.get("preview_3_linhas")
    )
    res_ia = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sistema_instrucoes},
            {
                "role": "user",
                "content": (
                    f"Tipo: {tipo_processamento}. Conteudo: {conteudo_para_ia}. "
                    f"Pedido original: {state['user_request']}"
                ),
            },
        ],
    )

    return {
        "messages": [
            f"Processamento concluido via {tipo_processamento}.",
            f"Analise do especialista: {res_ia.choices[0].message.content}",
        ]
    }
