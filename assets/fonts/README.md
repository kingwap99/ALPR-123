# TW Plate Sans Bold

An unofficial TTF/WOFF2 font derived from the Taiwan Highway Bureau new-style plate glyph specification. It is intended for plate mockups, UI demos, and signage experiments.

## Files

- `TWPlateSans-Bold.ttf` — installable TrueType font
- `TWPlateSans-Bold.woff2` — web font
- `specimen.png` — specimen sheet

## Coverage

- Uppercase A–Z
- Digits 0–9
- Hyphen, space
- `. , : ; /`
- Lowercase input maps to the uppercase glyphs

The official new plate encoding does not use `I`, `O`, or `4`; they are included here so the font remains usable for general text and mockups.

## Install

macOS: double-click `TWPlateSans-Bold.ttf`, then choose **Install Font** in Font Book.

Windows: right-click the TTF and choose **Install** or **Install for all users**.

Web:

```css
@font-face {
  font-family: "TW Plate Sans";
  src: url("TWPlateSans-Bold.woff2") format("woff2");
  font-weight: 700;
  font-style: normal;
  font-display: swap;
}
```

## Metrics

- Units per em: 1000
- Cap height: 700
- Ascender: 800
- Descender: -200
- Weight class: 700
- Width class: condensed (3)

## Source and terms

The glyph shapes follow the [Highway Bureau new-plate font PDF](https://ws.thb.gov.tw/001/Upload/OldFile/resource/html/doc/%E7%9B%A3%E7%90%86%E6%A5%AD%E5%8B%99/%E7%89%8C%E7%85%A7/3.%E6%96%B0%E5%BC%8F%E8%99%9F%E7%89%8C%E4%BD%BF%E7%94%A8%E4%B9%8B%E8%8B%B1%E6%96%87%E5%AD%97%E3%80%81%E6%95%B8%E5%AD%97%E5%AD%97%E9%AB%94.pdf), using the reference patterns from [Taiwan_License_Plate_Recognition](https://github.com/KuoFuKai/Taiwan_License_Plate_Recognition).

This is not an official government font and is not endorsed by the Highway Bureau. Verify upstream terms before redistributing the font or using it commercially.
