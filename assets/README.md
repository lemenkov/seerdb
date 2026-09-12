# The seerdb mark

A seer is an oracle. The mark reads twice: the standard database glyph is a
cylinder — a stack of discs — and viewed head-on that same stack becomes an
iris. Engineers see a database, then notice the eye.

The mark deliberately avoids any echo of Oracle's trademark: no red-on-white
wordmark palette, no structural resemblance to their logo. seerdb is an
independent, clean-room project (see the notice in the top-level README) and the
mark should read as protocol tooling, not as an Oracle-adjacent product.

## Which file to use

| file | use it for |
| --- | --- |
| `seerdb-mark.svg` | The primary mark. Uses `currentColor`, so one file serves light and dark — inline it in HTML, or anywhere the surrounding CSS sets a colour. |
| `seerdb-mark-small.svg` | Sizes at or below ~24px. The three-disc stack cannot resolve that small, so it reduces to the ring and pupil with thickened weight. The pupil matches the primary mark exactly, so it reads as the same mark rather than a different one. |
| `seerdb-mark-ink.svg` | Fixed dark mark, for `<img>` and other contexts where `currentColor` has nothing to inherit. |
| `seerdb-mark-paper.svg` | Fixed light mark, for dark grounds. |
| `seerdb-avatar-460.png` | GitHub organisation avatar — teal on ink, with margin. Must be uploaded by hand; GitHub does not expose avatars through its API. |
| `seerdb-mark-256.png` | Raster for PyPI and anywhere an SVG is not accepted. |
| `favicon-32.png`, `favicon-16.png` | Favicons, generated from `seerdb-mark-small.svg`. |

## Colours

| token | hex | note |
| --- | --- | --- |
| ink | `#101619` | Near-black with a blue bias. Ground for the avatar. |
| paper | `#F4F6F5` | Off-white with a faint green-grey bias. |
| accent | `#147F73` | Instrument teal, for light grounds. |
| accent (dark) | `#4FC2B0` | Lifted teal, for dark grounds — including the avatar. |

The mark works in a single colour and never depends on opacity, so it survives
one-colour printing, embroidery and favicon reduction.

## Licence and use

The mark (the SVG and PNG files here) is licensed **CC-BY-ND-4.0**, not MIT like
the rest of the repository. It is content rather than code: redistribute it
freely to refer to seerdb — in articles, package listings, talks, a link back —
but do not alter it into a variant brand.

Please don't use the mark in a way that suggests seerdb endorses, or is
affiliated with, something it isn't. That includes branding a fork or derivative
as if it were this project.

Two practical notes for packagers:

- CC-BY-ND is on Fedora's allowed-content list, but it is not DFSG-free, so a
  Debian package would need to strip or repack these files. If that request
  actually arrives, relicensing is worth reconsidering — the protection that
  matters here (nobody passing a fork off as seerdb) comes from trademark, which
  is retained independently of the copyright licence.
- `assets/README.md` itself is ordinary documentation and stays MIT.

## Regenerating the rasters

The PNGs are generated from the SVGs; edit the SVG and re-render rather than
touching a PNG:

```sh
magick -background none -density 400 assets/seerdb-mark-ink.svg  -resize 256x256 assets/seerdb-mark-256.png
magick -background none -density 400 assets/seerdb-mark-small.svg -resize 32x32   assets/favicon-32.png
magick -background none -density 400 assets/seerdb-mark-small.svg -resize 16x16   assets/favicon-16.png
```

The avatar is rendered from an SVG that adds the ink ground and margin around
the primary mark's geometry.
