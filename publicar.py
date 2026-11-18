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

GRAPH = "https://graph.facebook.com/v21.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"
DRIVE = "https://www.googleapis.com/drive/v3"
FOLDER_ID = "10G6Sq8OkwhJXu1auwvWQe0Gh8a5loZLi"  # 03 REELS PRONTOS
IG_USER_ID = os.environ.get("IG_USER_ID", "17841411266083237")
TOKEN = os.environ["IG_PAGE_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
REEL_ARG = os.environ.get("REEL", "").strip().upper()  # ex.: R017 (opcional)
STATE_FILE = "posted.json"
NAME_RE = re.compile(r"^(R\d{3})_(\d{4}-\d{2}-\d{2})_.*\.mp4$", re.IGNORECASE)
AUTH = {"Authorization": "Bearer " + TOKEN}


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
        check(r, "Baixar vídeo")
        with open(path, "wb") as out:
            for chunk in r.iter_content(1024 * 1024):
                out.write(chunk)
    size = os.path.getsize(path)
    print("Vídeo baixado: %.1f MB" % (size / 1e6))
    if size < 100_000:
        fail("Vídeo baixado parece vazio ou corrompido.")

    r = check(
        requests.post(
            "%s/%s/media" % (GRAPH, IG_USER_ID),
            headers=AUTH,
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true",
            },
            timeout=60,
        ),
        "Criar container no Instagram",
    )
    container = r.json()["id"]
    print("Container criado: " + container)

    with open(path, "rb") as f:
        r = check(
            requests.post(
                "%s/%s" % (RUPLOAD, container),
                headers=dict(AUTH, offset="0", file_size=str(size)),
                data=f,
                timeout=900,
            ),
            "Enviar vídeo ao Instagram",
        )
    print("Vídeo enviado.")

    status = ""
    for _ in range(60):
        r = check(
            requests.get(
                "%s/%s" % (GRAPH, container),
                headers=AUTH,
                params={"fields": "status_code,status"},
                timeout=60,
            ),
            "Consultar status",
        )
        status = r.json().get("status_code", "")
        print("Status: " + status)
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            fail("Instagram recusou o vídeo: " + r.text[:500])
        time.sleep(10)
    if status != "FINISHED":
        fail("Instagram não terminou de processar o vídeo a tempo.")

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
