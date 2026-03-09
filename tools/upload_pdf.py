from __future__ import annotations

import argparse
from pathlib import Path

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload a PDF to zotero-bridge.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--bridge-token", required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--item-key")
    parser.add_argument("--doi")
    parser.add_argument("--collection-key")
    parser.add_argument("--request-id")
    parser.add_argument("--create-top-level", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with args.file.open("rb") as handle:
        files = {
            "file": (
                args.file.name,
                handle,
                "application/pdf",
            )
        }
        data = {
            "itemKey": args.item_key or "",
            "doi": args.doi or "",
            "collectionKey": args.collection_key or "",
            "requestId": args.request_id or "",
            "createTopLevelAttachmentIfNeeded": str(args.create_top_level).lower(),
        }
        response = httpx.post(
            f"{args.base_url.rstrip('/')}/v1/papers/upload-pdf-multipart",
            headers={"Authorization": f"Bearer {args.bridge_token}"},
            data=data,
            files=files,
            timeout=120.0,
        )
    response.raise_for_status()
    print(response.text)


if __name__ == "__main__":
    main()
