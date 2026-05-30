#!/usr/bin/env python3
"""Проверка, можно ли достучаться до VFS Global через список прокси.

Формат прокси (одна строка = один прокси):
    host:port:user:pass

Прокси читаются из файла proxies.txt (рядом со скриптом) либо из аргументов:
    python3 check_proxies.py                # читает proxies.txt
    python3 check_proxies.py proxies.txt    # явный путь к файлу

Для каждого прокси скрипт:
  1. узнаёт внешний IP (api.ipify.org) — подтверждает, что прокси вообще живой;
  2. делает запрос к VFS Global и печатает HTTP-статус.

Зависимости: requests  ->  pip install requests
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Нужен пакет requests:  pip install requests")

# Цель проверки. Можно поменять на нужный URL VFS (например, страницу конкретной страны).
VFS_URL = "https://visa.vfsglobal.com/"
IP_URL = "https://api.ipify.org?format=json"

TIMEOUT = 30          # секунд на запрос
MAX_WORKERS = 5       # сколько прокси проверять параллельно

# Похожий на реальный браузер заголовок — VFS режет «голые» python-запросы.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}


# niceproxy.io отвечает по SOCKS5, а не по HTTP-CONNECT (проверено curl'ом:
# http-туннель виснет, socks5 отдаёт IP мгновенно). socks5h = DNS резолвит
# сам прокси (на стороне РФ), а не наша машина — это важно для гео-блоков.
PROXY_SCHEME = "socks5h"


def parse_proxy(line: str) -> str | None:
    """host:port:user:pass  ->  socks5h://user:pass@host:port"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) != 4:
        print(f"[!] Пропускаю строку (не host:port:user:pass): {line}")
        return None
    host, port, user, password = parts
    return f"{PROXY_SCHEME}://{user}:{password}@{host}:{port}"


def short(url: str) -> str:
    """Скрыть пароль при выводе: scheme://user:pass@host:port -> host:port (user)"""
    try:
        creds, hostport = url.split("@", 1)
        user = creds.split("//", 1)[1].split(":", 1)[0]
        return f"{hostport} ({user})"
    except (ValueError, IndexError):
        return url


def check(proxy_url: str) -> dict:
    label = short(proxy_url)
    proxies = {"http": proxy_url, "https": proxy_url}
    result = {"label": label, "ip": None, "vfs_status": None, "error": None}

    start = time.monotonic()
    try:
        # 1) живой ли прокси + какой выходной IP
        r_ip = requests.get(IP_URL, proxies=proxies, timeout=TIMEOUT)
        result["ip"] = r_ip.json().get("ip")

        # 2) дотягивается ли до VFS
        r_vfs = requests.get(
            VFS_URL, proxies=proxies, headers=HEADERS, timeout=TIMEOUT
        )
        result["vfs_status"] = r_vfs.status_code
    except requests.exceptions.RequestException as exc:
        result["error"] = type(exc).__name__
    finally:
        result["elapsed"] = round(time.monotonic() - start, 1)
    return result


def load_proxies(argv: list[str]) -> list[str]:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).with_name("proxies.txt")
    if not path.exists():
        sys.exit(
            f"Файл с прокси не найден: {path}\n"
            "Создай proxies.txt (host:port:user:pass в каждой строке) "
            "или передай путь аргументом."
        )
    proxies = [p for p in (parse_proxy(l) for l in path.read_text().splitlines()) if p]
    if not proxies:
        sys.exit("В файле нет валидных прокси.")
    return proxies


def main() -> None:
    proxies = load_proxies(sys.argv)
    print(f"Проверяю {len(proxies)} прокси на доступ к {VFS_URL}\n")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check, p): p for p in proxies}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            if res["error"]:
                print(f"[FAIL] {res['label']:45} {res['error']} ({res['elapsed']}s)")
            else:
                ok = res["vfs_status"] and res["vfs_status"] < 400
                tag = "[ OK ]" if ok else "[WARN]"
                print(
                    f"{tag} {res['label']:45} "
                    f"IP={res['ip']:<16} VFS={res['vfs_status']} ({res['elapsed']}s)"
                )

    good = sum(1 for r in results if r["vfs_status"] and r["vfs_status"] < 400)
    print(f"\nИтог: {good}/{len(results)} прокси достучались до VFS (HTTP < 400).")


if __name__ == "__main__":
    main()
