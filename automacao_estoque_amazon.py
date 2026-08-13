"""
Automação diária: move pedidos de venda Amazon FBA de 'Em aberto' para
'Atendido Amazon' assim que a nota fiscal já estiver emitida, disparando
a baixa de estoque configurada no Gerenciador de Transições do Bling.

Variáveis de ambiente necessárias (configuradas como Secrets no GitHub):
  BLING_CLIENT_ID
  BLING_CLIENT_SECRET
  BLING_REFRESH_TOKEN

Variáveis opcionais:
  SITUACAO_DESTINO_NOME   (default: "Atendido Amazon")
  SITUACAO_ORIGEM_NOME    (default: "Em aberto")
  MODO_TESTE              ("true" = só mostra o que faria, não altera nada)
"""

import base64
import os
import sys
import json
import urllib.request
import urllib.parse

BLING_BASE = "https://www.bling.com.br/Api/v3"

CLIENT_ID = os.environ["BLING_CLIENT_ID"]
CLIENT_SECRET = os.environ["BLING_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["BLING_REFRESH_TOKEN"]

SITUACAO_DESTINO_NOME = os.environ.get("SITUACAO_DESTINO_NOME", "Atendido Amazon")
SITUACAO_ORIGEM_NOME = os.environ.get("SITUACAO_ORIGEM_NOME", "Em aberto")
MODO_TESTE = os.environ.get("MODO_TESTE", "true").lower() == "true"


def _request(method, path, token=None, body=None, params=None):
    url = f"{BLING_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"ERRO HTTP {e.code} em {method} {path}: {err_body}", file=sys.stderr)
        raise


def refresh_access_token():
    """Troca o refresh_token por um access_token novo (e um refresh_token novo)."""
    pair = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    auth_b64 = base64.b64encode(pair).decode("utf-8")
    url = f"{BLING_BASE}/oauth/token"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", f"Basic {auth_b64}")
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data["access_token"], data["refresh_token"]


def listar_situacoes(token):
    """Lista as situações de pedidos de venda disponíveis, pra achar os IDs pelo nome."""
    data = _request("GET", "/situacoes/modulos", token=token)
    modulos = data.get("data", [])
    id_modulo_pedido_venda = None
    for m in modulos:
        if "venda" in m.get("descricao", "").lower():
            id_modulo_pedido_venda = m.get("id")
            break
    if id_modulo_pedido_venda is None:
        raise RuntimeError("Não encontrei o módulo de Pedidos de Venda em /situacoes/modulos")

    data = _request("GET", f"/situacoes/modulos/{id_modulo_pedido_venda}", token=token)
    situacoes = data.get("data", [])
    return {s["nome"]: s["id"] for s in situacoes}


def listar_pedidos_em_aberto(token, id_situacao_origem):
    """Busca pedidos de venda na situação de origem (ex: Em aberto), paginando."""
    pedidos = []
    pagina = 1
    while True:
        data = _request(
            "GET", "/pedidos/vendas", token=token,
            params={"situacao": id_situacao_origem, "pagina": pagina, "limite": 100},
        )
        lote = data.get("data", [])
        if not lote:
            break
        pedidos.extend(lote)
        pagina += 1
        if len(lote) < 100:
            break
    return pedidos


def tem_nota_fiscal(pedido):
    """Confere se o pedido já tem NF vinculada (equivalente ao 'N' vermelho na tela)."""
    nf = pedido.get("notaFiscal")
    return bool(nf and nf.get("id"))


def mudar_situacao(token, id_pedido, id_situacao_destino):
    return _request(
        "PATCH", f"/pedidos/vendas/{id_pedido}/situacoes/{id_situacao_destino}",
        token=token,
    )


def main():
    print("Renovando access token...")
    access_token, novo_refresh_token = refresh_access_token()

    if novo_refresh_token != REFRESH_TOKEN:
        # Grava o novo refresh_token num arquivo que o workflow do GitHub Actions
        # vai ler e salvar de volta como secret.
        with open("novo_refresh_token.txt", "w") as f:
            f.write(novo_refresh_token)
        print("Novo refresh_token gerado (será salvo pelo workflow).")

    print("Buscando situações cadastradas...")
    situacoes = listar_situacoes(access_token)

    if SITUACAO_ORIGEM_NOME not in situacoes:
        print(f"Situação de origem '{SITUACAO_ORIGEM_NOME}' não encontrada. "
              f"Disponíveis: {list(situacoes.keys())}", file=sys.stderr)
        sys.exit(1)
    if SITUACAO_DESTINO_NOME not in situacoes:
        print(f"Situação de destino '{SITUACAO_DESTINO_NOME}' não encontrada. "
              f"Disponíveis: {list(situacoes.keys())}", file=sys.stderr)
        sys.exit(1)

    id_origem = situacoes[SITUACAO_ORIGEM_NOME]
    id_destino = situacoes[SITUACAO_DESTINO_NOME]

    print(f"Buscando pedidos em '{SITUACAO_ORIGEM_NOME}'...")
    pedidos = listar_pedidos_em_aberto(access_token, id_origem)
    print(f"{len(pedidos)} pedido(s) encontrado(s) em '{SITUACAO_ORIGEM_NOME}'.")

    processados = 0
    ignorados_sem_nf = 0
    erros = []

    for pedido in pedidos:
        numero = pedido.get("numero")
        id_pedido = pedido.get("id")

        if not tem_nota_fiscal(pedido):
            ignorados_sem_nf += 1
            continue

        if MODO_TESTE:
            print(f"[MODO TESTE] Pedido {numero} tem NF -> mudaria para "
                  f"'{SITUACAO_DESTINO_NOME}' (dispara baixa de estoque)")
            processados += 1
            continue

        try:
            mudar_situacao(access_token, id_pedido, id_destino)
            print(f"Pedido {numero}: situação alterada para "
                  f"'{SITUACAO_DESTINO_NOME}' com sucesso.")
            processados += 1
        except Exception as e:
            erros.append((numero, str(e)))

    print("\n--- Resumo ---")
    print(f"Processados: {processados}")
    print(f"Ignorados (sem NF ainda): {ignorados_sem_nf}")
    print(f"Erros: {len(erros)}")
    for numero, msg in erros:
        print(f"  Pedido {numero}: {msg}")

    if erros:
        sys.exit(1)


if __name__ == "__main__":
    main()
