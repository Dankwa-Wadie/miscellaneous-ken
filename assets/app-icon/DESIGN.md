# Miscellaneous Ken app icon

Generated using the built-in image generation tool. The PNG is the master artwork; the ICNS packages standard Dock/Finder sizes without redesigning the image.

## Final generation prompt

Use case: logo-brand. Create one production-ready macOS app icon for "Miscellaneous Ken", a personal short-video editorial studio. Square 1024x1024 PNG. A single confident sculptural lime-green capital K monogram, with the upper diagonal suggesting a right-facing video play triangle, centered on a deep charcoal rounded-square tile. Match existing app colors #101114 and #aaf27d. Restrained premium macOS material: satin enamel monogram, subtle bevel and soft upper-left light, gentle dimensionality, no glossy plastic excess. Clean bold silhouette, highly legible at 32px. Tile occupies about 84% of canvas with equal transparent margins; actual transparent background outside rounded square, no checkerboard. Front facing, no perspective tilt, no scene or device mockup. No other text, no tiny details, no extra symbols, no border ring. Exactly one icon, not a presentation sheet.

## Packaging

Run `bash deploy/package-icon.sh`, then `bash deploy/build-app.sh`. The app bundle references `MiscellaneousKen.icns` through `CFBundleIconFile`.
