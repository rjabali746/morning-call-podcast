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

# Dicionário de pronúncia para jargão financeiro que o TTS costuma soletrar
# ou pronunciar mal. Só inclua siglas cuja expansão seja inequívoca — as que
# o modelo já lê bem (PIB, IPCA, Selic, CEO) ficam de fora de propósito.
PRONUNCIA = {
    "EUA": "Estados Unidos",
    "BC":  "Banco Central",
    "CVM": "Comissão de Valores Mobiliários",
    "BNDES": "B N D E S",
}

def _valor_com_moeda(inteiro, moeda):
    """'1' → '1 real'/'1 dólar'; caso geral → 'N reais'/'N dólares'."""
    if inteiro == "1":
        return "1 real" if moeda == "reais" else "1 dólar"
    return f"{inteiro} {moeda}"

def _moeda_para_fala(m):
    """Converte moeda para fala natural em pt-BR, tratando centavos:
      'R$ 5,15'        → '5 reais e 15 centavos'
      'R$ 1.234,00'    → '1234 reais'
      'US$ 300 milhões'→ '300 milhões de dólares'
      'R$ 5,15 bilhões'→ '5,15 bilhões de reais'  (a vírgula vira 'vírgula' depois)
    """
    valor = m.group(1)
    mag   = (m.group(2) or "").strip()
    is_real    = m.group(0).lstrip().startswith("R$")
    moeda      = "reais" if is_real else "dólares"
    cent_moeda = "centavos" if is_real else "centavos de dólar"
    if mag:                                   # 5,15 bilhões de reais
        return f"{valor} {mag} de {moeda}"
    valor = valor.replace(".", "")            # remove separador de milhar
    if "," in valor:                          # tem centavos
        inteiro, dec = valor.split(",", 1)
        inteiro = inteiro or "0"
        dec = (dec + "00")[:2]
        if dec == "00":
            return _valor_com_moeda(inteiro, moeda)
        return f"{_valor_com_moeda(inteiro, moeda)} e {dec} {cent_moeda}"
    return _valor_com_moeda(valor, moeda)

def _normalizar_para_fala(texto):
    """
    Normaliza números e símbolos para leitura natural pelo TTS em pt-BR.
    Ex.: 'R$ 5,15' → '5 reais e 15 centavos'; '13,25%' → '13 vírgula 25 por cento';
         '05h03' → '05 horas e 03'.
    Reduz erros de pronúncia sem gastar quota extra.
    """
    # 1. Separador de milhar (ponto): 1.234.567 → 1234567  (evita 'um ponto...')
    texto = re.sub(r"\d{1,3}(?:\.\d{3})+",
                   lambda m: m.group(0).replace(".", ""), texto)
    # 2. Moeda R$ / US$ — antes das % para não conflitar.
    #    Magnitudes maiores primeiro; espaço + magnitude ficam no grupo opcional.
    texto = re.sub(
        r"(?:R\$|US\$)\s*([\d.,]+)(?:\s+(bilh(?:ão|ões)|milh(?:ão|ões)|trilh(?:ão|ões)|mil))?",
        _moeda_para_fala, texto)
    # 3. Porcentagem
    texto = re.sub(r"(\d+(?:,\d+)?)\s*%", r"\1 por cento", texto)
    # 4. Horários: 12h44 → '12 horas e 44'; 12h → '12 horas'
    texto = re.sub(r"\b(\d{1,2})h(\d{2})\b", r"\1 horas e \2", texto)
    texto = re.sub(r"\b(\d{1,2})h\b", r"\1 horas", texto)
    # 5. Vírgula decimal restante → ' vírgula ' falado (13,25 → 13 vírgula 25).
    #    Só entre dígitos, para não afetar listas ('A, B') nem datas.
    texto = re.sub(r"(\d),(\d)", r"\1 vírgula \2", texto)
    # 6. Dicionário de siglas (palavra inteira, sensível a maiúsculas)
    for sigla, expan in PRONUNCIA.items():
        if expan != sigla:
            texto = re.sub(rf"\b{re.escape(sigla)}\b", expan, texto)
    return texto

def limpar_texto_para_audio(texto):
    """Remove elementos visuais que ficam estranhos no áudio."""
    texto = re.sub(r"={3,}", "", texto)
    texto = re.sub(r"-{3,}", "", texto)
    texto = re.sub(r"\bhttps?://\S+", "", texto)
    texto = re.sub(r"\*+", "", texto)
    texto = re.sub(r"#{1,6}\s*", "", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    # Normaliza números/símbolos/siglas para leitura natural (Optimização 4)
    texto = _normalizar_para_fala(texto)
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

    def _eh_portugues(v):
        blob = " ".join(str(x) for x in v.get("labels", {}).values()).lower()
        blob += " " + v.get("name", "").lower()
        return ("portug" in blob or "brazil" in blob or "brasil" in blob
                or blob.strip().endswith(" pt") or " pt " in blob)

    # Vozes em português primeiro, para achar o sotaque certo na hora.
    pt = [v for v in vozes if _eh_portugues(v)]
    outras = [v for v in vozes if not _eh_portugues(v)]

    def _linha(v, marca=""):
        labels = v.get("labels", {})
        lang   = labels.get("language", "")
        acc    = labels.get("accent", "")
        gen    = labels.get("gender", "")
        print(f"  {marca}ID: {v['voice_id']}")
        print(f"  {marca}Nome: {v['name']} | {lang} {acc} {gen}")
        print()

    print(f"\n🎙️  {len(vozes)} vozes disponíveis:\n")
    if pt:
        print("🇧🇷 ─ PORTUGUÊS / BRASIL (use uma destas para o sotaque certo) ─")
        for v in pt:
            _linha(v, marca="⭐ ")
    else:
        print("⚠️  Nenhuma voz em português na conta. Adicione uma pelo Voice Library:")
        print("    https://elevenlabs.io/app/voice-library  → filtre 'Portuguese (Brazil)'")
        print("    → clique 'Add to my voices' → rode este comando de novo.\n")
    print("── Demais vozes ──")
    for v in outras:
        _linha(v)
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


def verificar_ou_descobrir_voice_id(api_key, voice_id_configurado):
    """
    Verifica se o voice_id configurado existe na conta.
    Se não existir (404), descobre automaticamente a primeira voz disponível.
    Retorna o voice_id válido ou lança exceção se a conta não tiver nenhuma voz.
    """
    # 1. Testar o voice_id configurado
    try:
        resp = requests.get(
            f"{ELEVENLABS_BASE}/voices/{voice_id_configurado}",
            headers={"xi-api-key": api_key},
            timeout=10
        )
        if resp.status_code == 200:
            nome = resp.json().get("name", voice_id_configurado)
            print(f"  🎙️  Voz confirmada: {nome} ({voice_id_configurado})")
            return voice_id_configurado
        print(f"  ⚠️  Voice ID '{voice_id_configurado}' inválido (HTTP {resp.status_code}) — buscando voz disponível...")
    except Exception as e:
        print(f"  ⚠️  Erro ao verificar voice_id: {e} — buscando voz disponível...")

    # 2. Auto-descoberta: listar vozes da conta
    try:
        resp = requests.get(
            f"{ELEVENLABS_BASE}/voices",
            headers={"xi-api-key": api_key},
            timeout=10
        )
        if resp.status_code == 200:
            vozes = resp.json().get("voices", [])
            if vozes:
                # Preferir vozes com "brazil" ou "portuguese" nos labels
                def _score_pt(v):
                    blob = " ".join(str(x) for x in v.get("labels", {}).values()).lower()
                    blob += " " + v.get("name", "").lower()
                    return ("portug" in blob or "brazil" in blob or "brasil" in blob)

                pt_vozes = [v for v in vozes if _score_pt(v)]
                voz_escolhida = pt_vozes[0] if pt_vozes else vozes[0]
                vid = voz_escolhida["voice_id"]
                nome = voz_escolhida.get("name", vid)
                print(f"  ✅ Voz encontrada automaticamente: {nome} ({vid})")
                return vid
    except Exception as e:
        print(f"  ⚠️  Erro ao listar vozes: {e}")

    raise RuntimeError(
        f"Voice ID '{voice_id_configurado}' inválido e nenhuma voz disponível na conta ElevenLabs. "
        "Verifique o secret ELEVENLABS_VOICE_ID no GitHub e as vozes em https://elevenlabs.io/app/voice-lab"
    )

def gerar_chunk_audio(api_key, voice_id, model, texto, language_code=None):
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
    # language_code força o idioma — suportado só pelos modelos turbo/flash v2.5.
    # (O multilingual_v2 ignora/rejeita o parâmetro, então só enviamos quando cabe.)
    if language_code and ("turbo" in model or "flash" in model):
        body["language_code"] = language_code
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
    lang_code = el_config.get("language_code", "pt")

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
            audio_bytes += gerar_chunk_audio(api_key, voice_id, model, chunk, lang_code)
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
