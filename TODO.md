# TODO

## Bugs

- [ ] **URGENT — Confirmed: mirror-dedupe's apt mirror advertises `Acquire-By-Hash: yes` without actually implementing it — real correctness bug, not a theory. Fix is small (~20 lines), located precisely.**
  Verified 2026-07-30 by direct comparison:
  - `archive.ubuntu.com` (true upstream): `Release` has `Acquire-By-Hash: yes`, and
    `dists/noble-updates/main/binary-amd64/by-hash/SHA256/` genuinely exists (200 OK).
  - `mirror.private`'s copy of the same repo: serves the *same* `Acquire-By-Hash: yes` line
    in its `Release` (copied verbatim from upstream during sync — mirror-dedupe holds no
    signing key for it, so this line can't be stripped even as a fallback), but
    `by-hash/SHA256/` returns **404** — the directory structure that field promises was
    never mirrored.
  This means the mirror is lying to any apt client about a capability it doesn't have.
  Hasn't caused visible breakage so far because apt gracefully falls back to the fixed
  (non-hash) path when by-hash 404s — but that's relying on client-side fallback behaviour
  that isn't guaranteed forever, and it means the actual protection by-hash exists to
  provide — immunity to exactly the fixed-path staleness/race class of bug below — isn't
  really present for the production `ubuntu` mirror, just not being exercised. Very likely
  the same root defect underlying the `mungerware` staleness incident below, observed from
  the opposite direction (serving side here; upstream-fetch side there) — investigate
  together, not as two unrelated bugs.

  **Root cause traced to `mirror_dedupe/repos/apt/release.py`, `Release.stream()`.**
  `entries` (the list driving what gets synced) is built *purely* from paths literally
  listed in Release's `MD5Sum`/`SHA1`/`SHA256` sections. By-hash paths are never listed
  there (confirmed: `by-hash` does not appear anywhere in a real `Release` file's text) —
  so the indexer is structurally blind to by-hash, not filtering it out, it simply has no
  way to know it exists.

  **Fix is small and self-contained** — no new fetch mechanism, no new discovery pass,
  no signing involvement:
  - Where `primary_index` (and each `variant`) is built in the `for base, group in
    by_base.items()` loop, the entry already carries `algorithm`, `checksum`, `size`, and
    `path` — everything needed. Yield one additional index node per entry with the path
    transformed to `<dirname>/by-hash/<ALGO>/<checksum>`, same metadata otherwise.
  - That node flows through the exact same generic `sync()`/pool pipeline as every other
    index file already does. Current-version case: the checksum is already pooled (the
    primary index synced moments earlier in the same run), so it hits the existing
    double-checked pool lookup and hardlinks for free — no real second download, no new
    storage.
  - No new retention/cleanup logic needed either: once a newer `Release` stops referencing
    an old hash, that pool object's link count drops and the *existing* orphan sweep (same
    mechanism that already removes stale pool files after each sync) retires it naturally.
  - Historical by-hash entries upstream keeps for its own transition window are a separate,
    smaller decision (mirror them too, or only ever carry the current one) — not required
    for the core fix.
  - Estimated at under 20 lines, entirely inside `stream()`. Tim's own estimate, and the
    code supports it — this is genuinely small, not a half-day feature.

- [ ] **Investigate: incremental apt sync marked `Packages`/`Packages.gz` as "Unchanged" once when upstream had genuinely changed — cause not yet confirmed.**
  Observed 2026-07-30 on `mungerware` repo: pushed new packages (`dcism`, `dcism-osc`) to
  `apt.mungerware.com` (GitHub Pages, fronted by Fastly), then ran `mirror-dedupe sync mungerware`.
  The sync fetched the new `Release`/`InRelease` but marked `noble/main/binary-{amd64,arm64}/Packages.gz`
  as `Unchanged`, continuing to serve a stale copy — confirmed by comparing the file served from
  `mirror.stack` against `apt.mungerware.com` byte-for-byte at the time. A full wipe + fresh sync
  fixed it.
  **Two competing explanations, not yet distinguished:**
  1. A fetch-ordering bug in the apt module — that same sync run's log showed `noble/Release`
     (plain, unsigned companion file) downloaded *last*, after `noble`'s own `Packages`/`Packages.gz`
     were already processed/linked, which could mean the "Unchanged" check ran before fresh
     reference data was available. Possibly related to the companion `Release`/`InRelease`
     distinction bug from commit `319a06c` and its follow-up.
  2. **CDN edge-cache lag, not a mirror-dedupe bug at all**: `apt.mungerware.com` serves via Fastly
     with `Cache-Control: max-age=600`. If the sync's request hit a different edge node than later
     manual checks, shortly after a fresh push, it could have gotten genuinely stale content straight
     from the CDN — explaining the exact same symptom with zero code involvement.
  Do not assume either explanation without evidence — reproduce deliberately (e.g. force a change,
  wait out the CDN cache window fully, then sync and check ordering/headers) before treating this as
  a confirmed code bug.

## Deployment & Infrastructure

- [ ] `archive.ubuntu.com` is behind Cloudflare CDN which does not proxy
  rsync port 873. Use `rsync://rsync.archive.ubuntu.com/ubuntu` instead
  (the `rsync.` subdomain points directly to Canonical's servers).
  Same applies to ports: use `rsync://rsync.ports.ubuntu.com/ubuntu-ports`.
  Config files `/etc/mirror-dedupe/repos-enabled/*.conf` have been updated
  but this may break if Canonical changes their DNS setup.
- [x] Document the full deployment flow from release to munger repo in one place.
  See `PUBLISHING.md` — pipeline now includes `deploy-apt.yml` pushing `.deb` to `Munger/packages`.
