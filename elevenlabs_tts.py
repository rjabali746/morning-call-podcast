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

import os, sys, json, glob, re, math, requests
from datetime import datetime

BASE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
AUDIO_DIR   = os.path.join(BASE, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"

# Limite de chars por chamada API (ElevenLabs aceita até 5000)
CHUNK_SIZE = 4500      # alvo por chunk — deixa folga para o rebalanceamento
HARD_LIMIT = 4900      # teto absoluto aceito pela API
MIN_CHUNK_SIZE = 600   # abaixo disso o modelo perde contexto e "gagueja"

# Contexto passado ao TTS nas junções entre chunks. Melhora a prosódia na
# emenda (entonação e ritmo continuam naturais) e NÃO é sintetizado nem
# cobrado — serve apenas para condicionar o modelo.
CONTEXTO_CHARS = 500

# ── Ajustes de voz ────────────────────────────────────────────────────────────
# Para locução jornalística o que importa é consistência, não expressividade.
# stability alta + style zerado reduzem drasticamente os artefatos e as
# "alucinações" do modelo — que é o que fazia o locutor dar pau no episódio.
# Sobrescrevíveis por variável de ambiente, sem editar código.
VOICE_STABILITY  = float(os.environ.get("ELEVENLABS_STABILITY")  or 0.70)
VOICE_SIMILARITY = float(os.environ.get("ELEVENLABS_SIMILARITY") or 0.80)
VOICE_STYLE      = float(os.environ.get("ELEVENLABS_STYLE")      or 0.0)
OUTPUT_FORMAT    = os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "mp3_44100_128"


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

# Meses em português para conversão de datas numéricas
_MESES_PT = {
    "01": "janeiro", "02": "fevereiro", "03": "março",   "04": "abril",
    "05": "maio",    "06": "junho",     "07": "julho",   "08": "agosto",
    "09": "setembro","10": "outubro",   "11": "novembro","12": "dezembro",
}

# Expansão das abreviações de magnitude financeira (bi/mi/tri são comuns no
# Valor Econômico e causavam "travamento" porque o TTS não as reconhecia
# no contexto de "R$ 1,2 bi").
_MAG_EXPAND = {
    "bi":      "bilhões",  "mi":      "milhões",  "tri":     "trilhões",
    "bilhão":  "bilhão",   "bilhões": "bilhões",
    "milhão":  "milhão",   "milhões": "milhões",
    "trilhão": "trilhão",  "trilhões":"trilhões",
    # variantes sem acento (texto raspado nem sempre preserva acentuação)
    "bilhao":  "bilhão",   "bilhoes": "bilhões",
    "milhao":  "milhão",   "milhoes": "milhões",
    "trilhao": "trilhão",  "trilhoes":"trilhões",
    "mil":     "mil",
}

# Dicionário de pronúncia: siglas que o TTS costuma soletrar ou ler errado.
# Só inclua siglas cuja expansão seja inequívoca — as que o modelo já lê bem
# (PIB, IPCA, Selic, CEO, Fed) ficam de fora de propósito.
PRONUNCIA = {
    "EUA":   "Estados Unidos",
    "BC":    "Banco Central",
    "CVM":   "Comissão de Valores Mobiliários",
    "BNDES": "B N D E S",
    "FGTS":  "F G T S",
    "INSS":  "I N S S",
    "TCU":   "T C U",
    "LCI":   "L C I",
    "LCA":   "L C A",
    "CDB":   "C D B",
    "FII":   "F I I",
    "ETF":   "E T F",
    "IPO":   "I P O",
    "M&A":   "fusões e aquisições",
    "p.p.":  "pontos percentuais",
    "bps":   "pontos-base",
}

# ── Números por extenso (pt-BR) ───────────────────────────────────────────────
# CAUSA RAIZ dos "travamentos" da narração: o ElevenLabs decide sozinho como ler
# um dígito e, em pt-BR, erra com frequência — repete, gagueja ou pula o número.
# A solução robusta é não enviar dígito nenhum: tudo vira palavra antes do TTS.

_UNI      = ["zero","um","dois","três","quatro","cinco","seis","sete","oito","nove"]
_DEZ      = ["dez","onze","doze","treze","quatorze","quinze","dezesseis",
             "dezessete","dezoito","dezenove"]
_DEZENAS  = ["","","vinte","trinta","quarenta","cinquenta","sessenta",
             "setenta","oitenta","noventa"]
_CENTENAS = ["","cento","duzentos","trezentos","quatrocentos","quinhentos",
             "seiscentos","setecentos","oitocentos","novecentos"]

_ESCALAS = [(10**12, "trilhão", "trilhões"),
            (10**9,  "bilhão",  "bilhões"),
            (10**6,  "milhão",  "milhões"),
            (10**3,  "mil",     "mil")]

def _grupo_ate_999(n):
    """Converte 1–999 por extenso."""
    if n == 0:
        return ""
    if n == 100:
        return "cem"
    c, r = divmod(n, 100)
    partes = []
    if c:
        partes.append(_CENTENAS[c])
    if r:
        if r < 10:
            partes.append(_UNI[r])
        elif r < 20:
            partes.append(_DEZ[r - 10])
        else:
            d, u = divmod(r, 10)
            partes.append(_DEZENAS[d] + (f" e {_UNI[u]}" if u else ""))
    return " e ".join(partes)

def num_por_extenso(n):
    """Converte um inteiro para extenso em pt-BR (até casa dos trilhões)."""
    n = int(n)
    if n < 0:
        return "menos " + num_por_extenso(-n)
    if n == 0:
        return "zero"
    if n >= 10**15:                 # absurdamente grande — não vale arriscar
        return " ".join(_UNI[int(d)] for d in str(n))

    grupos, resto = [], n
    for valor, sing, plur in _ESCALAS:
        q, resto = divmod(resto, valor)
        if not q:
            continue
        if valor == 10**3:
            grupos.append("mil" if q == 1 else f"{_grupo_ate_999(q)} mil")
        else:
            grupos.append(f"{_grupo_ate_999(q)} {sing if q == 1 else plur}")
    if resto:
        grupos.append(_grupo_ate_999(resto))

    if len(grupos) == 1:
        return grupos[0]
    # Regra do 'e' em pt-BR: liga o último grupo quando ele é < 100,
    # múltiplo de 100, ou quando não há resto ('um milhão e duzentos mil').
    usar_e = (resto == 0) or (resto < 100) or (resto % 100 == 0)
    sep = " e " if usar_e else ", "
    return ", ".join(grupos[:-1]) + sep + grupos[-1]

def _decimal_por_extenso(dec):
    """Parte decimal: '35'→'trinta e cinco'; '05'→'zero cinco'; '125'→dígitos."""
    if len(dec) <= 2 and not dec.startswith("0"):
        return num_por_extenso(int(dec))
    return " ".join(_UNI[int(d)] for d in dec)

def _valor_com_moeda(inteiro, moeda):
    """'1' → '1 real'/'1 dólar'; caso geral → 'N reais'/'N dólares'."""
    if inteiro == "1":
        return "1 real" if moeda == "reais" else "1 dólar"
    return f"{inteiro} {moeda}"

def _moeda_para_fala(m):
    """Converte moeda para fala natural em pt-BR, tratando centavos e
    abreviações de magnitude (bi, mi, tri):
      'R$ 1,2 bi'      → '1,2 bilhões de reais'   (a vírgula vira 'vírgula' depois)
      'R$ 5,15'        → '5 reais e 15 centavos'
      'R$ 1.234,00'    → '1234 reais'
      'US$ 300 milhões'→ '300 milhões de dólares'
    """
    valor      = m.group(1)
    mag_raw    = (m.group(2) or "").strip().lower()
    is_real    = m.group(0).lstrip().startswith("R$")
    moeda      = "reais" if is_real else "dólares"
    cent_moeda = "centavos" if is_real else "centavos de dólar"

    if mag_raw:
        mag = _MAG_EXPAND.get(mag_raw, mag_raw)
        # 'mil' não leva preposição: "quinhentos mil reais", jamais
        # "quinhentos mil DE reais" (só milhão/bilhão/trilhão pedem o 'de').
        if mag == "mil":
            return f"{valor} mil {moeda}"
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

# Padrão de magnitude para uso nas regex.
# Aceita as formas sem acento (bilhao/bilhoes) porque o texto raspado nem sempre
# preserva a acentuação — e um "trilhões" não reconhecido vira
# "seis reais e vinte centavos trilhoes", que destrói a narração.
_MAG_PAT = (r"bilh(?:ão|ões|ao|oes)|milh(?:ão|ões|ao|oes)|trilh(?:ão|ões|ao|oes)"
            r"|tri|bi|mi|mil")

def _normalizar_para_fala(texto):
    """
    Normaliza números e símbolos para leitura natural pelo TTS em pt-BR.
    Elimina os principais padrões que causam "travamentos" na narração:
    abreviações financeiras (bi/mi/tri), datas numéricas, percentuais com
    sinal, horários com dois-pontos e ordinais.
    """
    # 1. Datas numéricas: 18/07/2026 → '18 de julho de 2026'; 18/07 → '18 de julho'
    def _data(m):
        dia = str(int(m.group(1)))
        mes = _MESES_PT.get(m.group(2), m.group(2))
        ano = m.group(3)
        return f"{dia} de {mes}" + (f" de {ano}" if ano else "")
    texto = re.sub(r"\b(\d{1,2})/(\d{2})(?:/(\d{4}))?\b", _data, texto)

    # 2. Separador de milhar (ponto): 1.234.567 → 1234567
    texto = re.sub(r"\d{1,3}(?:\.\d{3})+",
                   lambda m: m.group(0).replace(".", ""), texto)

    # 3. Moeda R$ / US$ — inclui abreviações bi/mi/tri além das formas plenas.
    #    O valor é `\d+(?:[.,]\d+)*` e NÃO `[\d.,]+`: a classe gulosa engolia o
    #    ponto final da frase ("R$ 45,20. Alta de 2%" perdia o ponto e virava
    #    uma frase só), apagando a pausa da narração e estragando o corte em
    #    chunks, que depende da pontuação.
    texto = re.sub(
        rf"(?:R\$|US\$)\s*(\d+(?:[.,]\d+)*)(?:\s+({_MAG_PAT})\b)?",
        _moeda_para_fala, texto)

    # 4. Magnitudes sem símbolo de moeda: '4,5 bi lucro' → '4,5 bilhões lucro'
    def _num_mag(m):
        mag = _MAG_EXPAND.get(m.group(2).lower(), m.group(2))
        return f"{m.group(1)} {mag}"
    texto = re.sub(rf"(\d+(?:[.,]\d+)?)\s+(bi|mi|tri)\b", _num_mag, texto)

    # 5. Porcentagem (aceita sinal + ou -)
    texto = re.sub(r"([+-]?\d+(?:,\d+)?)\s*%", r"\1 por cento", texto)

    # 6. Horários: 12h44 → '12 horas e 44'; 14:30 → '14 horas e 30'
    texto = re.sub(r"\b(\d{1,2})h(\d{2})\b", r"\1 horas e \2", texto)
    texto = re.sub(r"\b(\d{1,2})h\b",         r"\1 horas",      texto)
    texto = re.sub(r"\b(\d{1,2}):(\d{2})\b",  r"\1 horas e \2", texto)

    # 7. Ordinais: 1º → primeiro, 2ª → segunda (até 10º/10ª)
    _ORD_M = ["","primeiro","segundo","terceiro","quarto","quinto",
               "sexto","sétimo","oitavo","nono","décimo"]
    _ORD_F = ["","primeira","segunda","terceira","quarta","quinta",
               "sexta","sétima","oitava","nona","décima"]
    def _ordinal(m):
        n = int(m.group(1))
        lst = _ORD_F if m.group(2) == "ª" else _ORD_M
        return lst[n] if 1 <= n < len(lst) else m.group(0)
    texto = re.sub(r"\b(\d+)([ºª])\b", _ordinal, texto)

    # 8. Sinais colados a números → palavra
    texto = re.sub(r"(?<!\w)\+(?=\d)", "mais ",  texto)
    texto = re.sub(r"(?<![\w\d])-(?=\d)", "menos ", texto)

    # 9. Decimais por extenso: '13,25' → 'treze vírgula vinte e cinco'
    texto = re.sub(
        r"(\d+),(\d+)",
        lambda m: f"{num_por_extenso(m.group(1))} vírgula {_decimal_por_extenso(m.group(2))}",
        texto)

    # 10. Inteiros restantes por extenso — depois deste passo NÃO sobra
    #     nenhum dígito no texto enviado ao TTS.
    texto = re.sub(r"\d+", lambda m: num_por_extenso(m.group(0)), texto)

    # 11. Dicionário de siglas e expressões (palavra inteira)
    for sigla, expan in PRONUNCIA.items():
        texto = re.sub(rf"(?<!\w){re.escape(sigla)}(?!\w)", expan, texto)

    # 12. Limpeza de espaços duplicados gerados pelas substituições
    texto = re.sub(r"[ \t]{2,}", " ", texto)
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
    Divide o texto em chunks EQUILIBRADOS, respeitando parágrafos e frases.

    Por que equilibrado e não guloso: o preenchimento guloso (encher até o teto
    e jogar o resto no último chunk) costuma deixar um chunk final minúsculo.
    Um trecho curto dá pouquíssimo contexto ao modelo e é a causa mais comum de
    narração truncada, gaguejada ou "alucinada" no fim do episódio — exatamente
    o defeito relatado. Dividindo em partes de tamanho parecido, nenhum chunk
    fica curto o bastante para disparar esse comportamento.
    """
    if len(texto) <= tamanho:
        return [texto]

    def _empacotar(alvo):
        """Preenche chunks respeitando parágrafos (e frases, se preciso)."""
        chunks, atual = [], ""

        def fechar():
            nonlocal atual
            if atual.strip():
                chunks.append(atual.strip())
            atual = ""

        for paragrafo in texto.split("\n\n"):
            if not paragrafo.strip():
                continue
            if len(paragrafo) > alvo:          # parágrafo gigante → por frases
                fechar()
                for frase in re.split(r"(?<=[.!?])\s+", paragrafo):
                    # Frase que sozinha excede o limite duro da API (texto sem
                    # pontuação) precisa ser quebrada por palavras, senão a
                    # chamada volta HTTP 400 e o episódio inteiro falha.
                    while len(frase) > HARD_LIMIT:
                        fechar()
                        corte = frase.rfind(" ", 0, HARD_LIMIT)
                        corte = corte if corte > 0 else HARD_LIMIT
                        chunks.append(frase[:corte].strip())
                        frase = frase[corte:].lstrip()
                    if atual and len(atual) + len(frase) + 1 > alvo:
                        fechar()
                    atual += (" " if atual else "") + frase
                continue
            if atual and len(atual) + len(paragrafo) + 2 > alvo:
                fechar()
            atual += ("\n\n" if atual else "") + paragrafo
        fechar()
        return chunks

    # 1ª passada: preenchimento até o teto → número MÍNIMO de chunks.
    # Menos chunks = menos emendas = menos oportunidade de falha.
    base = _empacotar(tamanho)

    # 2ª passada: com o mesmo número de chunks, redistribui para que fiquem
    # parelhos. Só adota o resultado se não aumentar a contagem de chunks.
    if len(base) > 1:
        equilibrado = _empacotar(math.ceil(len(texto) / len(base)))
        if len(equilibrado) <= len(base):
            base = equilibrado

    # Rede de segurança: chunk final curto demais é fundido no anterior
    # (desde que o resultado caiba no limite duro da API).
    while len(base) > 1 and len(base[-1]) < MIN_CHUNK_SIZE:
        if len(base[-2]) + len(base[-1]) + 2 > HARD_LIMIT:
            break
        ultimo = base.pop()
        base[-1] = base[-1] + "\n\n" + ultimo

    return base


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
        elif resp.status_code == 401:
            print(f"  ❌ ELEVENLABS_API_KEY inválida (HTTP 401). Verifique o secret no GitHub.")
        else:
            print(f"  ⚠️  /user/subscription retornou HTTP {resp.status_code}")
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
            print(f"  ⚠️  /voices retornou lista vazia (HTTP 200). Tentando vozes pré-construídas...")
        else:
            print(f"  ⚠️  /voices retornou HTTP {resp.status_code}. Tentando vozes pré-construídas...")
    except Exception as e:
        print(f"  ⚠️  Erro ao listar vozes: {e}. Tentando vozes pré-construídas...")

    # 3. Último recurso: testar IDs de vozes pré-construídas estáveis do ElevenLabs.
    # Estas vozes existem em todas as contas (inclusive plano gratuito).
    VOZES_PREBUILTAS = [
        ("Daniel",  "onwK4e9ZLuTAKqWW03F9"),   # Daniel — masculina, jornalística
        ("Rachel",  "21m00Tcm4TlvDq8ikWAM"),   # Rachel — feminina, clara
        ("Adam",    "pNInz6obpgDQGcFmaJgB"),   # Adam — masculina, neutra
        ("Antoni",  "ErXwobaYiN019PkySvjV"),   # Antoni — masculina, expressiva
        ("Josh",    "TxGEqnHWrfWFTfGW9XjX"),   # Josh — masculina, jovem
    ]
    for nome, vid in VOZES_PREBUILTAS:
        try:
            r = requests.get(
                f"{ELEVENLABS_BASE}/voices/{vid}",
                headers={"xi-api-key": api_key},
                timeout=8
            )
            if r.status_code == 200:
                print(f"  ✅ Voz pré-construída disponível: {nome} ({vid})")
                return vid
        except Exception:
            pass

    raise RuntimeError(
        f"Voice ID '{voice_id_configurado}' inválido e nenhuma voz acessível. "
        "Verifique: (1) API key correta no secret ELEVENLABS_API_KEY; "
        "(2) Conta ativa em https://elevenlabs.io; "
        "(3) Vozes em https://elevenlabs.io/app/voice-lab"
    )

def gerar_chunk_audio(api_key, voice_id, model, texto, language_code=None,
                      previous_text=None, next_text=None):
    """
    Gera áudio para um chunk de texto via ElevenLabs API.

    previous_text / next_text: trechos vizinhos passados como CONTEXTO. O modelo
    usa-os para manter entonação e ritmo contínuos na emenda entre chunks, mas
    não os sintetiza nem cobra por eles. Sem isso, cada chunk recomeça "do zero"
    e a junção soa como um corte seco — ou pior, o modelo perde o fio no início.
    """
    url  = f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}"
    body = {
        "text": texto,
        "model_id": model,
        "voice_settings": {
            "stability":        VOICE_STABILITY,   # alto = locução consistente
            "similarity_boost": VOICE_SIMILARITY,  # fidelidade à voz
            "style":            VOICE_STYLE,       # 0 = sem exagero expressivo
            "use_speaker_boost": True,
        },
    }
    # Formato fixo em todos os chunks — indispensável porque os MP3s são
    # concatenados byte a byte; bitrates diferentes causariam estalos.
    params = {"output_format": OUTPUT_FORMAT}

    if previous_text:
        body["previous_text"] = previous_text[-CONTEXTO_CHARS:]
    if next_text:
        body["next_text"] = next_text[:CONTEXTO_CHARS]

    # language_code força o idioma — suportado só pelos modelos turbo/flash v2.5.
    # (O multilingual_v2 ignora/rejeita o parâmetro, então só enviamos quando cabe.)
    if language_code and ("turbo" in model or "flash" in model):
        body["language_code"] = language_code
    headers = {
        "xi-api-key":   api_key,
        "Content-Type": "application/json",
        "Accept":       "audio/mpeg"
    }

    resp = requests.post(url, json=body, params=params, headers=headers, timeout=90)

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
            # Mesmo contexto de vizinhança usado no pipeline — mantém a
            # prosódia contínua nas emendas entre chunks.
            audio_bytes += gerar_chunk_audio(
                api_key, voice_id, model, chunk, lang_code,
                previous_text=chunks[i - 2] if i > 1 else None,
                next_text=chunks[i] if i < len(chunks) else None,
            )
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
