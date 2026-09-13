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

PROVEDOR — funciona com qualquer serviço, de graça
  Quase todo provedor fala o mesmo dialeto (o "chat/completions" da OpenAI),
  então trocar de um para outro é só mudar variável de ambiente. Basta ter a
  chave de UM deles; o módulo detecta sozinho qual está disponível.

  Gratuitos, sem cartão de crédito (setembro/2026):
    GROQ_API_KEY        console.groq.com     — llama 3.3 70B, 1.000 req/dia
    GEMINI_API_KEY      aistudio.google.com  — Gemini Flash, modelo de ponta
    OPENROUTER_API_KEY  openrouter.ai        — catálogo de modelos ':free',
                                               incluindo os Kimi da Moonshot
  Pagos, se um dia quiser:
    ANTHROPIC_API_KEY, MOONSHOT_API_KEY

  O podcast faz UMA chamada por dia, de ~3.000 tokens. Qualquer um dos
  gratuitos cobre isso com folga enorme.

Configuração (variáveis de ambiente):
  <PROVEDOR>_API_KEY  — a chave. Sem nenhuma, a etapa é simplesmente pulada.
  IA_PROVEDOR         — força um provedor (groq|gemini|openrouter|moonshot|
                        anthropic). Se ausente, usa o primeiro com chave.
  IA_MODELO           — troca o modelo do provedor escolhido
  IA_BASE_URL         — endpoint próprio (qualquer API compatível com OpenAI)
  IA_PESO             — padrão: 2.0 (quanto cada ponto da nota vale no total)
"""

import os
import json
import urllib.request
import urllib.error

# ── Provedores conhecidos ─────────────────────────────────────────────────────
# (url, modelo padrão, variável da chave, formato)
PROVEDORES = {
    "groq": (
        "https://api.groq.com/openai/v1/chat/completions",
        "llama-3.3-70b-versatile", "GROQ_API_KEY", "openai"),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-2.5-flash", "GEMINI_API_KEY", "openai"),
    "openrouter": (
        "https://openrouter.ai/api/v1/chat/completions",
        "moonshotai/kimi-k2:free", "OPENROUTER_API_KEY", "openai"),
    "moonshot": (
        "https://api.moonshot.ai/v1/chat/completions",
        "kimi-k2-0711-preview", "MOONSHOT_API_KEY", "openai"),
    "anthropic": (
        "https://api.anthropic.com/v1/messages",
        "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY", "anthropic"),
}

# Ordem de preferência quando IA_PROVEDOR não é informado: gratuitos primeiro.
ORDEM_PADRAO = ["groq", "gemini", "openrouter", "moonshot", "anthropic"]

PESO_IA   = float(os.environ.get("IA_PESO") or 2.0)
TIMEOUT   = 60
MAX_CAND  = 25     # candidatas enviadas por chamada
CHARS_CTX = 320    # trecho de cada matéria enviado junto do título


def detectar_provedor():
    """
    Descobre qual provedor usar: o forçado por IA_PROVEDOR, ou o primeiro da
    ordem de preferência que tenha chave definida.
    Devolve (nome, url, modelo, chave, formato) ou None se não houver nenhuma.
    """
    forcado = (os.environ.get("IA_PROVEDOR") or "").strip().lower()
    ordem   = [forcado] if forcado in PROVEDORES else ORDEM_PADRAO

    for nome in ordem:
        url, modelo_pad, var_chave, formato = PROVEDORES[nome]
        chave = os.environ.get(var_chave, "").strip()
        if not chave:
            continue
        return (nome,
                os.environ.get("IA_BASE_URL") or url,
                os.environ.get("IA_MODELO")   or modelo_pad,
                chave, formato)
    return None


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


# A Groq (e outros) ficam atrás da Cloudflare, que barra cliente sem
# identificação com "error code: 1010" — HTTP 403. O urllib manda
# "Python-urllib/3.11" por padrão e leva bloqueio. Identificar-se resolve.
USER_AGENT = "morning-call-jabali/1.0 (+https://github.com/rjabali746/morning-call-podcast)"


def _chamar_api(url, modelo, chave, formato, prompt):
    """Uma chamada de inferência. Dois formatos cobrem todos os provedores."""
    if formato == "anthropic":
        corpo = {"model": modelo, "max_tokens": 1500,
                 "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": chave,
                   "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:   # dialeto OpenAI — Groq, Gemini, OpenRouter, Moonshot e afins
        corpo = {"model": modelo, "max_tokens": 1500, "temperature": 0,
                 "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": f"Bearer {chave}",
                   "Content-Type": "application/json"}
    headers["User-Agent"] = USER_AGENT
    headers["Accept"]     = "application/json"

    dados = json.dumps(corpo).encode("utf-8")

    # `requests` primeiro: a pilha TLS dele passa pela Cloudflare com mais
    # facilidade que a do urllib. Cai para a biblioteca padrão se não existir.
    try:
        import requests as _rq
        r = _rq.post(url, data=dados, headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} — {r.text[:200]}")
        resposta = r.json()
    except ImportError:
        req = urllib.request.Request(url, data=dados, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resposta = json.loads(r.read().decode("utf-8"))

    if formato == "anthropic":
        return resposta["content"][0]["text"]
    return resposta["choices"][0]["message"]["content"]


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
    if not noticias:
        return noticias

    escolhido = detectar_provedor()
    if not escolhido:
        print("  ℹ️  Nenhuma chave de IA configurada — classificação por IA desativada.")
        print("     Grátis, sem cartão: GROQ_API_KEY, GEMINI_API_KEY ou OPENROUTER_API_KEY.")
        return noticias
    nome, url, modelo, chave, formato = escolhido
    if api_key:                       # chave explícita vence a do ambiente
        chave = api_key

    candidatas = noticias[:MAX_CAND]
    try:
        bruto = _chamar_api(url, modelo, chave, formato,
                            _montar_prompt(perfil, candidatas))
        notas = _extrair_json(bruto)
    except urllib.error.HTTPError as e:
        detalhe = ""
        try:
            detalhe = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        print(f"  ⚠️  IA indisponível ({nome}, HTTP {e.code}) — mantendo ordem "
              f"por palavras-chave. {detalhe}")
        return noticias
    except Exception as e:
        print(f"  ⚠️  Classificação por IA falhou ({nome}: {e}) — mantendo ordem "
              f"por palavras-chave.")
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

    print(f"\n  🤖 Classificação por IA ({nome}/{modelo}, {aplicadas} notícias):")
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
