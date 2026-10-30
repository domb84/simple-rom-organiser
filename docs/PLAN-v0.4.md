# v0.4 plan: a clean library, a clean archive, and the archive on S3 (MEGA S4)

Goal (the user, 2026-10-24): the library stays local; everything Build library sets aside goes to the archive, and the archive can
be an S3 bucket (MEGA S4 first, any S3-compatible service). Both must stay clean: no empty folders, no stray files, nothing of the
app's own lying around.

## What is there today (the user's folders, 2026-10-24)

* Library folders: N64 has 96 empty folders, Dreamcast 11, Mega Drive and Switch 1 each. The Amiga folder still holds `_excluded`,
  `_superseded`, `_incomplete` and `_unmatched` (25,667 files) from builds made before the archive folder was set. Every library
  root holds the app's undo logs (`.romorg-undo-*.json`; deleted at the next scan since Undo was removed, Amendment 65).
* Archive folder (`~/MEGA/Emulation/rom-archive`, 31 GB, 5,558 files): `<system folder>/<_reason>/...`, no empty folders, no app
  files.
* Why the empty folders stay: a build removes a folder only when its own moves emptied it (`organiser._remove_empty_dirs` is given the
  folders it touched); folders that were empty before, or emptied by something else, are never looked at.

## Rules both places keep (the "clean" contract)

1. **No empty folders**, after every job (build, convert, WHDLoad build, archive, restore). Exceptions: the system's root and
   the folders the app never touches (`Platform.protected_dirs`: Kickstarts, the WHDLoad installs ...).
2. **No app files in the library**: no logs or journals at all (Undo is gone); temporary and part files only while a job runs.
   Leftovers of an interrupted run are cleaned at the next start.
3. **No reason folders in the library** when an archive is set: what the rules set aside goes straight to the archive (today's
   behaviour), and a build first sweeps old `_excluded` / `_superseded` / ... folders out (today: only when the archive folder is
   given to that build).
4. **Nothing in the archive without a reason**: every file sits under `<system>/<_reason>/` (or `<system>/Saves/<game>/`), nothing
   at the top level, no zero-byte "folder" objects on S3, no unfinished multipart uploads left behind.
5. **No manual step**: Build library keeps both places clean by itself (the user: "if I build a library then things get archived as
   now; there's no manual step"). A test helper checks rules 1-4 after every build in the tests; the app has no tidy button.

## Phases

### Phase 0 - tidy what is local (no S3 needed) - done 2026-10-24 (Amendment 64)
* Prune every empty folder under the library root and the archive after each job (bottom-up, protected folders kept), not only
  the ones the job emptied.
* Undo removed altogether (the user's choice, Amendment 65): no logs or journals anywhere; old logs are deleted at the next scan.
* Every build with an archive sweeps the library's old reason folders (`_excluded` ... and `_converted_originals` when originals are
  deleted) into the archive, so the Amiga's 25,667 files go with its next build.
* Tests: a build on a folder with old empty folders and stray app files leaves neither.

### Phase 1 - an S3 client (stdlib only: urllib + hmac / hashlib, AWS Signature Version 4)
* ListObjectsV2 (size, ETag), HeadObject (metadata), PutObject with `Content-MD5` and `x-amz-meta-*`, multipart upload (parts with
  `Content-MD5`; abort on failure; list and abort stale ones), GetObject with `Range`, DeleteObject(s), CopyObject. Retries with
  back-off; streaming (never a whole file in memory); several uploads at once for small files.
* Settings: endpoint (MEGA S4's region endpoint), region, bucket, prefix, access key; the secret key in its own file in the data
  folder (mode 0600), never shown again, never logged. "Test connection": list, put a probe object, head it (is the ETag the MD5?
  is the metadata back?), delete it.
* What MEGA S4 supports (its spec, github.com/meganz/s4-specs): no `x-amz-checksum-*` and no GetObjectAttributes; `Content-MD5`,
  `x-amz-meta-*`, `Range`, CopyObject with `x-amz-metadata-directive`, multipart: yes. So: the app's own checksums go into metadata.
* Tests: an S3 server in the tests (http.server, the subset above, SigV4 checked) so everything runs offline; the real S4 only with
  an opt-in environment variable.

**Status 2026-10-24:** `romorg/s3.py` (client) and `tests/test_s3.py` (offline S3 server in the tests, SigV4 checked; AWS's own
signature example) are done. Checked on the user's MEGA S4 test bucket (global endpoint `s3.g.megas4.com`, path-style):
signing with region `us-east-1` is accepted; a single PUT's ETag is the MD5 (a multipart one is not: `<md5 of md5s>-<parts>`);
`x-amz-meta-*` comes back (non-ASCII sent `url:`-encoded); `Content-MD5` is enforced; Range, multipart (5 MiB parts), CopyObject with
REPLACE, DeleteObjects and ListMultipartUploads all work. Phase 1 is done (settings, the secret in its own 0600 file, "Test connection"),
and phase 2's archive on S3 too (Amendment 66): builds upload, check and delete; the file tree's Archive tab is the bucket.

### Phase 2 - the archive on S3
* The archive becomes "a folder or a bucket": one shared archive (a bucket + prefix, or a folder) for every system, which a system can
  override (its own bucket / prefix / folder). Archiving a file: upload to `<prefix>/<system>/<_reason>/<path>` with Content-MD5 and
  metadata (`crc32`, `sha1`, `md5`, size; a zip / 7z: its members' names and CRCs), check (HEAD: size, ETag / metadata), then delete
  the local file - no local copy is kept (the user). Never over an object of another content (a different checksum: a numbered name, as locally).
* An inventory of the bucket (list once, kept in the data folder, refreshed when a job writes): "in the archive" counts, the tidy
  check, and finding a game in the archive without downloading anything.
* Saves of archived games go with them (`Saves/<game>/`), as locally.

### Phase 3 - using the archive - done 2026-10-24 (Amendment 67): browse, multi-select Restore (the archive keeps its copy),
restored files converted to the library's form, missing games found in the archive. Not done: saves. Other tools' objects in the bucket are out of scope (the user: the app manages the bucket).
* Browse the archive (per system, per reason) and bring a game back into the library (download, check, place; the next build decides
  again).
* Matching objects other tools put in the bucket: zip / 7z by range reads of their directory (a few KB), loose files by their ETag
  when it is an MD5 (single-part uploads), the rest streamed.

## Decisions (the user, 2026-10-24)
* After an upload is checked the local copy is deleted ("bin it").
* No migration of the existing local archive: the user tidies it from scratch.
* One shared archive, overridable per system.
* No tidy check / button: every build leaves the library and the archive clean.
* No Undo: removed (2026-10-24), the user never used it.
