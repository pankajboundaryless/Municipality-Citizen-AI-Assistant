"""
Standalone diagnostic test for UiPath Document Understanding ID extraction.
Run from the project root:  python test_uipath_id.py
Reads credentials from .env — no server startup required.
"""

import asyncio
import logging
import pathlib

from dotenv import load_dotenv

load_dotenv()

# Import AFTER load_dotenv so env vars are available at call time
from uipath_fetch_id import _cfg, _is_configured, extract_id_fields

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

SAMPLE_FILE = pathlib.Path(r"C:\Users\stefa\Desktop\Demo Data\HR\ci6.jpg")


async def main() -> None:
    print("\n=== UiPath Document Understanding — ID Extraction Test ===\n")

    # Print resolved config
    base, org, tenant, cid, csec, project = _cfg()
    print(f"  UIPATH_URL           = {base}")
    print(f"  UIPATH_ORGANIZATION  = {org  or '(not set)'}")
    print(f"  UIPATH_TENANT        = {tenant or '(not set)'}")
    print(f"  UIPATH_CLIENT_ID     = {cid[:4] + '…' if len(cid) > 4 else cid or '(not set)'}")
    print(f"  UIPATH_DU_PROJECT_ID = {project or '(not set)'}")

    if not _is_configured():
        print("ERROR: one or more required env vars are missing. Aborting.")
        print("  Required: UIPATH_ORGANIZATION, UIPATH_TENANT, UIPATH_CLIENT_ID,")
        print("            UIPATH_CLIENT_SECRET, UIPATH_DU_PROJECT_ID")
        return

    if not SAMPLE_FILE.exists():
        print(f"ERROR: sample file not found: {SAMPLE_FILE}")
        return

    file_bytes = SAMPLE_FILE.read_bytes()
    print(f"  File: {SAMPLE_FILE.name}  ({len(file_bytes):,} bytes)")
    print()
    print("Running extraction…")
    print("─" * 60)

    result = await extract_id_fields(file_bytes, SAMPLE_FILE.name)

    print("─" * 60)
    if result is None:
        print("\nResult: None — extraction returned no fields.")
        print("Check the WARNING lines above for the reason.")
    else:
        print(f"\nExtracted {len(result)} field(s):\n")
        for field, value in result.items():
            print(f"  {field}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
