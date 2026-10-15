Anonymous CLI speed and connection recovery, 2026-10-15
=====================================================

This repair is version ``4.15.4+fork.3``, based on fork commit ``a8fe166``.
It retains the public pagination protocol repaired in ``fork.2``.
All live validation uses public, logged-out requests, no user account
sessions, account cookies, API tokens, proxies or identity changes.

Confirmed causes
----------------

The public HTML reader added the normal exponential random sleep before
every post's metadata request: about 1.7 seconds on average, capped at
15 seconds. A twelve-post, picture-only baseline on the user-selected
``zosttgames_`` took 31.705 seconds, including 21.935 seconds of deliberate
client sleep. All 25 HTTP requests succeeded. No videos were downloaded.

Public HTML navigations also shared the legacy ``other`` JSON budget of
75 requests per eleven minutes. This became a per-post bottleneck once
public post pages supplied metadata. A virtual-clock regression reproduced
two 666-second waits during 160 otherwise immediate HTML requests. HTML
now has its own ``public_html`` category using the existing standard budget
of 200 requests per eleven minutes, without counting navigations toward
aggregate GraphQL quotas. The legacy JSON budget remains separate.

Public HTML and public WWW GraphQL random sleeps are capped at 0.25 seconds.
Sliding-window waits, server cooldowns and retry backoff still apply.
The cap removes the long random pauses without treating server limits
as permission to keep sending requests.

The public HTML reader previously made one attempt regardless of
``--max-connection-attempts``. One dropped connection while resolving a
post therefore ended profile iteration; restarting the command could then
fetch the same post successfully. It now retries network failures and
HTTP 5xx responses within that same metadata operation, using the configured
attempt limit (default three), backoff starting at one then two seconds,
and valid server ``Retry-After`` values. Explicit zero retains the existing
unlimited-network-attempt convention. Responses with HTTP 401, 403, 404 or
429, login/challenge redirects and configured abort statuses remain terminal
on the HTML path. A failing response cannot silently complete a feed.

The supplied ``ConnectionError`` does not identify the original socket,
DNS or TLS failure. The verified defect is the absence of recovery for a
transient failure, not evidence that Instagram changed the object format
again. Cookies from the public connection remain isolated from any account
session.

Regression checks
-----------------

All 68 offline tests passed on Python 3.12 and against the installed wheel
on Python 3.14. Pylint returned 10.00/10 and mypy reported no issues.
The consuming application's 161 focused integration tests also passed
against the installed ``fork.3`` wheel.
The new failures were reproduced before the fixes. Tests exercise
network/5xx recovery, attempt limits, ``Retry-After``, safe diagnostics,
terminal restrictions and public-request timing/accounting.

An integrated synthetic feed of 160 distinct posts drops the connection
while resolving post 81. The repaired iterator retries that post, returns
all 160 in order with no missing or duplicated post and traverses thirteen
additional pages. Running the new tests against the previous committed
HTTP context reproduces the failure. No production checkpoints, archives,
tracked identities, queued jobs or credentials are reset.

Run the offline checks with::

    python -m unittest discover -s test -p 'test_*.py'
    pylint instaloader
    mypy -m instaloader

Live validation and installation
--------------------------------

The packaged CLI is validated with::

    instaloader zosttgames_ --count 160 --no-videos --no-video-thumbnails \
        --no-captions --no-metadata-json --no-profile-pic

The canary is bounded to 600 requests, fifteen minutes and one GiB, stops
immediately at HTTP 401/403/429, and writes only into a temporary directory.
Its download and timing results are recorded in the wheel's deployment
manifest. The installed wheel completed all 160 distinct posts in
170.166 seconds: thirteen next-page requests and 334 HTTP requests in total,
all HTTP 200. It downloaded 160 distinct JPEG files, totalling 19,043,573
bytes; every file passed FFprobe validation. No videos were downloaded.
The longest deliberate sleep was 0.25 seconds, with 40.221 seconds of client
sleep over the whole run. No live retry was needed; transient recovery was
verified separately by the synthetic dropped-connection regression.

This is a bounded correctness test, not a promise that any network
or Instagram usage limit permits uninterrupted downloads indefinitely.

Install this fork into the environment that owns the CLI, then verify::

    python -m pip install --upgrade .
    instaloader --version

The version must be ``4.15.4+fork.3``. A wheel and adjacent SHA-256/source
manifest identify the deployed code. The prior ``fork.2`` wheel is retained
for rollback. Source changes are local until committed and pushed; an
installation on this computer does not update the MacBook.

The local CLI and consuming Studio environment use the same verified wheel.
Only task-paused services were restored. A fresh read-only observation
window starts at 2026-10-15 23:15:51 UTC and ends one day later; its baseline
contains the previous build and this investigation's worker pause.
