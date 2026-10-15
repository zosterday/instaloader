CLI reliability repair, 2026-10-14
=================================

This fork is version ``4.15.4+fork.2``. Relevant profile-resolution and
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
* Anonymous pagination now uses the public ordered connection's own query,
  ``PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_connection``
  (document ``27389614800735091``). Its variables are ``id`` (the scoped
  profile ID), ``first`` and ``after``; its result is
  ``data.node.polaris_ordered_timeline_connection``. The logged-in query
  ``28975909992013618`` and its native timeline shape remain separate.
* The public WWW transport uses ``/api/graphql``, the profile HTML's first
  LSD bootstrap definition, its anonymous CSRF cookie and same-origin fetch
  metadata: ``Sec-Fetch-Site: same-origin``, ``Sec-Fetch-Mode: cors`` and
  ``Sec-Fetch-Dest: empty``. Omitting this metadata reproduced controller
  error 1357054 even with the correct document and cursor. Adding it returned
  twelve new posts on each of two successive pages, with the same user agent
  and no login. No browser dependency was added to the package.
* Public HTML and pagination share a separate anonymous session. It never
  imports the account session. Profile and post bootstraps are kept separate;
  API cookie updates retain their domains, so a rotated CSRF cookie and its
  header cannot disagree. Guard-prefixed WWW JSON and application errors are
  handled explicitly. Nonadvancing, repeated or empty incomplete pages stop
  with a recoverable error rather than marking the feed complete.
* Resume loading recognizes raw and enveloped node, anonymous and legacy
  feed checkpoints, validates their fields and retains extension data.
  Public-preview checkpoints from ``fork.1`` migrate to the matching public
  document after checking their numeric profile ID, context, username,
  referer and complete known query variables. Their cursor, remaining edges
  and original query identity are retained. Native XDT feed cursors cannot
  be relabelled as public ordered cursors; incompatible state is preserved
  with the existing quarantine and diagnostic workflow.
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

All 62 offline tests passed on Python 3.12 and 3.14. Pylint returned
10.00/10 and mypy reported no issues. The consuming application's 161
focused integration tests passed against the installed wheel.

The offline tests are also included in the existing CI matrix. The new
regressions exercise 25 posts across three pages, page-boundary
freeze/load/thaw, migration of the previous 12-post public checkpoint,
identity mismatches, account-session isolation, CSRF cookie rotation,
terminal controller errors and incomplete pagination.

The repaired source CLI downloaded 25 posts from ``biologistmichael`` across
three pages in 65.5 seconds: 53 HTTP requests, all HTTP 200, exit status 0.
All 25 media files were nonempty, had distinct SHA-256 hashes and passed
FFprobe validation; they totalled 89,108,997 bytes. Canaries write only to
temporary directories. They use no owned account credentials, changed
identity, proxies, CAPTCHA handling or retry bypass.

The final installed wheel, checked byte-for-byte against the source,
enumerated 37 unique posts across four pages in 89.0 seconds: 41 HTTP
requests, all HTTP 200, exit status 0. This separate canary resolved post
metadata without downloading media. A copy of the checkpoint from the
previous failed twelve-post run also resumed through post 25 successfully,
using 18 HTTP requests in 45.5 seconds; its original file was preserved.

The previous ``fork.1`` test stopped after twelve posts with HTTP 401.
It sent the public HTML cursor to the logged-in timeline query, so that
result did not establish an external anonymous-pagination restriction.
The current repair addresses the protocol mismatch instead of accepting
first-page access as a complete-feed repair.

Old native checkpoints remain recoverable but cannot be translated by
changing their document ID. A bounded probe using the old
``media_id_user_id`` cursor shape returned a null ordered connection;
it must not be interpreted as successful end-of-feed enumeration. No
production checkpoint, tracked identity, archive or database record was
rewritten during validation.

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
    instaloader biologistmichael --count 25 --no-metadata-json --no-video-thumbnails

Verify the executable's environment: an existing ``instaloader`` command
may run an upstream installation rather than this checkout. A packaged
wheel and SHA-256 deployment manifest identify the verified local build.
Do not claim sustained daily throughput from a short canary. A post-repair
observation must distinguish unique jobs, media, backend operations and
actual HTTP requests, and retain external authorization/cooldown failures.

The local CLI and consuming Studio environment were installed with
``4.15.4+fork.2``; the previous wheel was retained for rollback. Only the
services paused for this task were restored. A read-only observation window
runs from 2026-10-14 11:45:02 UTC through 2026-10-15 11:45:02 UTC. Its prior
24-hour baseline contains the previous build and the investigation's
worker pause, so a direct throughput comparison requires that context.
Source changes remain local; this task did not push GitHub changes or
install the wheel remotely on the MacBook.
