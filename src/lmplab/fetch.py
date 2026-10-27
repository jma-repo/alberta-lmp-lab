"""Download the AESO public files this project is built on (they are never committed).

AESO Engage serves documents through a page that starts the download; its `/download`
endpoint redirects to the file itself, which is what is requested here.
"""

from __future__ import annotations

import httpx

from lmplab.config import Settings


def run(settings: Settings, force: bool = False) -> list[str]:
    lines = []
    for name, source in settings.config["sources"].items():
        target = settings.source_path(name)
        if target.exists() and not force:
            lines.append(f"{name}: present ({target.stat().st_size / 1e6:.1f} MB)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", source["url"], follow_redirects=True, timeout=180.0) as response:
            response.raise_for_status()
            if "text/html" in response.headers.get("content-type", ""):
                raise RuntimeError(f"{name}: expected a file but got an HTML page from {source['url']}")
            with target.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        lines.append(f"{name}: downloaded {target.stat().st_size / 1e6:.1f} MB from {source['url']}")
    return lines
