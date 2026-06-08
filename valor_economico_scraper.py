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
from datetime import datetime
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

# ── Parâmetros de tempo do podcast ────────────────────────────────────────────
WPM_PODCAST      = 140   # palavras por minuto narradas em português (ElevenLabs)
MAX_MIN_PODCAST  = 20    # duração máxima do episódio em minutos
MAX_CHARS_RESUMO = 1500  # caracteres máx por resumo de notícia (~250 palavras)
# Palavras fixas de intro + outro (estimativa)
_PALAVRAS_INTRO_OUTRO = 70

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
        return sum(1 for p in PALAVRAS_CHAVE if p in texto), []

    # ── Pontuação por tema ────────────────────────────────────────────────────
    for tema in perfil.get("temas", []):
        nome     = tema.get("nome", "?")
        peso     = tema.get("peso", 1)
        palavras = tema.get("palavras", [])
        matches  = [p for p in palavras if p.lower() in texto]
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
            if entidade.lower() in texto:
                score += bonus_cfg
                detalhes.append(f"{tier_key}(+{bonus_cfg}:{entidade.strip()})")

    # ── Penalizações ─────────────────────────────────────────────────────────
    for pen in perfil.get("penalizacoes", []):
        peso_pen = pen.get("peso", -2)
        for palavra in pen.get("palavras", []):
            if palavra.lower() in texto:
                score += peso_pen
                detalhes.append(f"penalidade({peso_pen}:{palavra})")
                break

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
    # 1. Palavras-chave base
    if any(p in t for p in PALAVRAS_CHAVE):
        return True
    # 2. Palavras do perfil personalizado (se disponível)
    perfil = carregar_perfil()
    if perfil:
        for palavra in _todas_palavras_perfil(perfil):
            if palavra in t:
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

def selecionar_por_tempo(noticias, max_min=MAX_MIN_PODCAST, wpm=WPM_PODCAST, min_score=6):
    """
    Seleciona notícias enriquecidas em ordem de score até o limite de tempo.

    Regras:
    1. Só inclui artigos com score_relevancia ≥ min_score (filtra ruído).
    2. Como a lista JÁ está ordenada por score (maior → menor), ao encontrar
       o primeiro artigo abaixo do limiar encerra — todos os seguintes também estarão.
    3. Adiciona greedy até que a próxima notícia ultrapassaria max_min.

    Referência de calibração (com MAX_CHARS_RESUMO=1500 chars ≈ 250 palavras):
      • Intro + outro:  ~70 palavras fixas
      • Por notícia:    título (~10 pal) + overhead (~5 pal) + resumo (~250 pal) ≈ 265 pal
      • 10 notícias:    70 + 10×265 ≈ 2720 pal ≈ 19.4 min  ← teto
      •  8 notícias:    70 +  8×265 ≈ 2190 pal ≈ 15.6 min  ← alvo
    """
    max_palavras = max_min * wpm
    palavras_usadas = _PALAVRAS_INTRO_OUTRO
    selecionadas = []
    ignoradas_score = 0

    for n in noticias:
        score = n.get("score_relevancia", 0)

        # Lista ordenada por score: primeiro artigo abaixo do limiar encerra
        if score < min_score:
            ignoradas_score += 1
            break

        conteudo = n.get("conteudo_completo") or n.get("resumo") or ""
        resumo   = resumir_noticia(conteudo, max_chars=MAX_CHARS_RESUMO)
        titulo   = n.get("titulo", "")

        palavras_noticia = len(titulo.split()) + 5 + len(resumo.split())

        if palavras_usadas + palavras_noticia > max_palavras:
            break  # próxima notícia ultrapassaria o limite de tempo

        palavras_usadas += palavras_noticia
        selecionadas.append(n)

    tempo_min = palavras_usadas / wpm
    tempo_seg = int((tempo_min % 1) * 60)
    tempo_str = f"{int(tempo_min)}min{tempo_seg:02d}s"

    print(f"\n  ⏱️  Seleção por tempo: {len(selecionadas)} notícias selecionadas"
          f"  (min_score={min_score}, max={max_min}min)")
    print(f"       Duração estimada: ~{tempo_str}"
          f"  ({palavras_usadas} palavras @ {wpm} wpm)")
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

def resumir_noticia(conteudo, max_chars=500):
    """
    Extrai um resumo conciso de até max_chars do conteúdo do artigo.
    Pega os 2-3 primeiros parágrafos mais relevantes.
    Resultado ideal: 2-4 frases que capturam a essência da notícia.
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
            if len(resumo) + len(frase) + 1 <= max_chars:
                resumo += (" " if resumo else "") + frase
            else:
                break
        if len(resumo) >= max_chars * 0.7:
            break

    return resumo.strip()


def formatar_para_podcast(noticias):
    """
    Gera o roteiro narrado do episódio.

    Recebe a lista de notícias JÁ selecionadas por selecionar_por_tempo()
    — sem cap fixo de quantidade, o limitador é o tempo.

    Resumo por notícia: até MAX_CHARS_RESUMO chars (~250 palavras),
    suficiente para episódios de 15-20 minutos com 8-10 notícias.
    """
    meses = ["janeiro","fevereiro","março","abril","maio","junho",
             "julho","agosto","setembro","outubro","novembro","dezembro"]
    hoje = datetime.now()
    dia_semana = ["Segunda-feira","Terça-feira","Quarta-feira","Quinta-feira",
                  "Sexta-feira","Sábado","Domingo"][hoje.weekday()]
    data_str = f"{dia_semana}, {hoje.day} de {meses[hoje.month-1]} de {hoje.year}"

    linhas = [
        f"Morning Call Jabali. {data_str}.",
        "",
        "Bom dia! Você está ouvindo o Morning Call Jabali, seu resumo diário das principais notícias "
        "de crédito, finanças e mercados do Valor Econômico. Vamos direto ao ponto.",
        "",
    ]

    for i, n in enumerate(noticias, 1):
        titulo   = n["titulo"]
        conteudo = n.get("conteudo_completo") or n.get("resumo") or ""
        resumo   = resumir_noticia(conteudo, max_chars=MAX_CHARS_RESUMO)

        linhas.append(f"Notícia {i}. {titulo}.")
        linhas.append("")
        if resumo:
            linhas.append(resumo)
        linhas.append("")

    linhas += [
        f"Essas foram as {len(noticias)} principais notícias de hoje do Valor Econômico.",
        "Tenha um excelente dia de negócios. Até amanhã!",
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
    texto = formatar_para_podcast(noticias, max_noticias=5)
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
