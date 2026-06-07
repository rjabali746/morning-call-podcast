#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geração de áudio profissional via ElevenLabs API.
Voz: Daniel (pt-BR) — qualidade profissional de podcast.

Uso:
    python3 elevenlabs_tts.py                  # usa o texto mais recente
    python3 elevenlabs_tts.py arquivo.txt      # usa arquivo específico
    python3 elevenlabs_tts.py --listar-vozes   # lista vozes disponíveis
"""

import os, sys, json, glob, re, requests
from datetime import datetime

BASE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
AUDIO_DIR   = os.path.join(BASE, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"

# Limite de chars por chamada API (ElevenLabs aceita até 5000)
CHUNK_SIZE = 4800


# ============================================================================
# UTILITÁRIOS
# ============================================================================

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)

def get_texto_mais_recente():
    arquivos = glob.glob(os.path.join(BASE, "texto_episodio_*.txt"))
    if not arquivos:
        print("❌ Nenhum arquivo texto_episodio_*.txt encontrado!")
        print("   Execute primeiro: python3 valor_economico_scraper.py")
        sys.exit(1)
    return max(arquivos, key=os.path.getmtime)

def limpar_texto_para_audio(texto):
    """Remove elementos visuais que ficam estranhos no áudio."""
    texto = re.sub(r"={3,}", "", texto)
    texto = re.sub(r"-{3,}", "", texto)
    texto = re.sub(r"\bhttps?://\S+", "", texto)
    texto = re.sub(r"\*+", "", texto)
    texto = re.sub(r"#{1,6}\s*", "", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()

def dividir_em_chunks(texto, tamanho=CHUNK_SIZE):
    """
    Divide o texto em chunks respeitando parágrafos e frases.
    ElevenLabs tem limite de ~5000 chars por chamada.
    """
    if len(texto) <= tamanho:
        return [texto]

    chunks = []
    paragrafos = texto.split("\n\n")
    chunk_atual = ""

    for paragrafo in paragrafos:
        if len(chunk_atual) + len(paragrafo) + 2 <= tamanho:
            chunk_atual += ("\n\n" if chunk_atual else "") + paragrafo
        else:
            if chunk_atual:
                chunks.append(chunk_atual)
            # Se parágrafo sozinho é maior que o limite, divide por frases
            if len(paragrafo) > tamanho:
                frases = re.split(r'(?<=[.!?])\s+', paragrafo)
                chunk_atual = ""
                for frase in frases:
                    if len(chunk_atual) + len(frase) + 1 <= tamanho:
                        chunk_atual += (" " if chunk_atual else "") + frase
                    else:
                        if chunk_atual:
                            chunks.append(chunk_atual)
                        chunk_atual = frase
            else:
                chunk_atual = paragrafo

    if chunk_atual:
        chunks.append(chunk_atual)

    return chunks


# ============================================================================
# ELEVENLABS API
# ============================================================================

def listar_vozes(api_key):
    """Lista todas as vozes disponíveis na conta."""
    resp = requests.get(
        f"{ELEVENLABS_BASE}/voices",
        headers={"xi-api-key": api_key}
    )
    resp.raise_for_status()
    vozes = resp.json().get("voices", [])
    print(f"\n🎙️  {len(vozes)} vozes disponíveis:\n")
    for v in vozes:
        labels = v.get("labels", {})
        lang   = labels.get("language", "")
        acc    = labels.get("accent", "")
        gen    = labels.get("gender", "")
        print(f"  ID: {v['voice_id']}")
        print(f"  Nome: {v['name']} | {lang} {acc} {gen}")
        print()
    return vozes

def verificar_conta(api_key):
    """Verifica saldo de caracteres disponíveis."""
    try:
        resp = requests.get(
            f"{ELEVENLABS_BASE}/user/subscription",
            headers={"xi-api-key": api_key},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            usado    = data.get("character_count", 0)
            limite   = data.get("character_limit", 0)
            restante = limite - usado
            plano    = data.get("tier", "unknown")
            print(f"  📊 Plano: {plano} | Usado: {usado:,} | Restante: {restante:,} chars")
            return restante
    except Exception as e:
        print(f"  ⚠️  Não foi possível verificar saldo: {e}")
    return None

def gerar_chunk_audio(api_key, voice_id, model, texto):
    """Gera áudio para um chunk de texto via ElevenLabs API."""
    url  = f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}"
    body = {
        "text": texto,
        "model_id": model,
        "voice_settings": {
            "stability":        0.55,   # 0-1: mais alto = mais consistente
            "similarity_boost": 0.80,   # 0-1: mais alto = mais fiel à voz
            "style":            0.20,   # expressividade
            "use_speaker_boost": True
        }
    }
    headers = {
        "xi-api-key":   api_key,
        "Content-Type": "application/json",
        "Accept":       "audio/mpeg"
    }

    resp = requests.post(url, json=body, headers=headers, timeout=60)

    if resp.status_code != 200:
        raise Exception(f"ElevenLabs API erro {resp.status_code}: {resp.text[:200]}")

    return resp.content  # bytes do MP3


# ============================================================================
# GERAÇÃO COMPLETA
# ============================================================================

def gerar_audio(txt_file=None):
    print("=" * 65)
    print("ELEVENLABS TTS — MORNING CALL JABALI")
    print(f"Executado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 65)

    config    = load_config()
    el_config = config.get("elevenlabs", {})
    api_key   = el_config.get("api_key", "")
    voice_id  = el_config.get("voice_id", "onwK4e9ZLuTAKqWW03F9")
    model     = el_config.get("model", "eleven_multilingual_v2")

    if not api_key:
        print("❌ API key do ElevenLabs não encontrada no config.json")
        sys.exit(1)

    # Verificar conta
    print(f"\n🔑 Verificando conta ElevenLabs...")
    restante = verificar_conta(api_key)

    # Arquivo de texto
    if not txt_file:
        txt_file = get_texto_mais_recente()

    print(f"\n📄 Texto: {os.path.basename(txt_file)}")
    with open(txt_file, "r", encoding="utf-8") as f:
        texto_bruto = f.read()

    texto = limpar_texto_para_audio(texto_bruto)
    n_chars = len(texto)
    print(f"   {n_chars:,} chars | ~{n_chars // 150} minutos de áudio estimado")

    if restante is not None and n_chars > restante:
        print(f"⚠️  Atenção: texto ({n_chars} chars) > saldo disponível ({restante} chars)")
        resp = input("   Continuar mesmo assim? (s/n): ")
        if resp.lower() != "s":
            sys.exit(0)

    # Dividir em chunks se necessário
    chunks = dividir_em_chunks(texto)
    print(f"\n🔊 Gerando áudio em {len(chunks)} parte(s)...")

    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    mp3_final  = os.path.join(AUDIO_DIR, f"podcast_{ts}.mp3")

    audio_bytes = b""
    for i, chunk in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] {len(chunk):,} chars... ", end="", flush=True)
        t0 = datetime.now()
        try:
            audio_bytes += gerar_chunk_audio(api_key, voice_id, model, chunk)
            secs = (datetime.now() - t0).seconds
            print(f"✅ ({secs}s)")
        except Exception as e:
            print(f"❌ {e}")
            if audio_bytes:
                print("   Salvando o que foi gerado até agora...")
            break

    if not audio_bytes:
        print("❌ Nenhum áudio gerado.")
        sys.exit(1)

    # Salvar MP3
    with open(mp3_final, "wb") as f:
        f.write(audio_bytes)

    tamanho_mb = os.path.getsize(mp3_final) / (1024 * 1024)

    print(f"\n" + "=" * 65)
    print(f"✅ ÁUDIO GERADO COM SUCESSO!")
    print(f"   Arquivo: {os.path.basename(mp3_final)}")
    print(f"   Tamanho: {tamanho_mb:.1f} MB")
    print(f"   Pasta:   {AUDIO_DIR}")
    print("=" * 65)

    return mp3_final


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    if "--listar-vozes" in sys.argv:
        config  = load_config()
        api_key = config["elevenlabs"]["api_key"]
        listar_vozes(api_key)
    else:
        txt = sys.argv[1] if len(sys.argv) > 1 else None
        gerar_audio(txt_file=txt)
