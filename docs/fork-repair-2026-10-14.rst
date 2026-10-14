CLI reliability repair, 2026-10-14
=================================

This fork is version ``4.15.4+fork.1``. Relevant profile-resolution and
query changes were compared with official master
``58c07c6393c526c3c69413c257e71f8b6c9df7ea`` (2026-10-13) and release
``v4.15.4`` (2026-10-12). This is a selective backport that retains the
fork's Python 3.9 floor and anonymous checkpoint compatibility.

Verified causes and changes
---------------------------

* The installed 4.15.3 CLI failed an anonymous profile-info request with
  HTTP 401 and ``require_login``. The same public profile's HTML returned
  its identity and twelve timeline entries. Profile lookup now uses the
  upstream public-page reader first, without importing any account session.
* Current public profile and post pages contain ``xig_user_by_username``,
  ``polaris_ordered_timeline_connection`` and ``xig_polaris_media`` data.
  Timeline previews omit timestamps and complete media data. The fork reuses
  the embedded timeline and resolves those previews through their public
  post pages. Explicit gating, authentication, challenge and cooldown
  responses stop the affected request path.
* HTML's scoped ``id`` differs from the numeric account ``pk``. Profile
  identity uses ``pk``; cached numeric IDs are fenced. A collaborator post
  owner cannot rename a profile. Logged-in relationship metadata remains
  unresolved until its existing authenticated metadata path supplies it.
  Authenticated behavior was tested with synthetic fixtures only.
* Profile CLI targets ignored ``--count`` even though the downloader
  supports it. The option now reaches profile downloads, and the iterator
  stops before fetching an extra post's metadata.
* Newest-post comparisons rewrapped the previous first node on every post,
  repeating lazy metadata requests. The wrapped first item is now cached.
* Resume loading recognizes raw and enveloped node, anonymous and legacy
  feed checkpoints, validates their fields and retains extension data.
  Known anonymous web-query state is migrated narrowly from document
  ``38154989454116081`` to ``28975909992013618`` after matching its numeric
  profile identity, username, context and referer. Cursor, remaining edges
  and original query identity are retained. Arbitrary document IDs and
  mobile cursors are not translated.
* Unusable checkpoints are preserved as ``.quarantine-*`` artifacts with
  a diagnostic JSON record before a fresh iteration begins. Resume-only
  loading rejects Post documents before any legacy post lookup can run.
* Web HTTP 401, 403 and 404 retain their distinct types and are terminal.
  HTTP 429 retries remain bounded and respect server ``Retry-After``.
  Server and network failures retain their configured retry limit.
  Legacy mobile 401 responses containing ``please wait`` retain the
  existing rate-controller cooldown behavior.
  Non-object or invalid JSON produces a clear response error. Diagnostics
  omit request query strings and embedded URL credentials.

Checks and limits
-----------------

Offline regression tests run with::

    python -m unittest discover -s test -p 'test_*.py'
    pylint instaloader
    mypy -m instaloader

The offline tests are also included in the existing CI matrix. Tests were
run locally on Python 3.12 and 3.14. No owned or managed account sessions
were used. The installed CLI downloaded one image and one video from
``biologistmichael`` with five anonymous HTTP requests in 5.7 seconds,
exit status 0. Nonempty files were checked with FFprobe and SHA-256:
2,904,464 image bytes and 5,326,440 video bytes. Canaries write only to
temporary directories and preserve interrupted checkpoints. Earlier
canary instrumentation consumed streaming response bodies and produced
zero-byte temporary media; those outputs are excluded from success evidence.

A second canary requested thirteen posts without downloading media. It
read twelve public post pages, then the pagination query returned HTTP 401
with ``require_login=true``. It stopped after fifteen requests in thirty
seconds and saved its checkpoint. Anonymous older-post pagination therefore
remains externally restricted in this environment; first-page success
does not establish complete-feed access. No authenticated workaround was
used, and no sustained-throughput target is claimed.

All 226 existing public-page resume files loaded offline: 92 anonymous and
134 node checkpoints. The two named handoff artifacts retained their
saved edges and cursors, with the original files unchanged. External
acceptance of an old opaque cursor by a new query remains a server-side
compatibility limit; a failed continuation saves recoverable state.

The separate caller fixes cover raw-checkpoint deserialization, stable-ID
fencing, local/transient/auth error classification, fallback suppression
after explicit restrictions and independent official Graph retry deadlines.
No tracked identities, archives or prior retry-reset state were reconciled
or reset. Official Graph payload errors, Drive HTTP 403 and missing altered
SSD outputs remain separate; this library patch does not resolve them.

Using this fork
---------------

From this checkout, install into the intended environment::

    python -m pip install --upgrade .
    instaloader --version
    instaloader biologistmichael --count 1 --no-metadata-json --no-video-thumbnails

Verify the executable's environment: an existing ``instaloader`` command
may run an upstream installation rather than this checkout. A packaged
wheel and SHA-256 deployment manifest identify the verified local build.
Do not claim sustained daily throughput from a short canary. A post-repair
observation must distinguish unique jobs, media, backend operations and
actual HTTP requests, and retain external authorization/cooldown failures.
