#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper para Valor Econômico — Scraping direto das páginas de seção
Usa autenticação via API da Globo (sistema de login do Valor)

Estratégia:
  1. Login via API da Globo (login.globo.com)
  2. Scraping das páginas de seção (financas, empresas, mercados)
  3. Extração de título + resumo + link de cada artigo
  4. Filtragem por palavras-chave de crédito/fintech
  5. Busca de conteúdo completo dos top 5 artigos
"""

import os, json, time, sys, re, requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Selenium (usado para artigos com paywall JavaScript)
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

CONFIG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
COOKIE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valor_cookies.json")
PERFIL_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perfil_interesses.json")
POOL_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool_reserva.json")

# ── Pool de reserva ───────────────────────────────────────────────────────────
# Notícias boas que foram raspadas mas não couberam no episódio do dia ficam
# guardadas por alguns dias. Quando um dia rende pouca coisa relevante, o
# episódio se completa com elas — em vez de raspar o fundo do tacho e narrar
# alguma coisa "nada a ver" só para bater o mínimo.
POOL_DIAS       = 3    # validade: notícia com mais de 3 dias sai do pool
POOL_SCORE_MIN  = 10   # só guarda o que é claramente relevante
POOL_MAX        = 40   # teto de itens guardados

# ── Parâmetros de tempo e tamanho do podcast ──────────────────────────────────
WPM_PODCAST      = 140   # palavras por minuto narradas em português (ElevenLabs)
MAX_MIN_PODCAST  = 10    # duração máxima do episódio em minutos
MAX_CHARS_RESUMO = 1300  # caracteres máx por resumo de notícia (texto bruto)

# Caracteres (normalizados) por palavra falada — usado para estimar duração a
# partir do tamanho real do texto. Medir pelas palavras do texto BRUTO
# subestimava a duração, porque números viram muitas palavras ao ir por extenso.
_CHARS_POR_PALAVRA = 6.5

# PISO de notícias por episódio. Garantido mesmo que o score fique abaixo do
# limiar ou que os tetos de tempo/caracteres sejam atingidos — um episódio com
# menos de 3 notícias fica pobre demais para justificar a publicação.
MIN_NOTICIAS_EPISODIO = 3

# Teto RÍGIDO de caracteres JÁ NORMALIZADOS enviados ao TTS por episódio — é o
# que efetivamente consome quota. Com Flash v2.5 cada caractere custa METADE de
# um crédito, e o plano Creator dá 121.000 créditos/mês:
#   9000 chars × 0,5 = 4.500 créditos/episódio
#   4.500 × 22 dias úteis = 99.000 créditos/mês  →  82% do plano  ✓
# Os 18% de folga cobrem meses com 23 dias úteis e reexecuções por falha.
MAX_CHARS_EPISODIO = 9000

# Teto por notícia, também medido JÁ NORMALIZADO. Dimensionado para caber
# 5 notícias no episódio:  250 (intro/outro) + 5 × (1600 + 135) = 8.925 chars ✓
# (135 = overhead de rótulo/título). Com o piso de 3, sobra folga de sobra.
MAX_CHARS_TTS_RESUMO = 1600

# Estimativas fixas usadas no cálculo dos tetos
_PALAVRAS_INTRO_OUTRO   = 45    # palavras de intro + outro (abertura enxuta)
_CHARS_INTRO_OUTRO      = 240   # caracteres de intro + outro
_CHARS_OVERHEAD_NOTICIA = 45    # rótulo + título + quebras de linha

# Numerais por extenso usados no roteiro — nenhum dígito chega ao TTS
_CARDINAIS_F = {1:"uma", 2:"duas", 3:"três", 4:"quatro", 5:"cinco",
                6:"seis", 7:"sete", 8:"oito", 9:"nove", 10:"dez"}
_ORDINAIS_F  = {1:"Primeira", 2:"Segunda", 3:"Terceira", 4:"Quarta",
                5:"Quinta", 6:"Sexta", 7:"Sétima", 8:"Oitava",
                9:"Nona", 10:"Décima"}


def chars_no_tts(texto):
    """
    Quantos caracteres este texto REALMENTE consumirá no ElevenLabs.

    A normalização para fala expande números por extenso ("R$ 6,2 bi" vira
    "seis vírgula dois bilhões de reais"), o que pode inflar o texto em 30–70%
    em matérias densas de dados. Medir o texto bruto subestimaria a quota e
    estouraria o plano. Aqui medimos o texto final, já normalizado.

    Se o módulo de TTS não estiver disponível (uso isolado do scraper), cai
    num multiplicador conservador.
    """
    try:
        from elevenlabs_tts import _normalizar_para_fala
        return len(_normalizar_para_fala(texto))
    except Exception:
        # Inflação típica medida em matérias reais: 1,01–1,08; em matérias
        # saturadas de números chega a 1,7. 1,25 é um meio-termo conservador.
        return int(len(texto) * 1.25)

# Páginas de seção do Valor (scraping direto, sem RSS)
VALOR_SECOES = [
    ("Finanças",   "https://valor.globo.com/financas/"),
    ("Empresas",   "https://valor.globo.com/empresas/"),
    ("Mercados",   "https://valor.globo.com/financas/mercados/"),
    ("Brasil",     "https://valor.globo.com/brasil/"),
    ("Agro",       "https://valor.globo.com/agro/"),
]

# Fallback: URLs antigas do valor.com.br
VALOR_SECOES_FALLBACK = [
    ("Finanças",   "https://www.valor.com.br/financas"),
    ("Empresas",   "https://www.valor.com.br/empresas"),
    ("Mercados",   "https://www.valor.com.br/mercados"),
]

# Palavras-chave base — usadas como filtro mínimo de relevância (fallback sem perfil)
# Inclui os termos core do negócio do Roberto: crédito PF + adquirência + PME
PALAVRAS_CHAVE = [
    # Crédito PF (core)
    "cartão de crédito", "crédito rotativo", "parcelamento",
    "financiamento de veículo", "financiamento de moto",
    "crédito consignado", "consignado", "crédito pessoal",
    "empréstimo pessoal", "bnpl", "inadimplência",
    "inadimplencia", "score de crédito", "serasa",
    # Adquirência (core)
    "adquirente", "adquirência", "maquininha", "cielo", "rede",
    "mdr", "taxa de desconto", "credenciamento", "split de pagamento",
    "tap to pay", "infinitepay",
    # Crédito PME (core)
    "capital de giro", "antecipação de recebíveis", "recebíveis",
    "microcrédito", "crédito para mei", "pronampe", "factoring",
    # Mercado financeiro geral
    "crédito", "credito", "financiamento", "empréstimo", "emprestimo",
    "juros", "selic", "banco", "pagamento", "pix", "fintech",
    "open finance", "open banking", "banco central", "bcb",
    "spread", "bradesco", "itaú", "itau", "santander", "btg",
]

# ============================================================================
# PERFIL DE INTERESSES (carregado de perfil_interesses.json)
# ============================================================================

_perfil_cache = None

# ── Casamento de termos por PALAVRA INTEIRA ───────────────────────────────────
# O código antigo usava `palavra in texto`, casamento por substring. Isso fazia
# o filtro disparar em lugares absurdos:
#     "ip"   casava em part[ip]ação, princ[íp]io, equ[ip]e
#     "fed"  casava em [fed]eral, con[fed]eração
#     "iso"  casava em [iso]lamento, prov[isó]rio
#     "rede" casava em pa[rede], ap[rende]
# Uma manchete como "Governo federal amplia rede de atendimento" tirava 16
# pontos — mais que muita notícia legítima de crédito. Daí as notícias
# "nada a ver". Com fronteira de palavra, só casa o termo de verdade.
_PADRAO_CACHE = {}

def _fragmento_flexivel(palavra):
    """
    Regex de UMA palavra, tolerante a plural em pt-BR.

    Sem isso, exigir palavra inteira criaria um problema novo: o perfil tem
    "adquirente" mas a manchete diz "adquirentes"; tem "microcrédito" mas a
    manchete diz "microcréditos". O filtro passaria a descartar notícia boa —
    e pior, no portão eh_relevante(), antes mesmo de pontuar.
    """
    w = re.escape(palavra)
    if len(palavra) < 3:
        return w                                   # "ip", "bb": sem flexão
    if palavra.endswith("ão"):                     # cartão → cartões
        return re.escape(palavra[:-2]) + r"(?:ão|ões|ãos|ães)"
    if palavra.endswith("s"):                      # juros, recebíveis: invariante
        return w
    if palavra.endswith("l"):                      # digital → digitais
        return r"(?:" + w + r"|" + re.escape(palavra[:-1]) + r"is)"
    if palavra.endswith("m"):                      # bem → bens
        return r"(?:" + w + r"|" + re.escape(palavra[:-1]) + r"ns)"
    if palavra.endswith(("r", "z")):               # mulher → mulheres
        return w + r"(?:es)?"
    return w + r"s?"                               # caso geral


def casa_termo(palavra, texto):
    """
    True se `palavra` aparece em `texto` como termo inteiro (não substring),
    aceitando a forma plural.

    O casamento por substring era a maior fonte de falso positivo — mas trocar
    por igualdade exata criaria falsos negativos nos plurais. As duas coisas
    juntas: fronteira de palavra + flexão de número.
    """
    p = (palavra or "").strip().lower()
    if not p:
        return False
    padrao = _PADRAO_CACHE.get(p)
    if padrao is None:
        corpo = r"\s+".join(_fragmento_flexivel(w) for w in p.split())
        # (?<!\w) e (?!\w) em vez de \b: funcionam também com termos que
        # começam/terminam em símbolo, como "s&p" e "m&a".
        padrao = re.compile(r"(?<!\w)" + corpo + r"(?!\w)")
        _PADRAO_CACHE[p] = padrao
    return bool(padrao.search(texto))

def carregar_perfil():
    """Carrega o perfil de interesses personalizado do Roberto."""
    global _perfil_cache
    if _perfil_cache is not None:
        return _perfil_cache
    if os.path.exists(PERFIL_FILE):
        try:
            with open(PERFIL_FILE, "r", encoding="utf-8") as f:
                _perfil_cache = json.load(f)
            ent_cfg = _perfil_cache.get("entidades_prioritarias", {})
            total_ent = sum(
                len(ent_cfg.get(t, {}).get("lista", []))
                for t in ("tier1_empresa", "tier2_concorrentes_diretos", "tier3_relevantes")
            )
            print(f"  ✅ Perfil de interesses carregado ({len(_perfil_cache.get('temas', []))} temas, "
                  f"{total_ent} entidades em 3 tiers)")
            return _perfil_cache
        except Exception as e:
            print(f"  ⚠️  Erro ao carregar perfil_interesses.json: {e}")
    return None


def _todas_palavras_perfil(perfil):
    """Retorna set com todas as palavras do perfil (para filtro de relevância)."""
    palavras = set()
    if not perfil:
        return palavras
    # Palavras dos temas
    for tema in perfil.get("temas", []):
        for p in tema.get("palavras", []):
            palavras.add(p.lower())
    # Entidades dos 3 tiers
    entidades_cfg = perfil.get("entidades_prioritarias", {})
    for tier_key in ("tier1_empresa", "tier2_concorrentes_diretos", "tier3_relevantes"):
        for entidade in entidades_cfg.get(tier_key, {}).get("lista", []):
            palavras.add(entidade.lower())
    return palavras


def calcular_score_perfil(noticia, perfil):
    """
    Calcula score de relevância baseado no perfil de interesses.
    Retorna (score_total, detalhes) — score mais alto = mais relevante.

    Lógica de pontuação:
    ┌──────────────────────────────────────────────────────────────────┐
    │  Temas (peso × min(hits, 2)):                                    │
    │    Peso 7 → até +14  (crédito PF, adquirência, PME)             │
    │    Peso 5 → até +10  (fintechs, política monetária)             │
    │    Peso 4 → até  +8  (executivos, ratings, IA, regulação)       │
    │    Peso 2-3 → até +6  (macro, ESG, internacional)               │
    │                                                                  │
    │  Entidades (sistema de 3 tiers):                                 │
    │    Tier 1 — Mercado Pago / Mercado Livre: +6 por ocorrência     │
    │    Tier 2 — Concorrentes diretos:          +4 por ocorrência     │
    │    Tier 3 — Incumbentes / reguladores:     +3 por ocorrência     │
    │                                                                  │
    │  Penalizações: subtraem conforme peso configurado                │
    │                                                                  │
    │  Score esperado:                                                 │
    │    Artigo CORE (MP + crédito PF + adquirência) → 25–40 pts      │
    │    Artigo estratégico (fintech + Selic) → 15–25 pts             │
    │    Artigo secundário (macro, ESG) → 2–8 pts                     │
    └──────────────────────────────────────────────────────────────────┘
    """
    texto = (noticia.get("titulo", "") + " " + noticia.get("resumo", "")).lower()
    score = 0
    detalhes = []

    if not perfil:
        return sum(1 for p in PALAVRAS_CHAVE if casa_termo(p, texto)), []

    # ── Pontuação por tema ────────────────────────────────────────────────────
    for tema in perfil.get("temas", []):
        nome     = tema.get("nome", "?")
        peso     = tema.get("peso", 1)
        palavras = tema.get("palavras", [])
        matches  = [p for p in palavras if casa_termo(p, texto)]

        # Termos ambíguos: só valem se o tema já foi ancorado por um termo
        # inequívoco. "rede" é adquirente, mas também é "rede elétrica" e
        # "rede de atendimento" — só conta se vier junto de maquininha, MDR etc.
        if matches:
            contextuais = [p for p in tema.get("palavras_contextuais", [])
                           if casa_termo(p, texto)]
            matches += contextuais

        if matches:
            contribuicao = peso * min(len(matches), 2)  # cap: 2 hits por tema
            score += contribuicao
            detalhes.append(f"{nome}(+{contribuicao}:{','.join(matches[:2])})")

    # ── Bônus por entidade — sistema de 3 tiers ──────────────────────────────
    entidades_cfg = perfil.get("entidades_prioritarias", {})

    for tier_key, tier_bonus in [
        ("tier1_empresa",            6),
        ("tier2_concorrentes_diretos", 4),
        ("tier3_relevantes",         3),
    ]:
        tier = entidades_cfg.get(tier_key, {})
        bonus_cfg = tier.get("bonus", tier_bonus)
        for entidade in tier.get("lista", []):
            if casa_termo(entidade, texto):
                score += bonus_cfg
                detalhes.append(f"{tier_key}(+{bonus_cfg}:{entidade.strip()})")

    # ── Penalizações ─────────────────────────────────────────────────────────
    for pen in perfil.get("penalizacoes", []):
        peso_pen = pen.get("peso", -2)
        for palavra in pen.get("palavras", []):
            if casa_termo(palavra, texto):
                score += peso_pen
                detalhes.append(f"penalidade({peso_pen}:{palavra})")
                break

    # ── Vetos ─────────────────────────────────────────────────────────────────
    # Assunto vetado zera a notícia, por mais pontos que ela tenha acumulado.
    # Serve para casos em que a penalização por peso não basta — uma matéria de
    # política ou esporte que cite "Itaú" de passagem não deve entrar por causa
    # do bônus de entidade.
    for palavra in perfil.get("vetos", []):
        if casa_termo(palavra, texto):
            detalhes.append(f"VETO({palavra})")
            return -99, detalhes

    return score, detalhes

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# ============================================================================
# UTILITÁRIOS
# ============================================================================

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ config.json não encontrado em: {CONFIG_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ config.json com erro de formato: {e}")
        sys.exit(1)

def eh_relevante(texto):
    """
    Verifica se o artigo é relevante para o Morning Call.
    Usa palavras-chave base + todas as palavras do perfil personalizado.
    """
    t = texto.lower()
    # 1. Palavras-chave base (casamento por palavra inteira)
    if any(casa_termo(p, t) for p in PALAVRAS_CHAVE):
        return True
    # 2. Palavras do perfil personalizado (se disponível)
    perfil = carregar_perfil()
    if perfil:
        for palavra in _todas_palavras_perfil(perfil):
            if casa_termo(palavra, t):
                return True
    return False

def limpar_texto(texto):
    if not texto:
        return ""
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto

# ============================================================================
# AUTENTICAÇÃO GLOBO
# ============================================================================

def login_globo(session, email, password):
    """
    Autentica via API da Globo (sistema usado pelo Valor Econômico).
    Tenta múltiplos endpoints e métodos. Retorna True se bem-sucedido.
    """
    print(f"\n🔐 Autenticando no Valor Econômico ({email})...")

    # --- Método 1: API REST da Globo (v2 e v3) ---
    endpoints = [
        ("POST", "https://login.globo.com/api/authentication",
         {"payload": {"email": email, "password": password, "serviceId": 4654}}),
        ("POST", "https://login.globo.com/api/authentication",
         {"payload": {"email": email, "password": password, "serviceId": 4728}}),
        ("POST", "https://id.globo.com/auth/sign_in",
         {"email": email, "password": password}),
    ]

    for method, url, payload in endpoints:
        try:
            resp = session.request(
                method, url,
                json=payload,
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": "https://valor.globo.com",
                    "Referer": "https://valor.globo.com/",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=15
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    glbid = (data.get("glbId") or data.get("id") or
                             data.get("userInfo", {}).get("glbId") or
                             data.get("data", {}).get("glbId"))
                    if glbid:
                        print(f"  ✅ Login OK via {url.split('/')[4]} — GLBID obtido")
                        for domain in [".globo.com", ".valor.globo.com", ".valor.com.br"]:
                            session.cookies.set("GLBID", glbid, domain=domain)
                        return True
                except Exception:
                    pass
            print(f"  ⚠️  {url.split('/')[-1]}: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️  {url}: {e}")

    # --- Método 2: Login via página HTML do Globo ID ---
    try:
        print("  🔄 Tentando login via página HTML (Globo ID)...")
        # Página de login do Valor que redireciona para o Globo ID
        login_page_url = "https://login.globo.com/login/438"
        resp = session.get(login_page_url, headers=HEADERS, timeout=10, allow_redirects=True)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extrair campos ocultos do formulário
        form_data = {"login": email, "password": password}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name") or inp.get("id")
            val  = inp.get("value", "")
            if name:
                form_data[name] = val

        # URL de submit do formulário
        form = soup.find("form")
        action = form.get("action") if form else login_page_url
        if action and not action.startswith("http"):
            action = "https://login.globo.com" + action

        resp2 = session.post(
            action or login_page_url,
            data=form_data,
            headers={
                **HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": login_page_url,
            },
            timeout=15,
            allow_redirects=True,
        )

        cookies_atuais = {c.name.lower(): c.value for c in session.cookies}
        glbid = cookies_atuais.get("glbid") or cookies_atuais.get("glb_id")
        if glbid:
            print(f"  ✅ Login via HTML OK — GLBID obtido")
            return True

        # Verificar se está logado pelo conteúdo da página
        if any(t in resp2.text.lower() for t in ["logout", "sair", "minha conta"]):
            print("  ✅ Login confirmado pelo conteúdo da página")
            return True

        print(f"  ⚠️  HTML login: status {resp2.status_code}, sem cookie GLBID")

    except Exception as e:
        print(f"  ⚠️  HTML login erro: {e}")

    print("  ⚠️  Login sem sucesso — continuando sem autenticação (conteúdo pode ser parcial)")
    return False


# ============================================================================
# SCRAPING DAS SEÇÕES
# ============================================================================

def scrape_secao(session, nome, url):
    """
    Extrai lista de artigos de uma página de seção do Valor.
    Retorna lista de dicts: {titulo, link, resumo, secao}
    """
    noticias = []
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"    ⚠️  HTTP {resp.status_code}")
            return noticias

        soup = BeautifulSoup(resp.text, "html.parser")

        # ----------------------------------------------------------------
        # Padrão 1: cards de notícia com classe "feed-post" (Globo/Valor)
        # ----------------------------------------------------------------
        cards = soup.select("div.feed-post, div.bastian-feed-item, article.feed-post")
        if not cards:
            # Padrão 2: elementos <article>
            cards = soup.find_all("article")
        if not cards:
            # Padrão 3: listas de links com h2/h3 dentro de main/section
            cards = soup.select("main a, section a, .content a")

        vistos = set()
        for card in cards[:30]:
            try:
                # Tentar extrair título
                titulo_el = (
                    card.select_one("h2, h3, .feed-post-title, .post-title, .title") or
                    (card if card.name == "a" else card.find("a"))
                )
                if not titulo_el:
                    continue
                titulo = limpar_texto(titulo_el.get_text())
                if not titulo or len(titulo) < 10:
                    continue

                # Tentar extrair link
                link_el = card.find("a", href=True) if card.name != "a" else card
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://valor.globo.com" + link

                if not link or link in vistos:
                    continue
                vistos.add(link)

                # Tentar extrair resumo
                resumo_el = card.select_one(
                    "p, .feed-post-body, .post-summary, .chapeu + p"
                )
                resumo = limpar_texto(resumo_el.get_text()) if resumo_el else ""

                # Filtrar por relevância
                if not eh_relevante(titulo + " " + resumo):
                    continue

                noticias.append({
                    "titulo":             titulo,
                    "link":               link,
                    "resumo":             resumo[:300],
                    "secao":              nome,
                    "fonte":              "Valor Econômico",
                    "data":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "conteudo_completo":  ""
                })

            except Exception:
                continue

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Erro de conexão: {e}")
    except Exception as e:
        print(f"    ❌ Erro: {e}")

    return noticias


def buscar_noticias(session):
    """
    Tenta scraping nas URLs principais, depois nas fallback.
    """
    print("\n📰 Extraindo notícias das seções do Valor...")
    todas = []

    for nome, url in VALOR_SECOES:
        print(f"  → {nome}: {url}")
        itens = scrape_secao(session, nome, url)
        print(f"    {'✓' if itens else '⚠️ '} {len(itens)} notícias relevantes")
        todas.extend(itens)
        time.sleep(1)

    if not todas:
        print("\n  🔄 Tentando URLs alternativas (valor.com.br)...")
        for nome, url in VALOR_SECOES_FALLBACK:
            print(f"  → {nome}: {url}")
            itens = scrape_secao(session, nome, url)
            print(f"    {'✓' if itens else '⚠️ '} {len(itens)} notícias relevantes")
            todas.extend(itens)
            time.sleep(1)

    # Deduplicar por título
    vistos, unicas = set(), []
    for n in todas:
        chave = n["titulo"].lower()[:50]
        if chave not in vistos:
            vistos.add(chave)
            unicas.append(n)

    # ── Scoring com perfil personalizado ─────────────────────────────────────
    perfil = carregar_perfil()

    def score_com_perfil(n):
        s, _ = calcular_score_perfil(n, perfil)
        return s

    unicas.sort(key=score_com_perfil, reverse=True)

    # Armazenar score em cada artigo (usado depois na seleção por tempo)
    for n in unicas:
        s, det = calcular_score_perfil(n, perfil)
        n["score_relevancia"] = s
        n["score_detalhes"]   = det

    # Log dos scores para transparência (top 8)
    # Score de referência com perfil v2:
    #   Artigo core (crédito PF / adquirência / PME) → 14–30 pts
    #   Artigo estratégico (fintech / Selic) → 10–20 pts
    #   Artigo secundário (macro, ESG) → 2–8 pts
    print(f"\n  📊 Total: {len(unicas)} notícias únicas e relevantes")
    print(f"  🎯 Ranking por perfil de interesses (top {min(8, len(unicas))}):")
    for i, n in enumerate(unicas[:8], 1):
        s, detalhes = calcular_score_perfil(n, perfil)
        # Ícone de prioridade baseado no score
        if s >= 14:
            icone = "🔴"   # core do negócio
        elif s >= 8:
            icone = "🟡"   # estratégico
        else:
            icone = "⚪"   # contexto
        detalhe_str = " | ".join(detalhes[:3]) if detalhes else "base"
        print(f"     {i}. {icone} [{s:>3}pts] {n['titulo'][:60]}")
        if detalhes:
            print(f"           ↳ {detalhe_str}")

    return unicas


# ============================================================================
# SELEÇÃO POR TEMPO
# ============================================================================

def carregar_pool():
    """Lê o pool de reserva, já descartando o que passou da validade."""
    if not os.path.exists(POOL_FILE):
        return []
    try:
        with open(POOL_FILE, encoding="utf-8") as f:
            pool = json.load(f)
    except Exception:
        return []
    limite = (datetime.now() - timedelta(days=POOL_DIAS)).isoformat()
    vivos  = [n for n in pool if n.get("_guardado_em", "") >= limite]
    if len(pool) != len(vivos):
        print(f"  🗑️  Pool: {len(pool) - len(vivos)} notícia(s) vencida(s) descartada(s)")
    return vivos


def salvar_pool(pool_antigo, candidatas, usadas_links):
    """
    Atualiza o pool: remove o que foi narrado, acrescenta as boas que sobraram.

    Guarda só o essencial (título, link, score e um trecho do conteúdo), para o
    arquivo não crescer sem controle.
    """
    agora  = datetime.now().isoformat()
    vistos = set(usadas_links)
    novo   = []

    for n in pool_antigo:                      # mantém o que ainda não foi usado
        if n.get("link") in vistos:
            continue
        vistos.add(n.get("link"))
        novo.append(n)

    for n in candidatas:                       # acrescenta as boas de hoje
        link = n.get("link")
        if not link or link in vistos:
            continue
        if n.get("score_relevancia", 0) < POOL_SCORE_MIN:
            continue
        vistos.add(link)
        conteudo = (n.get("conteudo_completo") or n.get("resumo") or "")
        novo.append({
            "titulo": n.get("titulo", ""),
            "link": link,
            "score_relevancia": n.get("score_relevancia", 0),
            "conteudo_completo": conteudo[:MAX_CHARS_RESUMO * 2],
            "_guardado_em": n.get("_guardado_em", agora),
        })

    novo.sort(key=lambda x: x.get("score_relevancia", 0), reverse=True)
    novo = novo[:POOL_MAX]
    try:
        with open(POOL_FILE, "w", encoding="utf-8") as f:
            json.dump(novo, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️  Não consegui salvar o pool: {e}")
    return novo


def selecionar_com_resgate(noticias, pool=None, min_noticias=MIN_NOTICIAS_EPISODIO,
                           **kwargs):
    """
    Monta o episódio em três camadas, da melhor para a pior:

      1. Notícias de hoje que passam no critério de relevância (score ≥ limiar).
      2. Se faltou para o mínimo, RESGATA as de maior score dos últimos dias
         que ainda não foram narradas — conteúdo bom que não coube antes.
      3. Só se ainda faltar, aceita as fracas de hoje.

    Assim o episódio mantém o tamanho sem nunca precisar narrar algo irrelevante
    enquanto houver notícia boa guardada.
    """
    selecionadas = selecionar_por_tempo(noticias, min_noticias=0, **kwargs)
    escolhidos   = {n.get("link") for n in selecionadas}

    # ── Camada 2: resgate do pool ────────────────────────────────────────────
    resgatadas = []
    if len(selecionadas) < min_noticias and pool:
        reserva = sorted((n for n in pool if n.get("link") not in escolhidos),
                         key=lambda x: x.get("score_relevancia", 0), reverse=True)
        for n in reserva:
            if len(selecionadas) >= min_noticias:
                break
            selecionadas.append(n)
            escolhidos.add(n.get("link"))
            resgatadas.append(n)
        if resgatadas:
            print(f"\n  ♻️  {len(resgatadas)} notícia(s) resgatada(s) do pool de reserva:")
            for n in resgatadas:
                dia = (n.get("_guardado_em", "")[:10] or "?")
                print(f"       • [{n.get('score_relevancia','?'):>3}pts] ({dia}) {n.get('titulo','')[:58]}")

    # ── Camada 3: último recurso — as fracas de hoje ─────────────────────────
    if len(selecionadas) < min_noticias:
        sobras = [n for n in noticias if n.get("link") not in escolhidos]
        sobras.sort(key=lambda x: x.get("score_relevancia", 0), reverse=True)
        faltam = min_noticias - len(selecionadas)
        if sobras:
            print(f"\n  ⚠️  Pool vazio — completando com {min(faltam, len(sobras))} "
                  f"notícia(s) de score baixo para bater o mínimo de {min_noticias}:")
        for n in sobras[:faltam]:
            print(f"       • [{n.get('score_relevancia','?'):>3}pts] {n.get('titulo','')[:58]}")
            selecionadas.append(n)
            escolhidos.add(n.get("link"))

    if len(selecionadas) < min_noticias:
        print(f"\n  ⚠️  Só foi possível montar {len(selecionadas)} notícia(s) "
              f"(mínimo desejado: {min_noticias}).")

    return selecionadas


def selecionar_por_tempo(noticias, max_min=MAX_MIN_PODCAST, wpm=WPM_PODCAST,
                         min_score=6, max_chars=MAX_CHARS_EPISODIO,
                         min_noticias=0):
    """
    Seleciona notícias enriquecidas em ordem de score, respeitando os tetos de
    tempo e de caracteres — mas SEMPRE entregando pelo menos `min_noticias`.

    Regras:
    1. Piso rígido: as primeiras `min_noticias` da lista entram no episódio
       independentemente de score ou de teto. Um episódio com 1–2 notícias fica
       pobre demais; é preferível estourar um pouco o teto do que publicar isso.
    2. A partir da (min_noticias+1)-ésima, só entra quem tiver
       score_relevancia ≥ min_score E couber nos tetos de tempo/caracteres.
    3. Como a lista já vem ordenada por score, o primeiro artigo abaixo do
       limiar encerra a varredura — todos os seguintes também estariam.

    Referência de calibração (MAX_CHARS_RESUMO=1300 chars ≈ 215 palavras):
      • Intro + outro:  ~45 palavras fixas
      • Por notícia:    título (~10 pal) + overhead (~5 pal) + resumo (~215 pal) ≈ 230 pal
      • 3 notícias:     45 + 3×230 ≈  735 pal ≈ 5.3 min | ~4415 chars
      • 4 notícias:     45 + 4×230 ≈  965 pal ≈ 6.9 min | ~5760 chars
      • 5 notícias:     45 + 5×230 ≈ 1195 pal ≈ 8.5 min | ~7105 chars ← estoura
    """
    max_palavras = max_min * wpm
    palavras_usadas = _PALAVRAS_INTRO_OUTRO
    chars_usados    = _CHARS_INTRO_OUTRO
    selecionadas = []
    ignoradas_score = 0
    parou_por_chars = False
    forcadas_piso   = 0

    for n in noticias:
        score = n.get("score_relevancia", 0)

        conteudo = n.get("conteudo_completo") or n.get("resumo") or ""
        resumo   = resumir_noticia(conteudo, max_chars=MAX_CHARS_RESUMO,
                                  max_chars_tts=MAX_CHARS_TTS_RESUMO)
        titulo   = n.get("titulo", "")

        # Mede o custo REAL no TTS (números por extenso inflam bastante o texto)
        chars_noticia    = (chars_no_tts(titulo) + chars_no_tts(resumo)
                            + _CHARS_OVERHEAD_NOTICIA)
        # Duração estimada a partir dos chars normalizados, não do texto bruto
        palavras_noticia = chars_noticia / _CHARS_POR_PALAVRA

        # ── Piso rígido: as primeiras `min_noticias` entram sempre ────────────
        if len(selecionadas) < min_noticias:
            if score < min_score:
                forcadas_piso += 1
            palavras_usadas += palavras_noticia
            chars_usados    += chars_noticia
            selecionadas.append(n)
            continue

        # ── Acima do piso: score e tetos passam a valer ───────────────────────
        if score < min_score:
            ignoradas_score += 1
            break

        estoura_tempo = palavras_usadas + palavras_noticia > max_palavras
        estoura_chars = chars_usados + chars_noticia > max_chars
        if estoura_tempo or estoura_chars:
            parou_por_chars = estoura_chars and not estoura_tempo
            break

        palavras_usadas += palavras_noticia
        chars_usados    += chars_noticia
        selecionadas.append(n)

    if len(selecionadas) < min_noticias:
        print(f"\n  ⚠️  Só havia {len(selecionadas)} notícia(s) disponível(is) — "
              f"piso de {min_noticias} não pôde ser cumprido.")
    if forcadas_piso:
        print(f"  ⚑ {forcadas_piso} notícia(s) incluída(s) por piso mínimo "
              f"(score < {min_score})")

    tempo_min = palavras_usadas / wpm
    tempo_seg = int((tempo_min % 1) * 60)
    tempo_str = f"{int(tempo_min)}min{tempo_seg:02d}s"

    print(f"\n  ⏱️  Seleção: {len(selecionadas)} notícias selecionadas"
          f"  (min_score={min_score}, tetos: {max_min}min / {max_chars} chars)")
    print(f"       Duração estimada: ~{tempo_str}"
          f"  ({int(palavras_usadas)} palavras @ {wpm} wpm)")
    print(f"       Quota estimada: ~{chars_usados} chars"
          f"  (teto {max_chars} — controle de créditos ElevenLabs)")
    if parou_por_chars:
        print(f"       ⚑ Episódio limitado pelo teto de caracteres (quota), não pelo tempo")
    if ignoradas_score:
        print(f"       {ignoradas_score} artigo(s) ignorados por score < {min_score}")
    for i, n in enumerate(selecionadas, 1):
        sc = n.get("score_relevancia", "?")
        icone = "🔴" if isinstance(sc, int) and sc >= 14 else (
                "🟡" if isinstance(sc, int) and sc >= 8  else "⚪")
        print(f"       {i}. {icone} [{sc:>3}pts] {n['titulo'][:65]}")

    return selecionadas


# ============================================================================
# CONTEÚDO COMPLETO DOS ARTIGOS
# ============================================================================

# ============================================================================
# SELENIUM — DRIVER COMPARTILHADO (reutilizado entre artigos)
# ============================================================================

_selenium_driver = None

def get_selenium_driver(cookies_list):
    """Inicia (ou reutiliza) um driver Selenium com os cookies injetados."""
    global _selenium_driver
    if _selenium_driver:
        return _selenium_driver

    if not SELENIUM_OK:
        return None

    # Em ambiente CI (GitHub Actions) o Chrome headless frequentemente trava
    # na inicialização. O fallback via requests funciona bem — pulamos o Selenium.
    if os.environ.get("GITHUB_ACTIONS"):
        print("  ℹ️  Ambiente CI detectado — usando requests diretamente (sem Selenium)")
        return None

    print("  🔧 Iniciando Chrome (Selenium) para ler artigos completos...")
    opts = Options()
    opts.add_argument("--headless=new")          # invisível, roda em background
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts
        )
        # Abrir Valor primeiro para poder setar cookies no domínio correto
        driver.get("https://valor.globo.com")
        time.sleep(2)

        # Injetar todos os cookies
        for c in cookies_list:
            try:
                driver.add_cookie({
                    "name":   c["name"],
                    "value":  c["value"],
                    "domain": c.get("domain", ".valor.globo.com"),
                    "path":   c.get("path", "/"),
                })
            except Exception:
                pass

        _selenium_driver = driver
        print("  ✅ Chrome pronto com cookies de assinante injetados")
        return driver

    except Exception as e:
        print(f"  ⚠️  Selenium não disponível: {e}")
        return None


def fechar_selenium():
    global _selenium_driver
    if _selenium_driver:
        try:
            _selenium_driver.quit()
        except Exception:
            pass
        _selenium_driver = None


def extrair_texto_selenium(driver, url):
    """
    Navega para o artigo via Selenium, aguarda o JS carregar o conteúdo
    completo (paywall desbloqueado pelos cookies), e extrai o texto.
    """
    try:
        driver.get(url)

        # Aguardar o conteúdo principal aparecer (até 12 segundos)
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "div.mrf-article-body, div.content-text__container, "
                    "div[class*='article-body'], article"
                ))
            )
        except Exception:
            pass  # Continua mesmo sem o seletor ideal

        time.sleep(2)  # Dar tempo extra para o JS do Piano desbloquear

        # Extrair HTML renderizado
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        # Remover elementos de UI/navegação
        for tag in soup.select(
            "script, style, nav, header, footer, aside, "
            ".related-news, .newsletter, [class*='share'], "
            "[class*='comments'], [class*='sidebar'], "
            "[class*='paywall-message'], [class*='subscription-wall']"
        ):
            tag.decompose()

        # Seletores em ordem de preferência (Valor/Globo)
        seletores = [
            "div.mrf-article-body",
            "div[class*='mrf-article']",
            "div.content-text",
            "div[class*='content-text']",
            "div[class*='article-body']",
            "div[itemprop='articleBody']",
            "article",
        ]

        FRASES_PAYWALL = [
            "assine", "seja assinante", "para continuar lendo",
            "conteúdo exclusivo para assinantes", "cadastre-se",
            "faça login para", "acesso restrito", "acompanhe os mercados",
            "acessar gratuitamente",
        ]

        for sel in seletores:
            body = soup.select_one(sel)
            if not body:
                continue

            paras = []
            for el in body.find_all(["p", "h2", "h3", "blockquote"]):
                texto = el.get_text(separator=" ", strip=True)
                if len(texto) < 25:
                    continue
                tl = texto.lower()
                if any(f in tl for f in FRASES_PAYWALL):
                    continue
                paras.append(texto)

            if len(paras) >= 2:
                conteudo = "\n\n".join(paras)
                return conteudo, sel, len(paras)

        # Fallback: todos os <p> com texto substancial
        paras = []
        for p in soup.find_all("p"):
            texto = p.get_text(separator=" ", strip=True)
            if len(texto) < 40:
                continue
            tl = texto.lower()
            if any(f in tl for f in FRASES_PAYWALL):
                continue
            paras.append(texto)

        if paras:
            return "\n\n".join(paras), "fallback-p", len(paras)

        return "", "", 0

    except Exception as e:
        return "", f"erro: {e}", 0


def enriquecer_artigos(session, noticias, top=5, cookies_list=None):
    """
    Usa Selenium para buscar o conteúdo COMPLETO dos artigos.
    O JS do Piano usa os cookies para desbloquear o paywall no browser.
    """
    print(f"\n📄 Buscando conteúdo COMPLETO dos top {top} artigos (via Selenium)...")

    driver = get_selenium_driver(cookies_list or []) if SELENIUM_OK else None

    if not driver:
        print("  ⚠️  Selenium não disponível — usando requests (conteúdo pode ser parcial)")

    ok, parcial, bloqueado = 0, 0, 0

    for n in noticias[:top]:
        print(f"  → {n['titulo'][:65]}...")

        if driver:
            conteudo, seletor, n_paras = extrair_texto_selenium(driver, n["link"])
        else:
            # Fallback para requests se Selenium não disponível
            conteudo, seletor, n_paras = "", "requests-fallback", 0
            try:
                resp = session.get(n["link"], timeout=15)
                soup = BeautifulSoup(resp.text, "html.parser")
                paras = [p.get_text(strip=True) for p in soup.select(
                    "div.mrf-article-body p, div.content-text p"
                ) if len(p.get_text(strip=True)) > 30]
                conteudo = "\n\n".join(paras)
                n_paras = len(paras)
            except Exception:
                pass

        chars = len(conteudo)

        if chars > 800:
            n["conteudo_completo"] = conteudo
            ok += 1
            print(f"    ✅ {n_paras} parágrafos | {chars} chars | [{seletor}]")
        elif chars > 200:
            n["conteudo_completo"] = conteudo
            parcial += 1
            print(f"    ⚠️  Parcial: {n_paras} parágrafos | {chars} chars | [{seletor}]")
        else:
            print(f"    🔒 Paywall ativo — conteúdo não liberado ({chars} chars)")
            bloqueado += 1

        time.sleep(1.5)

    fechar_selenium()
    print(f"\n  📊 {ok} completos | {parcial} parciais | {bloqueado} bloqueados")
    return noticias


# ============================================================================
# FORMATAÇÃO PARA PODCAST
# ============================================================================

def resumir_noticia(conteudo, max_chars=500, max_chars_tts=None):
    """
    Extrai um resumo conciso do conteúdo do artigo (2-3 primeiros parágrafos).

    Dois limites, ambos por frase inteira — nunca corta no meio de uma frase,
    porque fragmento truncado é justamente o que faz o locutor tropeçar:

      max_chars      — tamanho do texto bruto.
      max_chars_tts  — tamanho DEPOIS da normalização para fala. Uma matéria
                       saturada de números infla até 70% ao virar extenso, então
                       o limite bruto sozinho subestimaria a quota. Quando
                       informado, este é o limite que realmente manda.
    """
    if not conteudo:
        return ""

    # Dividir em parágrafos e pegar os mais substanciais
    paragrafos = [p.strip() for p in conteudo.split("\n\n") if len(p.strip()) > 60]
    if not paragrafos:
        paragrafos = [p.strip() for p in conteudo.split("\n") if len(p.strip()) > 60]

    resumo = ""
    for p in paragrafos[:3]:
        # Pegar só as primeiras frases se o parágrafo for muito longo
        frases = re.split(r'(?<=[.!?])\s+', p)
        for frase in frases:
            if len(resumo) + len(frase) + 1 > max_chars:
                break
            candidato = (resumo + (" " if resumo else "") + frase)
            if max_chars_tts and chars_no_tts(candidato) > max_chars_tts:
                return resumo.strip()      # frase seguinte estouraria a quota
            resumo = candidato
        if len(resumo) >= max_chars * 0.7:
            break

    return resumo.strip()


def formatar_para_podcast(noticias):
    """
    Gera o roteiro narrado do episódio.

    Recebe a lista de notícias JÁ selecionadas por selecionar_com_resgate(),
    que garante o piso de MIN_NOTICIAS_EPISODIO (resgatando do pool quando o
    dia rende pouco) e respeita os tetos de tempo e de quota.

    Abertura enxuta, sem cabeçalho de data. Nenhum dígito no roteiro:
    numerais vão por extenso para não travar a narração.
    """
    # Abertura enxuta — sem cabeçalho de data nem apresentação longa.
    # "Jábali" acentuado ajuda o TTS a acertar a tônica na primeira sílaba.
    # O numeral vai por extenso porque dígitos soltos travam a narração.
    q = len(noticias)
    plural = "notícia" if q == 1 else "notícias"
    if q in _CARDINAIS_F:
        abertura = f"Vamos direto às {_CARDINAIS_F[q]} principais {plural}"
    else:
        abertura = "Vamos direto às principais notícias"

    linhas = [
        f"Morning Call Jábali. {abertura}.",
        "",
    ]

    for i, n in enumerate(noticias, 1):
        titulo   = n["titulo"]
        conteudo = n.get("conteudo_completo") or n.get("resumo") or ""
        resumo   = resumir_noticia(conteudo, max_chars=MAX_CHARS_RESUMO,
                                  max_chars_tts=MAX_CHARS_TTS_RESUMO)

        # Rótulo ordinal ("Primeira notícia") soa muito mais natural que
        # "Notícia um" e não deixa dígito algum para o TTS interpretar.
        # Acima da décima usa rótulo neutro — nunca um dígito solto no roteiro.
        rotulo = (f"{_ORDINAIS_F[i]} notícia" if i in _ORDINAIS_F
                  else "A seguir")
        linhas.append(f"{rotulo}. {titulo}.")
        linhas.append("")
        if resumo:
            linhas.append(resumo)
        linhas.append("")

    linhas += [
        "Por hoje é só. Tenha um excelente dia de negócios.",
    ]

    roteiro  = "\n".join(linhas)
    n_chars  = len(roteiro)
    n_words  = len(roteiro.split())
    duracao_min = n_words / WPM_PODCAST
    dur_str  = f"{int(duracao_min)}min{int((duracao_min % 1)*60):02d}s"
    custo    = (n_chars / 1000) * 0.015   # $0.015/1k chars (ElevenLabs pay-as-you-go)

    print(f"\n  📝 Roteiro final: {len(noticias)} notícias | "
          f"{n_words} palavras | ~{dur_str} | "
          f"{n_chars} chars | custo est. ${custo:.3f}")

    return roteiro


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 70)
    print("VALOR ECONÔMICO — SCRAPER DE NOTÍCIAS")
    print(f"Executado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 70)

    config = load_config()
    email    = config.get("valor_economico", {}).get("email", "")
    password = config.get("valor_economico", {}).get("password", "")

    session = requests.Session()
    session.headers.update(HEADERS)

    # Tentar carregar cookies salvos (mais confiável que login via API)
    if os.path.exists(COOKIE_FILE):
        print(f"\n🍪 Carregando cookies salvos de {os.path.basename(COOKIE_FILE)}...")
        try:
            with open(COOKIE_FILE) as f:
                cookies_salvos = json.load(f)
            for c in cookies_salvos:
                session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
            print(f"  ✅ {len(cookies_salvos)} cookies carregados — acesso de assinante ativo")
        except Exception as e:
            print(f"  ⚠️  Erro ao carregar cookies: {e}")
    elif email and password:
        login_globo(session, email, password)
    else:
        print("⚠️  Sem cookies nem credenciais — scraping sem autenticação")

    # Scraping das seções
    noticias = buscar_noticias(session)

    if not noticias:
        print("\n❌ Nenhuma notícia encontrada. Possíveis causas:")
        print("   - Site mudou a estrutura HTML")
        print("   - Bloqueio de IP/bot")
        print("   - Verifique sua conexão com a internet")
        sys.exit(1)

    # Conteúdo completo dos top artigos (via Selenium com cookies)
    cookies_list = []
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE) as f:
            cookies_list = json.load(f)
    noticias = enriquecer_artigos(session, noticias, top=5, cookies_list=cookies_list)

    # Salvar JSON
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.dirname(CONFIG_FILE)
    json_file = os.path.join(base, f"noticias_valor_{ts}.json")
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(noticias[:10], f, ensure_ascii=False, indent=2)
    print(f"\n💾 JSON: {json_file}")

    # Salvar texto do podcast
    texto = formatar_para_podcast(
        selecionar_com_resgate(noticias[:10], pool=carregar_pool()))
    txt_file = os.path.join(base, f"texto_episodio_{ts}.txt")
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(texto)
    print(f"📝 Texto podcast: {txt_file}")

    # Resumo final
    print("\n" + "=" * 70)
    print(f"TOP {min(5, len(noticias))} NOTÍCIAS SELECIONADAS:")
    print("=" * 70)
    for i, n in enumerate(noticias[:5], 1):
        icone = "✅" if n.get("conteudo_completo") else "📄"
        print(f"\n{i}. {icone} [{n['secao']}] {n['titulo']}")
        resumo = (n.get("conteudo_completo") or n.get("resumo") or "")[:120]
        if resumo:
            print(f"   {resumo}...")

    print("\n" + "=" * 70)
    print("✅ CONCLUÍDO!")
    print(f"   {len(noticias)} notícias encontradas → top 5 no texto do podcast")
    print("=" * 70)

    return noticias, texto


if __name__ == "__main__":
    main()
