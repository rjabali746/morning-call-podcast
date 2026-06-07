#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Publicação automática do podcast no GitHub Pages + Spotify.

Fluxo:
  1. Pega o MP3 mais recente da pasta audio/
  2. Faz upload para o repositório GitHub via API
  3. Gera/atualiza o feed.xml com URL pública correta
  4. Spotify detecta o novo episódio automaticamente (até 1h)

Uso:
    python3 publicar_podcast.py                  # usa MP3 mais recente
    python3 publicar_podcast.py caminho/audio.mp3
"""

import os, sys, json, glob, base64, re, struct
from datetime import datetime, timezone
import requests

BASE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
AUDIO_DIR   = os.path.join(BASE, "audio")

GITHUB_API  = "https://api.github.com"


# ============================================================================
# UTILITÁRIOS
# ============================================================================

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)

def get_mp3_mais_recente():
    mp3s = glob.glob(os.path.join(AUDIO_DIR, "*.mp3"))
    if not mp3s:
        print("❌ Nenhum arquivo .mp3 encontrado em audio/")
        print("   Execute primeiro: python3 elevenlabs_tts.py")
        sys.exit(1)
    return max(mp3s, key=os.path.getmtime)

def estimar_duracao_mp3(filepath):
    """Estima a duração do MP3 pelo tamanho do arquivo (bitrate 128kbps padrão)."""
    try:
        size_bytes = os.path.getsize(filepath)
        # 128kbps = 16KB/s
        segundos = size_bytes / 16000
        minutos  = int(segundos // 60)
        segs     = int(segundos % 60)
        return f"{minutos}:{segs:02d}", int(segundos)
    except Exception:
        return "10:00", 600

def data_rfc822(dt=None):
    """Retorna data no formato RFC 822 exigido pelo RSS."""
    if not dt:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# ============================================================================
# GITHUB API
# ============================================================================

class GitHubPublisher:
    def __init__(self, token, usuario, repo, branch="main"):
        self.token   = token
        self.usuario = usuario
        self.repo    = repo
        self.branch  = branch
        self.base    = f"{GITHUB_API}/repos/{usuario}/{repo}"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept":        "application/vnd.github.v3+json",
            "Content-Type":  "application/json",
        }

    def _get(self, path):
        r = requests.get(f"{self.base}{path}", headers=self.headers, timeout=30)
        return r

    def _put(self, path, body):
        r = requests.put(f"{self.base}{path}", headers=self.headers,
                         json=body, timeout=60)
        return r

    def verificar_repo(self):
        """Verifica se o repositório existe e está acessível."""
        r = self._get("")
        if r.status_code == 200:
            data = r.json()
            print(f"  ✅ Repositório: {data['full_name']} ({data['visibility']})")
            return True
        print(f"  ❌ Repositório não acessível: HTTP {r.status_code}")
        print(f"     {r.text[:200]}")
        return False

    def get_sha(self, caminho_repo):
        """Pega o SHA de um arquivo existente (necessário para atualizar)."""
        r = self._get(f"/contents/{caminho_repo}?ref={self.branch}")
        if r.status_code == 200:
            return r.json().get("sha")
        return None

    def upload_arquivo(self, caminho_local, caminho_repo, mensagem_commit):
        """Faz upload (create ou update) de um arquivo para o repositório."""
        with open(caminho_local, "rb") as f:
            conteudo_b64 = base64.b64encode(f.read()).decode("utf-8")

        sha_existente = self.get_sha(caminho_repo)

        body = {
            "message": mensagem_commit,
            "content": conteudo_b64,
            "branch":  self.branch,
        }
        if sha_existente:
            body["sha"] = sha_existente

        r = self._put(f"/contents/{caminho_repo}", body)

        if r.status_code in (200, 201):
            acao = "atualizado" if sha_existente else "criado"
            url  = r.json()["content"]["html_url"]
            print(f"  ✅ {acao}: {caminho_repo}")
            return True, url
        else:
            print(f"  ❌ Erro ao enviar {caminho_repo}: HTTP {r.status_code}")
            print(f"     {r.text[:300]}")
            return False, ""

    def upload_texto(self, conteudo_str, caminho_repo, mensagem_commit):
        """Faz upload de conteúdo textual (ex: feed.xml)."""
        conteudo_b64 = base64.b64encode(conteudo_str.encode("utf-8")).decode("utf-8")
        sha_existente = self.get_sha(caminho_repo)

        body = {
            "message": mensagem_commit,
            "content": conteudo_b64,
            "branch":  self.branch,
        }
        if sha_existente:
            body["sha"] = sha_existente

        r = self._put(f"/contents/{caminho_repo}", body)

        if r.status_code in (200, 201):
            acao = "atualizado" if sha_existente else "criado"
            print(f"  ✅ {acao}: {caminho_repo}")
            return True
        else:
            print(f"  ❌ Erro ao enviar {caminho_repo}: HTTP {r.status_code}")
            print(f"     {r.text[:300]}")
            return False

    def listar_audios_no_repo(self):
        """Lista os arquivos MP3 já no repositório."""
        r = self._get(f"/contents/audio?ref={self.branch}")
        if r.status_code == 200:
            return [f["name"] for f in r.json() if f["name"].endswith(".mp3")]
        return []


# ============================================================================
# GERADOR DE RSS FEED
# ============================================================================

def gerar_feed_xml(episodios, pages_url, podcast_config):
    """
    Gera RSS feed válido para Spotify com todos os episódios.
    episodios: lista de dicts com {nome_arquivo, titulo, descricao, data, duracao, tamanho}
    """
    imagem_url = podcast_config.get(
        "imagem_url",
        f"{pages_url}/cover.jpg"
    )

    linhas = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"',
        '  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"',
        '  xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        '  <channel>',
        f'    <title>{podcast_config["titulo"]}</title>',
        f'    <link>{pages_url}</link>',
        f'    <description>{podcast_config["descricao"]}</description>',
        f'    <language>pt-br</language>',
        f'    <itunes:author>{podcast_config["autor"]}</itunes:author>',
        f'    <itunes:explicit>false</itunes:explicit>',
        f'    <itunes:category text="Business"/>',
        f'    <itunes:image href="{imagem_url}"/>',
        f'    <image>',
        f'      <url>{imagem_url}</url>',
        f'      <title>{podcast_config["titulo"]}</title>',
        f'      <link>{pages_url}</link>',
        f'    </image>',
    ]

    for ep in episodios:
        audio_url = f"{pages_url}/audio/{ep['nome_arquivo']}"
        linhas += [
            '    <item>',
            f'      <title>{ep["titulo"]}</title>',
            f'      <description>{ep["descricao"]}</description>',
            f'      <pubDate>{ep["data"]}</pubDate>',
            f'      <guid isPermaLink="false">{audio_url}</guid>',
            f'      <enclosure url="{audio_url}"',
            f'                 length="{ep["tamanho"]}"',
            f'                 type="audio/mpeg"/>',
            f'      <itunes:duration>{ep["duracao"]}</itunes:duration>',
            f'      <itunes:explicit>false</itunes:explicit>',
            f'      <itunes:author>{podcast_config["autor"]}</itunes:author>',
            '    </item>',
        ]

    linhas += ['  </channel>', '</rss>']
    return "\n".join(linhas)


# ============================================================================
# MAIN
# ============================================================================

def publicar(mp3_path=None):
    print("=" * 65)
    print("PUBLICAÇÃO DO PODCAST — GITHUB PAGES + SPOTIFY")
    print(f"Executado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 65)

    config    = load_config()
    gh_config = config.get("github", {})
    token     = gh_config.get("token", "")
    usuario   = gh_config.get("usuario", "")
    repo      = gh_config.get("repo", "")
    branch    = gh_config.get("branch", "main")
    pages_url = gh_config.get("pages_url", "").rstrip("/")

    if not all([token, usuario, repo, pages_url]):
        print("❌ Configuração do GitHub incompleta no config.json")
        sys.exit(1)

    podcast_config = {
        "titulo":    "Morning Call Jabali",
        "descricao": "Resumo diário das principais notícias de crédito, "
                     "pagamentos e finanças do Valor Econômico.",
        "autor":     "Roberto Jabali",
        "imagem_url": f"{pages_url}/cover.jpg",
    }

    # Arquivo de áudio
    if not mp3_path:
        mp3_path = get_mp3_mais_recente()

    nome_mp3   = os.path.basename(mp3_path)
    tamanho    = os.path.getsize(mp3_path)
    duracao_str, duracao_seg = estimar_duracao_mp3(mp3_path)
    tamanho_mb = tamanho / (1024 * 1024)

    # Gerar título do episódio a partir do nome do arquivo
    # ex: podcast_20260605_161551.mp3 → "Morning Call - 05/06/2026"
    match = re.search(r'(\d{4})(\d{2})(\d{2})', nome_mp3)
    if match:
        y, m, d = match.groups()
        data_ep  = datetime(int(y), int(m), int(d), 6, 30, 0, tzinfo=timezone.utc)
        titulo_ep = f"Morning Call - {d}/{m}/{y}"
    else:
        data_ep  = datetime.now(timezone.utc)
        titulo_ep = f"Morning Call - {data_ep.strftime('%d/%m/%Y')}"

    print(f"\n📻 Episódio: {titulo_ep}")
    print(f"   Arquivo:  {nome_mp3} ({tamanho_mb:.1f} MB | ~{duracao_str})")

    # Iniciar GitHub publisher
    print(f"\n🐙 Conectando ao GitHub ({usuario}/{repo})...")
    gh = GitHubPublisher(token, usuario, repo, branch)

    if not gh.verificar_repo():
        sys.exit(1)

    # PASSO 1: Upload do MP3
    print(f"\n📤 Enviando áudio para GitHub...")
    ok_mp3, _ = gh.upload_arquivo(
        caminho_local  = mp3_path,
        caminho_repo   = f"audio/{nome_mp3}",
        mensagem_commit = f"🎙️ Novo episódio: {titulo_ep}"
    )
    if not ok_mp3:
        print("❌ Falha ao enviar o MP3. Abortando.")
        sys.exit(1)

    # PASSO 2: Listar todos os episódios no repositório para montar o feed
    print(f"\n📋 Listando episódios no repositório...")
    audios_no_repo = gh.listar_audios_no_repo()

    # Garantir que o episódio atual está na lista
    if nome_mp3 not in audios_no_repo:
        audios_no_repo.append(nome_mp3)

    audios_no_repo.sort(reverse=True)  # mais recente primeiro
    print(f"  📊 {len(audios_no_repo)} episódio(s) no total")

    # Construir lista de episódios para o feed
    episodios = []
    for nome in audios_no_repo:
        m = re.search(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', nome)
        if m:
            y,mo,d,h,mi,s = m.groups()
            dt = datetime(int(y),int(mo),int(d),int(h),int(mi),int(s), tzinfo=timezone.utc)
            titulo = f"Morning Call - {d}/{mo}/{y}"
        else:
            dt = datetime.now(timezone.utc)
            titulo = f"Morning Call - {dt.strftime('%d/%m/%Y')}"

        # Tamanho real só temos para o episódio atual
        if nome == nome_mp3:
            tam = tamanho
            dur = duracao_str
        else:
            tam = 0
            dur = "10:00"

        episodios.append({
            "nome_arquivo": nome,
            "titulo":       titulo,
            "descricao":    f"Principais notícias de crédito e finanças - {titulo}",
            "data":         data_rfc822(dt),
            "duracao":      dur,
            "tamanho":      tam,
        })

    # PASSO 3: Gerar e fazer upload do feed.xml
    print(f"\n📡 Atualizando feed RSS...")
    feed_xml = gerar_feed_xml(episodios, pages_url, podcast_config)

    ok_feed = gh.upload_texto(
        conteudo_str    = feed_xml,
        caminho_repo    = "feed.xml",
        mensagem_commit = f"📡 Atualiza feed RSS — {titulo_ep}"
    )

    # Também salvar localmente
    feed_local = os.path.join(BASE, "feed.xml")
    with open(feed_local, "w", encoding="utf-8") as f:
        f.write(feed_xml)
    print(f"  💾 feed.xml salvo localmente também")

    # PASSO 4: Resultado final
    feed_url  = f"{pages_url}/feed.xml"
    audio_url = f"{pages_url}/audio/{nome_mp3}"

    print(f"\n{'='*65}")
    print(f"✅ PUBLICADO COM SUCESSO!")
    print(f"{'='*65}")
    print(f"\n  🎙️  Episódio: {titulo_ep}")
    print(f"  🔊 Áudio:    {audio_url}")
    print(f"  📡 Feed RSS: {feed_url}")
    print(f"\n  ⏰ O Spotify detecta o novo episódio em até 1 hora.")
    print(f"\n{'='*65}")
    print(f"PRÓXIMO PASSO — Adicionar feed ao Spotify (só na 1ª vez):")
    print(f"  1. Acesse: https://creators.spotify.com/")
    print(f"  2. Crie um novo podcast → 'Tenho um feed RSS'")
    print(f"  3. Cole: {feed_url}")
    print(f"{'='*65}\n")

    return feed_url, audio_url


if __name__ == "__main__":
    mp3 = sys.argv[1] if len(sys.argv) > 1 else None
    publicar(mp3_path=mp3)
