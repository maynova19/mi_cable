#!/usr/bin/env python3
"""
Genera una lista M3U propia a partir de listas públicas.

- Descarga cada fuente y guarda una copia en fuentes/ (respaldo: si la fuente
  original desaparece, se sigue usando la última copia guardada).
- Une todo, quita duplicados y canales excluidos.
- Prueba cada canal y deja solo los que responden.
- Agrega tus canales manuales de extras.m3u.
- Escribe lista.m3u (para el TV / VLC) e informe.md (resumen).

Solo usa la librería estándar de Python: no hay que instalar nada.
Uso:  python generar_lista.py
"""
import concurrent.futures as cf
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
CONFIG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
DIR_FUENTES = RAIZ / "fuentes"
SALIDA = RAIZ / "lista.m3u"
INFORME = RAIZ / "informe.md"
EXTRAS = RAIZ / "extras.m3u"

TIMEOUT = CONFIG.get("timeout_segundos", 10)
HILOS = CONFIG.get("hilos", 40)
UA_POR_DEFECTO = "VLC/3.0.20 LibVLC/3.0.20"


# ---------- lectura de listas ----------

def parsear_m3u(texto):
    """Devuelve una lista de canales: {extinf, opciones, url, nombre, grupo}."""
    canales, extinf, opciones = [], None, []
    for linea in texto.splitlines():
        l = linea.strip()
        if not l:
            continue
        if l.startswith("#EXTINF"):
            extinf, opciones = l, []
        elif l.startswith(("#EXTVLCOPT", "#KODIPROP")):
            if extinf:
                opciones.append(l)
        elif l.startswith("#"):
            continue
        else:
            if extinf:
                canales.append({
                    "extinf": extinf,
                    "opciones": opciones,
                    "url": l,
                    "nombre": nombre_de(extinf),
                    "grupo": atributo(extinf, "group-title") or "Sin grupo",
                })
            extinf, opciones = None, []
    return canales


def nombre_de(extinf):
    ultima_comilla = extinf.rfind('"')
    coma = extinf.find(",", ultima_comilla + 1 if ultima_comilla != -1 else 0)
    return extinf[coma + 1:].strip() if coma != -1 else extinf


def atributo(extinf, clave):
    m = re.search(rf'{clave}="([^"]*)"', extinf)
    return m.group(1) if m else None


def cabeceras_de(canal):
    h = {"User-Agent": UA_POR_DEFECTO}
    for op in canal["opciones"]:
        if op.startswith("#EXTVLCOPT:http-user-agent="):
            h["User-Agent"] = op.split("=", 1)[1]
        elif op.startswith("#EXTVLCOPT:http-referrer="):
            h["Referer"] = op.split("=", 1)[1]
    return h


# ---------- descarga de fuentes (con respaldo) ----------

def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA_POR_DEFECTO})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


def obtener_fuentes():
    DIR_FUENTES.mkdir(exist_ok=True)
    canales, estado = [], {}
    for nombre, url in CONFIG["fuentes"].items():
        archivo = re.sub(r"[^\w-]+", "_", nombre).strip("_") or "fuente"
        copia = DIR_FUENTES / f"{archivo}.m3u"
        try:
            texto = descargar(url)
            if "#EXTINF" not in texto:
                raise ValueError("no parece una lista M3U")
            copia.write_text(texto, encoding="utf-8")
            estado[nombre] = "descargada"
        except Exception as e:
            if copia.exists():
                texto = copia.read_text(encoding="utf-8")
                estado[nombre] = f"falló la descarga ({e}); usando respaldo guardado"
            else:
                estado[nombre] = f"falló la descarga ({e}) y no hay respaldo"
                continue
        for c in parsear_m3u(texto):
            c["fuente"] = nombre
            canales.append(c)
    return canales, estado


def es_ip_directa(url):
    """True si la URL apunta a una IP (http://1.2.3.4:8080/...) en vez de a un dominio."""
    import ipaddress
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def poner_grupo(extinf, grupo):
    """Cambia (o agrega) el group-title de una línea #EXTINF."""
    grupo = grupo.replace('"', "'")
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{grupo}"', extinf, count=1)
    return extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{grupo}"', 1)


# ---------- verificación de canales ----------

def pedir(url, cabeceras):
    req = urllib.request.Request(url, headers=cabeceras)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.geturl(), r.read(65536)


MARCAS_DRM = ("SAMPLE-AES", "WIDEVINE", "PLAYREADY", "FAIRPLAY", "EDEF8BA9", "9A04F079")


def funciona(canal):
    """True solo si se llega a descargar un pedazo real de video.

    Sigue la cadena HLS (lista maestra -> lista de calidad -> segmento) y
    descarta los canales con DRM, que ningún reproductor genérico puede abrir.
    """
    cab = cabeceras_de(canal)
    url = canal["url"]
    try:
        for _ in range(4):  # máximo 4 niveles de listas anidadas
            estado, url_final, cuerpo = pedir(url, cab)
            if estado != 200 or not cuerpo:
                return False
            if not cuerpo.lstrip().startswith(b"#EXTM3U"):
                return True  # llegamos a datos de video reales
            texto = cuerpo.decode("utf-8", "ignore")
            mayus = texto.upper()
            if "#EXT-X-KEY" in mayus or "#EXT-X-SESSION-KEY" in mayus:
                if any(m in mayus for m in MARCAS_DRM):
                    return False  # protegido con DRM
            siguiente = next(
                (l.strip() for l in texto.splitlines() if l.strip() and not l.strip().startswith("#")),
                None,
            )
            if not siguiente:
                return False  # lista vacía
            url = urllib.parse.urljoin(url_final, siguiente)
        return False
    except Exception:
        return False


# ---------- armado de la lista ----------

def main():
    canales, estado_fuentes = obtener_fuentes()
    if not canales:
        print("No se pudo obtener ninguna fuente; se conserva la lista anterior.")
        return 0

    # Duplicados y exclusiones
    excluir = [p.lower() for p in CONFIG.get("excluir_si_el_nombre_contiene", [])]
    sin_ip = CONFIG.get("excluir_ip_directa", False)
    ip_permitida = set(CONFIG.get("permitir_ip_en_fuentes", []))
    vistos, unicos, por_ip = set(), [], 0
    for c in canales:
        if c["url"] in vistos:
            continue
        if any(p in c["nombre"].lower() for p in excluir):
            continue
        if sin_ip and c["fuente"] not in ip_permitida and es_ip_directa(c["url"]):
            por_ip += 1
            continue
        vistos.add(c["url"])
        unicos.append(c)

    # Verificación en paralelo
    if CONFIG.get("verificar", True):
        print(f"Probando {len(unicos)} canales...")
        with cf.ThreadPoolExecutor(max_workers=HILOS) as ex:
            resultados = list(ex.map(funciona, unicos))
        vivos = [c for c, ok in zip(unicos, resultados) if ok]
        muertos = [c for c, ok in zip(unicos, resultados) if not ok]
    else:
        vivos, muertos = unicos, []

    if not vivos and SALIDA.exists():
        print("Ningún canal respondió (¿problema de red?); se conserva la lista anterior.")
        return 0

    # Categorías
    destacadas = CONFIG.get("categorias_destacadas", {})
    for c in vivos:
        c["destacada"] = None
        cats = [x.strip().lower() for x in c["grupo"].split(";")]
        for original, etiqueta in destacadas.items():
            if original.lower() in cats:
                c["destacada"] = etiqueta
                break

    # Agrupa por fuente
    modo = CONFIG.get("agrupar_por_fuente", "fuente_y_categoria")
    orden_fuente = {n: i for i, n in enumerate(CONFIG["fuentes"])}
    orden_dest = {e: i for i, e in enumerate(destacadas.values())}
    for c in vivos:
        if c["destacada"]:
            nuevo = f'{c["destacada"]} | {c["fuente"]}'
        elif modo == "fuente":
            nuevo = c["fuente"]
        elif modo == "fuente_y_categoria":
            nuevo = f'{c["fuente"]} | {c["grupo"]}'
        else:
            nuevo = c["grupo"]
        c["extinf"] = poner_grupo(c["extinf"], nuevo)
        c["grupo"] = nuevo
    # Primero las carpetas destacadas, luego cada fuente en el orden de config.json
    vivos.sort(key=lambda c: (
        0 if c["destacada"] else 1,
        orden_dest.get(c["destacada"], 0),
        orden_fuente.get(c["fuente"], 99),
        c["grupo"].lower(),
        c["nombre"].lower(),
    ))

    # Canales manuales (no se verifican: los pones tú)
    extras = parsear_m3u(EXTRAS.read_text(encoding="utf-8")) if EXTRAS.exists() else []

    lineas = ["#EXTM3U"]
    for c in extras + vivos:
        lineas.append(c["extinf"])
        lineas.extend(c["opciones"])
        lineas.append(c["url"])
    SALIDA.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    # Informe
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    inf = [
        "# Informe de la lista", "",
        f"Actualizada: {ahora}", "",
        "## Fuentes", "",
        *[f"- **{n}**: {e}" for n, e in estado_fuentes.items()], "",
        "## Resultado", "",
        f"- Excluidos por IP directa: {por_ip}",
        f"- Canales únicos revisados: {len(unicos)}",
        f"- Funcionando: {len(vivos)}",
        f"- Sin respuesta (descartados): {len(muertos)}",
        f"- Manuales (extras.m3u): {len(extras)}", "",
    ]
    if muertos:
        inf += ["## Descartados", "", *sorted(f"- {c['nombre']}" for c in muertos), ""]
    INFORME.write_text("\n".join(inf), encoding="utf-8")

    print(f"Listo: {len(vivos)} funcionando, {len(muertos)} descartados, {len(extras)} manuales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())