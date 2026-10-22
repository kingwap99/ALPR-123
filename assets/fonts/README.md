# TW Plate Sans family

An unofficial font family derived from the Taiwan Highway Bureau new-style plate glyph specification. It is intended for plate mockups, UI demos, signage, and display typography.

## Styles

| File | Family | Style | Weight |
|---|---|---|---|
| `TWPlateSans-Regular.ttf` | TW Plate Sans | Regular | 400 |
| `TWPlateSans-Bold.ttf` | TW Plate Sans | Bold | 700 |
| `TWPlateSans-Italic.ttf` | TW Plate Sans | Italic | 400 |
| `TWPlateSans-BoldItalic.ttf` | TW Plate Sans | Bold Italic | 700 |

Each style also has a `.woff2` web font. `specimen-family.png` shows all four.

## Coverage

- Uppercase A–Z
- Lowercase a–z, drawn as **small caps** to preserve the plate display style
- Digits 0–9
- Hyphen, space
- Basic punctuation: `. , : ; / ! ? + = *`

The official new plate encoding does not use `I`, `O`, or `4`; they are included so the family remains usable for general mockups.

## Install

macOS: double-click a TTF and choose **Install Font** in Font Book. Install all four files to get the complete family.

Windows: select the TTF files, right-click, and choose **Install** or **Install for all users**.

Web:

```css
@font-face {
  font-family: "TW Plate Sans";
  src: url("TWPlateSans-Regular.woff2") format("woff2");
  font-weight: 400;
  font-style: normal;
  font-display: swap;
}

@font-face {
  font-family: "TW Plate Sans";
  src: url("TWPlateSans-Bold.woff2") format("woff2");
  font-weight: 700;
  font-style: normal;
  font-display: swap;
}
```

## Source and terms

The glyph shapes follow the [Highway Bureau new-plate font PDF](https://ws.thb.gov.tw/001/Upload/OldFile/resource/html/doc/%E7%9B%A3%E7%90%86%E6%A5%AD%E5%8B%99/%E7%89%8C%E7%85%A7/3.%E6%96%B0%E5%BC%8F%E8%99%9F%E7%89%8C%E4%BD%BF%E7%94%A8%E4%B9%8B%E8%8B%B1%E6%96%87%E5%AD%97%E3%80%81%E6%95%B8%E5%AD%97%E5%AD%97%E9%AB%94.pdf), using the reference patterns from [Taiwan_License_Plate_Recognition](https://github.com/KuoFuKai/Taiwan_License_Plate_Recognition).

This is not an official government font and is not endorsed by the Highway Bureau. Verify upstream terms before redistributing the font or using it commercially.
