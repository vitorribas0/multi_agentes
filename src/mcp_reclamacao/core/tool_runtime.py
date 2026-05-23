from .. import server


TOOL_MAP = {
    "carregar_arquivo": server.carregar_arquivo,
    "filtrar_registros": server.filtrar_registros,
    "exportar_dataframe": server.exportar_dataframe,
    "normalizar_nlp": server.normalizar_nlp,
    "lematizar_nlp": server.lematizar_nlp,
    "filtrar_por_palavras": server.filtrar_por_palavras,
    "analisar_serie_temporal": server.analisar_serie_temporal,
    "agrupar_registros": server.agrupar_registros,
    "ocr_extrair_texto": server.ocr_extrair_texto,
    "listar_contexto_sessao": server.listar_contexto_sessao,
}
