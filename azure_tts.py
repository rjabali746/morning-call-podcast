#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
azure_tts.py — Síntese de voz via Azure AI Speech (vozes neurais pt-BR).

POR QUE EXISTE
  A ElevenLabs custa $22/mês no plano Creator. A Azure cobra $16 por MILHÃO de
  caracteres, e o episódio consome ~185 mil por mês — algo em torno de $3. O
  nível gratuito (F0) dá 500 mil caracteres mensais, que cobririam o consumo
  inteiro, mas a Microsoft o descreve como tier de teste e ele é limitado por
  throttling, então para produção o S0 pago é mais previsível.

DIFERENÇAS QUE IMPORTAM FRENTE À ELEVENLABS
  • O corpo da requisição é SSML, não JSON — e o texto precisa de escape XML.
  • O teto é de 10 MINUTOS DE ÁUDIO por requisição, não de caracteres. Como o
    episódio bate ~10,4 min, ele é fatiado em duas chamadas. Isso traz de volta
    a emenda que eliminamos na ElevenLabs, mas aqui o risco é outro: voz neural
    da Azure é determinística, não "alucina" nem gagueja em início de trecho
    como um modelo autorregressivo. A emenda é só um corte, não um defeito.
  • O cabeçalho User-Agent é OBRIGATÓRIO — sem ele a API recusa.
  • Não há endpoint de saldo. O consumo se acompanha no portal da Azure.

Configuração (variáveis de ambiente):
  AZURE_SPEECH_KEY     — chave do recurso de Fala (obrigatória)
  AZURE_SPEECH_REGION  — região do recurso, ex.: brazilsouth (padrão: brazilsouth)
  AZURE_VOICE          — nome da voz; se inválida, é descoberta em tempo de execução
  AZURE_OUTPUT_FORMAT  — padrão: audio-24khz-96kbitrate-mono-mp3
  AZURE_TAXA_FALA      — ritmo, ex.: "-5%" para um pouco mais devagar
"""

import os
import re
import json
import xml.sax.saxutils as _xml
import urllib.request
import urllib.error

REGIAO_PADRAO = os.environ.get("AZURE_SPEECH_REGION") or "brazilsouth"
VOZ_PADRAO    = os.environ.get("AZURE_VOICE") or "pt-BR-AntonioNeural"
# 96 kbps mono: qualidade de sobra para locução e arquivos ~25% menores que os
# 128 kbps de antes — o repositório acumula um MP3 por dia útil.
FORMATO       = os.environ.get("AZURE_OUTPUT_FORMAT") or "audio-24khz-96kbitrate-mono-mp3"
TAXA_FALA     = os.environ.get("AZURE_TAXA_FALA") or "0%"
IDIOMA        = "pt-BR"
TIMEOUT       = 120

# A Azure exige User-Agent com menos de 255 caracteres.
USER_AGENT = "morning-call-jabali/1.0"

# Teto de 10 min de áudio por requisição. A taxa medida em episódio real foi de
# ~810 caracteres por minuto narrado; 7000 chars ≈ 8,6 min deixa folga para
# variação de ritmo entre vozes.
CHARS_POR_MINUTO = 810
LIMITE_CHARS     = int(CHARS_POR_MINUTO * 10 * 0.86)   # ≈ 6966

# Ordem de preferência quando a voz precisa ser descoberta. Vozes "Multilingual"
# e "HD" soam melhor, mas são cobradas em faixa mais alta — ficam de fora do
# padrão justamente porque o objetivo aqui é baratear.
PREFERENCIA_VOZES = [
    "pt-BR-AntonioNeural",
    "pt-BR-FabioNeural",
    "pt-BR-HumbertoNeural",
    "pt-BR-JulioNeural",
    "pt-BR-NicolauNeural",
    "pt-BR-ValerioNeural",
    "pt-BR-FranciscaNeural",
]


def _endpoint(regiao):
    return f"https://{regiao}.tts.speech.microsoft.com/cognitiveservices/v1"


def _endpoint_vozes(regiao):
    return (f"https://{regiao}.tts.speech.microsoft.com"
            f"/cognitiveservices/voices/list")


def bytes_por_char_esperado(formato=None):
    """
    Bytes de MP3 por caractere de texto, derivado do bitrate do formato.
    Usado para validar se o áudio devolvido tem tamanho plausível — retorno
    curto demais significa narração truncada.
    """
    f = formato or FORMATO
    m = re.search(r"(\d+)kbitrate", f)
    kbps = int(m.group(1)) if m else 96
    bytes_por_segundo = kbps * 1000 / 8
    chars_por_segundo = CHARS_POR_MINUTO / 60
    return bytes_por_segundo / chars_por_segundo


def limite_chars():
    """Caracteres por requisição, respeitando o teto de 10 min de áudio."""
    return LIMITE_CHARS


def verificar_conta(chave=None, regiao=None):
    """
    A Azure não expõe saldo de caracteres por API. Confirmamos apenas que a
    chave e a região respondem, listando as vozes — assim um erro de
    credencial aparece aqui, e não no meio da geração.
    Retorna None (saldo desconhecido), que o pipeline já trata.
    """
    chave  = chave  or os.environ.get("AZURE_SPEECH_KEY", "").strip()
    regiao = regiao or REGIAO_PADRAO
    if not chave:
        print("  ❌ AZURE_SPEECH_KEY não definida.")
        return None
    try:
        vozes = _listar_vozes(chave, regiao)
        pt = [v for v in vozes if v.get("Locale", "").lower() == IDIOMA.lower()]
        print(f"  📊 Azure Speech | região: {regiao} | "
              f"{len(pt)} voz(es) pt-BR disponíveis")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("  ❌ AZURE_SPEECH_KEY inválida (HTTP 401).")
        elif e.code == 403:
            print(f"  ❌ Acesso negado (HTTP 403) — confira se a região "
                  f"'{regiao}' é a do seu recurso.")
        else:
            print(f"  ⚠️  Azure respondeu HTTP {e.code} ao listar vozes.")
    except Exception as e:
        print(f"  ⚠️  Não foi possível falar com a Azure: {e}")
    return None


def _listar_vozes(chave, regiao):
    req = urllib.request.Request(
        _endpoint_vozes(regiao),
        headers={"Ocp-Apim-Subscription-Key": chave,
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def descobrir_voz(chave=None, regiao=None, voz_configurada=None):
    """
    Confirma que a voz configurada existe; se não, escolhe uma pt-BR.

    Mesma lógica de resiliência que salvou o fluxo na ElevenLabs: nome de voz
    muda, é descontinuado ou vem com erro de digitação no secret, e o episódio
    não pode deixar de sair por isso. A Azure, inclusive, não oferece fixação
    de versão de voz — o modelo por trás de um nome pode mudar sem aviso.
    """
    chave  = chave  or os.environ.get("AZURE_SPEECH_KEY", "").strip()
    regiao = regiao or REGIAO_PADRAO
    alvo   = (voz_configurada or VOZ_PADRAO).strip()

    try:
        vozes = _listar_vozes(chave, regiao)
    except Exception as e:
        print(f"  ⚠️  Não consegui listar vozes ({e}) — usando '{alvo}' como está.")
        return alvo

    nomes = {v.get("ShortName", ""): v for v in vozes}
    if alvo in nomes:
        v = nomes[alvo]
        print(f"  🎙️  Voz confirmada: {alvo} ({v.get('Gender','?')})")
        return alvo

    print(f"  ⚠️  Voz '{alvo}' não existe nesta região — procurando alternativa pt-BR...")
    for candidata in PREFERENCIA_VOZES:
        if candidata in nomes:
            print(f"  ✅ Voz escolhida automaticamente: {candidata}")
            return candidata

    pt = [v for v in vozes if v.get("Locale", "").lower() == IDIOMA.lower()]
    if pt:
        escolhida = pt[0]["ShortName"]
        print(f"  ✅ Voz pt-BR encontrada: {escolhida}")
        return escolhida

    raise RuntimeError(
        f"Nenhuma voz pt-BR disponível na região '{regiao}'. "
        f"Confira a região do recurso no portal da Azure."
    )


def _montar_ssml(texto, voz, taxa=None):
    """
    Envolve o texto em SSML. O escape XML é obrigatório: um '&' solto no texto
    (comum em 'S&P') derruba a requisição com erro de parsing.
    """
    corpo = _xml.escape(texto)
    taxa  = taxa or TAXA_FALA
    prosody_ini = f'<prosody rate="{taxa}">' if taxa not in ("0%", "", None) else ""
    prosody_fim = "</prosody>" if prosody_ini else ""
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="{IDIOMA}">'
        f'<voice name="{voz}">{prosody_ini}{corpo}{prosody_fim}</voice>'
        f'</speak>'
    )


def sintetizar(texto, voz, chave=None, regiao=None, formato=None, taxa=None):
    """Gera o MP3 de um trecho. Retorna bytes."""
    chave   = chave  or os.environ.get("AZURE_SPEECH_KEY", "").strip()
    regiao  = regiao or REGIAO_PADRAO
    formato = formato or FORMATO
    if not chave:
        raise RuntimeError("AZURE_SPEECH_KEY não definida.")

    corpo = _montar_ssml(texto, voz, taxa).encode("utf-8")
    req = urllib.request.Request(
        _endpoint(regiao), data=corpo,
        headers={
            "Ocp-Apim-Subscription-Key": chave,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": formato,
            "User-Agent": USER_AGENT,
        })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detalhe = ""
        try:
            detalhe = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        # 429 no F0 = cota gratuita esgotada ou throttling do tier de teste.
        if e.code == 429:
            raise RuntimeError(
                f"Azure recusou por limite de uso (HTTP 429). No nível gratuito "
                f"(F0) a cota é de 500 mil chars/mês e há throttling. "
                f"Detalhe: {detalhe}")
        raise RuntimeError(f"Azure HTTP {e.code}: {detalhe}")


# Interface compatível com a do pipeline, para o provedor ser trocável
def gerar_chunk_audio(api_key, voice_id, model, texto, language_code=None,
                      previous_text=None, next_text=None):
    """
    Assinatura idêntica à de elevenlabs_tts.gerar_chunk_audio, para o pipeline
    chamar os dois provedores do mesmo jeito.

    `model`, `previous_text` e `next_text` são ignorados: a Azure não aceita
    contexto de vizinhança (e não precisa — voz neural não perde o fio entre
    requisições como um modelo autorregressivo).
    """
    return sintetizar(texto, voice_id, chave=api_key)


if __name__ == "__main__":
    import sys
    chave = os.environ.get("AZURE_SPEECH_KEY", "").strip()
    if not chave:
        print("Defina AZURE_SPEECH_KEY (e AZURE_SPEECH_REGION) antes de testar.")
        sys.exit(1)

    verificar_conta()
    voz = descobrir_voz()
    texto = (sys.argv[1] if len(sys.argv) > 1 else
             "Morning Call Jábali. Vamos direto às três principais notícias.")
    audio = sintetizar(texto, voz)
    destino = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "audio", "teste_azure.mp3")
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "wb") as f:
        f.write(audio)
    print(f"\n✅ {len(audio):,} bytes → {destino}")
    print(f"   {len(texto)} chars | ~{bytes_por_char_esperado():.0f} bytes/char esperado")
