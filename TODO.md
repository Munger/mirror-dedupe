# TODO

## Deployment & Infrastructure

- [ ] `archive.ubuntu.com` is behind Cloudflare CDN which does not proxy
  rsync port 873. Use `rsync://rsync.archive.ubuntu.com/ubuntu` instead
  (the `rsync.` subdomain points directly to Canonical's servers).
  Same applies to ports: use `rsync://rsync.ports.ubuntu.com/ubuntu-ports`.
  Config files `/etc/mirror-dedupe/repos-enabled/*.conf` have been updated
  but this may break if Canonical changes their DNS setup.
- [x] Document the full deployment flow from release to munger repo in one place.
  See `PUBLISHING.md` — pipeline now includes `deploy-apt.yml` pushing `.deb` to `Munger/packages`.
