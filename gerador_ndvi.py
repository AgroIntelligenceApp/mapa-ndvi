# -*- coding: utf-8 -*-
"""
Gerador de NDVI (Sentinel-2) para os talhoes do Supabase.
Grava imagem (base64) + data real da cena + NDVI medio + Area em Hectares.
"""
import io
import os
import json
import time
import base64
import math

import requests
from PIL import Image, ImageDraw
from supabase import create_client

# ================== CONFIGURACAO ==================
SUPABASE_URL       = "https://keiydgyountsuzjybsng.supabase.co"
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
CDSE_CLIENT_ID     = os.environ["CDSE_CLIENT_ID"]
CDSE_CLIENT_SECRET = os.environ["CDSE_CLIENT_SECRET"]
CLOUD_COVER_MAX = 30      # % maximo de nuvem aceito
IMG_SIZE = 512            # resolucao do PNG gerado
TOKEN_VIDA_UTILITY = 3000 # renova o token apos 50 min (ele dura ~60 min)
# ==================================================

_token = {"valor": None, "emitido_em": 0.0}


def get_token(forcar=False):
    """Retorna o token em cache se ainda estiver valido; senao, emite um novo."""
    agora = time.time()
    if (not forcar and _token["valor"] is not None
            and (agora - _token["emitido_em"]) < TOKEN_VIDA_UTILITY):
        return _token["valor"]

    r = requests.post(
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CDSE_CLIENT_ID,
            "client_secret": CDSE_CLIENT_SECRET,
        },
        timeout=30,
    )
    r.raise_for_status()
    _token["valor"] = r.json()["access_token"]
    _token["emitido_em"] = agora
    print(">>> Token do Copernicus renovado.")
    return _token["valor"]


def extrair_poligono(g):
    """Recebe geom (dict GeoJSON) e retorna os aneis do poligono principal."""
    if g.get("type") == "FeatureCollection":
        g = g["features"][0]
    if g.get("type") == "Feature":
        g = g["geometry"]
    if g["type"] == "Polygon":
        return g["coordinates"]
    if g["type"] == "MultiPolygon":
        return g["coordinates"][0]
    raise ValueError("Geometria nao suportada: " + str(g.get("type")))


def bbox_do_anel(anel):
    xs = [p[0] for p in anel]
    ys = [p[1] for p in anel]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_wkt(bbox):
    """Converte o bbox em WKT compacto (4 pontos, fechado)."""
    minx, miny, maxx, maxy = bbox
    return (f"POLYGON(({minx} {miny},{maxx} {miny},"
            f"{maxx} {maxy},{minx} {maxy},{minx} {miny}))")


def ultima_cena(wkt, depois_de):
    """Busca as 10 cenas mais recentes sobre a area (com atributos incluidos)
    e escolhe a mais nova com nuvem abaixo do limite."""
    filtro = (
        "Collection/Name eq 'SENTINEL-2'"
        " and contains(Name,'MSIL2A')"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')"
        f" and ContentDate/Start gt {depois_de}T00:00:00.000Z"
    )
    url = ("https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
           f"?$filter={filtro}"
           "&$orderby=ContentDate/Start desc"
           "&$top=10"
           "&$expand=Attributes")

    r = requests.get(url, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Catalogo {r.status_code}: {r.text[:300]}")

    for p in r.json().get("value", []):
        cobertura = None
        for attr in p.get("Attributes", []):
            if str(attr.get("Name", "")).lower() == "cloudcover":
                try:
                    cobertura = float(attr.get("Value"))
                except (TypeError, ValueError):
                    cobertura = None
                break
        if cobertura is None or cobertura < CLOUD_COVER_MAX:
            return p["ContentDate"]["Start"][:10]
    return None


# O Evalscript agora esconde o valor do NDVI (escalado de 0 a 255) no canal Alpha (4ª banda)
EVALSCRIPT = """
//VERSION=3
function setup() {
  return { input: ["B04","B08","dataMask"],
           output: { bands: 4, sampleType: "UINT8" } };
}
function evaluatePixel(s) {
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.000001);
  var r, g, b;
  if      (ndvi < 0.00) { r = 110; g = 70;  b = 40; }
  else if (ndvi < 0.15) { r = 215; g = 30;  b = 25; }
  else if (ndvi < 0.30) { r = 240; g = 140; b = 40; }
  else if (ndvi < 0.45) { r = 250; g = 220; b = 60; }
  else if (ndvi < 0.60) { r = 130; g = 200; b = 60; }
  else                  { r = 20;  g = 130; b = 40; }
  
  // Esconde o NDVI no canal Alpha: NDVI de -1 a 1 vira de 0 a 255
  var ndvi_scaled = Math.round((ndvi + 1) * 127.5);
  if (ndvi_scaled > 255) ndvi_scaled = 255;
  if (ndvi_scaled < 0) ndvi_scaled = 0;
  
  return [r, g, b, (s.dataMask == 1) ? ndvi_scaled : 0];
}
"""


def gerar_png_ndvi(bbox, data_cena):
    """Chama a Process API do Copernicus. Se der 401 (token expirado),
    renova o token e tenta mais uma vez."""
    body = {
        "input": {
            "bounds": {
                "bbox": list(bbox),
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": f"{data_cena}T00:00:00Z",
                        "to": f"{data_cena}T23:59:59Z",
                    },
                    "maxCloudCoverage": CLOUD_COVER_MAX,
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {
            "width": IMG_SIZE,
            "height": IMG_SIZE,
            "responses": [{
                "identifier": "default",
                "format": {"type": "image/png"},
            }],
        },
        "evalscript": EVALSCRIPT,
    }

    for tentativa in (1, 2):
        r = requests.post(
            "https://sh.dataspace.copernicus.eu/api/v1/process",
            json=body,
            headers={"Authorization": f"Bearer {get_token()}"},
            timeout=180,
        )
        if r.status_code == 401 and tentativa == 1:
            get_token(forcar=True)   # renova e tenta de novo
            continue
        break

    if r.status_code != 200:
        raise RuntimeError(f"Process API {r.status_code}: {r.text[:200]}")
    return r.content


def calcular_area_hectares(geom):
    """Calcula a area de um poligono GeoJSON em hectares (Formula Esferica)."""
    coords = extrair_poligono(geom)[0]
    R = 6378137.0 # Raio da Terra em metros
    area = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = math.radians(coords[i][0]), math.radians(coords[i][1])
        lon2, lat2 = math.radians(coords[i+1][0]), math.radians(coords[i+1][1])
        area += (lon2 - lon1) * (2 + math.sin(lat1) + math.sin(lat2))
    area = abs(area * R * R / 2.0)
    return area / 10000.0 # Converte m2 para Hectares


def recortar_e_calcular_ndvi(png_bytes, aneis, bbox):
    """Aplica mascara alpha deixando visivel apenas a area do poligono.
    Aproveita para ler o NDVI escondido no canal Alpha e calcular a media."""
    minx, miny, maxx, maxy = bbox
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = img.size

    def px(pontos):
        return [((x - minx) / (maxx - minx) * w,
                 (maxy - y) / (maxy - miny) * h) for x, y in pontos]

    mascara = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mascara)
    d.polygon(px(aneis[0]), fill=255)
    for furo in aneis[1:]:
        d.polygon(px(furo), fill=0)

    # 1. Calcula NDVI Medio
    pix = img.load()
    pix_mask = mascara.load()
    ndvi_soma = 0.0
    pixels_validos = 0
    
    for x in range(w):
        for y in range(h):
            if pix_mask[x, y] == 255: # Dentro do talhao
                alpha = pix[x, y][3]
                if alpha > 0:
                    # Converte o valor 0-255 de volta para -1 a 1
                    ndvi = (alpha / 127.5) - 1.0
                    ndvi_soma += ndvi
                    pixels_validos += 1

    ndvi_medio = 0.0
    if pixels_validos > 0:
        ndvi_medio = ndvi_soma / pixels_validos

    # 2. Aplica a mascara final para a imagem web (deixa transparente fora do talhao)
    img.putalpha(mascara)
    return img, ndvi_medio


def salvar(sb, talhao_id, img, data_cena, ndvi_medio, area_ha):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    
    # Arredonda valores para nao lotar o banco com casas decimais desnecessarias
    ndvi_round = round(ndvi_medio, 3)
    area_round = round(area_ha, 2)

    sb.table("talhoes").update({
        "imagem_ndvi": base64.b64encode(buf.getvalue()).decode(),
        "data_ndvi": data_cena,
        "tem_ndvi": True,
        "ndvi_medio": ndvi_round,
        "area_hectares": area_round
    }).eq("id", talhao_id).execute()
    
    print(f"      -> NDVI Medio: {ndvi_round} | Area: {area_round} ha")


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Autenticando no Copernicus...")
    get_token()

    talhoes = sb.table("talhoes").select(
        "id, codigo_talhao, geom, data_ndvi"
    ).execute().data
    print(f"Encontrados {len(talhoes)} talhoes.\n")

    atualizados = 0
    for t in talhoes:
        nome = t["codigo_talhao"]
        try:
            geom = t["geom"]
            if isinstance(geom, str):
                geom = json.loads(geom)

            aneis = extrair_poligono(geom)
            bbox = bbox_do_anel(aneis[0])
            depois = (t.get("data_ndvi") or "2020-01-01")[:10]

            cena = ultima_cena(bbox_wkt(bbox), depois)
            if not cena:
                print(f"[{nome}] ja esta em dia.")
                continue

            print(f"[{nome}] Buscando imagem de {cena}...")
            
            # Calcula area em hectares antes de baixar a imagem
            area_ha = calcular_area_hectares(geom)
            
            png = gerar_png_ndvi(bbox, cena)
            img, ndvi_medio = recortar_e_calcular_ndvi(png, aneis, bbox)
            
            salvar(sb, t["id"], img, cena, ndvi_medio, area_ha)
            atualizados += 1
            print(f"[{nome}] ATUALIZADO com sucesso!")
            
        except Exception as e:
            print(f"[{nome}] ERRO: {e}")
        finally:
            time.sleep(1.0)  # respeita o limite de requisicoes da API

    print(f"\nConcluido. {atualizados} talhao(ns) atualizado(s).")

    try:
        verif = sb.table("talhoes").select(
            "id", count="exact"
        ).not_.is_("data_ndvi", "null").execute()
        print(f"Verificacao final: {verif.count} talhoes com data_ndvi preenchida no banco.")
    except Exception as e:
        print(f"(Nao foi possivel rodar a verificacao final: {e})")


if __name__ == "__main__":
    main()
