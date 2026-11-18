#!/usr/bin/env python3
"""Publica no Instagram o Reel do dia do @confortare.brasil, lendo do Drive.

Fluxo: acha o vídeo cuja data (no nome do arquivo) é hoje -> pega a legenda na
planilha de índice -> baixa o vídeo -> envia ao Instagram -> publica.
Qualquer problema faz o script terminar com erro (o GitHub avisa por e-mail).
"""
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

GRAPH = "https://graph.facebook.com/v27.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v27.0"
DRIVE = "https://www.googleapis.com/drive/v3"
FOLDER_ID = "10G6Sq8OkwhJXu1auwvWQe0Gh8a5loZLi"  # 03 REELS PRONTOS
IG_USER_ID = os.environ.get("IG_USER_ID", "17841411266083237")
TOKEN = os.environ["IG_PAGE_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
REEL_ARG = os.environ.get("REEL", "").strip().upper()  # ex.: R017 (opcional)
STATE_FILE = "posted.json"
NAME_RE = re.compile(r"^(R\d{3})_(\d{4}-\d{2}-\d{2})_.*\.mp4$", re.IGNORECASE)
AUTH = {"Authorization": "Bearer " + TOKEN}
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "confortarebrasil/confortare-reels")
GH_API = "https://api.github.com/repos/" + GH_REPO
GH_AUTH = {"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/vnd.github+json"}

# Músicas do catálogo liberado do Instagram (audio_id). A cada 3 Reels troca
# para a próxima da lista. Para acrescentar, coloque mais itens aqui.
MUSICAS = [
    {"nome": "Mozart Lacrimosa Remix - Giulio Cercato", "audio_id": "1585340805646465"},
]
VOLUME_MUSICA = 70
VOLUME_VIDEO = 100
CAPA_MS = 1500  # momento do vídeo usado como capa (1,5 s)


def escolher_musica(code):
    n = int(code[1:])
    return MUSICAS[((n - 1) // 3) % len(MUSICAS)]


def fail(msg):
    print("ERRO: " + msg)
    sys.exit(1)


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip().lower()


def check(resp, what):
    if not resp.ok:
        fail("%s falhou (HTTP %s): %s" % (what, resp.status_code, resp.text[:500]))
    return resp


def drive_session():
    info = json.loads(os.environ["GDRIVE_SA_KEY"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return AuthorizedSession(creds)


def list_tree(s, folder_id):
    """Lista todos os arquivos da pasta e subpastas."""
    out, stack = [], [folder_id]
    while stack:
        fid = stack.pop()
        page = None
        while True:
            params = {
                "q": "'%s' in parents and trashed=false" % fid,
                "fields": "nextPageToken,files(id,name,mimeType)",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            data = check(s.get(DRIVE + "/files", params=params), "Listar Drive").json()
            for f in data.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    stack.append(f["id"])
                else:
                    out.append(f)
            page = data.get("nextPageToken")
            if not page:
                break
    return out


def find_caption(s, files, video_name, code):
    sheets = [f for f in files if f["mimeType"] == "application/vnd.google-apps.spreadsheet"]
    for sh in sheets:
        r = s.get(
            "%s/files/%s/export" % (DRIVE, sh["id"]),
            params={"mimeType": "text/csv"},
        )
        if not r.ok:
            continue
        rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
        for i, row in enumerate(rows):
            cells = [norm(c) for c in row]
            if "arquivo" in cells and any(c.startswith("legenda") for c in cells):
                col_file = cells.index("arquivo")
                col_cap = next(k for k, c in enumerate(cells) if c.startswith("legenda"))
                for data in rows[i + 1:]:
                    if len(data) > max(col_file, col_cap) and data[col_file].strip() == video_name:
                        cap = data[col_cap].strip()
                        if cap:
                            return cap
                break
    fail("Legenda de %s não encontrada nas planilhas de índice." % code)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    s = drive_session()
    files = list_tree(s, FOLDER_ID)
    videos = []
    for f in files:
        m = NAME_RE.match(f["name"])
        if m:
            videos.append((m.group(1).upper(), m.group(2), f))

    if REEL_ARG:
        chosen = [v for v in videos if v[0] == REEL_ARG]
        label = REEL_ARG
    else:
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d")
        chosen = [v for v in videos if v[1] == today]
        label = "data " + today
    if len(chosen) == 0:
        fail("Nenhum vídeo encontrado para %s no Drive." % label)
    if len(chosen) > 1:
        fail("Mais de um vídeo para %s: %s" % (label, [c[2]["name"] for c in chosen]))
    code, date, vf = chosen[0]
    print("Reel escolhido: %s (%s)" % (vf["name"], date))

    state = load_state()
    if code in state and not DRY_RUN:
        print("%s já foi publicado antes (%s). Nada a fazer." % (code, state[code].get("permalink", "")))
        return

    caption = find_caption(s, files, vf["name"], code)
    print("Legenda ok (%d caracteres)." % len(caption))

    path = "/tmp/reel.mp4"
    with s.get("%s/files/%s" % (DRIVE, vf["id"]), params={"alt": "media"}, stream=True) as r:
        check(r, "Baixar vídeo do Drive")
        with open(path, "wb") as out:
            for chunk in r.iter_content(1024 * 1024):
                out.write(chunk)
    size = os.path.getsize(path)
    print("Vídeo baixado: %.1f MB" % (size / 1e6))
    if size < 100_000:
        fail("Vídeo baixado parece vazio ou corrompido.")

    # Os vídeos não têm faixa de áudio; sem ela o Instagram não consegue
    # encaixar a música. Acrescentamos uma faixa de silêncio (a imagem não muda).
    import subprocess
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    info = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    if "Audio:" not in info:
        out = "/tmp/reel_audio.mp4"
        r = subprocess.run([
            ff, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart", out,
        ], capture_output=True, text=True)
        if r.returncode != 0:
            fail("Não consegui acrescentar a faixa de áudio: " + r.stderr[:500])
        path = out
        print("Faixa de silêncio acrescentada (%.1f MB)." % (os.path.getsize(path) / 1e6))
    else:
        print("Vídeo já tem faixa de áudio.")

    # O Instagram só aplica a música quando busca o vídeo por um link.
    # Colocamos só o vídeo do dia como anexo temporário no GitHub e apagamos no fim.
    tag = "tmp-%s-%d" % (code.lower(), int(time.time()))
    rel = check(
        requests.post(GH_API + "/releases", headers=GH_AUTH, json={
            "tag_name": tag, "name": tag, "prerelease": True,
            "body": "Arquivo temporário para o Instagram baixar. Apagado automaticamente.",
        }, timeout=60),
        "Criar anexo temporário",
    ).json()
    try:
        with open(path, "rb") as f:
            asset = check(
                requests.post(
                    "https://uploads.github.com/repos/%s/releases/%s/assets" % (GH_REPO, rel["id"]),
                    headers=dict(GH_AUTH, **{"Content-Type": "video/mp4"}),
                    params={"name": "%s.mp4" % code},
                    data=f,
                    timeout=600,
                ),
                "Enviar vídeo ao anexo temporário",
            ).json()
        video_url = asset["browser_download_url"]
        print("Link temporário criado.")

        musica = escolher_musica(code)
        print("Música: " + musica["nome"])

        def criar(audio, vv):
            data = {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": "true",
                "thumb_offset": str(CAPA_MS),
            }
            if audio:
                data["audio_configuration"] = json.dumps({
                    "audio_id": musica["audio_id"],
                    "audio_volume": VOLUME_MUSICA,
                    "video_volume": vv,
                })
            r = check(requests.post("%s/%s/media" % (GRAPH, IG_USER_ID), headers=AUTH, data=data, timeout=60),
                      "Criar container no Instagram")
            return r.json()["id"]

        def esperar(cid):
            for _ in range(60):
                r = check(requests.get("%s/%s" % (GRAPH, cid), headers=AUTH,
                                       params={"fields": "status_code,status"}, timeout=60), "Consultar status")
                j = r.json()
                if j.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                    return j
                time.sleep(10)
            return {"status_code": "TIMEOUT"}

        if DRY_RUN and os.environ.get("DIAGNOSTICO", "0") == "1":
            for nome, audio, vv in (("sem música", False, 100), ("com música, som do vídeo 100", True, 100),
                                    ("com música, som do vídeo 1", True, 1)):
                j = esperar(criar(audio, vv))
                print("DIAGNÓSTICO [%s]: %s" % (nome, j.get("status", j.get("status_code"))))
            fail("Diagnóstico concluído (nada publicado).")

        container = criar(True, VOLUME_VIDEO)
        print("Container criado: " + container)
        j = esperar(container)
        status = j.get("status_code", "")
        print("Status: " + status)
        if status != "FINISHED":
            fail("Instagram recusou o vídeo: " + json.dumps(j)[:500])
    finally:
        requests.delete("%s/releases/%s" % (GH_API, rel["id"]), headers=GH_AUTH, timeout=60)
        requests.delete("%s/git/refs/tags/%s" % (GH_API, tag), headers=GH_AUTH, timeout=60)
        print("Link temporário apagado.")

    if DRY_RUN:
        print("TESTE OK (dry-run): tudo funcionou, só não publiquei.")
        return

    r = check(
        requests.post(
            "%s/%s/media_publish" % (GRAPH, IG_USER_ID),
            headers=AUTH,
            data={"creation_id": container},
            timeout=120,
        ),
        "Publicar",
    )
    media_id = r.json()["id"]
    permalink = ""
    pr = requests.get(
        "%s/%s" % (GRAPH, media_id), headers=AUTH, params={"fields": "permalink"}, timeout=60
    )
    if pr.ok:
        permalink = pr.json().get("permalink", "")
    state[code] = {
        "media_id": media_id,
        "permalink": permalink,
        "publicado_em": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds"),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print("PUBLICADO: %s %s" % (code, permalink))


if __name__ == "__main__":
    main()
