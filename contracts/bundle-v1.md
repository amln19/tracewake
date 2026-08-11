# Deterministic bundle v1

A bundle transports one already-recorded Tracewake run. It is not an execution
environment and does not authorize the hosted system to run agent code.

## Archive

Bundle v1 is an uncompressed POSIX USTAR archive. Its digest is the lowercase
SHA-256 hexadecimal digest of all archive bytes. Producers write only regular
files and omit directory entries.

Entries are ordered by ascending UTF-8 path bytes. Every header uses:

| Field | Value |
| --- | --- |
| mode | `0644` |
| uid, gid | `0` |
| uname, gname | empty |
| mtime | `0` |
| type | regular file |
| linkname | empty |
| device numbers | `0` |

The archive has the USTAR end blocks and record padding emitted by Python's
`tarfile` USTAR writer. Validation rebuilds the canonical archive in memory and
requires byte equality, so alternative padding, header encodings, entry order,
or metadata are rejected even if a general tar reader would accept them.

Compression, PAX headers, GNU extensions, sparse entries, duplicate names,
absolute paths, backslashes, empty components, `.` or `..` components,
symlinks, hard links, directories, devices, and non-UTF-8 names are rejected.
An entry name is at most 100 UTF-8 bytes.

## Entries

The archive contains exactly:

* `manifest.json`;
* `events.jsonl`;
* one file for each referenced blob at
  `blobs/<first-two>/<next-two>/<64-lowercase-hex-digest>`.

There are no unreferenced blobs or additional metadata files.

`manifest.json` is one UTF-8 canonical JSON object followed by LF. Object keys
are sorted, separators are `,` and `:`, non-ASCII characters are emitted
directly, and no insignificant whitespace or BOM is allowed. It declares:

* bundle, cassette, and event-schema versions;
* run ID, event count, and logical run digest;
* the path, SHA-256 digest, and byte size of `events.jsonl`;
* the canonical path, SHA-256 digest, and byte size of every blob.

`events.jsonl` contains one canonical event object per line with a final LF.
Each line includes its zero-based `seq`. Sequences must be exactly
`0..event_count-1`. Event objects must validate under the declared event schema.
The manifest logical digest must equal the digest produced by Tracewake over the
canonical logical event stream. Event metadata remains outside that logical
identity, while the final bundle digest covers its transported bytes.

Each blob's bytes must hash to its filename and manifest digest. Its actual
size, manifest size, and every event `BlobRef.size` must agree.

## Limits

Limits are inclusive:

| Resource | Limit |
| --- | ---: |
| Archive bytes | 256 MiB |
| Expanded regular-file bytes | 256 MiB |
| Individual blob | 64 MiB |
| `events.jsonl` | 64 MiB |
| Events | 100,000 |
| Archive entries | 10,000 |

Because v1 is uncompressed, archive and expanded limits independently defend
the parser rather than expressing a permitted compression ratio.

## Production and validation

`tracewake.bundle.build_bundle` consumes only a fully validated cassette
directory, writes to a temporary file beside the destination, flushes it, and
atomically replaces the destination. `tracewake.bundle.validate_bundle` performs
no writes.

Validation checks the outer byte limit before parsing; validates archive type,
names, entry count, metadata, and exact canonical bytes; validates the manifest
and declared versions; then validates events, derived model-request and
tool-argument hashes, logical digest, references, blob bytes, digests, and
sizes. An invalid or unsupported bundle never becomes a
usable hosted run.
