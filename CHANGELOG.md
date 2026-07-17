# Changelog

This changelog suppose to follow rules defined in the [changelog.md](https://changelog.md)

## 0.1.0 - TBD

Initial minimum valuable product pre-release.

- **Added**: OpenTelemetry support
- **Added**: `evilflowers_lcpencrypt_worker.lcpencrypt` task introduced
- **Added**: `title`/`author` task params — for raw-PDF input the PDF is pre-packaged into a
  Readium Web Publication whose `manifest.json` carries the metadata, so the encrypted `.lcpdf`
  shows the correct title/author in Thorium instead of "no title and no authors available" ([#1](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/1))
- **Added**: Sample docs
- **Added**: Docker image
- **Fixed**: encrypted-file metadata lookup now probes LCP output extensions (`.lcpdf`, `.lcpa`, `.lcpdi`, `.webpub`)
