# Lossless compact format, version 1

`index.jsonl` has one row per original raw-archive file. Each row includes `path`,
`sha256` (the original file's bytes), and `size`. A `blob` field identifies binary
content in `blobs/sha256/<digest>`. A `record` field identifies a text record by the
SHA-256 of the exact reconstructed original bytes.

`records/*.jsonl` contain unique `{"id": digest, "parts": [...]}` objects. The collection
names organize record types; identifiers are globally unique across collections.
Each string part is encoded verbatim as UTF-8 during restoration, including its
original whitespace and newlines. A part `{"base64_sha256": digest}` means base64-encode
the bytes of the named blob and insert the resulting ASCII bytes at this position.
The surrounding data-URL prefix remains in the adjacent string part. Therefore
requests are reconstructed without reserializing the original JSON or changing its
key order, spacing, or escaping. The encoder verifies canonical base64 round-tripping
before substituting a payload. Other text is stored verbatim.

Identical files share records or blobs. All paths remain in the index, preserving
the provenance of repeated or retried observations; deduplication does not merge
experimental observations or alter the sample sizes. Non-raw files remain directly
readable in the compact tree. The original README is at `original-readme.md` because
the compact tree has its own instructions. The original `.gitignore` is stored as
`original-gitignore`; the compact version additionally ignores `restored/`.
The original checksum manifest is copied
back to the root of the restored tree.

The compact package and the reconstruction each use separate checksum verification.
These digests detect corruption and demonstrate equivalence to the frozen anonymous
release; they are not cryptographic authentication of the authors or scientific claims.
