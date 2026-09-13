#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classificar_por_ia.py — Reordena as candidatas do dia usando um modelo de
linguagem, como camada final sobre o filtro de palavras-chave.

POR QUE EXISTE
  Palavra-chave não entende contexto nem assunto novo. Ela acerta quando o
  vocabulário já está cadastrado e erra calada quando não está: um produto de
  crédito lançado com nome que ninguém previu passa despercebido, e uma matéria
  que cita "Itaú" de passagem parece relevante. O modelo lê a manchete e o
  primeiro parágrafo e julga como um leitor julgaria.

COMO SE ENCAIXA
  As palavras-chave continuam mandando no que é ELEGÍVEL — vetos, penalizações
  e o portão de relevância seguem intactos. A IA só reordena o que sobrou:

      score_final = score_palavras_chave + (nota_ia × PESO_IA)

  Assim a curadoria explícita do perfil (tiers, vetos) continua valendo, e o
  modelo desempata e corrige o que o vocabulário não alcança.

À PROVA DE FALHA
  Sem chave de API, sem rede, resposta malformada, timeout — qualquer problema
  devolve a lista exatamente como veio. O episódio nunca deixa de sair por
  causa desta etapa.

Configuração (variáveis de ambiente):
  ANTHROPIC_API_KEY   — chave da API (se ausente, a etapa é pulada)
  IA_MODELO           — padrão: claude-haiku-4-5-20251001
  IA_PESO             — padrão: 2.0 (quanto cada ponto da nota vale no total)
"""

import os
import json
import urllib.request
import urllib.error

API_URL   = "https://api.anthropic.com/v1/messages"
MODELO    = os.environ.get("IA_MODELO") or "claude-haiku-4-5-20251001"
PESO_IA   = float(os.environ.get("IA_PESO") or 2.0)
TIMEOUT   = 45
MAX_CAND  = 25     # candidatas enviadas por chamada
CHARS_CTX = 320    # trecho de cada matéria enviado junto do título


def _perfil_em_texto(perfil):
    """Resume o perfil JSON em português, para o modelo entender o critério."""
    if not perfil:
        return "Interesse geral em crédito, meios de pagamento e finanças no Brasil."

    linhas = []
    ctx = perfil.get("_meta", {}).get("contexto_profissional", "")
    if ctx:
        linhas.append(f"Contexto profissional: {ctx}.")

    temas = sorted(perfil.get("temas", []), key=lambda t: -t.get("peso", 0))
    linhas.append("\nTemas de interesse, do mais para o menos importante:")
    for t in temas:
        linhas.append(f"  - (peso {t.get('peso')}) {t.get('nome')}: {t.get('descricao','')}")

    ent = perfil.get("entidades_prioritarias", {})
    t1 = ent.get("tier1_empresa", {}).get("lista", [])
    if t1:
        linhas.append(f"\nEmpresa do ouvinte (prioridade máxima): {', '.join(t1[:4])}.")
    t2 = ent.get("tier2_concorrentes_diretos", {}).get("lista", [])
    if t2:
        linhas.append(f"Concorrentes diretos: {', '.join(t2[:10])}.")

    pen = [p.get("descricao", p.get("nome", "")) for p in perfil.get("penalizacoes", [])]
    if pen:
        linhas.append("\nNÃO interessa: " + "; ".join(pen) + ".")

    return "\n".join(linhas)


def _montar_prompt(perfil, candidatas):
    itens = []
    for i, n in enumerate(candidatas):
        corpo = (n.get("conteudo_completo") or n.get("resumo") or "")[:CHARS_CTX]
        itens.append(f"[{i}] {n.get('titulo','')}\n    {corpo}")

    return f"""Você ajuda a montar um podcast diário de notícias para um executivo brasileiro do setor financeiro. Abaixo está o perfil de interesses dele e as notícias candidatas de hoje.

PERFIL DO OUVINTE
{_perfil_em_texto(perfil)}

NOTÍCIAS CANDIDATAS
{chr(10).join(itens)}

Dê a cada notícia uma nota de 0 a 10 de relevância PARA ESTE OUVINTE:
  9-10 = decisão de negócio dele depende disso
  6-8  = acompanha de perto, quer saber hoje
  3-5  = contexto de mercado, bom saber
  0-2  = irrelevante para o trabalho dele

Julgue pelo conteúdo real, não pelo título. Uma matéria que só cita uma empresa conhecida de passagem é irrelevante. Uma matéria sem nenhuma palavra do perfil pode ser muito relevante se tratar do negócio dele.

Responda SOMENTE com um array JSON, sem texto antes ou depois:
[{{"i": 0, "nota": 7, "motivo": "razão em até 8 palavras"}}, ...]"""


def _chamar_api(api_key, prompt):
    corpo = json.dumps({
        "model": MODELO,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL, data=corpo,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        resposta = json.loads(r.read().decode("utf-8"))
    return resposta["content"][0]["text"]


def _extrair_json(texto):
    """Pega o array JSON mesmo se o modelo embrulhar em cerca de código."""
    ini = texto.find("[")
    fim = texto.rfind("]")
    if ini == -1 or fim == -1 or fim <= ini:
        raise ValueError("resposta sem array JSON")
    return json.loads(texto[ini:fim + 1])


def reordenar_por_ia(noticias, perfil=None, api_key=None, peso=PESO_IA):
    """
    Reordena `noticias` combinando o score de palavras-chave com a nota da IA.
    Devolve a lista reordenada. Em QUALQUER falha, devolve a lista original.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("  ℹ️  ANTHROPIC_API_KEY não definida — classificação por IA desativada.")
        return noticias
    if not noticias:
        return noticias

    candidatas = noticias[:MAX_CAND]
    try:
        bruto = _chamar_api(api_key, _montar_prompt(perfil, candidatas))
        notas = _extrair_json(bruto)
    except urllib.error.HTTPError as e:
        print(f"  ⚠️  IA indisponível (HTTP {e.code}) — mantendo ordem por palavras-chave.")
        return noticias
    except Exception as e:
        print(f"  ⚠️  Classificação por IA falhou ({e}) — mantendo ordem por palavras-chave.")
        return noticias

    aplicadas = 0
    for item in notas:
        try:
            i    = int(item["i"])
            nota = max(0, min(10, float(item["nota"])))
        except (KeyError, ValueError, TypeError):
            continue
        if not (0 <= i < len(candidatas)):
            continue
        n = candidatas[i]
        n["nota_ia"]   = nota
        n["motivo_ia"] = str(item.get("motivo", ""))[:60]
        # Vetada continua vetada: a IA reordena, não reabilita.
        if n.get("score_relevancia", 0) > -50:
            n["score_relevancia"] = n.get("score_relevancia", 0) + nota * peso
        aplicadas += 1

    if not aplicadas:
        print("  ⚠️  IA não devolveu nota utilizável — mantendo ordem por palavras-chave.")
        return noticias

    noticias.sort(key=lambda x: x.get("score_relevancia", 0), reverse=True)

    print(f"\n  🤖 Classificação por IA ({MODELO}, {aplicadas} notícias):")
    for n in noticias[:6]:
        if "nota_ia" in n:
            print(f"       [IA {n['nota_ia']:>4.1f}] {n.get('titulo','')[:52]}")
            if n.get("motivo_ia"):
                print(f"                 ↳ {n['motivo_ia']}")
    ignoradas = [n for n in noticias if "nota_ia" in n and n["nota_ia"] <= 2]
    if ignoradas:
        print(f"       {len(ignoradas)} notícia(s) rebaixada(s) pela IA (nota ≤ 2)")
    return noticias


if __name__ == "__main__":
    import sys
    perfil = None
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "perfil_interesses.json")
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            perfil = json.load(f)

    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            noticias = json.load(f)
    else:
        noticias = [
            {"titulo": "Mercado Pago entra no crédito consignado privado",
             "resumo": "A fintech passa a oferecer consignado para trabalhadores CLT.",
             "score_relevancia": 30},
            {"titulo": "Itaú patrocina festival de música em São Paulo",
             "resumo": "O banco assina o patrocínio do evento que acontece em outubro.",
             "score_relevancia": 3},
        ]

    for n in reordenar_por_ia(noticias, perfil):
        print(f"{n.get('score_relevancia'):>6.1f}  {n.get('titulo','')[:60]}")
