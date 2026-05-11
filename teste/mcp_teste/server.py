from fastmcp import FastMCP
import json

mcp = FastMCP(
    "mcp_teste",
    instructions="Servidor mcp de testes para testar o cadastro de skills e o fluxo de orquestração."
)

@mcp.tool(
        description=("Skill de teste com soma de 4 valores para validar o fluxo de orquestração e cadastro de skills.")
)
def skill_teste(a: int, b: int, c: int, d: int) -> str:
    resultado = a + b + c + d
    return f"O resultado da soma é: {resultado}"