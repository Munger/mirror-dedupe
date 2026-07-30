# TODO

## Bugs

- [x] **FIXED 2026-07-30 (v1.2.2) — mirror-dedupe's apt mirror advertised `Acquire-By-Hash: yes` without actually implementing it — real correctness bug, not a theory.**
  Implemented in `Release.stream()`, deployed via the real 1.2.2 apt package (not a
  hand-patched hotfix — that was reverted once the proper release landed). Verified live
  in production across all 6 configured repos on mirror.private:
  - `ubuntu`, `ubuntu-ports`: upstream advertises `Acquire-By-Hash`, by-hash files present
    and content-correct (hash of fetched file matches its by-hash filename).
  - `mungerware`: now also advertises it (see the `apt.mungerware.com` fix below),
    by-hash files present and content-correct, `Release` hash listing clean (no duplicate
    by-hash entries leaked in from generation-ordering).
  - `ubuntu-cloud`, `grafana`, `influxdb`: upstream doesn't advertise `Acquire-By-Hash`
    (confirmed directly against real upstream for `ubuntu-cloud` — genuinely absent from
    Canonical's own `Release`, not lost in mirroring), so the header isn't present on the
    served copy either (can't add it without the upstream's signing key) — but the by-hash
    *files* still exist and are content-correct anyway, since generation doesn't depend on
    upstream advertising the capability.
  Companion fix in `Munger/Packages`' `publish.yml`: generates its own by-hash tree and
  `Acquire-By-Hash: yes` header for `apt.mungerware.com` (that repo's own publish pipeline
  holds the real signing key, unlike mirror-dedupe mirroring it after the fact).

  Implementation lives in `mirror_dedupe/repos/apt/release.py`, `Release.stream()`: each
  `primary_index`/`variant` entry already carries `algorithm`/`checksum`/`size`/`path`, so
  one extra `Schema.VariantIndex` is yielded per entry with the path transformed to
  `<dirname>/by-hash/<Algo>/<checksum>` — sync-only (not parsed for children, same as the
  existing compression-variant handling), flows through the normal pool pipeline unchanged.
  Generated *after* `Release` itself is built (ordering matters for the publish side, to
  avoid `apt-ftparchive`'s directory scan cataloging by-hash files as duplicate entries).

  **Open follow-up, not yet decided — Tim's own question 2026-07-30:** should by-hash
  generation be conditional on the upstream actually advertising `Acquire-By-Hash`, rather
  than always-on? For repos where it isn't advertised (`ubuntu-cloud`, `grafana`,
  `influxdb` today), the generated files are currently pure overhead — extra pool entries,
  extra sync work — since no real apt client can discover or use them without the header.
  Gating on upstream's own flag would avoid that waste for repos that will never benefit.
  Counter-consideration: it's cheap (rides the existing pool/hash machinery, no real extra
  storage for the current-version case), and leaves the door open if a future mirror-dedupe
  version starts advertising `Acquire-By-Hash` on repos it serves even when upstream
  doesn't (a currently-blocked idea only because of the signing-key constraint on
  mirrored/signed repos — would need its own signing setup to pursue). Revisit before
  deciding either way.

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
