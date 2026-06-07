#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Morning Call Jabali — Pipeline Completo
=======================================
Orquestra as 3 etapas do podcast de forma resiliente:
  1. Scraping do Valor Econômico (Selenium + cookies)
  2. Geração de áudio via ElevenLabs (voz Daniel)
  3. Publicação no GitHub Pages (feed RSS → Spotify)

Funciona em dois modos:
  • Local:          lê configuração de config.json
  • GitHub Actions: lê de variáveis de ambiente (secrets do repositório)

Variáveis de ambiente (GitHub Actions):
  ELEVENLABS_API_KEY    — API key do ElevenLabs
  VALOR_COOKIES_JSON    — conteúdo do valor_cookies.json (JSON string)
  GH_TOKEN              — token do GitHub (GITHUB_TOKEN automático)
  GH_USUARIO            — usuário GitHub (ex: rjabali746)
  GH_REPO               — repositório (ex: morning-call-podcast)
  GH_PAGES_URL          — URL do GitHub Pages
"""

import os
import sys
import json
import time
import glob
import logging
import traceback
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# BASE DIR — funciona tanto localmente quanto no GitHub Actions
# ─────────────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent.resolve()

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING — console + arquivo pipeline.log
# ─────────────────────────────────────────────────────────────────────────────
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

_fh = logging.FileHandler(BASE / "pipeline.log", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_sh, _fh])
log = logging.getLogger("pipeline")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """
    Carrega configuração.
    Prioridade: variáveis de ambiente (GitHub Actions) → config.json (local)
    """
    if os.environ.get("ELEVENLABS_API_KEY"):
        log.info("📋 Modo: GitHub Actions (variáveis de ambiente)")
        return {
            "elevenlabs": {
                "api_key":  os.environ["ELEVENLABS_API_KEY"],
                "voice_id": os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9"),
                "model":    "eleven_multilingual_v2",
            },
            "github": {
                "token":     os.environ.get("GH_TOKEN", ""),
                "usuario":   os.environ.get("GH_USUARIO", "rjabali746"),
                "repo":      os.environ.get("GH_REPO", "morning-call-podcast"),
                "branch":    "main",
                "pages_url": os.environ.get(
                    "GH_PAGES_URL",
                    "https://rjabali746.github.io/morning-call-podcast"
                ),
            },
        }

    config_path = BASE / "config.json"
    if config_path.exists():
        log.info("📋 Modo: local (config.json)")
        with open(config_path) as f:
            return json.load(f)

    raise RuntimeError("Nenhuma configuração encontrada (env vars ou config.json)")


def preparar_cookies():
    """
    GitHub Actions: escreve cookies a partir de VALOR_COOKIES_JSON.
    Local: valor_cookies.json já existe.
    """
    cookies_env = os.environ.get("VALOR_COOKIES_JSON", "").strip()
    cookie_file = BASE / "valor_cookies.json"

    if cookies_env:
        with open(cookie_file, "w") as f:
            f.write(cookies_env)
        log.info("🍪 Cookies escritos a partir de VALOR_COOKIES_JSON")
    elif cookie_file.exists():
        log.info(f"🍪 Usando cookies locais: {cookie_file.name}")
    else:
        log.warning("⚠️  valor_cookies.json não encontrado — conteúdo pode ser parcial")


# ─────────────────────────────────────────────────────────────────────────────
# RETRY E FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

def com_retentativas(fn, tentativas: int = 3, espera: int = 30, nome: str = ""):
    """Executa fn() com até `tentativas` retentativas."""
    ultimo_erro = None
    for i in range(1, tentativas + 1):
        try:
            log.info(f"  ▶ Tentativa {i}/{tentativas}...")
            return fn()
        except Exception as e:
            ultimo_erro = e
            log.warning(f"  ⚠️  Tentativa {i} falhou: {e}")
            if i < tentativas:
                log.info(f"  ⏳ Aguardando {espera}s...")
                time.sleep(espera)

    raise RuntimeError(
        f"'{nome}' falhou após {tentativas} tentativas. Último erro: {ultimo_erro}"
    )


def carregar_noticias_fallback():
    """Retorna o JSON de notícias mais recente salvo localmente (fallback)."""
    jsons = sorted(
        glob.glob(str(BASE / "noticias_valor_*.json")),
        key=os.path.getmtime,
        reverse=True
    )
    if jsons:
        nome = Path(jsons[0]).name
        log.warning(f"  📦 Fallback: usando notícias de {nome}")
        with open(jsons[0]) as f:
            return json.load(f)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ETAPA 1 — SCRAPING
# ─────────────────────────────────────────────────────────────────────────────

def etapa_scraping() -> str:
    """Faz scraping do Valor, salva JSON e roteiro. Retorna caminho do .txt."""
    import requests as req
    from valor_economico_scraper import (
        buscar_noticias,
        enriquecer_artigos,
        formatar_para_podcast,
        HEADERS,
    )

    session = req.Session()
    session.headers.update(HEADERS)

    cookie_file  = BASE / "valor_cookies.json"
    cookies_list = []
    if cookie_file.exists():
        with open(cookie_file) as f:
            cookies_list = json.load(f)
        for c in cookies_list:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    noticias = buscar_noticias(session)
    if not noticias:
        raise ValueError("Nenhuma notícia encontrada")

    noticias  = enriquecer_artigos(session, noticias, top=5, cookies_list=cookies_list)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = BASE / f"noticias_valor_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(noticias[:10], f, ensure_ascii=False, indent=2)

    texto    = formatar_para_podcast(noticias, max_noticias=5)
    txt_path = BASE / f"texto_episodio_{ts}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(texto)

    log.info(f"  ✅ {len(noticias)} notícias | roteiro: {txt_path.name}")
    return str(txt_path)


# ─────────────────────────────────────────────────────────────────────────────
# ETAPA 2 — TTS (ElevenLabs)
# ─────────────────────────────────────────────────────────────────────────────

def etapa_tts(txt_path: str, config: dict) -> str:
    """Converte roteiro em MP3 via ElevenLabs. Retorna caminho do .mp3."""
    from elevenlabs_tts import (
        limpar_texto_para_audio,
        dividir_em_chunks,
        gerar_chunk_audio,
        verificar_conta,
    )

    el       = config["elevenlabs"]
    api_key  = el["api_key"]
    voice_id = el["voice_id"]
    model    = el["model"]

    restante = verificar_conta(api_key)

    with open(txt_path, encoding="utf-8") as f:
        texto_bruto = f.read()
    texto   = limpar_texto_para_audio(texto_bruto)
    n_chars = len(texto)
    log.info(f"  📝 {n_chars:,} chars | ~{n_chars // 150} min de áudio estimado")

    if restante is not None and n_chars > restante:
        raise RuntimeError(
            f"Saldo insuficiente: {n_chars} chars necessários, {restante} disponíveis"
        )

    chunks    = dividir_em_chunks(texto)
    audio_dir = BASE / "audio"
    audio_dir.mkdir(exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    mp3_path  = audio_dir / f"podcast_{ts}.mp3"

    audio_bytes = b""
    for i, chunk in enumerate(chunks, 1):
        log.info(f"  [{i}/{len(chunks)}] {len(chunk):,} chars...")
        audio_bytes += gerar_chunk_audio(api_key, voice_id, model, chunk)

    with open(mp3_path, "wb") as f:
        f.write(audio_bytes)

    tamanho_mb = mp3_path.stat().st_size / (1024 * 1024)
    log.info(f"  ✅ {mp3_path.name} ({tamanho_mb:.1f} MB)")
    return str(mp3_path)


# ─────────────────────────────────────────────────────────────────────────────
# ETAPA 3 — PUBLICAÇÃO (GitHub Pages)
# ─────────────────────────────────────────────────────────────────────────────

def etapa_publicacao(mp3_path: str, config: dict):
    """Upload do MP3 e atualização do feed.xml no GitHub Pages."""
    import re
    from datetime import timezone, datetime as dt
    from publicar_podcast import (
        GitHubPublisher,
        gerar_feed_xml,
        estimar_duracao_mp3,
        data_rfc822,
    )

    gh_cfg    = config["github"]
    token     = os.environ.get("GH_TOKEN") or gh_cfg.get("token", "")
    usuario   = gh_cfg["usuario"]
    repo      = gh_cfg["repo"]
    branch    = gh_cfg.get("branch", "main")
    pages_url = gh_cfg["pages_url"].rstrip("/")

    if not token:
        raise RuntimeError("Token do GitHub não encontrado")

    podcast_config = {
        "titulo":     "Morning Call Jabali",
        "descricao":  "Resumo diário das principais notícias de crédito, "
                      "pagamentos e finanças do Valor Econômico.",
        "autor":      "Roberto Jabali",
        "imagem_url": f"{pages_url}/cover.jpg",
    }

    nome_mp3       = Path(mp3_path).name
    tamanho        = Path(mp3_path).stat().st_size
    duracao_str, _ = estimar_duracao_mp3(mp3_path)

    match = re.search(r"(\d{4})(\d{2})(\d{2})", nome_mp3)
    if match:
        y, m, d   = match.groups()
        data_ep   = dt(int(y), int(m), int(d), 6, 30, 0, tzinfo=timezone.utc)
        titulo_ep = f"Morning Call - {d}/{m}/{y}"
    else:
        data_ep   = dt.now(timezone.utc)
        titulo_ep = f"Morning Call - {data_ep.strftime('%d/%m/%Y')}"

    log.info(f"  📻 Episódio: {titulo_ep}")

    gh = GitHubPublisher(token, usuario, repo, branch)
    if not gh.verificar_repo():
        raise RuntimeError(f"Repositório {usuario}/{repo} não acessível")

    # Upload MP3
    ok_mp3, _ = gh.upload_arquivo(
        caminho_local   = mp3_path,
        caminho_repo    = f"audio/{nome_mp3}",
        mensagem_commit = f"🎙️ Novo episódio: {titulo_ep}",
    )
    if not ok_mp3:
        raise RuntimeError("Falha ao enviar o MP3 para o GitHub")

    # Feed RSS
    audios = gh.listar_audios_no_repo()
    if nome_mp3 not in audios:
        audios.append(nome_mp3)
    audios.sort(reverse=True)

    episodios = []
    for nome in audios:
        mm = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", nome)
        if mm:
            y2, mo2, d2, h2, mi2, s2 = mm.groups()
            dt2     = dt(int(y2), int(mo2), int(d2),
                         int(h2), int(mi2), int(s2), tzinfo=timezone.utc)
            titulo2 = f"Morning Call - {d2}/{mo2}/{y2}"
        else:
            dt2     = dt.now(timezone.utc)
            titulo2 = f"Morning Call - {dt2.strftime('%d/%m/%Y')}"

        episodios.append({
            "nome_arquivo": nome,
            "titulo":       titulo2,
            "descricao":    f"Principais notícias de crédito e finanças - {titulo2}",
            "data":         data_rfc822(dt2),
            "duracao":      duracao_str if nome == nome_mp3 else "10:00",
            "tamanho":      tamanho     if nome == nome_mp3 else 0,
        })

    feed_xml = gerar_feed_xml(episodios, pages_url, podcast_config)
    ok_feed  = gh.upload_texto(
        conteudo_str    = feed_xml,
        caminho_repo    = "feed.xml",
        mensagem_commit = f"📡 Atualiza feed RSS — {titulo_ep}",
    )
    if not ok_feed:
        raise RuntimeError("Falha ao atualizar o feed.xml")

    with open(BASE / "feed.xml", "w", encoding="utf-8") as f:
        f.write(feed_xml)

    log.info(f"  ✅ Publicado!")
    log.info(f"     🔊 {pages_url}/audio/{nome_mp3}")
    log.info(f"     📡 {pages_url}/feed.xml")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    inicio = datetime.now()
    log.info("")
    log.info("=" * 60)
    log.info("🎙️  MORNING CALL JABALI — PIPELINE COMPLETO")
    log.info(f"📅 {inicio.strftime('%d/%m/%Y %H:%M:%S')}")
    log.info("=" * 60)

    config = load_config()
    preparar_cookies()

    # ── ETAPA 1: Scraping ────────────────────────────────────────────────────
    log.info("\n── ETAPA 1/3: SCRAPING ─────────────────────────────────────")
    txt_path = None
    try:
        txt_path = com_retentativas(
            etapa_scraping, tentativas=3, espera=30, nome="Scraping"
        )
    except RuntimeError as e:
        log.error(str(e))
        log.warning("🔄 Ativando fallback: notícias do dia anterior...")
        noticias_fb = carregar_noticias_fallback()
        if not noticias_fb:
            log.error("❌ Sem fallback disponível. Pipeline abortado.")
            sys.exit(1)
        from valor_economico_scraper import formatar_para_podcast
        texto    = formatar_para_podcast(noticias_fb, max_noticias=5)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        txt_path = str(BASE / f"texto_episodio_{ts}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(texto)
        log.info(f"  📄 Roteiro de fallback: {Path(txt_path).name}")

    # ── ETAPA 2: TTS ─────────────────────────────────────────────────────────
    log.info("\n── ETAPA 2/3: TTS (ElevenLabs) ─────────────────────────────")
    mp3_path = None
    _txt     = txt_path
    _cfg     = config
    try:
        mp3_path = com_retentativas(
            lambda: etapa_tts(_txt, _cfg),
            tentativas=3, espera=20, nome="TTS"
        )
    except RuntimeError as e:
        log.error(str(e))
        log.error("❌ Geração de áudio falhou. Pipeline abortado.")
        sys.exit(1)

    # ── ETAPA 3: Publicação ───────────────────────────────────────────────────
    log.info("\n── ETAPA 3/3: PUBLICAÇÃO (GitHub Pages) ─────────────────────")
    _mp3 = mp3_path
    try:
        com_retentativas(
            lambda: etapa_publicacao(_mp3, _cfg),
            tentativas=3, espera=15, nome="Publicação"
        )
    except RuntimeError as e:
        log.error(str(e))
        log.error("❌ Publicação falhou. Pipeline abortado.")
        sys.exit(1)

    # ── Resumo ────────────────────────────────────────────────────────────────
    duracao = int((datetime.now() - inicio).total_seconds())
    log.info("")
    log.info("=" * 60)
    log.info(f"✅ PIPELINE CONCLUÍDO COM SUCESSO em {duracao}s")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
