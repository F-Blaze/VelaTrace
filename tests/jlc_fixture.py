"""Tiny JLCPCB catalogue in the upstream schema, plus a fake HTTP layer. No network."""
import io
import json
from pathlib import Path
import sqlite3
import zipfile

from velatrace import jlc_catalog
from velatrace.jlc_http import JlcNetError

R = ("Resistors", "Chip Resistor - Surface Mount")
C = ("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT")
# code, (first, second), mpn, package, library type, description, price, stock
ROWS = [
    ("C25744", R, "0402WGF1002TCE", "0402", "Basic", "62.5mW 10kΩ 50V Thick Film Resistor ±1%", "1-:0.001", 5000000),
    ("C25804", R, "0603WAF1002T5E", "0603", "Basic", "100mW 10kΩ 75V Thick Film Resistor ±1%", "1-:0.002", 9000000),
    ("C21190", R, "0603WAF1001T5E", "0603", "Basic", "100mW 1kΩ 75V Thick Film Resistor ±1%", "1-:0.002", 8000000),
    ("C25900", R, "0402WGF4701TCE", "0402", "Basic", "62.5mW 4.7kΩ 50V Thick Film Resistor ±1%", "1-:0.001", 0),
    ("C375503", R, "RC0603FR-071KL", "0603", "Extended", "100mW 1kΩ 75V Thick Film Resistor ±1%",
     "1-199:0.004,200-:0.003", 120000),
    ("C1525", C, "CL05B104KO5NNNC", "0402", "Basic", "100nF 16V X7R ±10%", "1-:0.004", 24000000),
    ("C307331", C, "CL05B104KB54PNC", "0402", "Basic", "100nF 50V X7R ±10%", "1-:0.009", 14000000),
    ("C52923", C, "CL05A105KA5NQNC", "0402", "Basic", "1uF 25V X5R ±10%", "1-:0.006", 9000000),
    ("C15525", C, "CL05A106MQ5NUNC", "0402", "Basic", "10uF 6.3V X5R ±20%", "1-:0.012", 3000000),
    ("C1608", C, "GRM155R71C104KA88D", "0402", "Extended", "100nF 16V X7R ±10%",
     "1-99:0.0120,100-999:0.0080,1000-:0.0050", 60000),
    ("C8734", ("Embedded Processors & Controllers", "Microcontrollers (MCU/MPU/SOC)"), "STM32F103C8T6",
     "LQFP-48(7x7)", "Preferred", "64KB 72MHz ARM Cortex-M3", "1-9:1.590,10-:1.375", 150000),
    ("C2040", ("Embedded Processors & Controllers", "Microcontrollers (MCU/MPU/SOC)"), "RP2040",
     "LQFN-56(7x7)", "Extended", "12bit 133MHz", "1-9:0.987,10-29:0.900,30-:0.817", 72000),
]


def build_db(path: Path, rows=ROWS) -> Path:
    db = sqlite3.connect(path)
    db.execute("CREATE VIRTUAL TABLE parts USING fts5('LCSC Part', 'First Category', 'Second Category', "
               "'MFR.Part', 'Package', 'Solder Joint' unindexed, 'Manufacturer', 'Library Type', "
               "'Description', 'Datasheet' unindexed, 'Price' unindexed, 'Stock' unindexed, "
               "tokenize=\"trigram\")")
    db.executemany("INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (code, cat[0], cat[1], mpn, package, 2, "ACME", library, text,
         f"https://example.invalid/{code}.pdf", price, str(stock))
        for code, cat, mpn, package, library, text, price, stock in rows])
    db.commit()
    db.close()
    return path


def install(config: Path, rows=ROWS):
    """Put a verified fixture catalogue where load() expects it and return it opened."""
    server = Server(config / "upstream.db", rows=rows)
    jlc_catalog.download(config, fetch=server.fetch, min_basic_resistors=1, min_basic_capacitors=1)
    return jlc_catalog.load(config)


class Server:
    """Stands in for the GitHub Pages host: chunked zip, HEAD, GET and Range."""

    def __init__(self, scratch: Path, rows=ROWS, chunk=700, published="Sat, 26 Sep 2026 10:41:07 GMT"):
        scratch.parent.mkdir(parents=True, exist_ok=True)
        build_db(scratch, rows)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(scratch, "current-parts-fts5.db")
        scratch.unlink()
        self.blob = buffer.getvalue()
        self.files = {jlc_catalog.CHUNK_FILE.format(i + 1): self.blob[offset:offset + chunk]
                      for i, offset in enumerate(range(0, len(self.blob), chunk))}
        self.files[jlc_catalog.COUNT_FILE] = str(len(self.files)).encode()
        self.published = published
        self.requests = []
        self.fail_after = None  # bytes served by GETs before the connection "drops"
        self.served = 0

    def fetch(self, url, *, method="GET", headers=None, timeout=30.0, max_bytes=1_000_000,
              sink=None, cancel=None):
        assert url.startswith(jlc_catalog.BASE_URL), url
        name = url[len(jlc_catalog.BASE_URL):]
        self.requests.append((method, name, dict(headers or {})))
        data = self.files[name]
        found = {"content-length": str(len(data)), "last-modified": self.published}
        if method == "HEAD":
            return 200, found, b""
        status, start = 200, 0
        if "Range" in (headers or {}):
            start = int(headers["Range"].removeprefix("bytes=").rstrip("-"))
            status, found["content-range"] = 206, f"bytes {start}-{len(data) - 1}/{len(data)}"
        body = data[start:]
        assert len(body) <= max_bytes
        if sink is None:
            return status, found, body
        for offset in range(0, len(body), 256):
            if self.fail_after is not None and self.served >= self.fail_after:
                raise JlcNetError("connection dropped")
            piece = body[offset:offset + 256]
            sink(piece)
            self.served += len(piece)
        return status, found, b""


def live_payload(code, *, stock=1000, library="expand", prices=((1, 9, 0.05), (10, None, 0.04)),
                 mpn="MPN", buyable=True):
    return json.dumps({"code": 200, "data": {
        "componentCode": code, "componentModelEn": mpn, "stockCount": stock,
        "componentLibraryType": library, "isBuyComponent": "1" if buyable else "0",
        "prices": [{"startNumber": a, "endNumber": b if b is not None else -1, "productPrice": p}
                   for a, b, p in prices]}}).encode()
