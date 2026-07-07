#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sincronizar_favoritas.py — Aprendizado automático a partir de uma Planilha Google.

Fluxo (roda dentro do GitHub Actions, antes de gerar o episódio):
  1. Baixa a planilha publicada como CSV (URL na variável FAVORITAS_CSV_URL).
  2. Lê as manchetes (coluna A). Linha começando com 'NAO:' — ou 2ª coluna
     marcada com nao/não/-/x — significa "não quero esse tipo de notícia".
  3. Ignora as manchetes que já foram processadas antes
     (registradas em manchetes_processadas.log, versionado no repositório).
  4. Refina o perfil com a MESMA lógica testada de aprender_perfil.py.
  5. Salva perfil_interesses.json e registra as manchetes no log.

É idempotente e à prova de falha: se a URL não estiver definida, se a planilha
não baixar, ou se não houver manchete nova, ele apenas sai sem alterar nada —
nunca quebra a geração do podcast.

Sem dependências externas — só biblioteca padrão do Python.
"""

import os
import io
import csv
import sys
import json
import datetime
import urllib.request

import aprender_perfil as ap   # reutiliza a lógica de aprendizado já testada

PERFIL_FILE = ap.PERFIL_FILE
LOG_FILE    = ap.LOG_FILE


def baixar_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "morning-call-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def ler_planilha(raw):
    """Retorna (gostei, nao) a partir do texto CSV."""
    gostei, nao = [], []
    reader = csv.reader(io.StringIO(raw))
    for i, row in enumerate(reader):
        if not row:
            continue
        titulo = (row[0] or "").strip()
        if not titulo:
            continue
        # pula uma eventual linha de cabeçalho
        if i == 0 and titulo.lower() in (
                "manchete", "manchetes", "titulo", "título", "headline", "noticia", "notícia"):
            continue
        flag = row[1].strip().lower() if len(row) > 1 else ""
        negativo = (titulo.upper().startswith("NAO:")
                    or titulo.upper().startswith("NÃO:")
                    or flag in ("nao", "não", "-", "x", "descartar", "descarte"))
        if titulo.upper().startswith("NAO:") or titulo.upper().startswith("NÃO:"):
            titulo = titulo.split(":", 1)[1].strip()
        (nao if negativo else gostei).append(titulo)
    return gostei, nao


def ja_processadas():
    """Set (lowercase) das manchetes já processadas em execuções anteriores."""
    vistos = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            for linha in f:
                if "] " not in linha:
                    continue
                resto = linha.split("] ", 1)[1].strip()
                if resto[:3] in ("(+)", "(-)"):
                    resto = resto[3:].strip()
                if resto:
                    vistos.add(resto.lower())
    return vistos


def registrar_log(gostei, nao):
    carimbo = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for m in gostei:
            f.write(f"[{carimbo}] (+) {m}\n")
        for m in nao:
            f.write(f"[{carimbo}] (-) {m}\n")


def main():
    url = os.environ.get("FAVORITAS_CSV_URL", "").strip()
    if not url:
        print("ℹ️  FAVORITAS_CSV_URL não definido — aprendizado por planilha desativado.")
        return

    try:
        raw = baixar_csv(url)
    except Exception as e:
        print(f"⚠️  Não consegui baixar a planilha ({e}). Aprendizado pulado.")
        return

    gostei, nao = ler_planilha(raw)
    vistos = ja_processadas()
    gostei = [m for m in gostei if m.lower() not in vistos]
    nao    = [m for m in nao    if m.lower() not in vistos]

    if not gostei and not nao:
        print("ℹ️  Nenhuma manchete nova na planilha.")
        return

    with open(PERFIL_FILE, encoding="utf-8") as f:
        perfil = json.load(f)

    plano = ap.analisar(perfil, gostei, nao)
    ap.imprimir_resumo(gostei, nao, plano)
    ap.aplicar(perfil, gostei, plano)

    with open(PERFIL_FILE, "w", encoding="utf-8") as f:
        json.dump(perfil, f, ensure_ascii=False, indent=2)

    registrar_log(gostei, nao)
    print(f"\n✅ Perfil atualizado a partir da planilha "
          f"({len(gostei)} gostei, {len(nao)} descartes).")


if __name__ == "__main__":
    main()
