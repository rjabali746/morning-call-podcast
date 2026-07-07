#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aprender_perfil.py — Refina o perfil de interesses a partir das manchetes
que o Roberto gostou (ou não).

FLUXO SIMPLES
  1. Cole manchetes em  manchetes_favaritas.txt  (uma por linha).
       - Linha normal          → "gostei desse tipo de notícia"
       - Linha começando NAO:   → "não quero esse tipo de notícia"
  2. Rode:  python3 aprender_perfil.py
  3. Reveja o resumo e confirme. O perfil_interesses.json é atualizado
     (com backup automático) e a caixa de entrada é arquivada.
  4. Rode  bash setup_github.sh  para o podcast passar a usar o perfil novo.

O QUE ELE FAZ
  - Pontua cada manchete com a MESMA lógica do scraper (temas + tiers +
    penalizações) para saber o que já é bem capturado vs. o que é novidade.
  - Nas manchetes "novidade" (score baixo), extrai termos candidatos que
    ainda NÃO estão no perfil e os adiciona a um tema-coringa
    ("novos_interesses_a_revisar", peso 4) — que já passa a influenciar a
    seleção e pode ser promovido a um tema definitivo depois.
  - Nas manchetes NAO:, adiciona os termos a uma penalização
    ("descartadas_pelo_usuario", peso -4).
  - Registra as manchetes gostadas em historico_exemplos.
  - Backup automático do perfil antes de qualquer escrita.

Sem dependências externas — só a biblioteca padrão do Python.
"""

import os
import re
import sys
import json
import shutil
import datetime
from collections import Counter

BASE        = os.path.dirname(os.path.abspath(__file__))
PERFIL_FILE = os.path.join(BASE, "perfil_interesses.json")
INBOX_FILE  = os.path.join(BASE, "manchetes_favoritas.txt")
LOG_FILE    = os.path.join(BASE, "manchetes_processadas.log")

TEMA_NOVOS   = "novos_interesses_a_revisar"
PESO_NOVOS   = 4
PEN_DESCARTE = "descartadas_pelo_usuario"
PESO_DESCARTE = -4

# Score abaixo do qual a manchete é considerada "novidade" (mal capturada).
# Um tema core (peso 7) com 1 hit já dá 7; abaixo disso é sinal de lacuna.
LIMIAR_NOVIDADE = 7

# Nº máximo de termos novos incorporados por execução (evita poluir o perfil).
MAX_TERMOS_POR_RUN = 15

# Stopwords pt-BR + ruído comum de manchete financeira.
STOPWORDS = {
    "a", "o", "os", "as", "um", "uma", "uns", "umas", "de", "do", "da", "dos",
    "das", "em", "no", "na", "nos", "nas", "por", "para", "pra", "com", "sem",
    "sob", "sobre", "entre", "ao", "aos", "à", "às", "e", "ou", "mas", "que",
    "se", "sua", "seu", "suas", "seus", "ja", "já", "não", "nao", "mais",
    "menos", "muito", "muita", "ser", "ter", "vai", "vão", "vao", "tem",
    "após", "apos", "ante", "até", "ate", "como", "quando", "onde", "qual",
    "quais", "isso", "este", "esta", "esse", "essa", "aquele", "aquela",
    "pode", "podem", "deve", "devem", "foi", "são", "sao", "era", "eram",
    "diz", "dizer", "vê", "ver", "vem", "novo", "nova", "novos", "novas",
    "ano", "anos", "hoje", "amanha", "amanhã", "ontem", "semana", "mes",
    "mês", "meses", "dia", "dias", "R$", "US$", "r$", "us$",
}

# Verbos e palavras genéricas de manchete — NÃO viram termo de interesse
# (senão poluem o perfil e, pior, penalizam notícias legítimas por
#  correspondência de substring). Ex.: "define", "avança", "américa".
GENERICOS = {
    "avança", "avanca", "avancam", "avançam", "ganha", "ganham", "ganhar",
    "define", "definir", "definem", "lança", "lanca", "lançam", "amplia",
    "ampliam", "ampliar", "cresce", "cresceu", "crescem", "sobe", "subiu",
    "sobem", "cai", "caiu", "caem", "faz", "fazer", "fica", "ficar", "prevê",
    "preve", "estuda", "planeja", "anuncia", "anunciam", "aprova", "aprovam",
    "espaço", "espaco", "grande", "maior", "menor", "alta", "baixa", "forte",
    "fraco", "trimestre", "semestre", "balanço", "balanco",
}

# ---------------------------------------------------------------------------
# Pontuação — réplica fiel de calcular_score_perfil() do valor_economico_scraper.py
# (mantida inline para o script não depender do selenium.)
# ---------------------------------------------------------------------------
def calcular_score(titulo, perfil):
    texto = titulo.lower()
    score = 0
    temas_hit = []

    for tema in perfil.get("temas", []):
        peso     = tema.get("peso", 1)
        palavras = tema.get("palavras", [])
        matches  = [p for p in palavras if p.lower() in texto]
        if matches:
            score += peso * min(len(matches), 2)
            temas_hit.append(tema.get("nome", "?"))

    ent_cfg = perfil.get("entidades_prioritarias", {})
    for tier_key, tier_fallback in [
        ("tier1_empresa", 6),
        ("tier2_concorrentes_diretos", 4),
        ("tier3_relevantes", 3),
    ]:
        tier  = ent_cfg.get(tier_key, {})
        bonus = tier.get("bonus", tier_fallback)
        for ent in tier.get("lista", []):
            if ent.lower() in texto:
                score += bonus
                temas_hit.append(tier_key)

    for pen in perfil.get("penalizacoes", []):
        peso_pen = pen.get("peso", -2)
        for palavra in pen.get("palavras", []):
            if palavra.lower() in texto:
                score += peso_pen
                break

    return score, temas_hit


def palavras_ja_no_perfil(perfil):
    """Set com tudo que o perfil já reconhece (para não duplicar termos)."""
    pal = set()
    for tema in perfil.get("temas", []):
        for p in tema.get("palavras", []):
            pal.add(p.lower())
    ent_cfg = perfil.get("entidades_prioritarias", {})
    for tk in ("tier1_empresa", "tier2_concorrentes_diretos", "tier3_relevantes"):
        for e in ent_cfg.get(tk, {}).get("lista", []):
            pal.add(e.lower())
    for pen in perfil.get("penalizacoes", []):
        for p in pen.get("palavras", []):
            pal.add(p.lower())
    return pal


def _eh_conteudo(tok):
    """True se o token é uma palavra 'de conteúdo' (não filler/verbo genérico)."""
    return (len(tok) >= 4 and tok not in STOPWORDS
            and tok not in GENERICOS and not tok.isdigit())


def extrair_candidatos(titulo, ja_existentes, permitir_unigram=True):
    """
    Extrai termos candidatos de uma manchete.
      - Bigrams: só quando AMBAS as palavras são de conteúdo (frase específica).
      - Unigrams: só palavras de conteúdo com 6+ letras (evita ruído curto).
    'permitir_unigram=False' força só bigrams — usado nas penalizações, para
    nunca inserir termo único (que casaria como substring em notícias legítimas).
    """
    toks = re.findall(r"[a-zà-ú0-9&]+", titulo.lower())
    toks = [t for t in toks
            if t not in STOPWORDS and len(t) > 2 and not t.isdigit()]
    grams = []
    for i in range(len(toks) - 1):                     # bigrams (preferidos)
        a, b = toks[i], toks[i + 1]
        if _eh_conteudo(a) and _eh_conteudo(b):
            grams.append(f"{a} {b}")
    if permitir_unigram:                               # unigrams fortes
        for t in toks:
            if _eh_conteudo(t) and len(t) >= 6:
                grams.append(t)
    # descarta o que o perfil já reconhece (como substring)
    out = []
    for g in grams:
        if g in ja_existentes:
            continue
        if any(g in pe or pe in g for pe in ja_existentes):
            continue
        out.append(g)
    return out


def ler_inbox():
    """Retorna (gostei, nao_quero) — listas de manchetes limpas."""
    if not os.path.exists(INBOX_FILE):
        return [], []
    gostei, nao = [], []
    with open(INBOX_FILE, encoding="utf-8") as f:
        for linha in f:
            s = linha.strip()
            if not s or s.startswith("#"):
                continue
            if s.upper().startswith("NAO:") or s.upper().startswith("NÃO:"):
                nao.append(s.split(":", 1)[1].strip())
            else:
                gostei.append(s)
    return gostei, nao


def garantir_tema(perfil, nome, descricao, peso):
    for tema in perfil.get("temas", []):
        if tema.get("nome") == nome:
            return tema
    tema = {"nome": nome, "descricao": descricao, "peso": peso, "palavras": []}
    perfil.setdefault("temas", []).append(tema)
    return tema


def garantir_penalizacao(perfil, nome, descricao, peso):
    for pen in perfil.get("penalizacoes", []):
        if pen.get("nome") == nome:
            return pen
    pen = {"nome": nome, "descricao": descricao, "peso": peso, "palavras": []}
    perfil.setdefault("penalizacoes", []).append(pen)
    return pen


def arquivar_inbox(gostei, nao):
    carimbo = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for m in gostei:
            f.write(f"[{carimbo}] (+) {m}\n")
        for m in nao:
            f.write(f"[{carimbo}] (-) {m}\n")
    # Reescreve a caixa só com o cabeçalho de instruções.
    cabecalho = []
    with open(INBOX_FILE, encoding="utf-8") as f:
        for linha in f:
            if linha.lstrip().startswith("#") or not linha.strip():
                cabecalho.append(linha)
            else:
                break
    with open(INBOX_FILE, "w", encoding="utf-8") as f:
        f.writelines(cabecalho)


def _salience(item):
    """Ordena candidatos: mais frequentes → bigrams → mais longos."""
    termo, freq = item
    return (freq, 1 if " " in termo else 0, len(termo))


def analisar(perfil, gostei, nao):
    """
    Análise pura (não modifica o perfil). Retorna um 'plano' com os termos
    a adicionar. Usado tanto pelo fluxo manual quanto pela sincronização.
    """
    ja = palavras_ja_no_perfil(perfil)

    bem, novidades = [], []
    cand = Counter()
    for m in gostei:
        score, _ = calcular_score(m, perfil)
        if score >= LIMIAR_NOVIDADE:
            bem.append((m, score))
        else:
            novidades.append((m, score))
            for c in extrair_candidatos(m, ja):
                cand[c] += 1
    termos_novos = [t for t, _ in sorted(cand.items(), key=_salience,
                                         reverse=True)][:MAX_TERMOS_POR_RUN]

    cand_neg = Counter()
    for m in nao:
        for c in extrair_candidatos(m, ja, permitir_unigram=False):
            cand_neg[c] += 1
    termos_descarte = [t for t, _ in sorted(cand_neg.items(), key=_salience,
                                            reverse=True)][:MAX_TERMOS_POR_RUN]

    return {"bem": bem, "novidades": novidades,
            "termos_novos": termos_novos, "termos_descarte": termos_descarte}


def imprimir_resumo(gostei, nao, plano):
    print("\n" + "=" * 60)
    print("🧠  Aprendizado do perfil — resumo")
    print("=" * 60)
    print(f"  Manchetes lidas:   {len(gostei)} gostei · {len(nao)} não quero")
    print(f"  Já bem capturadas: {len(plano['bem'])}")
    print(f"  Novidades (lacuna):{len(plano['novidades'])}")
    if plano["termos_novos"]:
        print(f"\n  ➕ Termos novos → tema '{TEMA_NOVOS}' (peso {PESO_NOVOS}):")
        for t in plano["termos_novos"]:
            print(f"       • {t}")
    if plano["termos_descarte"]:
        print(f"\n  ➖ Termos de descarte → '{PEN_DESCARTE}' (peso {PESO_DESCARTE}):")
        for t in plano["termos_descarte"]:
            print(f"       • {t}")
    if not plano["termos_novos"] and not plano["termos_descarte"]:
        print("\n  ✅ Nada de novo a adicionar — o perfil já cobre bem essas manchetes.")
    print("=" * 60)


def aplicar(perfil, gostei, plano):
    """Aplica o plano ao dict `perfil` (modifica in place)."""
    if plano["termos_novos"]:
        tema = garantir_tema(
            perfil, TEMA_NOVOS,
            "Termos aprendidos das manchetes favoritas — revisar e promover a temas definitivos.",
            PESO_NOVOS)
        existentes = {p.lower() for p in tema["palavras"]}
        for t in plano["termos_novos"]:
            if t not in existentes:
                tema["palavras"].append(t)

    if plano["termos_descarte"]:
        pen = garantir_penalizacao(
            perfil, PEN_DESCARTE,
            "Termos de assuntos que o Roberto marcou como indesejados.",
            PESO_DESCARTE)
        existentes = {p.lower() for p in pen["palavras"]}
        for t in plano["termos_descarte"]:
            if t not in existentes:
                pen["palavras"].append(t)

    hist = perfil.setdefault("historico_exemplos", [])
    for m in gostei:
        if m not in hist:
            hist.append(m)
    meta = perfil.setdefault("_meta", {})
    meta["ultima_atualizacao"] = datetime.date.today().isoformat()
    meta["total_exemplos_analisados"] = meta.get("total_exemplos_analisados", 0) + len(gostei)


def main():
    auto = "--yes" in sys.argv or "-y" in sys.argv

    if not os.path.exists(PERFIL_FILE):
        print(f"❌ perfil_interesses.json não encontrado em {PERFIL_FILE}")
        sys.exit(1)

    with open(PERFIL_FILE, encoding="utf-8") as f:
        perfil = json.load(f)

    gostei, nao = ler_inbox()
    if not gostei and not nao:
        print("ℹ️  Nenhuma manchete nova em manchetes_favoritas.txt.")
        print("    Cole suas manchetes lá e rode de novo.")
        return

    plano = analisar(perfil, gostei, nao)
    imprimir_resumo(gostei, nao, plano)

    if not auto:
        resp = input("\nAplicar estas mudanças ao perfil? [s/N] ").strip().lower()
        if resp not in ("s", "sim", "y", "yes"):
            print("Cancelado. Nada foi alterado.")
            return

    carimbo = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BASE, f"perfil_interesses.backup-{carimbo}.json")
    shutil.copy2(PERFIL_FILE, backup)

    aplicar(perfil, gostei, plano)

    with open(PERFIL_FILE, "w", encoding="utf-8") as f:
        json.dump(perfil, f, ensure_ascii=False, indent=2)

    arquivar_inbox(gostei, nao)

    print(f"\n✅ Perfil atualizado.  Backup: {os.path.basename(backup)}")
    print("   Caixa de entrada arquivada em manchetes_processadas.log")
    print("\n👉 Para o podcast usar o perfil novo, rode:")
    print("   bash setup_github.sh")


if __name__ == "__main__":
    main()
