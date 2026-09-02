# Changelog

All notable changes to ThirtyStash are documented here.

## [1.2.0-beta.2] - 2026-09-01

### Changed
- Moved the Food Add/Scan section above inventory so barcode scanning is always immediately accessible, including on mobile with long inventories.
- Kept inventory search, filtering, sorting, and CSV export below the food-entry workflow.

## [1.2.0-beta.1] - 2026-08-26

### Added
- Public-repository hardening and GitHub Actions CI, including a Docker runtime health smoke test.
- CSRF protection for state-changing browser requests.
- Automatic per-installation persistent Flask secret generation with safe multi-worker initialization.
- Local runtime Quagga2 asset; the pinned bundle is downloaded during image build.
- Repository license, security policy, contribution guide, third-party notices, and tests.

### Changed
- README rewritten for first-time self-hosters.
- Flask direct-run debug mode disabled.
- Docker Compose no longer ships a shared placeholder secret.

## [1.1] - 2026-08-26
- Added local system diagnostics/status.
- Added stronger server-side validation.
- Improved mobile inventory layout and navigation.

## [1.0] - 2026-08-26
- Added search/filter/sort, duplicate barcode awareness, and a dedicated Settings page.

## [0.9] - 2026-08-26
- Added validated backup restore and two-stage reset with pre-action safety backups.

## [0.8] - 2026-08-26
- Added immediate save-to-backup-folder action.

## [0.7] - 2026-08-26
- Added optional once-daily scheduled backups with user-selected time, timezone, and destination subfolder.

## [0.6] - 2026-08-26
- Added integrity-checked full SQLite ZIP backups.

## [0.5] - 2026-08-26
- Added inventory editing, food lot tracking, and attention/expiry dashboard.

## [0.4] - 2026-08-26
- Replaced the earlier barcode decoder with Quagga2 for reliable UPC/EAN photo scanning.
