#!/usr/bin/env python3
"""
Installs the Unbiased News Tab (/news) into a local open-webui git checkout.
Usage:
    python3 apply_patch.py /path/to/open-webui
"""
import os
import shutil
import sys


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python3 apply_patch.py /path/to/open-webui")
        sys.exit(1)

    repo = os.path.abspath(sys.argv[1])
    here = os.path.dirname(os.path.abspath(__file__))

    # 1. Copy backend router
    dst_router = os.path.join(repo, "backend/open_webui/routers/news.py")
    shutil.copy2(os.path.join(here, "backend/open_webui/routers/news.py"), dst_router)
    print("Installed:", dst_router)

    # 2. Copy SvelteKit /news page
    dst_page_dir = os.path.join(repo, "src/routes/(app)/news")
    os.makedirs(dst_page_dir, exist_ok=True)
    dst_page = os.path.join(dst_page_dir, "+page.svelte")
    shutil.copy2(os.path.join(here, "src/routes/(app)/news/+page.svelte"), dst_page)
    print("Installed:", dst_page)

    # 3. Wire router into backend/open_webui/main.py
    main_py = os.path.join(repo, "backend/open_webui/main.py")
    if os.path.exists(main_py):
        txt = open(main_py, encoding="utf-8").read()
        if "routers.news" not in txt and "news," not in txt:
            txt = txt.replace(
                "from open_webui.routers import (",
                "from open_webui.routers import (\n    news,",
                1,
            )
            txt = txt.replace(
                'app.include_router(notes.router, prefix="/api/v1/notes", tags=["notes"])',
                'app.include_router(notes.router, prefix="/api/v1/notes", tags=["notes"])\napp.include_router(news.router, prefix="/api/v1/news", tags=["news"])',
                1,
            )
            open(main_py, "w", encoding="utf-8").write(txt)
            print("Patched:", main_py)

    # 4. Insert clickable News tab into Sidebar.svelte
    sidebar_svelte = os.path.join(repo, "src/lib/components/layout/Sidebar.svelte")
    snippet = open(os.path.join(here, "Sidebar.news-snippet.svelte"), encoding="utf-8").read()
    if os.path.exists(sidebar_svelte):
        stxt = open(sidebar_svelte, encoding="utf-8").read()
        if 'href="/news"' not in stxt and 'href="/notes"' in stxt:
            idx = stxt.find('href="/notes"')
            block_start = stxt.rfind("<div", 0, idx)
            if block_start != -1:
                stxt = stxt[:block_start] + snippet + "\n" + stxt[block_start:]
                open(sidebar_svelte, "w", encoding="utf-8").write(stxt)
                print("Patched:", sidebar_svelte)
        else:
            print("Sidebar already patched or /notes anchor not found; insert Sidebar.news-snippet.svelte manually.")


if __name__ == "__main__":
    main()
