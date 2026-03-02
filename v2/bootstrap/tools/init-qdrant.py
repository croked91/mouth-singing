"""Create QDrant collections required by the karaoke app.

Run this before bootstrap to ensure collections exist:

    python init-qdrant.py --host localhost --port 6333
"""

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

COLLECTIONS = [
    ("audio_features", 45, Distance.COSINE),
    ("lyrics_embeddings", 384, Distance.COSINE),
    ("transitions", 45, Distance.COSINE),
]
PAYLOAD_INDEXES = ["status", "language", "source"]


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Create QDrant collections for karaoke app")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6333)
    args = parser.parse_args()

    client = QdrantClient(host=args.host, port=args.port)
    existing = {c.name for c in client.get_collections().collections}

    for name, dim, distance in COLLECTIONS:
        if name not in existing:
            client.create_collection(name, vectors_config=VectorParams(size=dim, distance=distance))
            for field in PAYLOAD_INDEXES:
                client.create_payload_index(name, field_name=field, field_schema=PayloadSchemaType.KEYWORD)
            print(f"Created {name} ({dim}d)")
        else:
            print(f"Exists: {name}")

    # Extra index for transitions
    if "transitions" not in existing:
        client.create_payload_index(
            "transitions", field_name="from_track_id", field_schema=PayloadSchemaType.KEYWORD
        )
        print("Created from_track_id index on transitions")


if __name__ == "__main__":
    main()
